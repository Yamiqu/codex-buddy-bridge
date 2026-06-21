import asyncio
import json
import os
import socket
import shutil
import tempfile
import unittest
from pathlib import Path
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


class FakeRouterClient:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.is_connected = True
        self.submit_ok = True
        self.submissions: list[tuple[object, tuple[int | None, ...]]] = []

    def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def submit_user_input_response(self, prompt, answers):
        self.submissions.append((prompt, answers))
        return self.submit_ok


def _latest_transport_with_lines():
    return next((transport for transport in reversed(FakeBleTransport.instances) if transport.lines), None)


class _OnDemandDaemonTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeBleTransport.instances.clear()
        self.transport_factory = patch.object(daemon_module, "BleTransport", FakeBleTransport)
        self.transport_factory.start()

        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = _make_test_endpoint(self.tmpdir)
        self.config = DaemonConfig(
            socket_path=self.socket_path,
            device_prefix="Claude-",
            address=None,
            permission_wait=2.0,
            connect_timeout=2.0,
            idle_state_sync_interval=0.05,
            session_scan_path=os.path.join(self.tmpdir, "sessions"),
            session_rescan_interval=0.05,
            token_ledger_path=os.path.join(self.tmpdir, "token-ledger.json"),
        )
        Path(self.config.session_scan_path).mkdir(parents=True, exist_ok=True)
        self.daemon = Daemon(self.config)
        self.daemon._router = FakeRouterClient()
        self.server = await ipc.serve(self.socket_path, self.daemon._handle_event)

    async def asyncTearDown(self):
        self.transport_factory.stop()
        if self.daemon._state_sync_task is not None:
            self.daemon._state_sync_task.cancel()
            try:
                await self.daemon._state_sync_task
            except asyncio.CancelledError:
                pass
        if self.daemon._interactive_task is not None:
            self.daemon._interactive_task.cancel()
            try:
                await self.daemon._interactive_task
            except asyncio.CancelledError:
                pass
        self.server.close()
        await self.server.wait_closed()
        if ipc.endpoint_has_filesystem_artifact(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _make_test_endpoint(tmpdir: str) -> str:
    if ipc.supports_unix_sockets():
        return os.path.join(tmpdir, "test.sock")
    return f"tcp://127.0.0.1:{_reserve_tcp_port()}"


def _reserve_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class PermissionRequestFlowTests(_OnDemandDaemonTestBase):
    def _write_session_file(self, relative_path: str, content: str = "{}\n") -> None:
        path = Path(self.config.session_scan_path) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    async def test_session_start_pushes_running_state_over_ble(self):
        self._write_session_file("2026/05/09/one.jsonl")
        self._write_session_file("2026/05/09/two.jsonl")

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "session_start", "payload": {"session_id": "s1", "cwd": "/repo", "source": "startup"}},
        )

        transport = None
        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = next((t for t in reversed(FakeBleTransport.instances) if t.lines), None)
            if transport is not None:
                break

        self.assertTrue(FakeBleTransport.instances, "session_start never triggered a BLE sync")
        self.assertIsNotNone(transport, "session_start never produced a BLE snapshot")
        self.assertTrue(any('"time"' in line for line in transport.lines), "missing time frame")
        self.assertTrue(any('"cmd":"owner"' in line for line in transport.lines), "missing owner frame")

        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["tokens"], 0)
        self.assertEqual(snapshot["tokens_today"], 0)
        self.assertEqual(snapshot["running"], 0)
        self.assertEqual(snapshot["waiting"], 0)
        self.assertEqual(transport.close_calls, 1)

    async def test_running_state_heartbeats_while_session_is_active(self):
        self.daemon.config.state_sync_interval = 0.05

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 2:
                break

        self.assertGreaterEqual(len(FakeBleTransport.instances), 2, "expected a heartbeat sync after initial state push")
        for transport in FakeBleTransport.instances[:2]:
            snapshot = json.loads(transport.lines[-1])
            self.assertEqual(snapshot["running"], 1)
            self.assertEqual(snapshot["waiting"], 0)

    async def test_idle_state_heartbeats_without_user_events(self):
        self._write_session_file("2026/05/09/one.jsonl")

        self.daemon._ensure_background_tasks()

        for _ in range(120):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 2 and all(
                transport.lines for transport in FakeBleTransport.instances[:2]
            ):
                break

        self.assertGreaterEqual(len(FakeBleTransport.instances), 2, "expected repeated idle heartbeats")
        for transport in FakeBleTransport.instances[:2]:
            snapshot = json.loads(transport.lines[-1])
            self.assertEqual(snapshot["running"], 0)
            self.assertEqual(snapshot["waiting"], 0)

    async def test_user_prompt_submit_pushes_running_state_over_ble(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break

        self.assertTrue(FakeBleTransport.instances, "user_prompt_submit never triggered a BLE sync")
        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "user_prompt_submit never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["running"], 1)
        self.assertEqual(snapshot["waiting"], 0)

    async def test_duplicate_user_prompt_submit_does_not_double_count_turn(self):
        event = {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}}
        await asyncio.to_thread(ipc.send_oneshot, self.socket_path, event)
        await asyncio.to_thread(ipc.send_oneshot, self.socket_path, event)

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break

        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "duplicate prompt never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["running"], 1)

    async def test_new_turn_in_same_session_replaces_stale_turn(self):
        self.daemon.config.state_sync_interval = 0.05

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t2"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["running"], 1)

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "turn_id": "t2", "stop_reason": "done"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if len(FakeBleTransport.instances) >= 2 and transport is not None:
                break

        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "turn aborted state sync never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["running"], 0)
        self.assertEqual(snapshot["msg"], "Codex idle")

    async def test_session_rescan_clears_running_turn_for_removed_session_file(self):
        session_id = "11111111-1111-1111-1111-111111111111"
        self._write_session_file(f"2026/05/11/rollout-2026-05-11T00-00-00-{session_id}.jsonl")

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": session_id, "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["running"], 1)

        shutil.rmtree(self.config.session_scan_path, ignore_errors=True)
        Path(self.config.session_scan_path).mkdir(parents=True, exist_ok=True)
        await self.daemon._rescan_session_total(trigger_sync=True)

        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 2 and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["running"], 0)
        self.assertEqual(snapshot["msg"], "Codex idle")

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

    async def test_stop_event_pushes_idle_state_over_ble(self):
        self._write_session_file(
            "2026/05/09/rollout-2026-05-09T00-00-00-s1.jsonl",
            '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":125}}}}\n',
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "turn_id": "t1", "stop_reason": "done"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if len(FakeBleTransport.instances) >= 2 and transport is not None:
                break

        self.assertGreaterEqual(len(FakeBleTransport.instances), 2, "stop never triggered a BLE sync")
        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "stop never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["tokens"], 125)
        self.assertEqual(snapshot["tokens_today"], 125)
        self.assertEqual(snapshot["running"], 0)
        self.assertEqual(snapshot["waiting"], 0)
        self.assertEqual(snapshot["msg"], "Codex idle")

    async def test_stop_without_turn_id_clears_session_turn(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "stop_reason": "interrupted"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if len(FakeBleTransport.instances) >= 2 and transport is not None:
                break

        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "stop without turn id never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["running"], 0)
        self.assertEqual(snapshot["msg"], "Codex idle")

    async def test_interactive_waiting_round_trip_restores_busy_when_running(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "interactive_start", "payload": {"session_id": "s1", "turn_id": "t1", "kind": "input"}},
        )
        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break
        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "interactive start never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["running"], 1)
        self.assertEqual(snapshot["waiting"], 1)
        self.assertEqual(snapshot["msg"], "input needed")

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "interactive_end", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )
        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                latest = json.loads(FakeBleTransport.instances[-1].lines[-1])
                if latest["waiting"] == 0:
                    snapshot = latest
                    break
        self.assertEqual(snapshot["running"], 1)
        self.assertEqual(snapshot["waiting"], 0)
        self.assertEqual(snapshot["msg"], "Codex running")

    async def test_interactive_and_permission_waiting_counts_stack(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "interactive_start", "payload": {"session_id": "s1", "turn_id": "t1", "kind": "choice"}},
        )
        await asyncio.sleep(0.05)
        self.daemon._session.on_waiting()
        self.daemon._request_state_sync()
        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break
        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "stacked waiting state never produced a BLE snapshot")
        snapshot = json.loads(transport.lines[-1])
        self.assertEqual(snapshot["waiting"], 2)
        self.assertEqual(snapshot["msg"], "approve: tool")

    async def test_interactive_end_is_idempotent_and_does_not_underflow(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "interactive_start", "payload": {"session_id": "s1", "turn_id": "t1", "kind": "input"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "interactive_end", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "interactive_end", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )
        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break
        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["waiting"], 0)

    async def test_interactive_inferred_from_request_user_input_and_function_output(self):
        sid = "11111111-2222-3333-4444-555555555555"
        path = Path(self.config.session_scan_path) / "2026/05/10" / f"rollout-2026-05-10T17-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "turn_context", "payload": {"turn_id": "t1"}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "name": "request_user_input",
                                "arguments": json.dumps(
                                    {
                                        "threadId": sid,
                                        "questions": [
                                            {
                                                "header": "Scope",
                                                "id": "scope",
                                                "question": "Which scope?",
                                                "options": [
                                                    {"label": "Local", "description": "local only"},
                                                    {"label": "Global", "description": "all"},
                                                    {"label": "", "description": "other"},
                                                ],
                                            }
                                        ],
                                    }
                                ),
                                "call_id": "call-1",
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.daemon._session_file_offsets[str(path)] = 0
        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.assertTrue(changed)
        self.assertEqual(self.daemon._session.waiting_out, 1)
        self.daemon._refresh_interactive_snapshot()
        self.assertIsNotNone(self.daemon._interactive_snapshot.prompt)
        self.assertEqual(self.daemon._interactive_snapshot.prompt.questions[0].options, ("Local", "Global"))

        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "function_call_output", "call_id": "call-1", "output": "{}"},
                    }
                )
                + "\n"
            )
        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.assertTrue(changed)
        self.assertEqual(self.daemon._session.waiting_out, 0)

    async def test_interactive_inferred_from_recent_tail_on_first_scan(self):
        sid = "99999999-2222-3333-4444-555555555555"
        path = Path(self.config.session_scan_path) / "2026/05/10" / f"rollout-2026-05-10T17-05-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "turn_context", "payload": {"turn_id": "t-recent"}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "name": "request_user_input",
                                "arguments": json.dumps(
                                    {
                                        "threadId": sid,
                                        "questions": [
                                            {
                                                "header": "Mode",
                                                "id": "mode",
                                                "question": "Which mode?",
                                                "options": [
                                                    {"label": "Fast", "description": "fast"},
                                                    {"label": "Safe", "description": "safe"},
                                                ],
                                            }
                                        ],
                                    }
                                ),
                                "call_id": "call-recent",
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )

        self.assertTrue(changed)
        self.assertEqual(self.daemon._session.waiting_out, 1)
        self.daemon._refresh_interactive_snapshot()
        self.assertIsNotNone(self.daemon._interactive_snapshot.prompt)
        self.assertEqual(self.daemon._interactive_snapshot.prompt.turn_id, "t-recent")

    async def test_interactive_inferred_end_on_turn_aborted(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        path = Path(self.config.session_scan_path) / "2026/05/10" / f"rollout-2026-05-10T17-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "turn_context", "payload": {"turn_id": "t1"}}),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "name": "request_user_input",
                                "arguments": json.dumps(
                                    {
                                        "threadId": sid,
                                        "questions": [
                                            {
                                                "header": "Mode",
                                                "id": "mode",
                                                "question": "Which mode?",
                                                "options": [
                                                    {"label": "Strict", "description": "strict"},
                                                    {"label": "Loose", "description": "loose"},
                                                ],
                                            }
                                        ],
                                    }
                                ),
                                "call_id": "call-2",
                            },
                        }
                    ),
                    json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "t1"}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.daemon._session_file_offsets[str(path)] = 0
        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.assertTrue(changed)
        self.assertEqual(self.daemon._session.waiting_out, 0)
        self.assertEqual(self.daemon._session.running, 0)

    async def test_turn_aborted_in_session_log_clears_running_without_stop_hook(self):
        session_id = "cccccccc-2222-3333-4444-555555555555"
        path = Path(self.config.session_scan_path) / "2026/05/10" / f"rollout-2026-05-10T17-00-00-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}}) + "\n",
            encoding="utf-8",
        )

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": session_id, "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["running"], 1)

        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "t1"}}) + "\n"
            )

        self.daemon._session_file_offsets[str(path)] = 0
        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.assertTrue(changed)
        self.assertEqual(self.daemon._session.running, 0)
        self.daemon._request_state_sync()

        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 2 and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["running"], 0)
        self.assertEqual(snapshot["msg"], "Codex idle")

    async def test_interactive_waiting_pushes_state_without_detail_payload(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        path = Path(self.config.session_scan_path) / "2026/05/10" / f"rollout-2026-05-10T17-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"type": "turn_context", "payload": {"turn_id": "t1"}})
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "arguments": json.dumps(
                            {
                                "threadId": sid,
                                "questions": [
                                    {
                                        "header": "Scope",
                                        "id": "scope",
                                        "question": "Which scope?",
                                        "options": [
                                            {"label": "Local", "description": "local only"},
                                            {"label": "Global", "description": "all"},
                                        ],
                                    },
                                    {
                                        "header": "Mode",
                                        "id": "mode",
                                        "question": "Which mode?",
                                        "options": [
                                            {"label": "Fast", "description": "fast"},
                                            {"label": "Safe", "description": "safe"},
                                        ],
                                    },
                                ],
                            }
                        ),
                        "call_id": "call-3",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.daemon._session_file_offsets[str(path)] = 0
        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.assertTrue(changed)
        self.daemon._refresh_interactive_snapshot()
        self.daemon._ensure_background_tasks()

        for _ in range(80):
            await asyncio.sleep(0.02)
            transport = _latest_transport_with_lines()
            if transport is not None:
                break

        transport = _latest_transport_with_lines()
        self.assertIsNotNone(transport, "interactive waiting never produced a BLE snapshot")
        self.assertTrue(FakeBleTransport.instances)
        self.assertFalse(any('"interactive"' in line for line in transport.lines))
        self.assertTrue(any('"waiting":1' in line for line in transport.lines))
        self.assertTrue(any('"msg":"choice needed"' in line for line in transport.lines))
        self.assertIsNone(self.daemon._interactive_task)
        self.assertEqual(self.daemon._interactive_snapshot.prompt.question_index, 0)
        self.assertEqual(self.daemon._interactive_snapshot.prompt.question_total, 2)
        self.assertEqual(self.daemon._interactive_snapshot.prompt.status, "input")

        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-3",
                            "output": "{\"answers\":{}}",
                        },
                    }
                )
                + "\n"
            )

        changed = daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.assertTrue(changed)
        self.daemon._refresh_interactive_snapshot()
        self.assertIsNone(self.daemon._interactive_snapshot.prompt)
        self.assertEqual(self.daemon._session.waiting_out, 0)

    async def test_interactive_does_not_start_router_submission_flow(self):
        sid = "bbbbbbbb-2222-3333-4444-555555555555"
        path = Path(self.config.session_scan_path) / "2026/05/10" / f"rollout-2026-05-10T17-00-00-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"type": "turn_context", "payload": {"turn_id": "t1"}})
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "arguments": json.dumps(
                            {
                                "threadId": sid,
                                "questions": [
                                    {
                                        "header": "Scope",
                                        "id": "scope",
                                        "question": "Which scope?",
                                        "options": [
                                            {"label": "Local", "description": "local only"},
                                            {"label": "Global", "description": "all"},
                                        ],
                                    }
                                ],
                            }
                        ),
                        "call_id": "call-router-fail",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.daemon._session_file_offsets[str(path)] = 0
        daemon_module._scan_interactive_events_from_files(
            self.config.session_scan_path,
            self.daemon._session_file_offsets,
            self.daemon._session_turn_by_file,
            self.daemon._interactive_calls,
            self.daemon._session,
            self.daemon._log,
        )
        self.daemon._refresh_interactive_snapshot()
        self.daemon._ensure_background_tasks()

        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break

        self.assertIsNone(self.daemon._interactive_task)
        self.assertEqual(self.daemon._router.submissions, [])

    async def test_background_rescan_updates_total_for_next_heartbeat(self):
        self._write_session_file("2026/05/09/one.jsonl")
        self.daemon.config.state_sync_interval = 0.05

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if any(json.loads(t.lines[-1])["total"] == 1 for t in FakeBleTransport.instances if t.lines):
                break

        first = next(
            json.loads(t.lines[-1])
            for t in FakeBleTransport.instances
            if t.lines and json.loads(t.lines[-1])["total"] == 1
        )
        self.assertEqual(first["total"], 1)

        session_file = Path(self.config.session_scan_path) / "2026/05/09/one.jsonl"
        session_file.unlink()

        for _ in range(120):
            await asyncio.sleep(0.02)
            with_lines = [t for t in FakeBleTransport.instances if t.lines]
            if len(with_lines) >= 2:
                latest = json.loads(with_lines[-1].lines[-1])
                if latest["total"] == 0:
                    break

        with_lines = [t for t in FakeBleTransport.instances if t.lines]
        latest = json.loads(with_lines[-1].lines[-1])
        self.assertEqual(latest["running"], 1)
        self.assertEqual(latest["total"], 0)
        self.assertEqual(latest["tokens"], 0)
        self.assertEqual(latest["tokens_today"], 0)

    async def test_stop_event_sends_incremental_session_token_delta(self):
        self._write_session_file(
            "2026/05/09/rollout-2026-05-09T00-00-00-s1.jsonl",
            (
                '{"type":"event_msg","payload":{"type":"token_count","info":null}}\n'
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":120}}}}\n'
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":90}}}}\n'
            ),
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "turn_id": "t1", "stop_reason": "done"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 2 and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["tokens"], 120)
        self.assertEqual(snapshot["tokens_today"], 120)

        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t2"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "turn_id": "t2", "stop_reason": "done"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 4 and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["tokens"], 0)
        self.assertEqual(snapshot["tokens_today"], 120)

    async def test_stop_event_only_sends_new_token_growth(self):
        session_path = "2026/05/09/rollout-2026-05-09T00-00-00-s1.jsonl"
        self._write_session_file(
            session_path,
            '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":120}}}}\n',
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t1"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "turn_id": "t1", "stop_reason": "done"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(FakeBleTransport.instances) >= 2 and FakeBleTransport.instances[-1].lines:
                break

        first_snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(first_snapshot["tokens"], 120)
        self.assertEqual(first_snapshot["tokens_today"], 120)

        self._write_session_file(
            session_path,
            (
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":120}}}}\n'
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":170}}}}\n'
            ),
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "user_prompt_submit", "payload": {"session_id": "s1", "turn_id": "t2"}},
        )
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "stop", "payload": {"session_id": "s1", "turn_id": "t2", "stop_reason": "done"}},
        )

        for _ in range(80):
            await asyncio.sleep(0.02)
            if FakeBleTransport.instances and FakeBleTransport.instances[-1].lines:
                break

        snapshot = json.loads(FakeBleTransport.instances[-1].lines[-1])
        self.assertEqual(snapshot["tokens"], 50)
        self.assertEqual(snapshot["tokens_today"], 170)
        ledger = json.loads(Path(self.config.token_ledger_path).read_text(encoding="utf-8"))
        self.assertEqual(ledger["total_tokens"], 170)
        self.assertEqual(len(ledger["daily_tokens"]), 1)
        self.assertEqual(next(iter(ledger["daily_tokens"].values())), 170)
        self.assertEqual(ledger["session_output_totals"]["s1"], 170)

    async def test_unknown_events_dont_touch_ble(self):
        await asyncio.to_thread(
            ipc.send_oneshot,
            self.socket_path,
            {"event": "bogus", "payload": {"session_id": "s1"}},
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
