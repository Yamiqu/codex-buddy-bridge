#!/usr/bin/env python3
"""Codex session_start hook → ClaudeCodeBuddy daemon.

Forwards session_start events to the bridge daemon over Unix socket.
Fire-and-forget: no response expected or needed.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from codex_buddy_bridge.ipc import DEFAULT_SOCKET_PATH, send_oneshot  # noqa: E402

# Stray prints go to stderr so stdout stays clean.
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
        "event": "session_start",
        "payload": {
            "source": event.get("source", "startup"),
            "session_id": event.get("session_id"),
        },
    }

    ok = send_oneshot(socket_path, payload, timeout=2.0)
    if not ok:
        print("hook: daemon unreachable (non-fatal)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
