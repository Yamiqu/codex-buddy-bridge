"""Newline-delimited JSON over a Unix domain socket.

The daemon runs ``serve`` to handle hook events. Hook scripts use the sync
helpers (``send_oneshot`` / ``send_and_wait``) so they don't pull in asyncio
or any third-party deps; only stdlib.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Dict, Optional

DEFAULT_SOCKET_PATH = "/tmp/codex-buddy.sock"

EventHandler = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


async def serve(socket_path: str, handler: EventHandler) -> asyncio.AbstractServer:
    """Listen on ``socket_path`` and call ``handler`` for each request line.

    The handler may return a dict, which is written back as a single newline
    response, or ``None`` for fire-and-forget events. Caller is responsible
    for ``server.close()`` / ``server.wait_closed()`` on shutdown.
    """
    log = logging.getLogger("codex-buddy.ipc")
    _unlink_if_exists(socket_path)

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                log.warning("Bad IPC line: %s", exc)
                return
            if not isinstance(payload, dict):
                return
            try:
                response = await handler(payload)
            except Exception:  # noqa: BLE001 - daemon must keep serving
                log.exception("IPC handler raised")
                response = {"decision": "error"}
            if response is not None:
                writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
                with contextlib.suppress(Exception):
                    await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    server = await asyncio.start_unix_server(on_connection, path=socket_path)
    os.chmod(socket_path, 0o600)
    log.info("IPC server listening on %s", socket_path)
    return server


def _unlink_if_exists(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def send_oneshot(socket_path: str, payload: dict[str, Any], timeout: float = 2.0) -> bool:
    """Send a single JSON line and close. Returns False on any failure."""
    try:
        with _connect(socket_path, timeout) as sock:
            sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def send_and_wait(
    socket_path: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any] | None:
    """Send a JSON line and read one JSON response line. Returns None on any failure."""
    try:
        with _connect(socket_path, timeout) as sock:
            sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            buf = bytearray()
            sock.settimeout(timeout)
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 65536:
                    return None
            if b"\n" not in buf:
                return None
            line, _, _ = buf.partition(b"\n")
            response = json.loads(line.decode("utf-8"))
            return response if isinstance(response, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _connect(socket_path: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path)
    return sock
