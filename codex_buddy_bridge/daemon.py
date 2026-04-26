"""On-demand bridge daemon.

The IPC server stays up persistently (cheap, doesn't touch BLE). The buddy
peripheral only supports one BLE central at a time, so we *do not* hold a
persistent BLE connection — that would lock out the Claude Hardware Buddy.
Instead, BLE is acquired per-approval:

    permission_request → lock → connect → time/owner/prompt frames →
    await decision (or timeout) → send clear → disconnect → release lock.

If acquiring BLE fails (Claude has the device, advertising is paused, etc.),
the hook gets `no_buddy` and Codex falls back to its native approval prompt.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import logging
import os
import pwd
import signal
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import ipc
from .ble_transport import BleTransport
from .protocol import (
    ApprovalRequest,
    PROMPT_HINT_LIMIT,
    PROMPT_ID_LIMIT,
    PROMPT_TOOL_LIMIT,
    PermissionDecision,
    build_clear_snapshot,
    build_owner_frame,
    build_prompt_snapshot,
    build_time_frame,
    parse_permission_decision,
)

DEFAULT_PERMISSION_WAIT_SECONDS = 105.0  # < hook timeout (115s) in hooks.json
DEFAULT_CONNECT_TIMEOUT_SECONDS = 8.0    # short scan/connect window so the hook
                                         # can give up quickly if Claude has the buddy


@dataclass
class DaemonConfig:
    socket_path: str
    device_prefix: str
    address: Optional[str]
    permission_wait: float = DEFAULT_PERMISSION_WAIT_SECONDS
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS


class Daemon:
    def __init__(self, config: DaemonConfig):
        self.config = config
        self._ble_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._stop_event = asyncio.Event()
        self._log = logging.getLogger("codex-buddy.daemon")

    async def run(self) -> None:
        self._server = await ipc.serve(self.config.socket_path, self._handle_event)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                pass

        self._log.info(
            "Daemon ready (on-demand BLE): socket=%s device_prefix=%s",
            self.config.socket_path,
            self.config.device_prefix,
        )
        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._log.info("Daemon shutting down")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(self.config.socket_path)
        except FileNotFoundError:
            pass

    async def _handle_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        event = payload.get("event")
        body = payload.get("payload") or {}
        if not isinstance(body, dict):
            body = {}

        if event == "permission_request":
            return await self._handle_permission_request(body)

        # session_start / stop and any other events are accepted but ignored:
        # we can't drive the buddy display without holding BLE, and holding
        # BLE is exactly what we mustn't do in on-demand mode.
        self._log.debug("Event %r ignored in on-demand mode", event)
        return None

    async def _handle_permission_request(self, body: dict[str, Any]) -> dict[str, Any]:
        request = _request_from_payload(body)
        # Serialize: only one BLE session at a time. A second request waits
        # behind the first; if the queue exceeds the hook timeout (115s),
        # the second hook gives up and Codex falls back to native UI.
        async with self._ble_lock:
            return await self._run_approval(request)

    async def _run_approval(self, request: ApprovalRequest) -> dict[str, Any]:
        transport = BleTransport(
            device_name_prefix=self.config.device_prefix,
            address=self.config.address,
        )
        loop = asyncio.get_running_loop()
        decision_future: asyncio.Future[PermissionDecision] = loop.create_future()

        def on_line(line: bytes):
            return self._consume_decision(line, request.id, decision_future)

        try:
            await asyncio.wait_for(
                transport.connect(on_line, scan_timeout=self.config.connect_timeout),
                timeout=self.config.connect_timeout + 5.0,
            )
        except asyncio.TimeoutError:
            self._log.warning("BLE connect timed out for %s", request.id)
            return {"decision": "no_buddy", "reason": "BLE connect timed out"}
        except Exception as exc:  # noqa: BLE001 - Claude may hold the device
            self._log.warning("BLE connect failed for %s: %s", request.id, exc)
            return {"decision": "no_buddy", "reason": str(exc)}

        try:
            await self._send_greeting(transport)
            await transport.write_line(build_prompt_snapshot(request))
            self._log.info("Pending approval %s for %s: %s", request.id, request.tool, request.hint)
            try:
                decision = await asyncio.wait_for(decision_future, timeout=self.config.permission_wait)
            except asyncio.TimeoutError:
                self._log.warning("Approval %s timed out after %.0fs", request.id, self.config.permission_wait)
                return {"decision": "timeout"}
            await transport.write_line(build_clear_snapshot())
            return {
                "decision": "allow" if decision is PermissionDecision.APPROVE_ONCE else "deny",
                "request_id": request.id,
            }
        finally:
            try:
                await transport.close()
                self._log.info("Released BLE for %s", request.id)
            except Exception as exc:  # noqa: BLE001
                self._log.debug("close() raised: %s", exc)

    async def _consume_decision(
        self,
        line: bytes,
        expected_id: str,
        decision_future: "asyncio.Future[PermissionDecision]",
    ) -> None:
        decision = parse_permission_decision(line)
        if decision is None:
            return
        if decision.id != expected_id:
            self._log.warning(
                "Buddy decision id mismatch: got %r expected %r", decision.id, expected_id
            )
            return
        if not decision_future.done():
            decision_future.set_result(decision.decision)

    async def _send_greeting(self, transport: BleTransport) -> None:
        epoch = int(time.time())
        tz_offset = -time.timezone if time.daylight == 0 else -time.altzone
        try:
            await transport.write_line(build_time_frame(epoch, tz_offset))
            await transport.write_line(build_owner_frame(_owner_name()))
        except Exception as exc:  # noqa: BLE001
            self._log.debug("Greeting frames failed (non-fatal): %s", exc)


def _request_from_payload(body: dict[str, Any]) -> ApprovalRequest:
    turn_id = str(body.get("turn_id") or body.get("session_id") or "unknown")
    tool_name = str(body.get("tool_name") or "tool")
    tool_input = body.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"raw": str(tool_input)}

    digest_src = json.dumps(tool_input, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha1(digest_src).hexdigest()[:6]
    # Firmware caps promptId at 39 chars (src/data.h: char promptId[40]). Keep
    # the last 16 of turn_id (UUID suffix is unique enough) plus 6-char input
    # hash so the same turn issuing distinct tool calls still gets a unique id.
    turn_short = turn_id[-16:] if len(turn_id) > 16 else turn_id
    rid = f"c-{turn_short}-{digest}"
    if len(rid) > PROMPT_ID_LIMIT:
        rid = rid[:PROMPT_ID_LIMIT]

    hint = (
        tool_input.get("description")
        or tool_input.get("command")
        or tool_input.get("path")
        or tool_input.get("query")
        or _first_string_value(tool_input)
        or tool_name
    )
    hint = " ".join(str(hint).split())

    return ApprovalRequest(
        id=rid,
        tool=tool_name[:PROMPT_TOOL_LIMIT],
        hint=hint[:PROMPT_HINT_LIMIT],
    )


def _first_string_value(d: dict[str, Any]) -> str | None:
    for v in d.values():
        if isinstance(v, str) and v.strip():
            return v
    return None


def _owner_name() -> str:
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos
        first = (gecos or "").split(",", 1)[0].strip()
        if first:
            return first.split(" ", 1)[0]
    except Exception:  # noqa: BLE001
        pass
    return getpass.getuser() or "Codex"


async def main(config: DaemonConfig) -> None:
    daemon = Daemon(config)
    await daemon.run()
