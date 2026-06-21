#!/usr/bin/env python3
"""Codex UserPromptSubmit hook -> ClaudeCodeBuddy daemon.

Forwards per-turn message submission events so the buddy can wake up and show
running state even when the user keeps working inside an existing session.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from codex_buddy_bridge.ipc import DEFAULT_SOCKET_PATH, send_oneshot  # noqa: E402

_real_stdout = sys.stdout
sys.stdout = sys.stderr


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"hook: bad stdin JSON: {exc}", file=sys.stderr)
        return 0

    socket_path = os.environ.get("CODEX_BUDDY_SOCKET", DEFAULT_SOCKET_PATH)
    payload = {
        "event": "user_prompt_submit",
        "payload": {
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
        },
    }

    ok = send_oneshot(socket_path, payload, timeout=2.0)
    if not ok:
        print("hook: daemon unreachable (non-fatal)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
