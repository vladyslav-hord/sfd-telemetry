import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from types import SimpleNamespace

from analyzer.dashboard import _compact_report, _live_summary, build_dashboard
from analyzer import report as report_module
from analyzer.report import base_report, write_report


class DashboardTests(unittest.TestCase):
    def test_population_contract_does_not_confuse_entities_with_sessions(self):
        payload = {
            "report_date": "2026-08-24",
            "generated_at": "2026-08-25T00:00:00Z",
            "data_cutoff": "2026-08-24T23:59:00Z",
            "server": {
                "player_entities": 1731, "human_players": 83, "bot_players": 0,
                "human_sessions": 1721, "bot_sessions": 10, "sessions": 1731,
                "rounds": 12, "combat": {},
            },
            "players": [{"player_identity_id": str(i), "is_bot": False} for i in range(83)],
        }
        summary = _compact_report(payload)
        self.assertEqual(summary["kpi"]["players"], 83)
        self.assertEqual(summary["kpi"]["observed_entities"], 1731)
        self.assertEqual(summary["counts"]["players"]["total"], 83)
        self.assertEqual(summary["population"]["human_sessions"], 1721)
        self.assertEqual(summary["population"]["sessions"], 1731)
        self.assertNotEqual(summary["kpi"]["players"], summary["population"]["sessions"])

    def test_top_n_counts_and_network_percentiles_are_explicit(self):
        payload = {
            "server": {"humans": 150, "bots": 0, "combat": {}},
            "players": [{"player_identity_id": str(i), "playtime_seconds": i} for i in range(150)],
            "network": {"sessions": [{
                "player_identity_id": str(i), "p95": 90 + i, "p99": 100 + i,
                "max": 400 + i, "samples": 20,
            } for i in range(150)]},
            "rounds": [{"duration_seconds": 60} for _ in range(150)],
            "patterns": [{"trigger": "melee_hit"} for _ in range(4)],
            "data_quality": {},
        }
        summary = _compact_report(payload)
        self.assertEqual(summary["counts"]["players"], {"shown": 100, "total": 150})
        self.assertEqual(summary["counts"]["network"], {"shown": 100, "total": 150})
        self.assertEqual(summary["network"]["resolved_sessions"], 150)
        self.assertEqual(summary["network"]["unresolved_sessions"], 0)
        self.assertEqual(summary["counts"]["rounds"], {"shown": 100, "total": 150})
        self.assertEqual(summary["counts"]["patterns"], {"shown": 1, "total": 1, "source_total": 4})
        self.assertEqual(summary["network"]["max_player_p95"], 239)
        self.assertTrue(all(row["p95"] != row["max"] for row in summary["network"]["outliers"]))
        self.assertTrue(any(row["network"]["p95"] is not None for row in summary["players"]))
        encoded = json.dumps(summary)
        self.assertNotIn('"player_session_id"', encoded)

    def test_day_freshness_has_as_of_when_cutoff_is_missing(self):
        summary = _compact_report({
            "report_date": "2026-08-24", "generated_at": "2026-08-25T00:00:00Z",
            "server": {"combat": {}}, "data_quality": {},
        })
        self.assertEqual(summary["freshness"]["as_of"], "2026-08-25T00:00:00Z")

    def test_live_uses_time_window_and_keeps_p95_separate_from_max(self):
        analytics = sqlite3.connect(":memory:")
        analytics.row_factory = sqlite3.Row
        analytics.executescript("""
            CREATE TABLE agg_server_minute(minute_start TEXT, metrics_json TEXT);
            CREATE TABLE agg_player_minute(minute_start TEXT, player_session_id TEXT, metrics_json TEXT);
            CREATE TABLE agg_network_minute(minute_start TEXT, player_session_id TEXT, ping_count INTEGER, ping_sum REAL, ping_min REAL, ping_max REAL, histogram_json TEXT);
        """)
        analytics.executemany("INSERT INTO agg_server_minute VALUES(?,?)", [
            ("2026-08-24T17:00:00Z", '{"events":999}'),
            ("2026-08-24T20:00:00Z", '{"events":10}'),
            ("2026-08-24T21:00:00Z", '{"events":20}'),
        ])
        analytics.executemany("INSERT INTO agg_player_minute VALUES(?,?,?)", [
            ("2026-08-24T17:00:00Z", "old", '{}'),
            ("2026-08-24T21:00:00Z", "current", '{}'),
        ])
        analytics.execute("INSERT INTO agg_network_minute VALUES(?,?,?,?,?,?,?)", (
            "2026-08-24T21:00:00Z", "current", 100, 5000, 10, 500,
            '{"10":95,"500":5}',
        ))
        summary = _live_summary(SimpleNamespace(), analytics)
        analytics.close()
        self.assertEqual(summary["kpi"]["events"], 30)
        self.assertIsNone(summary["population"]["player_entities"])
        self.assertIsNone(summary["population"]["unique_players"])
        self.assertEqual(summary["population"]["unknown_sessions"], 1)
        self.assertEqual(summary["freshness"]["window_start"], "2026-08-24T18:00:00Z")
        self.assertLess(summary["kpi"]["p95_ping"], summary["kpi"]["ping_max"])

    def test_live_maps_sessions_to_distinct_identities_and_marks_unknowns(self):
        with TemporaryDirectory() as tmp:
            telemetry_path = Path(tmp) / "telemetry.sqlite"
            telemetry = sqlite3.connect(telemetry_path)
            telemetry.execute("CREATE TABLE player_sessions(player_session_id TEXT, player_identity_id TEXT, is_bot INTEGER)")
            telemetry.executemany("INSERT INTO player_sessions VALUES(?,?,?)", [("s1", "human-1", 0), ("s2", "human-1", 0), ("b1", "bot-1", 1), ("ub", None, 1)])
            telemetry.commit()
            telemetry.close()
            analytics = sqlite3.connect(":memory:")
            analytics.row_factory = sqlite3.Row
            analytics.executescript("""
                CREATE TABLE agg_server_minute(minute_start TEXT, metrics_json TEXT);
                CREATE TABLE agg_player_minute(minute_start TEXT, player_session_id TEXT, metrics_json TEXT);
                CREATE TABLE agg_network_minute(minute_start TEXT, player_session_id TEXT, ping_count INTEGER, ping_sum REAL, ping_min REAL, ping_max REAL, histogram_json TEXT);
            """)
            minute = "2026-08-24T21:00:00Z"
            analytics.execute("INSERT INTO agg_server_minute VALUES(?,?)", (minute, '{"events":4}'))
            analytics.executemany("INSERT INTO agg_player_minute VALUES(?,?,?)", [(minute, session, '{"events":1}') for session in ("s1", "s2", "b1", "u1", "ub")])
            analytics.executemany("INSERT INTO agg_network_minute VALUES(?,?,?,?,?,?,?)", [(minute, session, 10, 500, 10, 100, '{"10":10}') for session in ("s1", "s2", "b1", "u1", "ub")])
            summary = _live_summary(SimpleNamespace(telemetry_database=str(telemetry_path)), analytics)
            analytics.close()
        population = summary["population"]
        self.assertEqual(population["player_entities"], 2)
        self.assertEqual(population["unique_players"], 2)
        self.assertEqual(population["sessions_window"], 5)
        self.assertEqual(population["human_players"], 1)
        self.assertEqual(population["bot_players"], 1)
        self.assertEqual(population["human_sessions"], 2)
        self.assertEqual(population["bot_sessions"], 1)
        self.assertEqual(population["unknown_sessions"], 2)
        self.assertEqual(population["unknown_bot_sessions"], 1)
        self.assertEqual(summary["network"]["resolved_sessions"], 3)
        self.assertEqual(summary["network"]["unresolved_sessions"], 2)
        self.assertTrue(any(row["sessions"] == 2 for row in summary["players"]))
        self.assertTrue(any(row["mapping"] == "session only" for row in summary["network"]["outliers"]))

    def test_live_distinguishes_caught_up_source_idle_from_backlog(self):
        with TemporaryDirectory() as tmp:
            telemetry_path = Path(tmp) / "telemetry.sqlite"
            telemetry = sqlite3.connect(telemetry_path)
            telemetry.execute("CREATE TABLE events(event_id INTEGER PRIMARY KEY, utc_timestamp TEXT)")
            now = datetime.now(timezone.utc)
            telemetry.executemany("INSERT INTO events VALUES(?,?)", [(1, (now - timedelta(seconds=120)).isoformat()), (2, (now - timedelta(seconds=100)).isoformat()), (3, (now - timedelta(seconds=92)).isoformat())])
            telemetry.commit()
            telemetry.close()

            def summary_for(checkpoint_id: int, processed_at: datetime):
                analytics = sqlite3.connect(":memory:")
                analytics.row_factory = sqlite3.Row
                analytics.executescript("""
                    CREATE TABLE agg_server_minute(minute_start TEXT, metrics_json TEXT, updated_at TEXT);
                    CREATE TABLE processing_checkpoints(consumer_name TEXT, last_event_id INTEGER, processed_at TEXT);
                """)
                minute = (now - timedelta(minutes=1)).isoformat()
                analytics.execute("INSERT INTO agg_server_minute VALUES(?,?,?)", (minute, '{"events":1}', processed_at.isoformat()))
                analytics.execute("INSERT INTO processing_checkpoints VALUES('live_analyzer',?,?)", (checkpoint_id, processed_at.isoformat()))
                result = _live_summary(SimpleNamespace(telemetry_database=str(telemetry_path)), analytics)
                analytics.close()
                return result

            caught_up = summary_for(3, now - timedelta(seconds=92))
            caught_freshness = caught_up["freshness"]
            self.assertEqual(caught_freshness["backlog_lag_seconds"], 0.0)
            self.assertEqual(caught_freshness["lag_seconds"], 0.0)
            self.assertGreater(caught_freshness["source_idle_seconds"], 80)
            self.assertNotIn("Pipeline backlog", {item["title"] for item in caught_up["incidents"]})

            telemetry = sqlite3.connect(telemetry_path)
            telemetry.executemany("UPDATE events SET utc_timestamp=? WHERE event_id=?", [( (now - timedelta(seconds=40)).isoformat(), 1), ((now - timedelta(seconds=30)).isoformat(), 2), ((now - timedelta(seconds=10)).isoformat(), 3)])
            telemetry.commit()
            telemetry.close()
            behind = summary_for(1, now - timedelta(seconds=40))
            behind_freshness = behind["freshness"]
            self.assertGreater(behind_freshness["backlog_lag_seconds"], 20)
            self.assertGreater(behind_freshness["source_idle_seconds"], 0)
            self.assertEqual(behind["kpi"]["lag_seconds"], behind_freshness["backlog_lag_seconds"])

    def test_live_is_backward_compatible_with_an_empty_legacy_database(self):
        with sqlite3.connect(":memory:") as analytics:
            summary = _live_summary(SimpleNamespace(), analytics)
        self.assertEqual(summary["schema_version"], 3)
        self.assertEqual(summary["kind"], "live")
        self.assertIsNone(summary["freshness"]["as_of"])
    def test_compact_summary_has_sections_and_no_sensitive_raw_fields(self):
        payload = base_report(
            "2026-08-24", "Europe/Warsaw", "run", "2026-08-24T22:00:00Z",
            {"sessions": 2, "rounds": 1, "humans": 1, "bots": 1,
             "events_by_type": {"chat_message": 10},
             "combat": {"damage": 20, "inferred_kills": 1},
             "data_quality": {"event_count": 10},
             "retention": {"active_players": 1},
             "environment": {"available": True,
                 "interactions_by_type": {"player_kick_object": 2},
                 "object_categories": {"barrel": 2},
                 "motifs": [{"motif": "kick>hit", "occurrences": 2, "player_session_id": "secret-session"}],
                 "interaction_heatmap": [{"x": 1, "y": 2, "count": 3, "source_event_id": "secret-event"}],
                 "episodes": [{"source_event_id": 42, "trigger": "scene_window_complete", "coverage": 1.0}],
                 "barrel_boost_candidates": [{"object_name": "OilBarrel", "object_id": "secret-object", "player_session_id": "secret-player"}]}},
            [],
            [{"player_identity_id": "persistent-id", "is_bot": False, "sessions": 1,
              "playtime_seconds": 60, "combat": {}, "input": {}, "movement": {},
              "skill_profile": {}, "statistics": {}}],
            [], [], "disabled",
            [{"player_a_session_id": "a", "player_b_session_id": "b", "damage": 1, "events": 1}],
            [], [],
        )
        payload["chat"] = {"raw_messages": ["must not be exported"]}
        payload["response_json"] = "must not be exported"
        summary = _compact_report(payload)
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertLess(len(encoded), 30000)
        self.assertNotIn("persistent-id", encoded)
        self.assertNotIn("secret-player", encoded)
        self.assertNotIn("response_json", encoded)
        self.assertNotIn("raw_messages", encoded)
        self.assertNotIn("secret-session", encoded)
        self.assertNotIn("secret-event", encoded)
        for key in ("timeline", "players", "maps", "rounds", "combat", "network", "environment", "patterns", "ai", "quality", "storage"):
            self.assertIn(key, summary)

    def test_report_serialization_removes_exact_server_duplicates(self):
        server = {"humans": 1, "data_quality": {"events": 1},
                  "retention": {"active_players": 1}, "environment": {"available": True}}
        report = base_report("2026-08-24", "UTC", "run", "cutoff", server,
                             [], [], [], [], "disabled", [], [], [])
        self.assertNotIn("data_quality", report["server"])
        self.assertNotIn("retention", report["server"])
        self.assertNotIn("environment", report["server"])
        self.assertEqual(report["data_quality"], {"events": 1})

    def test_report_uses_versioned_fallback_when_target_is_locked(self):
        with TemporaryDirectory() as directory:
            reports = Path(directory) / "reports"
            reports.mkdir()
            target = reports / "2026-08-24.json"
            target.write_text('{"old":true}\n', encoding="utf-8")
            original_replace = report_module.os.replace

            def locked_replace(source, destination):
                if Path(destination) == target:
                    raise PermissionError("simulated Notepad lock")
                return original_replace(source, destination)

            payload = {"report_date": "2026-08-24", "value": "fresh"}
            with patch("analyzer.report.os.replace", side_effect=locked_replace), patch("analyzer.report.time.sleep"):
                result = write_report(str(reports), "2026-08-24", payload)
            self.assertNotEqual(result, target)
            self.assertIn(".fallback-", result.name)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"old":true}\n')
            self.assertEqual(json.loads(result.read_text(encoding="utf-8")), payload)

    def test_dashboard_selects_fresh_fallback_and_links_its_artifacts(self):
        with TemporaryDirectory() as directory:
            reports = Path(directory) / "reports"
            old = base_report("2026-08-24", "UTC", "old", "cutoff", {"humans": 0, "bots": 0, "data_quality": {"event_count": 1}, "retention": {}, "environment": {}}, [], [], [], [], "disabled", [], [], [])
            fresh = base_report("2026-08-24", "UTC", "fresh", "cutoff", {"humans": 0, "bots": 0, "data_quality": {"event_count": 99}, "retention": {}, "environment": {}}, [], [], [], [], "disabled", [], [], [])
            write_report(str(reports), "2026-08-24", old)
            target = reports / "2026-08-24.json"
            original_replace = report_module.os.replace

            def locked_replace(source, destination):
                if Path(destination) == target:
                    raise PermissionError("simulated Notepad lock")
                return original_replace(source, destination)

            with patch("analyzer.report.os.replace", side_effect=locked_replace), patch("analyzer.report.time.sleep"):
                fallback = write_report(str(reports), "2026-08-24", fresh)
            index = build_dashboard(str(reports))
            summary = json.loads((index.parent / "days" / "2026-08-24.json").read_text(encoding="utf-8"))
            index_data = json.loads((index.parent / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["kpi"]["events"], 99)
            self.assertEqual(index_data["days"][0]["artifact"], fallback.name)
            self.assertTrue(index_data["days"][0]["json"].endswith(".json"))
            self.assertIn("2026-08-24", index_data["days"][0]["href"])

    def test_day_html_contains_structure_not_raw_dump(self):
        with TemporaryDirectory() as directory:
            reports = Path(directory) / "reports"
            report = base_report("2026-08-24", "UTC", "run", "cutoff",
                                 {"humans": 0, "bots": 0, "data_quality": {},
                                  "retention": {}, "environment": {}},
                                 [], [], [], [], "disabled", [], [], [])
            write_report(str(reports), "2026-08-24", report)
            index = build_dashboard(str(reports))
            day = index.parent / "days" / "2026-08-24.html"
            content = day.read_text(encoding="utf-8")
            self.assertIn("id='dashboard-app'", content)
            self.assertIn("id='players-table'", content)
            self.assertIn("id='ai-content'", content)
            for section in ("incidents", "overview", "timeline", "maps", "rounds", "combat", "network", "environment", "patterns", "ai", "quality", "storage"):
                self.assertIn(f"id='{section}'", content)
            self.assertNotIn("<pre>", content)
            self.assertLess(day.stat().st_size, 50000)
            self.assertTrue((day.parent / "2026-08-24.json").exists())
            js = (day.parent.parent / "assets" / "dashboard.js").read_text(encoding="utf-8")
            self.assertIn("data-sortable=\"true\"", js)
            self.assertIn("type=\"search\"", js)
            self.assertIn("setInterval(poll,60000)", js)
            self.assertIn("cache:'no-store'", js)


if __name__ == "__main__":
    unittest.main()
