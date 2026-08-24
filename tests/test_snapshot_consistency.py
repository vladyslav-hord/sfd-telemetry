import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from analyzer import metrics
from analyzer.config import Config
from analyzer.main import analyze_day


ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-08-24"


class SnapshotConsistencyTests(unittest.TestCase):
    def _telemetry(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript((ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "INSERT INTO server_sessions(server_session_id,started_at,first_event_at,last_event_at) VALUES(?,?,?,?)",
            ("server", "2026-08-24T10:00:00Z", "2026-08-24T10:00:00Z", "2026-08-24T10:00:01Z"),
        )
        connection.execute("INSERT INTO players VALUES(?,?,?,?)", ("p1", "host_scoped", "2026-08-24T10:00:00Z", "2026-08-24T10:00:01Z"))
        connection.execute(
            """INSERT INTO player_sessions(
                player_session_id,server_session_id,player_identity_id,joined_at,left_at,
                duration_ms,is_user,is_bot,is_host,is_moderator,joined_as_spectator,initial_spectating
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("ps1", "server", "p1", "2026-08-24T10:00:00Z", "2026-08-24T10:00:01Z", 1000, 1, 0, 0, 0, 0, 0),
        )
        connection.execute(
            """INSERT INTO events(schema_version,event_type,server_session_id,sequence,utc_timestamp,data_json,raw_json)
               VALUES(1,'server_started','server',1,'2026-08-24T10:00:00Z','{}','{}')"""
        )
        connection.commit()
        connection.close()

    def test_report_uses_one_snapshot_and_next_run_sees_writer_append(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_path = root / "telemetry.sqlite3"
            self._telemetry(telemetry_path)
            config = Config(
                telemetry_database=str(telemetry_path),
                analytics_database=str(root / "analytics.sqlite3"),
                report_directory=str(root / "reports"),
                openai_enabled=False,
            )
            original_quality = metrics.data_quality
            injected = False

            def append_from_second_connection(conn, start, end):
                nonlocal injected
                result = original_quality(conn, start, end)
                if not injected:
                    writer = sqlite3.connect(telemetry_path)
                    try:
                        writer.execute("PRAGMA foreign_keys=ON")
                        writer.execute("INSERT INTO players VALUES(?,?,?,?)", ("p2", "host_scoped", "2026-08-24T10:00:02Z", "2026-08-24T10:00:03Z"))
                        writer.execute(
                            """INSERT INTO player_sessions(
                                player_session_id,server_session_id,player_identity_id,joined_at,left_at,
                                duration_ms,is_user,is_bot,is_host,is_moderator,joined_as_spectator,initial_spectating
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            ("ps2", "server", "p2", "2026-08-24T10:00:02Z", "2026-08-24T10:00:03Z", 1000, 1, 0, 0, 0, 0, 0),
                        )
                        writer.execute(
                            """INSERT INTO events(schema_version,event_type,server_session_id,sequence,utc_timestamp,data_json,raw_json)
                               VALUES(1,'server_started','server',2,'2026-08-24T10:00:02Z','{}','{}')"""
                        )
                        writer.execute(
                            """INSERT INTO rounds(round_id,server_session_id,map_name,started_at,ended_at,duration_ms,player_count,human_count,bot_count,result_source)
                               VALUES('round-2','server','test-map','2026-08-24T10:00:02Z','2026-08-24T10:00:03Z',1000,1,1,0,'exact')"""
                        )
                        writer.commit()
                        injected = True
                    finally:
                        writer.close()
                return result

            metrics.data_quality = append_from_second_connection
            try:
                first_path = analyze_day(config, __import__('datetime').date(2026, 8, 24), "test")
            finally:
                metrics.data_quality = original_quality

            first = json.loads(first_path.read_text(encoding="utf-8"))
            self.assertTrue(injected)
            self.assertEqual(first["server"]["sessions"], 1)
            self.assertEqual(first["server"]["human_players"], 1)
            self.assertEqual(first["server"]["rounds"], 0)
            self.assertEqual(first["data_quality"]["event_count"], 1)
            self.assertEqual(first["server"]["source_snapshot"]["max_event_id"], 1)
            self.assertEqual(first["server"]["source_snapshot"]["max_utc_timestamp"], "2026-08-24T10:00:00Z")

            second_path = analyze_day(config, __import__('datetime').date(2026, 8, 24), "test")
            second = json.loads(second_path.read_text(encoding="utf-8"))
            self.assertEqual(second["server"]["sessions"], 2)
            self.assertEqual(second["server"]["human_players"], 2)
            self.assertEqual(second["server"]["rounds"], 1)
            self.assertEqual(second["data_quality"]["event_count"], 2)
            self.assertEqual(second["server"]["source_snapshot"]["max_event_id"], 2)
            self.assertEqual(second["server"]["source_snapshot"]["max_utc_timestamp"], "2026-08-24T10:00:02Z")


if __name__ == "__main__":
    unittest.main()
