import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from analyzer.metrics import infer_death_attribution, percentile, ping_metrics, stat_deltas
from analyzer.scene import _barrel_candidates, load_episode, object_category
from analyzer.features import movement_features
from analyzer.config import Config
from analyzer.main import analyze_day
from analyzer.dashboard import _episode_page
from analyzer.llm import valid_gameplay
from analyzer.report import validate_report


class MetricsTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([1, 2, 3, 4], .5), 2.5)

    def test_ping_metrics_estimate_jitter_and_time(self):
        samples = [("2026-08-01T10:00:00Z", 90), ("2026-08-01T10:00:05Z", 160), ("2026-08-01T10:00:10Z", 160)]
        result = ping_metrics(samples)
        self.assertEqual(result["spikes"]["50"], 1)
        self.assertEqual(result["seconds_above"]["100"], 5.0)

    def test_empty_ping_is_not_zero_coverage(self):
        self.assertEqual(ping_metrics([]), {"samples": 0, "coverage": 0.0})

    def test_stat_delta_uses_first_and_last_snapshot(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE player_stat_snapshots(player_session_id TEXT, utc_timestamp TEXT, stats_json TEXT)")
            conn.executemany("INSERT INTO player_stat_snapshots VALUES(?,?,?)", [("a", "2026-08-01T00:00:00Z", '{"TotalShotsFired":2}'), ("a", "2026-08-01T00:01:00Z", '{"TotalShotsFired":7}')])
            result = stat_deltas(conn, ["a"], "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
        self.assertEqual(result["a"]["TotalShotsFired"], 5)

    def test_movement_distance_and_static_time(self):
        samples = [{"game_ms": 0, "x": 0, "y": 0}, {"game_ms": 1000, "x": 3, "y": 4}, {"game_ms": 2000, "x": 3, "y": 4}]
        result = movement_features(samples)
        self.assertEqual(result["distance"], 5)
        self.assertEqual(result["static_seconds"], 1)

    def test_report_rejects_raw_chat(self):
        with self.assertRaises(ValueError):
            validate_report({"schema_version": 1, "chat": {"raw_messages": ["no"]}})

    def test_kill_attribution_uses_final_hit_and_assist_threshold(self):
        death = {"event_id": 9, "utc_timestamp": "2026-08-01T00:00:10Z", "round_id": "r", "victim_session_id": "victim"}
        damage = [
            {"event_id": 1, "utc_timestamp": "2026-08-01T00:00:04Z", "round_id": "r", "victim_session_id": "victim", "attacker_session_id": "assist", "damage": 60},
            {"event_id": 2, "utc_timestamp": "2026-08-01T00:00:09.5Z", "round_id": "r", "victim_session_id": "victim", "attacker_session_id": "killer", "damage": 20},
        ]
        self.assertEqual(infer_death_attribution(death, damage), ("killer", "high", ["assist"]))

    def test_kill_attribution_refuses_stale_or_cross_round_damage(self):
        death = {"event_id": 9, "utc_timestamp": "2026-08-01T00:00:10Z", "round_id": "r", "victim_session_id": "victim"}
        stale = [{"event_id": 1, "utc_timestamp": "2026-08-01T00:00:06Z", "round_id": "r", "victim_session_id": "victim", "attacker_session_id": "a", "damage": 100}]
        other_round = [{"event_id": 2, "utc_timestamp": "2026-08-01T00:00:09Z", "round_id": "else", "victim_session_id": "victim", "attacker_session_id": "a", "damage": 100}]
        self.assertEqual(infer_death_attribution(death, stale), (None, "unattributed", []))
        self.assertEqual(infer_death_attribution(death, other_round), (None, "unattributed", []))

    def test_scene_category_and_barrel_detector_requires_both_trajectories(self):
        self.assertEqual(object_category("OilBarrel01"), "barrel")
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE scene_interactions(scene_interaction_id INTEGER,event_id INTEGER,round_id TEXT,game_ms REAL,player_session_id TEXT,target_entity_id INTEGER,interaction_type TEXT,utc_timestamp TEXT);
                CREATE TABLE scene_entities(scene_entity_id INTEGER,name TEXT);
                CREATE TABLE scene_samples(scene_entity_id INTEGER,round_id TEXT,game_ms REAL,velocity_x REAL,velocity_y REAL,x REAL,y REAL);
                CREATE TABLE state_samples(player_session_id TEXT,round_id TEXT,game_ms REAL,velocity_x REAL,velocity_y REAL,x REAL,y REAL);
            """)
            conn.execute("INSERT INTO scene_entities VALUES(1,'OilBarrel01')")
            conn.execute("INSERT INTO scene_interactions VALUES(1,10,'r',1000,'p',1,'player_kick_object','2026-08-01T00:00:01Z')")
            conn.executemany("INSERT INTO scene_samples VALUES(?,?,?,?,?,?,?)", [(1,'r',950,0,0,0,0),(1,'r',1050,3,0,1,0)])
            conn.executemany("INSERT INTO state_samples VALUES(?,?,?,?,?,?,?)", [('p','r',950,0,0,0,0),('p','r',1500,3,0,2,0)])
            result = _barrel_candidates(conn, "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
        self.assertEqual(result[0]["confidence"], "high")

    def test_scene_episode_missing_is_none(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE scene_windows(source_event_id INTEGER,round_id TEXT,trigger_game_ms REAL,trigger TEXT,coverage REAL,entities_json TEXT)")
            conn.execute("CREATE TABLE events(event_id INTEGER,utc_timestamp TEXT)")
            self.assertIsNone(load_episode(conn, 1))

    def test_scene_replay_has_required_controls(self):
        page = _episode_page({"trigger": "kick", "coverage": 1.0, "interactions": [], "limitations": [], "samples": [], "players": [], "entities": [], "trigger_game_ms": 0})
        self.assertIn("data-layer='players'", page)
        self.assertIn("data-layer='exact'", page)
        self.assertIn("id='scrubber'", page)

    def test_gameplay_schema_requires_matching_window(self):
        value = {"window_id": "scene:42", "classification": "normal_play", "mechanic_family": "unknown", "novelty_score": .2, "advantage_observed": False, "advantage_description": "none", "observations": [], "known_pattern_id": None, "candidate_signature_features": [], "confidence": .8, "evidence_event_ids": [42], "should_create_candidate": False}
        self.assertTrue(valid_gameplay(value, "scene:42"))
        self.assertFalse(valid_gameplay(value, "scene:43"))


class DailyIntegrationTests(unittest.TestCase):
    def test_daily_creates_analytics_and_safe_json(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            telemetry = root / "telemetry.sqlite3"
            schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(encoding="utf-8")
            conn = sqlite3.connect(telemetry)
            try:
                conn.executescript(schema)
                conn.execute("INSERT INTO server_sessions(server_session_id,started_at,first_event_at,last_event_at) VALUES('s','2026-08-24T10:00:00Z','2026-08-24T10:00:00Z','2026-08-24T10:00:05Z')")
                conn.execute("INSERT INTO players VALUES('p','host_scoped','2026-08-24T10:00:00Z','2026-08-24T10:05:00Z')")
                conn.execute("INSERT INTO player_sessions(player_session_id,server_session_id,player_identity_id,joined_at,left_at,duration_ms,is_user,is_bot,is_host,is_moderator,joined_as_spectator,initial_spectating) VALUES('ps','s','p','2026-08-24T10:00:00Z','2026-08-24T10:05:00Z',300000,1,0,0,0,0,0)")
                conn.execute("INSERT INTO events(schema_version,event_type,server_session_id,sequence,utc_timestamp,data_json,raw_json) VALUES(1,'network_sample','s',1,'2026-08-24T10:00:00Z','{}','{}')")
                conn.execute("INSERT INTO network_samples(event_id,player_session_id,utc_timestamp,ping_ms) VALUES(1,'ps','2026-08-24T10:00:00Z',110)")
                conn.commit()
            finally:
                conn.close()
            config = Config(telemetry_database=str(telemetry), analytics_database=str(root / "analytics.sqlite3"), report_directory=str(root / "reports"), openai_enabled=False)
            result = analyze_day(config, __import__('datetime').date(2026, 8, 24), "test")
            payload = __import__('json').loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"]["llm"], "disabled")
            self.assertEqual(payload["server"]["humans"], 1)
            self.assertNotIn("raw_messages", payload["chat"])
