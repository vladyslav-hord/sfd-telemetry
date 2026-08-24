import unittest

import sqlite3
from types import SimpleNamespace

from analyzer.llm import chat_windows, extract_annotation, reconcile_chat_annotations, request_hash, valid_chat_annotation, valid_narrative


class LlmTests(unittest.TestCase):
    def test_request_hash_is_stable(self):
        first = request_hash("chat", "1", "v1", "model", {"a": 1, "b": 2})
        second = request_hash("chat", "1", "v1", "model", {"b": 2, "a": 1})
        self.assertEqual(first, second)

    def test_strict_chat_annotation(self):
        value = {"chat_id": 3, "language": "ru", "intent": "unknown", "sentiment": "unknown", "topics": [], "toxicity": {"score": 0, "profanity": False, "harassment": False, "threat": False, "hate": False, "sexual": False, "spam": False, "targeted": False}, "target_actor_keys": [], "reply_to_chat_id": None, "explicit_leave_reason": {"present": False, "category": "unknown"}, "confidence": .5, "evidence_chat_ids": [3]}
        self.assertTrue(valid_chat_annotation(value, 3))
        value["unexpected"] = True
        self.assertFalse(valid_chat_annotation(value, 3))

    def test_extract_annotation_from_batch_envelope(self):
        annotation = {"chat_id": 3}
        envelope = {"response": {"body": {"output_text": '{"annotations":[{"chat_id":3}]}'}}}
        self.assertEqual(extract_annotation(envelope), annotation)

    def test_narrative_schema(self):
        value = {"headline": "x", **{key: [] for key in ("server_health", "player_experience", "map_findings", "network_findings", "player_highlights", "pattern_findings", "chat_findings", "possible_factors", "limitations")}}
        self.assertTrue(valid_narrative(value))

    def test_reconcile_removes_missing_chat(self):
        telemetry, analytics = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        telemetry.execute("CREATE TABLE chat_messages(chat_id INTEGER)")
        analytics.execute("CREATE TABLE chat_annotations(chat_id INTEGER)")
        analytics.execute("INSERT INTO chat_annotations VALUES(7)")
        self.assertEqual(reconcile_chat_annotations(analytics, telemetry), 1)
        self.assertEqual(analytics.execute("SELECT COUNT(*) FROM chat_annotations").fetchone()[0], 0)

    def test_chat_windows_do_not_overlap_core_ids(self):
        rows = [{"chat_id": index, "message": "x"} for index in range(25)]
        config = SimpleNamespace(max_chat_messages_per_request=30, max_input_chars_per_request=30000)
        windows = list(chat_windows(rows, config))
        self.assertEqual([len(core) for core, _ in windows], [20, 5])
        self.assertEqual({row["chat_id"] for core, _ in windows for row in core}, set(range(25)))
