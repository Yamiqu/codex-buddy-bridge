import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from codex_buddy_bridge import daemon as daemon_module
from codex_buddy_bridge import ipc
from codex_buddy_bridge.daemon import Daemon, DaemonConfig, _request_from_payload
from codex_buddy_bridge.protocol import PROMPT_ID_LIMIT


class FakeBleTransport:
    """Stand-in for the real BleTransport. Records every line the daemon
    would send and exposes deliver() so a test can simulate a buddy notify."""

    instances: list = []

    def __init__(self, device_name_prefix: str = "Claude-", address=None):
        self.device_name_prefix = device_name_prefix
        self.address = address
        self.lines: list[str] = []
        self._on_line = None
        self.is_connected = False
        self.connect_calls = 0
        self.close_calls = 0
        self.fail_connect: Exception | None = None
        FakeBleTransport.instances.append(self)

    async def connect(self, on_line, scan_timeout: float = 20.0) -> None:
        self.connect_calls += 1
        if self.fail_connect is not None:
            raise self.fail_connect
        self._on_line = on_line
        self.is_connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self.is_connected = False

    async def write_line(self, line: str) -> None:
        self.lines.append(line)

    async def deliver(self, line: bytes) -> None:
        if self._on_line is None:
            return
        result = self._on_line(line)
        if asyncio.iscoroutine(result):
            await result


class _OnDemandDaemonTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeBleTransport.instances.clear()
        self.transport_factory = patch.object(daemon_module, "BleTransport", FakeBleTransport)
        self.transport_factory.start()

        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "test.sock")
        self.config = DaemonConfig(
            socket_path=self.socket_path,
            device_prefix="Claude-",
            address=None,
            permission_wait=2.0,
            connect_timeout=2.0,
        )
        self.daemon = Daemon(self.config)
        self.server = await ipc.serve(self.socket_path, self.daemon._handle_event)

    async def asyncTearDown(self):
        self.transport_factory.stop()
        self.server.close()
        await self.server.wait_closed()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        os.rmdir(self.tmpdir)


class PermissionRequestFlowTests(_OnDemandDaemonTestBase):
    async def test_approval_round_trip_acquires_then_releases_ble(self):
        async def fire_request():
            return await asyncio.to_thread(
                ipc.send_and_wait,
                self.socket_path,
                {
                    "event": "permission_request",
                    "payload": {
                        "session_id": "s1",
                        "turn_id": "019dc346-2714-7852-b8c2-57a96ff90860",
                        "tool_name": "Bash",
                        "tool_input": {"command": "ls", "description": "list dir"},
                    },
                },
                5.0,
            )

        request_task = asyncio.create_task(fire_request())

        prompt_line = None
        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances:
                transport = FakeBleTransport.instances[-1]
                for line in transport.lines:
                    if '"prompt"' in line:
                        prompt_line = line
                        break
                if prompt_line is not None:
                    break

        self.assertIsNotNone(prompt_line, "daemon never sent prompt frame")
        prompt = json.loads(prompt_line)["prompt"]
        self.assertEqual(prompt["tool"], "Bash")
        self.assertLessEqual(len(prompt["id"]), PROMPT_ID_LIMIT)

        # Time + owner frames should have been sent before the prompt.
        all_lines = FakeBleTransport.instances[-1].lines
        self.assertTrue(any('"time"' in line for line in all_lines), "missing time frame")
        self.assertTrue(any('"cmd":"owner"' in line for line in all_lines), "missing owner frame")

        await FakeBleTransport.instances[-1].deliver(
            json.dumps({"cmd": "permission", "id": prompt["id"], "decision": "once"}).encode() + b"\n"
        )

        response = await request_task
        self.assertIsNotNone(response)
        self.assertEqual(response["decision"], "allow")
        self.assertEqual(response["request_id"], prompt["id"])

        # The daemon must release BLE so Claude Hardware Buddy can take over.
        transport = FakeBleTransport.instances[-1]
        self.assertEqual(transport.close_calls, 1)

        # A clear snapshot was sent before disconnect.
        last_line = transport.lines[-1]
        self.assertNotIn('"prompt"', last_line)

    async def test_no_buddy_returned_when_connect_fails(self):
        # Pre-create the transport instance the daemon will get and arm the failure.
        # daemon constructs its own; simulate by patching the factory.
        original = FakeBleTransport.__init__

        def init_with_failure(self, *a, **kw):
            original(self, *a, **kw)
            self.fail_connect = RuntimeError("device not advertising")

        with patch.object(FakeBleTransport, "__init__", init_with_failure):
            response = await asyncio.to_thread(
                ipc.send_and_wait,
                self.socket_path,
                {
                    "event": "permission_request",
                    "payload": {
                        "session_id": "s1",
                        "turn_id": "t1",
                        "tool_name": "Bash",
                        "tool_input": {"command": "ls"},
                    },
                },
                5.0,
            )

        self.assertIsNotNone(response)
        self.assertEqual(response["decision"], "no_buddy")

    async def test_timeout_returned_when_buddy_doesnt_press(self):
        self.daemon.config.permission_wait = 0.2

        response = await asyncio.to_thread(
            ipc.send_and_wait,
            self.socket_path,
            {
                "event": "permission_request",
                "payload": {
                    "session_id": "s1",
                    "turn_id": "t1",
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls"},
                },
            },
            5.0,
        )
        self.assertIsNotNone(response)
        self.assertEqual(response["decision"], "timeout")

        # Even on timeout, BLE is released.
        self.assertEqual(FakeBleTransport.instances[-1].close_calls, 1)

    async def test_session_start_and_stop_events_dont_touch_ble(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "session_start", "payload": {"session_id": "s1", "cwd": "/repo", "source": "startup"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "stop_reason": "done"}},
        )
        # Give the server a moment to dispatch.
        await asyncio.sleep(0.1)
        self.assertEqual(len(FakeBleTransport.instances), 0)


class RequestSynthesisTests(unittest.TestCase):
    def test_id_is_stable_for_same_input(self):
        req_a = _request_from_payload(
            {"turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        )
        req_b = _request_from_payload(
            {"turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        )
        self.assertEqual(req_a.id, req_b.id)
        self.assertTrue(req_a.id.startswith("c-"))

    def test_different_tool_input_yields_different_id(self):
        req_a = _request_from_payload(
            {"turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "ls"}}
        )
        req_b = _request_from_payload(
            {"turn_id": "t1", "tool_name": "Bash", "tool_input": {"command": "rm"}}
        )
        self.assertNotEqual(req_a.id, req_b.id)

    def test_hint_falls_back_through_known_keys(self):
        req = _request_from_payload(
            {"turn_id": "t1", "tool_name": "Edit", "tool_input": {"path": "/repo/foo.py"}}
        )
        self.assertIn("foo.py", req.hint)

    def test_id_fits_firmware_buffer_for_real_uuid(self):
        req = _request_from_payload(
            {
                "turn_id": "019dc346-2714-7852-b8c2-57a96ff90860",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /tmp/very/long/path"},
            }
        )
        self.assertLessEqual(len(req.id), PROMPT_ID_LIMIT)
        self.assertTrue(req.id.startswith("c-"))


if __name__ == "__main__":
    unittest.main()
