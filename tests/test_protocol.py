import json
import unittest

from codex_buddy_bridge.protocol import (
    ApprovalRequest,
    InteractivePrompt,
    InteractiveQuestion,
    PermissionDecision,
    build_clear_snapshot,
    build_owner_frame,
    build_prompt_snapshot,
    build_session_state_snapshot,
    build_state_snapshot,
    build_time_frame,
    parse_interactive_selection,
    parse_permission_decision,
    truncate_entry,
)
from codex_buddy_bridge.router_client import (
    build_user_input_response,
    find_matching_user_input_request_id,
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
        self.assertEqual(data["tokens"], 0)
        self.assertEqual(data["tokens_total"], 0)
        self.assertEqual(data["tokens_today"], 0)
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

    def test_builds_interactive_snapshot(self):
        prompt = InteractivePrompt(
            id="i-1",
            call_id="call-123",
            thread_id="thread-1",
            turn_id="turn-1",
            session_id="session-1",
            status="input",
            question_index=0,
            question_total=1,
            questions=(
                InteractiveQuestion(
                    id="q1",
                    header="Scope",
                    question="What scope do you want?",
                    options=("Local", "Global"),
                ),
            ),
        )
        data = json.loads(build_session_state_snapshot(running=1, waiting=1, total=1, interactive=prompt))
        self.assertEqual(data["tokens_total"], 0)
        self.assertEqual(data["interactive"]["id"], "i-1")
        self.assertEqual(data["interactive"]["call_id"], "call-123")
        self.assertEqual(data["interactive"]["status"], "input")
        self.assertEqual(data["interactive"]["question_count"], 1)
        self.assertEqual(data["interactive"]["question_index"], 0)
        self.assertEqual(data["interactive"]["questions"][0]["options"], ["Local", "Global"])

    def test_parses_interactive_selection(self):
        selection = parse_interactive_selection('{"cmd":"interactive_select","id":"i-1","question_index":1,"answer":2}\n')
        self.assertIsNotNone(selection)
        self.assertEqual(selection.id, "i-1")
        self.assertEqual(selection.question_index, 1)
        self.assertEqual(selection.answer, 2)

    def test_ignores_invalid_interactive_selection(self):
        self.assertIsNone(parse_interactive_selection('{"cmd":"interactive_select","id":"i-1","question_index":0,"answer":"x"}'))

    def test_truncate_entry_caps_long_text(self):
        line = truncate_entry("a" * 200)
        self.assertLessEqual(len(line), 60)

    def test_build_user_input_response(self):
        prompt = InteractivePrompt(
            id="i-1",
            call_id="call-123",
            thread_id="thread-1",
            turn_id="turn-1",
            session_id="session-1",
            status="input",
            question_index=0,
            question_total=2,
            questions=(
                InteractiveQuestion(
                    id="q1",
                    header="Scope",
                    question="What scope do you want?",
                    options=("Local", "Global"),
                ),
                InteractiveQuestion(
                    id="q2",
                    header="Mode",
                    question="What mode do you want?",
                    options=("Fast", "Safe"),
                ),
            ),
        )
        data = build_user_input_response(prompt, (1, 0))
        self.assertEqual(
            data,
            {
                "answers": {
                    "q1": {"answers": ["Global"]},
                    "q2": {"answers": ["Fast"]},
                }
            },
        )

    def test_matches_live_user_input_request_by_item_and_turn(self):
        prompt = InteractivePrompt(
            id="i-1",
            call_id="call-123",
            thread_id="thread-1",
            turn_id="turn-1",
            session_id="session-1",
            status="input",
            question_index=0,
            question_total=2,
            questions=(
                InteractiveQuestion(
                    id="q1",
                    header="Scope",
                    question="What scope do you want?",
                    options=("Local", "Global"),
                ),
                InteractiveQuestion(
                    id="q2",
                    header="Mode",
                    question="What mode do you want?",
                    options=("Fast", "Safe"),
                ),
            ),
        )
        request_id = find_matching_user_input_request_id(
            [
                {
                    "id": 11,
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "itemId": "call-123",
                        "turnId": "turn-1",
                        "questions": [{"id": "q1"}, {"id": "q2"}],
                    },
                }
            ],
            prompt,
        )
        self.assertEqual(request_id, "11")


if __name__ == "__main__":
    unittest.main()
