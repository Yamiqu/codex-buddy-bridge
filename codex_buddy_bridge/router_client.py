from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import uuid
from pathlib import Path
from typing import Any

from . import ipc
from .protocol import InteractivePrompt

ROUTER_SOCKET_DIR = Path("/tmp/codex-ipc")
ROUTER_SOCKET_GLOB = "ipc-*.sock"
ROUTER_PROTOCOL_VERSION = 0


def build_user_input_response(
    prompt: InteractivePrompt,
    answers: tuple[int | None, ...],
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {"answers": {}}
    for answer_idx, question in zip(answers, prompt.questions, strict=True):
        if answer_idx is None or answer_idx < 0 or answer_idx >= len(question.options):
            return None
        payload["answers"][question.id] = {"answers": [question.options[answer_idx]]}
    return payload


def find_matching_user_input_request_id(
    requests: list[dict[str, Any]],
    prompt: InteractivePrompt,
) -> str | None:
    expected_question_ids = tuple(question.id for question in prompt.questions)
    best_id: str | None = None
    best_score = -1
    pending_ids: list[str] = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        if request.get("method") != "item/tool/requestUserInput":
            continue
        if request.get("completed") is True:
            continue
        request_id = request.get("id")
        params = request.get("params")
        if not isinstance(request_id, (str, int)) or not isinstance(params, dict):
            continue
        pending_ids.append(str(request_id))

        item_id = params.get("itemId")
        turn_id = params.get("turnId")
        questions = params.get("questions")
        request_question_ids: tuple[str, ...] = ()
        if isinstance(questions, list):
            collected: list[str] = []
            for question in questions:
                if not isinstance(question, dict):
                    continue
                question_id = question.get("id")
                if isinstance(question_id, str) and question_id:
                    collected.append(question_id)
            request_question_ids = tuple(collected)

        score = 0
        if item_id == prompt.call_id:
            score += 8
        if turn_id == prompt.turn_id:
            score += 4
        if request_question_ids and request_question_ids == expected_question_ids:
            score += 2
        if score > best_score:
            best_id = str(request_id)
            best_score = score

    if best_score > 0:
        return best_id
    if len(pending_ids) == 1:
        return pending_ids[0]
    return None


def resolve_router_socket() -> Path | None:
    if not ipc.supports_unix_sockets():
        return None

    from_env = os.environ.get("CODEX_IPC_SOCKET")
    if from_env:
        candidate = Path(from_env).expanduser()
        if candidate.is_socket():
            return candidate

    if not ROUTER_SOCKET_DIR.exists():
        return None
    sockets = [path for path in ROUTER_SOCKET_DIR.glob(ROUTER_SOCKET_GLOB) if path.is_socket()]
    if not sockets:
        return None
    sockets.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return sockets[0]


class CodexRouterClient:
    def __init__(self, log: logging.Logger):
        self._log = log
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._conversation_requests: dict[str, list[dict[str, Any]]] = {}
        self._client_id: str | None = None
        self._socket_path: Path | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="codex-buddy-router")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._disconnect()

    async def submit_user_input_response(
        self,
        prompt: InteractivePrompt,
        answers: tuple[int | None, ...],
    ) -> bool:
        response = build_user_input_response(prompt, answers)
        if response is None:
            return False

        match = await self._wait_for_matching_request(prompt)
        if match is None:
            self._log.warning(
                "No live request_user_input request found for prompt=%s turn=%s call=%s",
                prompt.id,
                prompt.turn_id,
                prompt.call_id,
            )
            return False
        conversation_id, request_id = match

        result = await self.send_request(
            "thread-follower-submit-user-input",
            {
                "conversationId": conversation_id,
                "requestId": request_id,
                "response": response,
            },
            timeout=20.0,
        )
        return result is not None

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        connected = await self._wait_until_connected(timeout=min(timeout, 5.0))
        if not connected or self._writer is None:
            return None

        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_frame(
                {
                    "type": "request",
                    "requestId": request_id,
                    "sourceClientId": self._client_id or "initializing-client",
                    "version": ROUTER_PROTOCOL_VERSION,
                    "method": method,
                    "params": params,
                }
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self._log.debug("Router request failed: method=%s error=%s", method, exc)
            return None
        finally:
            self._pending.pop(request_id, None)

    async def _wait_until_connected(self, timeout: float) -> bool:
        self.start()
        if self._connected_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _wait_for_matching_request(
        self,
        prompt: InteractivePrompt,
    ) -> tuple[str, str] | None:
        for _ in range(20):
            exact_requests = self._conversation_requests.get(prompt.thread_id) or []
            request_id = find_matching_user_input_request_id(exact_requests, prompt)
            if request_id is not None:
                return prompt.thread_id, request_id

            fallback_matches: list[tuple[str, str]] = []
            for conversation_id, requests in self._conversation_requests.items():
                if conversation_id == prompt.thread_id:
                    continue
                request_id = find_matching_user_input_request_id(requests, prompt)
                if request_id is not None:
                    fallback_matches.append((conversation_id, request_id))
            if len(fallback_matches) == 1:
                self._log.debug(
                    "Interactive request matched via fallback conversation: prompt=%s expected=%s actual=%s",
                    prompt.id,
                    prompt.thread_id,
                    fallback_matches[0][0],
                )
                return fallback_matches[0]
            await asyncio.sleep(0.1)
        return None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            socket_path = resolve_router_socket()
            if socket_path is None:
                await asyncio.sleep(1.0)
                continue
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(str(socket_path))
                self._socket_path = socket_path
                await self._initialize()
                self._connected_event.set()
                self._log.debug("Connected to Codex IPC router: %s", socket_path)
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.debug("Router connection loop error: %s", exc)
            finally:
                await self._disconnect()
            await asyncio.sleep(1.0)

    async def _initialize(self) -> None:
        assert self._reader is not None
        assert self._writer is not None
        request_id = str(uuid.uuid4())
        await self._send_frame(
            {
                "type": "request",
                "requestId": request_id,
                "sourceClientId": "initializing-client",
                "version": ROUTER_PROTOCOL_VERSION,
                "method": "initialize",
                "params": {"clientType": "codex-buddy-bridge"},
            }
        )
        response = await self._read_frame()
        if response.get("type") != "response" or response.get("requestId") != request_id:
            raise RuntimeError("Unexpected initialize response from IPC router")
        result = response.get("result") or {}
        if not isinstance(result, dict) or not isinstance(result.get("clientId"), str):
            raise RuntimeError("IPC router initialize returned no clientId")
        self._client_id = result["clientId"]

    async def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            message = await self._read_frame()
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            if message_type == "response":
                request_id = str(message.get("requestId"))
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            if message_type == "broadcast":
                self._handle_broadcast(message)
                continue
            if message_type == "client-discovery-request":
                await self._send_frame(
                    {
                        "type": "client-discovery-response",
                        "requestId": message.get("requestId"),
                        "response": {"canHandle": False},
                    }
                )
                continue
            if message_type == "request":
                await self._send_frame(
                    {
                        "type": "response",
                        "requestId": message.get("requestId"),
                        "resultType": "error",
                        "error": "unsupported",
                    }
                )

    def _handle_broadcast(self, message: dict[str, Any]) -> None:
        if message.get("method") != "thread-stream-state-changed":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        change = params.get("change")
        if not isinstance(change, dict):
            return
        conversation_id = params.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id:
            conversation_state = change.get("conversationState")
            if isinstance(conversation_state, dict):
                conversation_id = conversation_state.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return

        change_type = change.get("type")
        if change_type == "snapshot":
            requests = change.get("requests")
            if isinstance(requests, list):
                self._conversation_requests[conversation_id] = [
                    request for request in requests if isinstance(request, dict)
                ]
            else:
                self._conversation_requests.setdefault(conversation_id, [])
            return

        if change_type != "patches":
            return
        patches = change.get("patches")
        if not isinstance(patches, list):
            return
        state: dict[str, Any] = {"requests": list(self._conversation_requests.get(conversation_id) or [])}
        for patch in patches:
            if not isinstance(patch, dict):
                continue
            path = patch.get("path")
            if not isinstance(path, list) or not path or path[0] != "requests":
                continue
            op = patch.get("op")
            if not isinstance(op, str):
                continue
            _apply_patch(state, path, op, patch.get("value"))
        requests = state.get("requests")
        if isinstance(requests, list):
            self._conversation_requests[conversation_id] = [
                request for request in requests if isinstance(request, dict)
            ]

    async def _disconnect(self) -> None:
        self._connected_event.clear()
        self._client_id = None
        self._socket_path = None
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.cancel()
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _send_frame(self, payload: dict[str, Any]) -> None:
        if self._writer is None:
            raise RuntimeError("IPC router writer is not connected")
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        frame = struct.pack("<I", len(data)) + data
        async with self._write_lock:
            self._writer.write(frame)
            await self._writer.drain()

    async def _read_frame(self) -> dict[str, Any]:
        if self._reader is None:
            raise RuntimeError("IPC router reader is not connected")
        header = await self._reader.readexactly(4)
        size = struct.unpack("<I", header)[0]
        data = await self._reader.readexactly(size)
        return json.loads(data.decode("utf-8"))


def _apply_patch(root: dict[str, Any], path: list[Any], op: str, value: Any) -> None:
    parent: Any = root
    for index, segment in enumerate(path[:-1]):
        next_segment = path[index + 1]
        if isinstance(parent, list) and isinstance(segment, int):
            while len(parent) <= segment:
                parent.append([] if isinstance(next_segment, int) else {})
            parent = parent[segment]
            continue
        if not isinstance(parent, dict) or not isinstance(segment, str):
            return
        child = parent.get(segment)
        if not isinstance(child, (dict, list)):
            child = [] if isinstance(next_segment, int) else {}
            parent[segment] = child
        parent = child

    if not path:
        return
    last = path[-1]
    if isinstance(parent, list) and isinstance(last, int):
        if op == "add":
            if last >= len(parent):
                parent.append(value)
            else:
                parent.insert(last, value)
            return
        if op == "replace":
            if 0 <= last < len(parent):
                parent[last] = value
            elif last == len(parent):
                parent.append(value)
            return
        if op == "remove":
            if 0 <= last < len(parent):
                parent.pop(last)
            return
        return

    if not isinstance(parent, dict) or not isinstance(last, str):
        return
    if op in {"add", "replace"}:
        parent[last] = value
    elif op == "remove":
        parent.pop(last, None)
