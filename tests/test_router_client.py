import logging
import unittest

from codex_buddy_bridge.protocol import InteractivePrompt, InteractiveQuestion
from codex_buddy_bridge.router_client import CodexRouterClient, find_matching_user_input_request_id


class RouterClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = CodexRouterClient(logging.getLogger("test-router"))

    def _prompt(self) -> InteractivePrompt:
        return InteractivePrompt(
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

    def test_snapshot_broadcast_updates_requests(self) -> None:
        self.client._handle_broadcast(
            {
                "method": "thread-stream-state-changed",
                "params": {
                    "conversationId": "thread-1",
                    "change": {
                        "type": "snapshot",
                        "conversationState": {"id": "thread-1"},
                        "requests": [
                            {
                                "id": 17,
                                "method": "item/tool/requestUserInput",
                                "params": {
                                    "itemId": "call-123",
                                    "turnId": "turn-1",
                                    "questions": [{"id": "q1"}],
                                },
                            }
                        ],
                    },
                },
            }
        )

        self.assertEqual(
            self.client._conversation_requests["thread-1"][0]["params"]["itemId"],
            "call-123",
        )

    def test_patch_broadcast_updates_requests(self) -> None:
        self.client._conversation_requests["thread-1"] = []
        self.client._handle_broadcast(
            {
                "method": "thread-stream-state-changed",
                "params": {
                    "conversationId": "thread-1",
                    "change": {
                        "type": "patches",
                        "patches": [
                            {
                                "op": "add",
                                "path": ["requests", 0],
                                "value": {
                                    "id": 22,
                                    "method": "item/tool/requestUserInput",
                                    "params": {
                                        "itemId": "call-123",
                                        "turnId": "turn-1",
                                        "questions": [{"id": "q1"}],
                                    },
                                },
                            }
                        ],
                    },
                },
            }
        )

        self.assertEqual(
            self.client._conversation_requests["thread-1"][0]["id"],
            22,
        )

    def test_request_match_falls_back_to_unique_pending_request(self) -> None:
        request_id = find_matching_user_input_request_id(
            [
                {
                    "id": 31,
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "itemId": "different-item",
                        "turnId": "different-turn",
                        "questions": [{"id": "not-the-same"}],
                    },
                }
            ],
            self._prompt(),
        )

        self.assertEqual(request_id, "31")


if __name__ == "__main__":
    unittest.main()
