import base64
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from analyzer.config import Config
from analyzer.live import LiveAnalyzer, run_maintenance
from collector.db import TelemetryDB
from collector.main import Collector
from collector.parser import PREFIX, parse_telemetry_line


ROOT = Path(__file__).resolve().parents[1]


def parsed(sequence: int, event_type: str, data: dict | None = None, server: str = "server-1"):
    envelope = {"v": 1, "seq": sequence, "ts": f"2026-08-24T10:00:{sequence:02d}.000Z", "type": event_type,
                "server_session": server, "round": "round-1", "player": "player-1", "game_ms": sequence * 100,
                "data": data or {}}
    return parse_telemetry_line(PREFIX + json.dumps(envelope, separators=(",", ":")))


class LiveAnalyzerTests(unittest.TestCase):
    def test_live_llm_is_opt_in_and_coalesces_high_signal_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            db = TelemetryDB(telemetry_path, ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            db.close()
            base = {"event_type": "chat_message", "server_session_id": "server-1", "round_id": "round-1", "player_session_id": "p1", "data_json": json.dumps({"message": "hello"})}
            rows = [{**base, "event_id": index, "utc_timestamp": f"2026-08-24T10:0{index}:00Z", "player_session_id": f"p{index}"} for index in range(1, 4)]
            disabled = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "disabled.sqlite3"), report_directory=str(root / "reports"), openai_enabled=True)
            analyzer = LiveAnalyzer(disabled, ROOT)
            try:
                analyzer._queue_chat_windows(rows, "2026-08-24T10:10:00Z")
                self.assertEqual(analyzer.analytics.execute("SELECT COUNT(*) FROM llm_jobs").fetchone()[0], 0)
            finally:
                analyzer.close()

            enabled = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "enabled.sqlite3"), report_directory=str(root / "reports"), openai_enabled=True, live_llm_enabled=True, live_llm_window_seconds=300, live_llm_min_messages=2)
            analyzer = LiveAnalyzer(enabled, ROOT)
            try:
                analyzer._queue_chat_windows(rows, "2026-08-24T10:10:00Z")
                job = analyzer.analytics.execute("SELECT source_id,model,payload_json FROM llm_jobs").fetchone()
                self.assertIsNotNone(job)
                self.assertTrue(job[0].startswith("chat:300:"))
                self.assertEqual(job[1], enabled.live_llm_model)
                self.assertEqual(len(json.loads(job[2])["messages"]), 3)
            finally:
                analyzer.close()

    def test_collector_avoids_existing_gzip_segment_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            raw = root / "raw" / day
            raw.mkdir(parents=True)
            (raw / "telemetry-000005.jsonl.gz").write_bytes(b"existing")
            config = {"_config_source": "test", "shared_storage_path": str(root / "spool.txt"), "database_path": str(root / "telemetry.sqlite3"), "raw_archive_dir": str(root / "raw"), "malformed_log_path": str(root / "malformed.jsonl")}
            collector = Collector(config)
            try:
                self.assertEqual(collector._active_path().name, "telemetry-000006.jsonl")
            finally:
                collector.close()

    def test_collector_closes_raw_segment_as_gzip_with_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_line = PREFIX + json.dumps({"v": 1, "seq": 1, "ts": "2026-08-24T10:00:01.000Z", "type": "test", "server_session": "server-1", "round": "round-1", "player": None, "game_ms": 1, "data": {}}, separators=(",", ":"))
            spool = root / "spool.txt"
            encoded = base64.b64encode(raw_line.encode("utf-8")).decode("ascii")
            spool.write_text("v.1.6.0.1|UTF8|header\nstring[]|slot_0|" + encoded + "\n", encoding="utf-8")
            config = {"_config_source": "test", "shared_storage_path": str(spool), "database_path": str(root / "telemetry.sqlite3"), "raw_archive_dir": str(root / "raw"), "malformed_log_path": str(root / "malformed.jsonl"), "raw_segment_max_bytes": 1024, "raw_archive_max_bytes": 4096}
            collector = Collector(config)
            try:
                self.assertEqual(collector.process_once(force=True)[0], 1)
            finally:
                collector.close()
            segments = list((root / "raw").rglob("*.jsonl.gz"))
            self.assertEqual(len(segments), 1)
            self.assertLessEqual(segments[0].stat().st_size, 1024)
            connection = sqlite3.connect(root / "telemetry.sqlite3")
            try:
                self.assertEqual(connection.execute("SELECT compression_status FROM raw_segments").fetchone()[0], "gzip")
            finally:
                connection.close()

    def test_collector_splits_raw_segments_by_server_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"_config_source": "test", "shared_storage_path": str(root / "spool.txt"), "database_path": str(root / "telemetry.sqlite3"), "raw_archive_dir": str(root / "raw"), "malformed_log_path": str(root / "malformed.jsonl")}
            collector = Collector(config)
            try:
                collector._archive([parsed(1, "test", server="server-1"), parsed(1, "test", server="server-2")])
                rows = collector.db.connection.execute("SELECT server_session_id,compression_status FROM raw_segments ORDER BY raw_segment_id").fetchall()
                self.assertEqual([row[0] for row in rows], ["server-1", "server-2"])
                self.assertEqual([row[1] for row in rows], ["gzip", "active"])
            finally:
                collector.close()

    def test_raw_ack_retries_without_blocking_and_marks_matching_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            db = TelemetryDB(telemetry_path, ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            db.connection.execute(
                "INSERT INTO raw_segments(path,size_bytes,server_session_id,first_sequence,last_sequence,compression_status,processing_status,retention_priority,created_at) VALUES(?,?,?,?,?,'gzip','available',5,?)",
                (str(root / "raw.jsonl.gz"), 1, "server-1", 1, 2, "2026-08-24T10:00:00Z"),
            )
            db.connection.commit(); db.close()
            config = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "data" / "analysis"), openai_enabled=False, live_raw_mark_busy_timeout_ms=25, live_raw_mark_interval_seconds=60)
            analyzer = LiveAnalyzer(config, ROOT)
            try:
                analyzer._raw_stop.set(); analyzer._raw_wakeup.set(); analyzer._raw_worker.join(timeout=1)
                lock = sqlite3.connect(telemetry_path, timeout=1)
                lock.execute("BEGIN IMMEDIATE")
                started = time.perf_counter()
                analyzer._mark_raw_segments([{"server_session_id": "server-1", "sequence": 2}], force=True)
                elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 0.5)
                self.assertEqual(lock.execute("SELECT processing_status FROM raw_segments").fetchone()[0], "available")
                lock.rollback(); lock.close()
                analyzer._mark_raw_segments([], force=True)
                self.assertEqual(analyzer.telemetry.execute("SELECT processing_status FROM raw_segments").fetchone()[0], "processed")
                analyzer._mark_raw_segments([{"server_session_id": "server-2", "sequence": 3}], force=True)
                self.assertIn("server-2", analyzer._raw_segment_sequences)
                delayed = sqlite3.connect(telemetry_path)
                delayed.execute(
                    "INSERT INTO raw_segments(path,size_bytes,server_session_id,first_sequence,last_sequence,compression_status,processing_status,retention_priority,created_at) VALUES(?,?,?,?,?,'gzip','available',5,?)",
                    (str(root / "delayed.gz"), 1, "server-2", 1, 3, "2026-08-24T10:00:00Z"),
                )
                delayed.commit(); delayed.close()
                analyzer._mark_raw_segments([], force=True)
                self.assertEqual(analyzer.telemetry.execute("SELECT processing_status FROM raw_segments WHERE server_session_id='server-2'").fetchone()[0], "processed")
            finally:
                analyzer.close()

    def test_scene_state_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            db = TelemetryDB(telemetry_path, ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            db.close()
            config = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "data" / "analysis"), openai_enabled=False, live_scene_state_cache_limit=2)
            analyzer = LiveAnalyzer(config, ROOT)
            try:
                analyzer._scene_state_cache_limit = 2
                for index in range(20):
                    analyzer._aggregate(
                        {"event_type": "scene_frame_batch", "utc_timestamp": "2026-08-24T10:00:00Z", "server_session_id": "server-1", "round_id": "round-1", "player_session_id": None, "game_ms": index},
                        {"entities": [{"x": 1, "y": 1, "object_id": index}]}, "2026-08-24T10:00:00Z",
                    )
                    self.assertLessEqual(len(analyzer.previous_objects), 2)
            finally:
                analyzer.close()

    def test_scene_state_cache_is_session_and_round_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            db = TelemetryDB(telemetry_path, ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            db.close()
            config = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "data" / "analysis"), openai_enabled=False)
            analyzer = LiveAnalyzer(config, ROOT)
            try:
                row = {"event_type": "scene_frame_batch", "utc_timestamp": "2026-08-24T10:00:00Z", "server_session_id": "server-1", "round_id": "round-1", "player_session_id": None, "game_ms": 0}
                analyzer._aggregate(row, {"entities": [{"x": 0, "y": 0, "vx": 0, "vy": 0, "object_id": 1}]}, row["utc_timestamp"])
                analyzer._aggregate({**row, "game_ms": 100}, {"entities": [{"x": 1, "y": 0, "vx": 1, "vy": 0, "object_id": 1}]}, row["utc_timestamp"])
                old_key = ("server-1", "round-1", "object", 1)
                self.assertIn(old_key, analyzer.previous_objects)
                self.assertGreater(analyzer._pending_json[("agg_scene_minute", ("2026-08-24T10:00:00Z", "round-1"))]["object_acceleration_sum"], 0)

                new_row = {**row, "server_session_id": "server-2", "game_ms": 0}
                analyzer._aggregate(new_row, {"entities": [{"x": 0, "y": 0, "vx": 0, "vy": 0, "object_id": 1}]}, row["utc_timestamp"])
                new_key = ("server-2", "round-1", "object", 1)
                self.assertNotIn(old_key, analyzer.previous_objects)
                self.assertIn(new_key, analyzer.previous_objects)
                analyzer._aggregate({**new_row, "game_ms": 100}, {"entities": [{"x": 1, "y": 0, "vx": 1, "vy": 0, "object_id": 1}]}, row["utc_timestamp"])
                self.assertGreater(analyzer._pending_json[("agg_scene_minute", ("2026-08-24T10:00:00Z", "round-1"))]["object_acceleration_sum"], 0)
            finally:
                analyzer.close()

    def test_checkpoint_and_minute_aggregates_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            db = TelemetryDB(telemetry_path, ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                db.connection.execute("INSERT INTO server_sessions(server_session_id,started_at,first_event_at,last_event_at) VALUES('server-1','2026-08-24T10:00:00Z','2026-08-24T10:00:00Z','2026-08-24T10:00:03Z')")
                db.insert_batch([
                    parsed(1, "network_sample", {"ping_ms": 80}),
                    parsed(2, "player_damage", {"attacker_session_id": "player-1", "victim_session_id": "victim", "damage": 20}),
                    parsed(3, "scene_window_complete", {"trigger": "explosion", "coverage": 1.0}),
                ])
            finally:
                db.close()
            config = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "data" / "analysis"), openai_enabled=False)
            analyzer = LiveAnalyzer(config, ROOT)
            try:
                self.assertEqual(analyzer.run_once(), 3)
                self.assertEqual(analyzer.run_once(), 0)
                self.assertEqual(analyzer.analytics.execute("SELECT last_event_id FROM processing_checkpoints WHERE consumer_name='live_analyzer'").fetchone()[0], 3)
                self.assertEqual(analyzer.analytics.execute("SELECT COUNT(*) FROM agg_server_minute").fetchone()[0], 1)
                self.assertEqual(analyzer.analytics.execute("SELECT COUNT(*) FROM episode_catalog").fetchone()[0], 1)
                self.assertEqual(analyzer.analytics.execute("SELECT occurrences FROM pattern_candidates WHERE signature='event:scene_window_complete'").fetchone()[0], 1)
            finally:
                analyzer.close()

    def test_batch_aggregation_coalesces_json_and_network_merges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            db = TelemetryDB(telemetry_path, ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                db.connection.execute("INSERT INTO server_sessions(server_session_id,started_at,first_event_at,last_event_at) VALUES('server-1','2026-08-24T10:00:00Z','2026-08-24T10:00:00Z','2026-08-24T10:00:10Z')")
                db.insert_batch([
                    parsed(sequence, "network_sample", {"ping_ms": 50 + sequence}) if sequence % 2 else parsed(sequence, "player_damage", {"attacker_session_id": "player-1", "victim_session_id": "victim", "damage": 10})
                    for sequence in range(1, 11)
                ])
            finally:
                db.close()
            config = Config(telemetry_database=str(telemetry_path), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "data" / "analysis"), openai_enabled=False)
            analyzer = LiveAnalyzer(config, ROOT)
            traces = []
            analyzer.analytics.set_trace_callback(traces.append)
            try:
                self.assertEqual(analyzer.run_once(), 10)
                self.assertEqual(sum("SELECT metrics_json FROM agg_server_minute" in statement for statement in traces), 1)
                metrics = json.loads(analyzer.analytics.execute("SELECT metrics_json FROM agg_server_minute").fetchone()[0])
                self.assertEqual(metrics["events"], 10)
                network = analyzer.analytics.execute("SELECT ping_count,ping_sum FROM agg_network_minute").fetchone()
                self.assertEqual((network[0], network[1]), (5, 275))
            finally:
                analyzer.close()

    def test_maintenance_keeps_schema_and_reports_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry = root / "telemetry.sqlite3"
            conn = sqlite3.connect(telemetry)
            conn.executescript((ROOT / "sql/schema.sql").read_text(encoding="utf-8"))
            conn.commit(); conn.close()
            config = Config(telemetry_database=str(telemetry), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "data" / "analysis"), openai_enabled=False)
            result = run_maintenance(config, ROOT)
            self.assertIn("raw", result)
            self.assertIn("sqlite", result)
            analytics = sqlite3.connect(config.analytics_database)
            try:
                names = {row[0] for row in analytics.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"processing_checkpoints", "episode_catalog", "llm_jobs", "llm_results"} <= names)
            finally:
                analytics.close()


if __name__ == "__main__":
    unittest.main()
