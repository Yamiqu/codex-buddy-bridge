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
from urllib.parse import urlparse

DEFAULT_TCP_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 8765


def supports_unix_sockets() -> bool:
    return hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")


def _default_endpoint() -> str:
    if supports_unix_sockets():
        return "/tmp/codex-buddy.sock"
    return f"tcp://{DEFAULT_TCP_HOST}:{DEFAULT_TCP_PORT}"


DEFAULT_SOCKET_PATH = _default_endpoint()

EventHandler = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]


async def serve(socket_path: str, handler: EventHandler) -> asyncio.AbstractServer:
    """Listen on ``socket_path`` and call ``handler`` for each request line.

    The handler may return a dict, which is written back as a single newline
    response, or ``None`` for fire-and-forget events. Caller is responsible
    for ``server.close()`` / ``server.wait_closed()`` on shutdown.
    """
    log = logging.getLogger("codex-buddy.ipc")
    endpoint = _parse_endpoint(socket_path)
    if endpoint["kind"] == "unix":
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

    if endpoint["kind"] == "unix":
        server = await asyncio.start_unix_server(on_connection, path=socket_path)
        os.chmod(socket_path, 0o600)
    else:
        server = await asyncio.start_server(
            on_connection,
            host=endpoint["host"],
            port=endpoint["port"],
        )
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
    endpoint = _parse_endpoint(socket_path)
    if endpoint["kind"] == "tcp":
        return socket.create_connection((endpoint["host"], endpoint["port"]), timeout=timeout)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        return sock
    except Exception:
        sock.close()
        raise


def endpoint_has_filesystem_artifact(socket_path: str) -> bool:
    return _parse_endpoint(socket_path)["kind"] == "unix"


def _parse_endpoint(socket_path: str) -> dict[str, Any]:
    if socket_path.startswith("tcp://"):
        parsed = urlparse(socket_path)
        if not parsed.hostname or parsed.port is None:
            raise ValueError(f"Invalid TCP IPC endpoint: {socket_path!r}")
        return {"kind": "tcp", "host": parsed.hostname, "port": parsed.port}

    if not supports_unix_sockets():
        raise RuntimeError(
            "Unix domain sockets are not supported on this platform; "
            "set CODEX_BUDDY_SOCKET to a tcp://127.0.0.1:PORT endpoint."
        )
    return {"kind": "unix", "path": socket_path}
