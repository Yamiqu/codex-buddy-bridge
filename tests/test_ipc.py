import asyncio
import os
import socket
import tempfile
import threading
import unittest

from codex_buddy_bridge import ipc


class IpcTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = _make_test_endpoint(self.tmpdir)

    def tearDown(self):
        if ipc.endpoint_has_filesystem_artifact(self.socket_path):
            for path in (self.socket_path,):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        os.rmdir(self.tmpdir)

    def _serve_in_thread(self, handler, ready: threading.Event):
        loop = asyncio.new_event_loop()
        server_holder: dict = {}

        async def boot():
            server_holder["server"] = await ipc.serve(self.socket_path, handler)
            ready.set()
            try:
                await server_holder["server"].serve_forever()
            except asyncio.CancelledError:
                pass

        def thread_target():
            try:
                loop.run_until_complete(boot())
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()

        thread = threading.Thread(target=thread_target, daemon=True)
        thread.start()

        def stop():
            async def _stop():
                server_holder["server"].close()
                await server_holder["server"].wait_closed()
            try:
                asyncio.run_coroutine_threadsafe(_stop(), loop).result(timeout=2)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)

        return stop

    def test_oneshot_event_dispatched(self):
        received = []

        async def handler(payload):
            received.append(payload)
            return None

        ready = threading.Event()
        stop = self._serve_in_thread(handler, ready)
        ready.wait(timeout=2)
        try:
            ok = ipc.send_oneshot(self.socket_path, {"event": "ping", "payload": {"x": 1}})
            self.assertTrue(ok)
            for _ in range(20):
                if received:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["event"], "ping")
            self.assertEqual(received[0]["payload"]["x"], 1)
        finally:
            try:
                stop()
            except Exception:
                pass

    def test_send_and_wait_returns_response(self):
        async def handler(payload):
            return {"decision": "allow", "request_id": payload["payload"]["id"]}

        ready = threading.Event()
        stop = self._serve_in_thread(handler, ready)
        ready.wait(timeout=2)
        try:
            response = ipc.send_and_wait(
                self.socket_path,
                {"event": "permission_request", "payload": {"id": "abc"}},
                timeout=2.0,
            )
            self.assertIsNotNone(response)
            self.assertEqual(response["decision"], "allow")
            self.assertEqual(response["request_id"], "abc")
        finally:
            try:
                stop()
            except Exception:
                pass

    def test_send_oneshot_returns_false_when_socket_missing(self):
        ok = ipc.send_oneshot(_missing_endpoint(), {"event": "x"})
        self.assertFalse(ok)

    def test_send_and_wait_returns_none_when_socket_missing(self):
        result = ipc.send_and_wait(_missing_endpoint(), {"event": "x"}, timeout=0.5)
        self.assertIsNone(result)


def _make_test_endpoint(tmpdir: str) -> str:
    if ipc.supports_unix_sockets():
        return os.path.join(tmpdir, "test.sock")
    return f"tcp://127.0.0.1:{_reserve_tcp_port()}"


def _missing_endpoint() -> str:
    if ipc.supports_unix_sockets():
        return "/tmp/codex-buddy-does-not-exist.sock"
    return f"tcp://127.0.0.1:{_reserve_tcp_port()}"


def _reserve_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
