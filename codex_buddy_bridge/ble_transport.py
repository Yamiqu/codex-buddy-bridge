from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Optional, Union
import asyncio
import logging

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

LineHandler = Callable[[bytes], Union[Awaitable[None], None]]


class BleTransport:
    """Single-shot BLE NUS client. Use BleConnectionManager for auto-reconnect."""

    def __init__(self, device_name_prefix: str = "Claude-", address: str | None = None):
        self.device_name_prefix = device_name_prefix
        self.address = address
        self._client = None
        self._line_buffer = bytearray()
        self._on_line: LineHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log = logging.getLogger("codex-buddy.ble")

    @property
    def is_connected(self) -> bool:
        return self._client is not None and getattr(self._client, "is_connected", False)

    async def connect(self, on_line: LineHandler, scan_timeout: float = 20.0) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise RuntimeError(
                "Missing BLE dependency. Install with: python3 -m pip install bleak"
            ) from exc

        self._on_line = on_line
        self._loop = asyncio.get_running_loop()
        if self.address:
            device = self.address
        else:
            self._log.info("Scanning for BLE device prefix %r (timeout=%.1fs)", self.device_name_prefix, scan_timeout)
            found = await BleakScanner.find_device_by_filter(
                lambda d, _: bool(d.name and d.name.startswith(self.device_name_prefix)),
                timeout=scan_timeout,
            )
            if found is None:
                raise RuntimeError(f"No BLE device found with prefix {self.device_name_prefix!r}")
            device = found
            self._log.info("Found %s (%s)", found.name, found.address)

        client = BleakClient(device)
        await client.connect()
        await client.start_notify(NUS_TX_UUID, self._handle_notify)
        self._client = client
        self._log.info("Connected to buddy BLE")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                self._log.debug("Disconnect raised: %s", exc)
            self._client = None

    async def write_line(self, line: str) -> None:
        if self._client is None:
            return
        data = line.encode("utf-8")
        for start in range(0, len(data), 160):
            await self._client.write_gatt_char(NUS_RX_UUID, data[start : start + 160], response=False)
            await asyncio.sleep(0.01)

    def _handle_notify(self, _sender: object, data: bytearray) -> None:
        self._line_buffer.extend(data)
        while True:
            newline_positions = [pos for pos in (self._line_buffer.find(b"\n"), self._line_buffer.find(b"\r")) if pos >= 0]
            if not newline_positions:
                return
            pos = min(newline_positions)
            line = bytes(self._line_buffer[:pos])
            del self._line_buffer[: pos + 1]
            if line and self._on_line is not None:
                self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        assert self._on_line is not None
        result = self._on_line(line)
        if asyncio.iscoroutine(result) and self._loop is not None:
            asyncio.run_coroutine_threadsafe(result, self._loop)


class BleConnectionManager:
    """Keeps a BleTransport connected, retrying with exponential backoff."""

    INITIAL_BACKOFF = 1.0
    MAX_BACKOFF = 60.0

    def __init__(
        self,
        transport: BleTransport,
        on_line: LineHandler,
        on_connected: Callable[[BleTransport], Awaitable[None]] | None = None,
    ):
        self._transport = transport
        self._on_line = on_line
        self._on_connected = on_connected
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._log = logging.getLogger("codex-buddy.ble.manager")

    @property
    def transport(self) -> BleTransport:
        return self._transport

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected

    async def write_line(self, line: str) -> None:
        if self._transport.is_connected:
            try:
                await self._transport.write_line(line)
            except Exception as exc:  # noqa: BLE001 - reconnect loop will recover
                self._log.warning("write_line failed: %s; will reconnect", exc)
                await self._transport.close()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ble-reconnect-loop")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._transport.close()

    async def _run(self) -> None:
        backoff = self.INITIAL_BACKOFF
        while not self._stopped.is_set():
            try:
                await self._transport.connect(self._on_line)
            except Exception as exc:  # noqa: BLE001 - any failure → backoff
                self._log.warning("BLE connect failed: %s; retrying in %.1fs", exc, backoff)
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.MAX_BACKOFF)
                continue

            backoff = self.INITIAL_BACKOFF
            if self._on_connected is not None:
                try:
                    await self._on_connected(self._transport)
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("on_connected callback failed: %s", exc)

            while not self._stopped.is_set() and self._transport.is_connected:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

            self._log.info("BLE connection lost; will reconnect")
            await self._transport.close()
