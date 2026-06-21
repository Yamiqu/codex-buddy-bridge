from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any

PROMPT_TOOL_LIMIT = 19
PROMPT_HINT_LIMIT = 43
PROMPT_ID_LIMIT = 39  # firmware src/data.h: char promptId[40] (39 + null)
ENTRY_LIMIT = 5
ENTRY_TEXT_LIMIT = 60
INTERACTIVE_ID_LIMIT = 31
INTERACTIVE_HEADER_LIMIT = 40
INTERACTIVE_QUESTION_LIMIT = 4
INTERACTIVE_OPTION_LIMIT = 4
INTERACTIVE_TEXT_LIMIT = 140


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    tool: str
    hint: str


class PermissionDecision(str, Enum):
    APPROVE_ONCE = "once"
    DENY = "deny"


@dataclass(frozen=True)
class BuddyDecision:
    id: str
    decision: PermissionDecision


@dataclass(frozen=True)
class InteractiveQuestion:
    id: str
    header: str
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class InteractivePrompt:
    id: str
    call_id: str
    thread_id: str
    turn_id: str
    session_id: str
    status: str
    question_index: int
    question_total: int
    questions: tuple[InteractiveQuestion, ...]


@dataclass(frozen=True)
class InteractiveSelection:
    id: str
    question_index: int
    answer: int


def build_prompt_snapshot(
    approval: ApprovalRequest,
    running: int = 0,
    waiting: int = 1,
    total: int = 1,
    tokens: int = 0,
    tokens_total: int = 0,
    tokens_today: int = 0,
    usage: dict[str, Any] | None = None,
) -> str:
    payload = {
        "total": total,
        "running": running,
        "waiting": waiting,
        "tokens": tokens,
        "tokens_total": tokens_total,
        "tokens_today": tokens_today,
        "msg": _truncate(f"approve: {approval.tool}", PROMPT_TOOL_LIMIT + 9),
        "prompt": {
            "id": approval.id,
            "tool": _truncate(approval.tool, PROMPT_TOOL_LIMIT),
            "hint": _truncate(approval.hint, PROMPT_HINT_LIMIT),
        },
    }
    if usage is not None:
        payload["usage"] = usage
    return _encode_line(payload)


def build_clear_snapshot() -> str:
    return _encode_line(
        {
            "total": 0,
            "running": 0,
            "waiting": 0,
            "completed": False,
            "msg": "Codex idle",
        }
    )


def build_session_state_snapshot(
    running: int = 0,
    waiting: int = 0,
    total: int = 0,
    tokens: int = 0,
    tokens_total: int = 0,
    tokens_today: int = 0,
    msg: str | None = None,
    interactive: InteractivePrompt | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    state_msg = msg if msg is not None else ("Codex running" if running else "Codex idle")
    payload: dict[str, Any] = {
        "total": total,
        "running": running,
        "waiting": waiting,
        "tokens": tokens,
        "tokens_total": tokens_total,
        "tokens_today": tokens_today,
        "completed": False,
        "msg": state_msg,
    }
    if interactive is not None:
        payload["interactive"] = _interactive_payload(interactive)
    if usage is not None:
        payload["usage"] = usage
    return _encode_line(payload)


def build_state_snapshot(snapshot: dict[str, Any]) -> str:
    """Encode an arbitrary heartbeat snapshot dict.

    The daemon's SessionState owns the field choices; this function only
    handles wire encoding so all frames go through one path.
    """
    return _encode_line(snapshot)


def build_time_frame(epoch_seconds: int, tz_offset_seconds: int) -> str:
    return _encode_line({"time": [epoch_seconds, tz_offset_seconds]})


def build_owner_frame(name: str) -> str:
    return _encode_line({"cmd": "owner", "name": name})


def parse_permission_decision(line: str | bytes) -> BuddyDecision | None:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        obj: dict[str, Any] = json.loads(line.strip())
    except json.JSONDecodeError:
        return None

    if obj.get("cmd") != "permission":
        return None
    request_id = obj.get("id")
    raw_decision = obj.get("decision")
    if not isinstance(request_id, str) or not isinstance(raw_decision, str):
        return None
    try:
        decision = PermissionDecision(raw_decision)
    except ValueError:
        return None
    return BuddyDecision(id=request_id, decision=decision)


def parse_interactive_selection(line: str | bytes) -> InteractiveSelection | None:
    obj = _parse_json_object(line)
    if obj is None or obj.get("cmd") != "interactive_select":
        return None
    prompt_id = obj.get("id")
    question_index = obj.get("question_index")
    answer = obj.get("answer")
    if not isinstance(prompt_id, str) or not isinstance(question_index, int) or not isinstance(answer, int):
        return None
    return InteractiveSelection(id=prompt_id, question_index=question_index, answer=answer)


def truncate_entry(text: str) -> str:
    return _truncate(text, ENTRY_TEXT_LIMIT)


def _encode_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"


def _interactive_payload(prompt: InteractivePrompt) -> dict[str, Any]:
    return {
        "id": _truncate(prompt.id, INTERACTIVE_ID_LIMIT),
        "call_id": _truncate(prompt.call_id, INTERACTIVE_ID_LIMIT),
        "turn_id": _truncate(prompt.turn_id, PROMPT_ID_LIMIT),
        "status": _truncate(prompt.status, 16),
        "question_count": prompt.question_total,
        "question_index": prompt.question_index,
        "questions": [
            {
                "id": _truncate(question.id, INTERACTIVE_ID_LIMIT),
                "header": _truncate(question.header, INTERACTIVE_HEADER_LIMIT),
                "question": _truncate(question.question, INTERACTIVE_TEXT_LIMIT),
                "options": [_truncate(option, INTERACTIVE_TEXT_LIMIT) for option in question.options],
            }
            for question in prompt.questions[:INTERACTIVE_QUESTION_LIMIT]
        ],
    }


def _parse_json_object(line: str | bytes) -> dict[str, Any] | None:
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        obj = json.loads(line.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _truncate(value: str, limit: int) -> str:
    return value[:limit].rstrip()
