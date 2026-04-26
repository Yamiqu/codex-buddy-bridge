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


def build_prompt_snapshot(approval: ApprovalRequest) -> str:
    return _encode_line(
        {
            "total": 1,
            "running": 0,
            "waiting": 1,
            "msg": _truncate(f"approve: {approval.tool}", PROMPT_TOOL_LIMIT + 9),
            "prompt": {
                "id": approval.id,
                "tool": _truncate(approval.tool, PROMPT_TOOL_LIMIT),
                "hint": _truncate(approval.hint, PROMPT_HINT_LIMIT),
            },
        }
    )


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


def truncate_entry(text: str) -> str:
    return _truncate(text, ENTRY_TEXT_LIMIT)


def _encode_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"


def _truncate(value: str, limit: int) -> str:
    return value[:limit].rstrip()
