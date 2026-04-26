import json
import unittest

from codex_buddy_bridge.protocol import (
    ApprovalRequest,
    PermissionDecision,
    build_clear_snapshot,
    build_owner_frame,
    build_prompt_snapshot,
    build_state_snapshot,
    build_time_frame,
    parse_permission_decision,
    truncate_entry,
)


class ProtocolTests(unittest.TestCase):
    def test_builds_prompt_snapshot_with_firmware_field_limits(self):
        payload = build_prompt_snapshot(
            ApprovalRequest(
                id="codex-1",
                tool="shell command that is too long",
                hint="Allow command: pio run --environment m5stick-c-plus and upload",
            )
        )

        data = json.loads(payload)

        self.assertEqual(data["total"], 1)
        self.assertEqual(data["running"], 0)
        self.assertEqual(data["waiting"], 1)
        self.assertEqual(data["prompt"]["id"], "codex-1")
        self.assertEqual(data["prompt"]["tool"], "shell command that")
        self.assertEqual(data["prompt"]["hint"], "Allow command: pio run --environment m5stic")

    def test_builds_clear_snapshot_without_prompt(self):
        data = json.loads(build_clear_snapshot())

        self.assertEqual(data["total"], 0)
        self.assertEqual(data["running"], 0)
        self.assertEqual(data["waiting"], 0)
        self.assertNotIn("prompt", data)

    def test_state_snapshot_passes_through_dict(self):
        snap = {
            "total": 2,
            "running": 1,
            "waiting": 1,
            "msg": "approve: Bash",
            "entries": ["10:42 git push", "10:41 yarn test"],
            "prompt": {"id": "codex-x", "tool": "Bash", "hint": "rm -rf /tmp/foo"},
        }

        line = build_state_snapshot(snap)

        self.assertTrue(line.endswith("\n"))
        self.assertEqual(json.loads(line), snap)

    def test_time_and_owner_frames(self):
        time_line = build_time_frame(1775731234, -25200)
        self.assertEqual(json.loads(time_line), {"time": [1775731234, -25200]})

        owner_line = build_owner_frame("Felix")
        self.assertEqual(json.loads(owner_line), {"cmd": "owner", "name": "Felix"})

    def test_parses_permission_decision(self):
        decision = parse_permission_decision(
            '{"cmd":"permission","id":"codex-1","decision":"once"}\n'
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.id, "codex-1")
        self.assertEqual(decision.decision, PermissionDecision.APPROVE_ONCE)

    def test_ignores_unrelated_or_invalid_lines(self):
        self.assertIsNone(parse_permission_decision("not json"))
        self.assertIsNone(parse_permission_decision('{"cmd":"status"}'))
        self.assertIsNone(parse_permission_decision('{"cmd":"permission","id":"x","decision":"forever"}'))

    def test_truncate_entry_caps_long_text(self):
        line = truncate_entry("a" * 200)
        self.assertLessEqual(len(line), 60)


if __name__ == "__main__":
    unittest.main()
