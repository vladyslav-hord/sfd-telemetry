import unittest

import sqlite3
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from analyzer.config import Config
from analyzer.llm import chat_windows, compact_narrative_payload, estimate_tokens, extract_annotation, moderate_chat, near_live_request_body, queue_gameplay_requests, queue_narrative_request, reconcile_chat_annotations, reconcile_near_live_jobs, request_hash, submit_near_live_jobs, submit_pending_batches, supersede_legacy_gameplay, sync_batches, valid_chat_annotation, valid_narrative

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LlmTests(unittest.TestCase):
    def analytics(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript((ROOT / "analyzer/schema.sql").read_text(encoding="utf-8"))
        return connection

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

    def test_live_is_strict_opt_in_and_request_body_is_bounded(self):
        config = Config()
        self.assertFalse(config.live_llm_enabled)
        body = near_live_request_body({"job_kind": "moderation", "payload_json": '{"messages":[]}'}, config)
        self.assertEqual(body["max_output_tokens"], 128)
        self.assertIn("prompt_cache_key", body)
        self.assertEqual(body["text"]["verbosity"], "low")

    def test_narrative_payload_is_hard_bounded(self):
        config = Config(max_narrative_payload_bytes=1000, max_narrative_input_tokens=250)
        payload = compact_narrative_payload("2026-08-24", {"server": {str(index): "x" * 1000 for index in range(100)}}, config)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 1000)
        self.assertLessEqual(estimate_tokens(encoded), 250)

    def test_logical_dedupe_survives_same_source_and_model_change(self):
        analytics = self.analytics()
        config = Config(openai_enabled=True, openai_model="gpt-5-nano")
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
                self.assertEqual(queue_narrative_request(analytics, "2026-08-24", {"server": {"events": 1}}, config), 1)
                changed = Config(openai_enabled=True, openai_model="gpt-5-nano-2025-08-07")
                self.assertEqual(queue_narrative_request(analytics, "2026-08-24", {"server": {"events": 999}}, changed), 0)
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0], 1)
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_logical_keys").fetchone()[0], 1)
        finally:
            analytics.close()

    def test_explicit_analysis_version_allows_reanalysis(self):
        analytics = self.analytics()
        first = Config(openai_enabled=True, llm_analysis_version="llm-v1")
        second = Config(openai_enabled=True, llm_analysis_version="llm-v2")
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
                self.assertEqual(queue_narrative_request(analytics, "2026-08-24", {"server": {"events": 1}}, first), 1)
                self.assertEqual(queue_narrative_request(analytics, "2026-08-24", {"server": {"events": 2}}, second), 1)
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_requests").fetchone()[0], 2)
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_logical_keys").fetchone()[0], 2)
        finally:
            analytics.close()

    def test_background_lifecycle_reconciles_usage_without_resubmit(self):
        analytics = self.analytics()
        config = Config(openai_enabled=True, live_llm_enabled=True)
        now = "2026-08-24T10:00:00Z"
        analytics.execute("INSERT INTO llm_jobs(job_id,source_type,source_id,job_kind,model,status,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", ("job-1", "chat", "chat:300:1", "moderation", config.live_llm_model, '{"messages": [{"event_id": 1}]}', now, now))
        analytics.commit()

        class Response:
            id = "resp-1"
            model = "gpt-5-nano"
            status = "queued"
            def model_dump(self):
                return {"id": self.id, "model": self.model, "status": self.status, "usage": None, "output": []}

        class Responses:
            def create(self, **_):
                return Response()
            def retrieve(self, _):
                result = {"id": "resp-1", "model": "gpt-5-nano", "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14, "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 1}}}
                result["output"] = [{"content": [{"type": "output_text", "text": '{"labels":[],"toxicity":0,"targeted":false,"target_player_session_ids":[],"conversation_role":"unknown","confidence":0,"evidence_message_ids":[1]}'}]}]
                return result

        class Client:
            responses = Responses()

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=lambda: Client())}):
                self.assertEqual(submit_near_live_jobs(analytics, config), 1)
                self.assertEqual(analytics.execute("SELECT status FROM llm_jobs WHERE job_id='job-1'").fetchone()[0], "submitted")
                self.assertEqual(reconcile_near_live_jobs(analytics, config, Client()), 1)
            self.assertEqual(analytics.execute("SELECT status FROM llm_jobs WHERE job_id='job-1'").fetchone()[0], "complete")
            self.assertEqual(analytics.execute("SELECT total_tokens FROM llm_cost_ledger WHERE job_id='job-1'").fetchone()[0], 14)
            self.assertEqual(analytics.execute("SELECT status FROM llm_results WHERE job_id='job-1'").fetchone()[0], "complete")
        finally:
            analytics.close()

    def test_batch_admission_uses_request_budget_not_chat_message_setting(self):
        analytics = self.analytics()
        config = Config(openai_enabled=True, max_batch_requests_per_run=1, max_batch_input_tokens_per_day=100000, max_batch_output_tokens_per_day=100000)
        now = "2026-08-24T10:00:00Z"
        analytics.executemany("INSERT INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", [
            ("hash-a", "gameplay", "window-a", "gameplay_analysis_v2", config.openai_model, '{"window":{"coverage":1}}', now, now),
            ("hash-b", "gameplay", "window-b", "gameplay_analysis_v2", config.openai_model, '{"window":{"coverage":1}}', now, now),
        ])
        analytics.commit()

        class Files:
            def create(self, **_):
                return SimpleNamespace(id="file-1")
        class Batches:
            def create(self, **_):
                return SimpleNamespace(id="batch-1", status="validating")
        class Client:
            files = Files()
            batches = Batches()

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=lambda: Client())}):
                self.assertEqual(__import__("analyzer.llm", fromlist=["submit_pending_batches"]).submit_pending_batches(analytics, config), "pending")
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_requests WHERE status='submitted'").fetchone()[0], 1)
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_requests WHERE status='queued'").fetchone()[0], 1)
        finally:
            analytics.close()

    def test_gameplay_payload_uses_ephemeral_labels_without_identifiers(self):
        analytics = self.analytics()
        config = Config(openai_enabled=True, live_llm_enabled=True, max_llm_anomaly_windows_per_day=1)
        window = {
            "source_window_id": "window-1",
            "player_session_id": "session-secret",
            "player_identity_id": "identity-secret",
            "display_name": "private-name",
            "round_id": "round-1",
            "source_event_ids": [17],
            "object_id": "entity-secret",
            "features": {"max_speed": 4.2, "player_session_id": "nested-secret", "state_json": {"player_identity_id": "nested-identity"}},
            "coverage": 1.0,
            "robust_z": 5.0,
        }
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
                self.assertEqual(queue_gameplay_requests(analytics, "2026-08-24", [window], config), 1)
            payload = json.loads(analytics.execute("SELECT request_json FROM llm_requests").fetchone()[0])
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("session-secret", encoded)
            self.assertNotIn("identity-secret", encoded)
            self.assertNotIn("private-name", encoded)
            self.assertNotIn("entity-secret", encoded)
            self.assertNotIn("player_session_id", encoded)
            self.assertEqual(payload["window"]["player_label"], "Player 1")
            self.assertEqual(payload["window"]["evidence_event_ids"], [17])
        finally:
            analytics.close()

    def test_legacy_moderation_is_chunked_and_skips_annotated(self):
        analytics = self.analytics()
        analytics.execute("INSERT INTO chat_annotations(chat_id,prompt_version,model,annotation_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (1, "moderation_v1", "omni-moderation-latest", "{}", "complete", "now", "now"))
        rows = [{"chat_id": index, "message": "annotated" if index == 1 else "x" * 500} for index in range(1, 8)]
        config = Config(max_moderation_messages_per_request=2, max_moderation_input_bytes_per_request=120, max_moderation_input_tokens_per_request=30, max_input_chars_per_request=500)
        calls = []

        class Result:
            flagged = False
            categories = {}
            category_scores = {}

        class Moderations:
            def create(self, **kwargs):
                calls.append(kwargs["input"])
                return SimpleNamespace(results=[Result() for _ in kwargs["input"]])

        class Client:
            moderations = Moderations()

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=lambda: Client())}):
                self.assertEqual(moderate_chat(analytics, rows, config), set())
            self.assertGreater(len(calls), 1)
            self.assertTrue(all(len(chunk) <= 2 for chunk in calls))
            self.assertTrue(all(len(json.dumps(chunk, ensure_ascii=False).encode("utf-8")) <= 120 for chunk in calls))
            self.assertTrue(all(estimate_tokens(json.dumps(chunk, ensure_ascii=False)) <= 30 for chunk in calls))
            self.assertNotIn("annotated", [message for chunk in calls for message in chunk])
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM chat_annotations WHERE status='complete'").fetchone()[0], 7)
        finally:
            analytics.close()

    def test_legacy_gameplay_supersede_is_idempotent_and_new_v2_is_sendable(self):
        analytics = self.analytics()
        now = "2026-08-24T00:00:00Z"
        analytics.execute("INSERT INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,attempts,created_at,updated_at) VALUES('old-queued','gameplay','old-window','gameplay_pattern_v1','model','queued','{\"window\":{\"player_session_id\":\"secret\"}}',0,?,?)", (now, now))
        analytics.execute("INSERT INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,attempts,created_at,updated_at) VALUES('old-submitted','gameplay','old-submitted','gameplay_pattern_v1','model','submitted','{\"window\":{\"player_session_id\":\"secret\"}}',1,?,?)", (now, now))
        analytics.execute("INSERT INTO llm_jobs(job_id,source_type,source_id,job_kind,model,status,payload_json,attempts,created_at,updated_at) VALUES('old-job-queued','gameplay','old-job','gameplay_pattern_v1','model','queued','{\"player_session_id\":\"secret\"}',0,?,?)", (now, now))
        analytics.execute("INSERT INTO llm_jobs(job_id,source_type,source_id,job_kind,model,status,payload_json,attempts,created_at,updated_at) VALUES('old-job-submitted','gameplay','old-job-submitted','gameplay_pattern_v1','model','submitted','{\"player_session_id\":\"secret\"}',1,?,?)", (now, now))
        analytics.commit()
        config = Config(openai_enabled=True, live_llm_enabled=True, max_llm_anomaly_windows_per_day=1, max_batch_requests_per_run=2)
        try:
            self.assertEqual(supersede_legacy_gameplay(analytics), 2)
            self.assertEqual(supersede_legacy_gameplay(analytics), 0)
            self.assertEqual(analytics.execute("SELECT status FROM llm_requests WHERE request_hash='old-queued'").fetchone()[0], "superseded")
            self.assertEqual(analytics.execute("SELECT status FROM llm_jobs WHERE job_id='old-job-queued'").fetchone()[0], "superseded")
            self.assertEqual(analytics.execute("SELECT status FROM llm_requests WHERE request_hash='old-submitted'").fetchone()[0], "submitted")
            self.assertEqual(analytics.execute("SELECT status FROM llm_jobs WHERE job_id='old-job-submitted'").fetchone()[0], "submitted")
            window = {"source_window_id": "new-window", "player_session_id": "new-secret", "round_id": "round-1", "source_event_ids": [9], "features": {"max_speed": 5.0}, "coverage": 1.0, "robust_z": 5.0}
            class Files:
                def create(self, **kwargs):
                    return SimpleNamespace(id="file-new")
            class Batches:
                def create(self, **kwargs):
                    return SimpleNamespace(id="batch-new", status="validating")
            class Client:
                files = Files()
                batches = Batches()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=lambda: Client())}):
                self.assertEqual(submit_near_live_jobs(analytics, config), 0)
                self.assertEqual(queue_gameplay_requests(analytics, "2026-08-24", [window], config), 1)
                new_row = analytics.execute("SELECT prompt_version,request_json,status FROM llm_requests WHERE source_id='new-window'").fetchone()
                self.assertEqual(new_row[0], "gameplay_analysis_v2")
                self.assertNotIn("new-secret", new_row[1])
                self.assertEqual(submit_pending_batches(analytics, config), "pending")
            self.assertEqual(analytics.execute("SELECT status FROM llm_requests WHERE source_id='new-window'").fetchone()[0], "submitted")
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_requests WHERE status='superseded'").fetchone()[0], 1)
            self.assertEqual(analytics.execute("SELECT COUNT(*) FROM llm_jobs WHERE status='superseded'").fetchone()[0], 1)
        finally:
            analytics.close()

    def test_sync_batches_successful_gameplay_records_non_default_version(self):
        analytics = self.analytics()
        now = "2026-08-24T00:00:00Z"
        analytics.execute("INSERT INTO llm_batches(batch_id,remote_batch_id,status,created_at,metadata_json) VALUES('batch-game','remote-game','submitted',?,'{}')", (now,))
        analytics.execute("INSERT INTO llm_requests(request_hash,batch_id,source_type,source_id,prompt_version,model,status,request_json,attempts,created_at,updated_at) VALUES('hash-game','batch-game','gameplay','window-1','gameplay_pattern_v1@llm-v2','gpt-5-nano','submitted','{}',1,?,?)", (now, now))
        gameplay = {"window_id": "window-1", "classification": "candidate", "mechanic_family": "movement", "novelty_score": 0.8, "advantage_observed": True, "advantage_description": "brief", "observations": ["x"], "known_pattern_id": None, "candidate_signature_features": ["speed"], "confidence": 0.8, "evidence_event_ids": [17], "should_create_candidate": True}
        result = {"custom_id": "hash-game", "response": {"body": {"output_text": json.dumps(gameplay), "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}}

        class FakeClient:
            def __init__(self):
                self.batches = SimpleNamespace(retrieve=lambda _: SimpleNamespace(status="completed", output_file_id="file-game"))
                self.files = SimpleNamespace(content=lambda _: SimpleNamespace(text=json.dumps(result)))

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=FakeClient)}):
                self.assertEqual(sync_batches(analytics), 1)
            self.assertEqual(analytics.execute("SELECT status FROM llm_requests WHERE request_hash='hash-game'").fetchone()[0], "complete")
            self.assertEqual(analytics.execute("SELECT analysis_version FROM llm_cost_ledger WHERE request_hash='hash-game'").fetchone()[0], "llm-v2")
        finally:
            analytics.close()

    def test_sync_batches_updates_only_matching_metric_version(self):
        telemetry, analytics = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        analytics.row_factory = sqlite3.Row
        analytics.executescript((ROOT / "analyzer/schema.sql").read_text(encoding="utf-8"))
        report = {"status": {"llm": "pending"}, "narrative": None}
        analytics.executemany("INSERT INTO daily_reports(report_date,metric_version,report_json,llm_status,generated_at,analysis_run_id) VALUES(?,?,?,?,?,?)", [
            ("2026-08-24", 1, "{}", "pending", "2026-08-24T00:00:00Z", "run-1"),
            ("2026-08-24", 2, json.dumps(report), "pending", "2026-08-24T00:00:00Z", "run-2"),
        ])
        analytics.execute("INSERT INTO llm_batches(batch_id,remote_batch_id,status,created_at,metadata_json) VALUES('batch-1','remote-1','submitted','2026-08-24T00:00:00Z','{}')")
        analytics.execute("INSERT INTO llm_requests(request_hash,batch_id,source_type,source_id,prompt_version,model,status,request_json,attempts,created_at,updated_at) VALUES('hash-1','batch-1','narrative','2026-08-24','narrative_v1','model','submitted','{}',1,'2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')")
        analytics.commit()

        narrative = {"headline": "ok", **{key: [] for key in ("server_health", "player_experience", "map_findings", "network_findings", "player_highlights", "pattern_findings", "chat_findings", "possible_factors", "limitations")}}
        result = {"custom_id": "hash-1", "response": {"body": {"output_text": json.dumps(narrative)}}}

        class FakeClient:
            def __init__(self):
                self.batches = SimpleNamespace(retrieve=lambda _: SimpleNamespace(status="completed", output_file_id="file-1"))
                self.files = SimpleNamespace(content=lambda _: SimpleNamespace(text=json.dumps(result)))

        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=FakeClient)}):
                self.assertEqual(sync_batches(analytics), 1)
            rows = analytics.execute("SELECT metric_version,llm_status,report_json FROM daily_reports ORDER BY metric_version").fetchall()
            self.assertEqual(rows[0][1], "pending")
            self.assertEqual(rows[1][1], "complete")
            self.assertEqual(json.loads(rows[1][2])["narrative"]["headline"], "ok")
        finally:
            telemetry.close(); analytics.close()
