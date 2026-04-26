#!/usr/bin/env python3
"""Codex PermissionRequest hook → ClaudeCodeBuddy BLE.

Reads the hook payload from stdin, asks the daemon (over Unix socket) for
the buddy's decision, and writes the Codex-required response JSON to stdout.

Failure modes (daemon unreachable, BLE disconnected, buddy timeout) all exit
with no stdout output, which Codex treats as "decline to decide" — the
native approval prompt then continues. The hook never blocks Codex on
hardware failures.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from codex_buddy_bridge.ipc import DEFAULT_SOCKET_PATH, send_and_wait  # noqa: E402

# Codex hook stdout must contain only the response JSON; route stray prints
# to stderr until we're ready to emit.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

# Internal client wait must be < the hook's timeout in hooks.json (115s).
CLIENT_WAIT_SECONDS = 110.0


def _emit_decision(behavior: str, message: str | None = None) -> None:
    decision: dict[str, object] = {"behavior": behavior}
    if message:
        decision["message"] = message
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }
    _real_stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    _real_stdout.flush()


def _decline() -> None:
    """Exit 0 with no stdout. Codex falls back to the native approval prompt."""
    _real_stdout.flush()


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"hook: bad stdin JSON: {exc}", file=sys.stderr)
        _decline()
        return 0

    socket_path = os.environ.get("CODEX_BUDDY_SOCKET", DEFAULT_SOCKET_PATH)

    request = {
        "event": "permission_request",
        "payload": {
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
            "tool_name": event.get("tool_name"),
            "tool_input": event.get("tool_input") or {},
            "cwd": event.get("cwd"),
        },
    }

    response = send_and_wait(socket_path, request, timeout=CLIENT_WAIT_SECONDS)
    if response is None:
        print("hook: daemon unreachable or no response; declining", file=sys.stderr)
        _decline()
        return 0

    decision = response.get("decision")
    if decision == "allow":
        _emit_decision("allow")
    elif decision == "deny":
        _emit_decision("deny", "Denied via ClaudeCodeBuddy")
    else:
        print(f"hook: daemon returned {decision!r}; declining", file=sys.stderr)
        _decline()
    return 0


if __name__ == "__main__":
    sys.exit(main())
