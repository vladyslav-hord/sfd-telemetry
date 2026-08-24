import sqlite3
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone

from analyzer.features import movement_features
from analyzer.metrics import (
    _ping_metrics_by_session,
    _stream_movement_features,
    input_metrics,
    leave_context,
    ping_metrics,
)
from analyzer.scene import _motifs


class StreamingMetricsTests(unittest.TestCase):
    def test_streamed_movement_matches_legacy_features(self):
        rows = [
            {"game_ms": 0, "x": 0, "y": 0, "state_json": '{"is_airborne":true}'},
            {"game_ms": 1000, "x": 3, "y": 4, "state_json": "{}"},
            {"game_ms": 2000, "x": 3, "y": 4, "state_json": '{"airborne":true}'},
        ]
        streamed = _stream_movement_features(iter(rows))
        legacy = movement_features(rows)
        self.assertEqual(streamed, legacy)

    def test_streamed_ping_metrics_matches_legacy(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("CREATE TABLE network_samples(sample_id INTEGER PRIMARY KEY, player_session_id TEXT, utc_timestamp TEXT, ping_ms INTEGER)")
            samples = [
                ("p", "2026-08-01T00:00:00Z", 90),
                ("p", "2026-08-01T00:00:05Z", 160),
                ("p", "2026-08-01T00:00:10Z", 160),
            ]
            connection.executemany("INSERT INTO network_samples(player_session_id,utc_timestamp,ping_ms) VALUES(?,?,?)", samples)
            streamed = _ping_metrics_by_session(connection, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")[0]
        legacy = ping_metrics([(row[1], row[2]) for row in samples])
        for key in ("samples", "median", "p95", "estimated_jitter", "longest_high_ping_interval"):
            self.assertAlmostEqual(streamed[key], legacy[key])
        self.assertEqual(streamed["spikes"], legacy["spikes"])
        self.assertEqual(streamed["seconds_above"], legacy["seconds_above"])

    def test_input_metrics_keeps_burst_semantics_without_event_lists(self):
        with sqlite3.connect(":memory:") as connection:
            rows = [("p", "2026-08-01T00:00:00Z", "key_input", '{"key":"W","event":"Pressed"}') for _ in range(5)]
            connection.execute("CREATE TABLE events(player_session_id TEXT,utc_timestamp TEXT,event_type TEXT,data_json TEXT)")
            connection.executemany("INSERT INTO events VALUES(?,?,?,?)", rows)
            result = input_metrics(connection, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
        self.assertEqual(result["p"]["events"], 5)
        self.assertEqual(result["p"]["transitions"], 5)
        self.assertEqual(result["p"]["input_burst_starts"], 1)

    def test_streamed_motifs_use_deterministic_tie_order_for_top50(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("CREATE TABLE scene_interactions(player_session_id TEXT,round_id TEXT,game_ms REAL,interaction_type TEXT,target_entity_id INTEGER,utc_timestamp TEXT)")
            rows = []
            for index in range(60):
                suffix = 59 - index
                rows.extend([
                    (f"p{index:03d}", "r", 0, f"z{suffix:02d}", index, "2026-08-01T00:00:00Z"),
                    (f"p{index:03d}", "r", 100, f"a{suffix:02d}", index, "2026-08-01T00:00:01Z"),
                ])
            connection.executemany("INSERT INTO scene_interactions VALUES(?,?,?,?,?,?)", rows)
            result = _motifs(connection, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
        expected = sorted((f"z{index:02d}>a{index:02d}" for index in range(60)))[:50]
        self.assertEqual([item["motif"] for item in result], expected)

    def test_leave_context_uses_bounded_query_count(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE player_sessions(player_session_id TEXT PRIMARY KEY, joined_at TEXT, left_at TEXT);
                CREATE TABLE combat_events(event_id INTEGER, victim_session_id TEXT);
                CREATE TABLE events(event_id INTEGER, event_type TEXT, utc_timestamp TEXT);
                CREATE TABLE network_samples(sample_id INTEGER PRIMARY KEY, player_session_id TEXT, utc_timestamp TEXT, ping_ms INTEGER);
            """)
            base = datetime(2026, 8, 1, tzinfo=timezone.utc)
            sessions = []
            for index in range(100):
                session_id = f"p-{index}"
                joined = (base + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
                left = (base + timedelta(seconds=index + 10)).isoformat().replace("+00:00", "Z")
                sessions.append((session_id, joined, left))
            connection.executemany("INSERT INTO player_sessions VALUES(?,?,?)", sessions)
            connection.executemany("INSERT INTO events VALUES(?,?,?)", [(index + 1, "player_death", row[2]) for index, row in enumerate(sessions)])
            connection.executemany("INSERT INTO combat_events VALUES(?,?)", [(index + 1, row[0]) for index, row in enumerate(sessions)])
            connection.executemany("INSERT INTO network_samples(player_session_id,utc_timestamp,ping_ms) VALUES(?,?,?)", [(row[0], row[2], 160) for row in sessions])
            session_rows = [connection.execute("SELECT * FROM player_sessions WHERE player_session_id=?", (row[0],)).fetchone() for row in sessions]
            statements = []
            connection.set_trace_callback(statements.append)
            result = leave_context(connection, session_rows)
            self.assertEqual(len(result), len(sessions))
            self.assertLessEqual(sum(statement.lstrip().upper().startswith("SELECT") for statement in statements), 2)

    def test_streamed_movement_memory_is_bounded(self):
        def rows():
            for index in range(100_000):
                yield {"game_ms": index * 250, "x": index * 0.1, "y": 0, "state_json": "{}"}

        tracemalloc.start()
        _stream_movement_features(rows())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 1_000_000)


if __name__ == "__main__":
    unittest.main()
