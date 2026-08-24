import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from analyzer.main import lock_path_for_command, main, recover_stale_analysis_runs


ROOT = Path(__file__).resolve().parents[1]


class HourlyOrchestrationTests(unittest.TestCase):
    def test_live_and_batch_use_distinct_lock_files(self):
        config = SimpleNamespace(analytics_database="data/analytics.sqlite3")

        live = lock_path_for_command(config, "live")
        hourly = lock_path_for_command(config, "hourly")

        self.assertEqual(live.name, "analytics.live.lock")
        self.assertEqual(hourly.name, "analytics.batch.lock")
        self.assertNotEqual(live, hourly)

    def test_daily_is_not_a_cli_mode(self):
        with self.assertRaises(SystemExit) as error:
            main(["daily"])
        self.assertEqual(error.exception.code, 2)

    def test_hourly_launcher_does_not_control_live_process(self):
        batch = (ROOT / "tools" / "run_batch_analysis.ps1").read_text(encoding="utf-8")
        supervisor = (ROOT / "start_analyzer.bat").read_text(encoding="utf-8")

        self.assertIn("[ValidateSet('hourly')]", batch)
        self.assertNotIn("analyzer.pause", batch)
        self.assertNotIn("Stop-Process", batch)
        self.assertNotIn("Start-Process", batch)
        self.assertNotIn("analyzer.pause", supervisor)

    def test_dashboard_launcher_reuses_listener_and_cleans_stale_exact_server(self):
        launcher = (ROOT / "open_dashboard.bat").read_text(encoding="utf-8")
        self.assertIn("Invoke-WebRequest", launcher)
        self.assertIn("Get-CimInstance Win32_Process", launcher)
        self.assertIn("http.server 8765", launcher)
        self.assertIn("launch_lock", launcher)
        self.assertIn("stop_matching_servers", launcher)
        self.assertIn("if !errorlevel! EQU 0 goto :open_browser", launcher)
        self.assertNotIn("taskkill /im py.exe", launcher.lower())

    def test_daily_launcher_removed(self):
        self.assertFalse((ROOT / "run_daily_analysis.bat").exists())
        self.assertIn("tools\\run_batch_analysis.ps1", (ROOT / "run_hourly_analysis.bat").read_text(encoding="utf-8"))

    def test_batch_start_recovers_stale_runs_without_touching_live_or_current(self):
        with TemporaryDirectory() as temporary:
            analytics = sqlite3.connect(Path(temporary) / "analytics.sqlite3")
            analytics.executescript((ROOT / "analyzer" / "schema.sql").read_text(encoding="utf-8"))
            analytics.executemany(
                "INSERT INTO analysis_runs(analysis_run_id,command,started_at,status) VALUES(?,?,?,'running')",
                [
                    ("hourly-stale", "hourly", "2026-08-24T19:00:00+00:00"),
                    ("rebuild-stale", "rebuild", "2026-08-24T19:30:00+00:00"),
                    ("live-run", "live", "2026-08-24T19:00:00+00:00"),
                    ("current-run", "hourly", "2026-08-24T20:00:00+00:00"),
                ],
            )
            analytics.execute("INSERT INTO analysis_runs(analysis_run_id,command,started_at,status) VALUES('complete-run','hourly','2026-08-24T18:00:00+00:00','complete')")
            recovered_at = datetime(2026, 8, 24, 21, 0, tzinfo=timezone.utc).isoformat()
            self.assertEqual(recover_stale_analysis_runs(analytics, recovered_at, "current-run"), 2)
            rows = {row[0]: row for row in analytics.execute("SELECT analysis_run_id,status,completed_at,error_text FROM analysis_runs")}
            self.assertEqual(rows["hourly-stale"][1], "failed")
            self.assertEqual(rows["rebuild-stale"][1], "failed")
            self.assertEqual(rows["live-run"][1], "running")
            self.assertEqual(rows["current-run"][1], "running")
            self.assertEqual(rows["complete-run"][1], "complete")
            self.assertEqual(rows["hourly-stale"][2], recovered_at)
            self.assertIn("interrupted: stale batch run recovered at batch start", rows["hourly-stale"][3])
            self.assertIn("duration_seconds=7200.0", rows["hourly-stale"][3])
            analytics.close()


if __name__ == "__main__":
    unittest.main()
