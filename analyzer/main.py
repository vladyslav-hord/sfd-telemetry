from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_config
from .dashboard import build_dashboard
from .llm import queue_chat_requests, queue_gameplay_requests, queue_narrative_request, reconcile_chat_annotations, submit_pending_batches, sync_batches
from .metrics import aggregate_day, day_bounds
from .patterns import candidate_state
from .report import base_report, validate_report, write_report
from .storage import open_analytics, open_telemetry
from .live import LiveAnalyzer, run_maintenance
from .migration import migrate_telemetry

METRIC_VERSION = 4
ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another analyzer process is already running") from exc
            locked = True
            try:
                handle.seek(0)
                handle.write("0")
                handle.flush()
            except OSError as exc:
                locked = False
                raise RuntimeError("Another analyzer process is already running") from exc
        yield
    finally:
        if locked and os.name == "nt":
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            handle.close()
        except OSError:
            pass


def lock_path_for_command(config, command: str) -> Path:
    """Serialize analytics batch writers without stopping the live consumer."""
    suffix = ".live.lock" if command == "live" else ".batch.lock"
    return Path(config.analytics_database).with_suffix(suffix)


def upsert(analytics, table: str, keys: tuple, values: tuple) -> None:
    columns = ",".join(keys)
    placeholders = ",".join("?" for _ in values)
    updates = ",".join(f"{key}=excluded.{key}" for key in keys if key not in {"report_date", "player_identity_id", "map_name", "metric_version", "weapon"})
    analytics.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders}) ON CONFLICT DO UPDATE SET {updates}", values)


def recover_stale_analysis_runs(analytics, completed_at: str | None = None, exclude_run_id: str | None = None) -> int:
    """Mark abandoned batch runs before a new batch starts under the batch lock."""
    completed_at = completed_at or datetime.now(timezone.utc).isoformat()
    query = "SELECT analysis_run_id,started_at FROM analysis_runs WHERE status='running' AND command IN ('hourly','rebuild')"
    params: tuple = ()
    if exclude_run_id:
        query += " AND analysis_run_id<>?"
        params = (exclude_run_id,)
    rows = analytics.execute(query, params).fetchall()
    for row in rows:
        try:
            started = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            duration = max(0.0, (datetime.fromisoformat(completed_at) - started).total_seconds())
            detail = f"; duration_seconds={duration:.1f}"
        except (TypeError, ValueError):
            detail = ""
        analytics.execute(
            "UPDATE analysis_runs SET completed_at=?,status='failed',error_text=? WHERE analysis_run_id=? AND status='running'",
            (completed_at, "interrupted: stale batch run recovered at batch start" + detail, row[0]),
        )
    if rows:
        analytics.commit()
    return len(rows)


def analyze_day(config, report_day: date, command: str = "hourly") -> Path:
    zone = ZoneInfo(config.timezone)
    start, end = day_bounds(report_day.isoformat(), zone)
    run_id, now = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
    telemetry = open_telemetry(config.telemetry_database)
    analytics = open_analytics(config.analytics_database, ROOT / "analyzer" / "schema.sql")
    try:
        recover_stale_analysis_runs(analytics, now, run_id)
        analytics.execute("INSERT INTO analysis_runs(analysis_run_id,command,started_at,status) VALUES(?,?,?,'running')", (run_id, command, now))
        # Do not keep a SQLite write transaction open while the full-day read phase runs.
        analytics.commit()
        sync_batches(analytics, config.report_directory)
        reconcile_chat_annotations(analytics, telemetry)
        # Pin every deterministic telemetry query to one WAL snapshot. The
        # collector may continue writing, but this report must not mix rows
        # observed before and after a mid-run append.
        telemetry.execute("BEGIN")
        watermark = telemetry.execute("SELECT MAX(event_id), MAX(utc_timestamp) FROM events WHERE utc_timestamp<?", (end,)).fetchone()
        try:
            server, players, maps, network, weapons, pairs, rounds, windows = aggregate_day(telemetry, start, end, config.individual_bot_reports, config.static_speed_threshold, config.anomaly_robust_z_threshold, config.anomaly_min_sample_coverage)
            identity_rows = telemetry.execute("SELECT player_session_id, COALESCE(player_identity_id,player_session_id) FROM player_sessions").fetchall()
        finally:
            # Release the read snapshot before analytics writes, LLM queueing,
            # report serialization, or dashboard generation.
            telemetry.rollback()
        server["source_snapshot"] = {
            "capture": "sqlite-read-transaction",
            "max_event_id": watermark[0],
            "max_utc_timestamp": watermark[1],
            "window_start": start,
            "window_end": end,
        }
        if watermark[0] is not None:
            analytics.execute("INSERT INTO analysis_watermarks(source_name,value,updated_at) VALUES('telemetry_events',?,?) ON CONFLICT(source_name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (json.dumps({"event_id": watermark[0], "utc_timestamp": watermark[1]}), now))
        analytics.commit()
        source_coverage = server["data_quality"]["source_coverage"]
        server["analysis_state"] = "in_progress" if command == "hourly" else "complete"
        prior_report = analytics.execute("SELECT llm_status,report_json FROM daily_reports WHERE report_date=? AND metric_version=?", (report_day.isoformat(), METRIC_VERSION)).fetchone()
        prior_llm_status = prior_report["llm_status"] if prior_report else None
        llm_status = "disabled" if not config.openai_enabled or not os.getenv("OPENAI_API_KEY") else "pending"
        if llm_status == "disabled" and prior_llm_status in {"complete", "partial", "pending"}:
            llm_status = prior_llm_status
        generated = datetime.now(timezone.utc).isoformat()
        quality = server["analysis_state"]
        upsert(analytics, "daily_server_metrics", ("report_date", "metric_version", "metrics_json", "source_coverage", "quality", "generated_at", "analysis_run_id"), (report_day.isoformat(), METRIC_VERSION, json.dumps(server), source_coverage, quality, generated, run_id))
        for player in players:
            metrics = {key: value for key, value in player.items() if key not in {"player_identity_id", "is_bot"}}
            upsert(analytics, "daily_player_metrics", ("report_date", "player_identity_id", "metric_version", "is_bot", "metrics_json", "source_coverage", "quality", "generated_at", "analysis_run_id"), (report_day.isoformat(), player["player_identity_id"], METRIC_VERSION, int(player["is_bot"]), json.dumps(metrics), source_coverage, quality, generated, run_id))
            profile = player.get("skill_profile")
            if profile:
                upsert(analytics, "player_style_profiles", ("player_identity_id", "metric_version", "features_json", "sample_rounds", "confidence", "generated_at", "analysis_run_id"), (player["player_identity_id"], METRIC_VERSION, json.dumps(profile), player["sessions"], profile["confidence"], generated, run_id))
        for item in maps:
            upsert(analytics, "daily_map_metrics", ("report_date", "map_name", "metric_version", "metrics_json", "source_coverage", "quality", "generated_at", "analysis_run_id"), (report_day.isoformat(), item["map_name"], METRIC_VERSION, json.dumps(item), source_coverage, quality, generated, run_id))
        for item in weapons:
            upsert(analytics, "daily_weapon_metrics", ("report_date", "weapon", "metric_version", "metrics_json", "source_coverage", "quality", "generated_at", "analysis_run_id"), (report_day.isoformat(), item["weapon"], METRIC_VERSION, json.dumps(item), source_coverage, quality, generated, run_id))
        identities = {row[0]: row[1] for row in identity_rows}
        for item in pairs:
            first, second = identities.get(item["player_a_session_id"], item["player_a_session_id"]), identities.get(item["player_b_session_id"], item["player_b_session_id"])
            if first == second:
                continue
            if first > second: first, second = second, first
            upsert(analytics, "daily_pair_metrics", ("report_date", "player_a_id", "player_b_id", "metric_version", "metrics_json", "source_coverage", "quality", "generated_at", "analysis_run_id"), (report_day.isoformat(), first, second, METRIC_VERSION, json.dumps(item), source_coverage, "derived", generated, run_id))
        for item in rounds:
            upsert(analytics, "round_metrics", ("round_id", "metric_version", "metrics_json", "source_coverage", "quality", "generated_at", "analysis_run_id"), (item["round_id"], METRIC_VERSION, json.dumps(item), source_coverage, item["result_quality"], generated, run_id))
        for item in windows:
            window_id = f"{report_day.isoformat()}:{item['source_window_id']}"
            analytics.execute("INSERT INTO candidate_windows(window_id,report_date,source_window_id,features_json,coverage,status,created_at) VALUES(?,?,?,?,?,'candidate',?) ON CONFLICT(source_window_id) DO UPDATE SET features_json=excluded.features_json,coverage=excluded.coverage,status='candidate'", (window_id, report_day.isoformat(), item["source_window_id"], json.dumps(item, ensure_ascii=False), item["features"]["coverage"], generated))
        # Candidates become catalog entries only after repeated, cross-round evidence.
        for row in analytics.execute("SELECT json_extract(features_json,'$.signature') signature, COUNT(*) occurrences, COUNT(DISTINCT json_extract(features_json,'$.player_session_id')) players, COUNT(DISTINCT json_extract(features_json,'$.round_id')) rounds FROM candidate_windows GROUP BY signature").fetchall():
            if not row["signature"]: continue
            state = candidate_state(row["occurrences"], row["players"], row["rounds"], 0.0, 0.0, 0.0, config.pattern_confirmation_occurrences)
            pattern_id = f"pattern_{row['signature']}"
            analytics.execute("INSERT INTO pattern_catalog(pattern_id,state,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(pattern_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at", (pattern_id, state, json.dumps({"occurrences": row["occurrences"], "players": row["players"], "rounds": row["rounds"]}), generated, generated))
        for item in windows:
            pattern_id = f"pattern_{item['signature']}"
            source_window_id = item["source_window_id"]
            confidence = min(1.0, abs(item["robust_z"] or 0) / max(config.anomaly_robust_z_threshold, 1))
            analytics.execute("INSERT INTO pattern_matches(pattern_id,source_window_id,confidence,quality,created_at) VALUES(?,?,?,?,?) ON CONFLICT(pattern_id,source_window_id) DO UPDATE SET confidence=excluded.confidence,quality=excluded.quality", (pattern_id, source_window_id, confidence, "derived", generated))
        scene_patterns = []
        for item in server["environment"].get("barrel_boost_candidates", []):
            if item["confidence"] not in {"high", "medium"}:
                continue
            scene_item = {**item, "signature": "scene_barrel_boost", "features": {"coverage": item["coverage"], "object_speed_gain": item["object_speed_gain"], "player_speed_gain": item["player_speed_gain"], "player_displacement": item["player_displacement"]}}
            scene_patterns.append(scene_item)
            analytics.execute("INSERT INTO pattern_catalog(pattern_id,state,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(pattern_id) DO UPDATE SET updated_at=excluded.updated_at", ("pattern_scene_barrel_boost", "candidate", json.dumps({"type": "object_assisted_movement", "subtype": "barrel_boost_candidate"}), generated, generated))
            analytics.execute("INSERT INTO candidate_windows(window_id,report_date,source_window_id,features_json,coverage,status,created_at) VALUES(?,?,?,?,?,'candidate',?) ON CONFLICT(source_window_id) DO UPDATE SET features_json=excluded.features_json,coverage=excluded.coverage,status='candidate'", (report_day.isoformat() + ":" + item["source_window_id"], report_day.isoformat(), item["source_window_id"], json.dumps(scene_item, ensure_ascii=False), item["coverage"], generated))
            analytics.execute("INSERT INTO pattern_matches(pattern_id,source_window_id,confidence,quality,created_at) VALUES(?,?,?,?,?) ON CONFLICT(pattern_id,source_window_id) DO UPDATE SET confidence=excluded.confidence,quality=excluded.quality", ("pattern_scene_barrel_boost", item["source_window_id"], 1.0 if item["confidence"] == "high" else .65, "derived", generated))
        # Release the batch writer before chat/LLM and filesystem work.
        analytics.commit()
        queue_chat_requests(analytics, telemetry, start, end, config)
        queue_gameplay_requests(analytics, report_day.isoformat(), [*windows, *scene_patterns], config)
        queue_narrative_request(analytics, report_day.isoformat(), {"server": server, "maps": maps, "network": {"sessions": network}, "weapons": weapons, "patterns": windows}, config)
        llm_status = submit_pending_batches(analytics, config)
        analytics.commit()
        payload = base_report(report_day.isoformat(), config.timezone, run_id, end, server, maps, players, weapons, network, llm_status, pairs, rounds, [*windows, *scene_patterns])
        if prior_report:
            prior_payload = json.loads(prior_report["report_json"])
            if prior_payload.get("narrative"):
                payload["narrative"] = prior_payload["narrative"]
        write_report(config.report_directory, report_day.isoformat(), payload)
        build_dashboard(config.report_directory, report_day.isoformat(), config.telemetry_database)
        upsert(analytics, "daily_reports", ("report_date", "metric_version", "report_json", "llm_status", "generated_at", "analysis_run_id"), (report_day.isoformat(), METRIC_VERSION, json.dumps(payload, ensure_ascii=False), llm_status, generated, run_id))
        analytics.execute("UPDATE analysis_runs SET completed_at=?, status='complete' WHERE analysis_run_id=?", (datetime.now(timezone.utc).isoformat(), run_id))
        analytics.commit()
        return Path(config.report_directory) / f"{report_day}.json"
    except Exception as exc:
        analytics.rollback()
        analytics.execute("UPDATE analysis_runs SET completed_at=?, status='failed', error_text=? WHERE analysis_run_id=?", (datetime.now(timezone.utc).isoformat(), str(exc)[:1000], run_id))
        analytics.commit()
        raise
    finally:
        telemetry.close()
        analytics.close()


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    # Accept --config before or after the subcommand, matching the documented Windows command.
    argv = list(sys.argv[1:] if argv is None else argv)
    config_override = None
    if "--config" in argv:
        position = argv.index("--config")
        try:
            config_override = argv[position + 1]
        except IndexError as exc:
            raise ValueError("--config requires a path") from exc
        del argv[position:position + 2]
    parser = argparse.ArgumentParser(description="Incremental SFD telemetry analytics")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    hourly = sub.add_parser("hourly", help="recompute the current in-progress local day"); hourly.add_argument("--date")
    rebuild = sub.add_parser("rebuild"); rebuild.add_argument("--from", dest="start", required=True); rebuild.add_argument("--to", dest="end", required=True)
    submit = sub.add_parser("submit-llm"); submit.add_argument("--date")
    export = sub.add_parser("export"); export.add_argument("--date", required=True)
    dashboard = sub.add_parser("dashboard"); dashboard.add_argument("--date"); dashboard.add_argument("--episode")
    sub.add_parser("sync-llm"); sub.add_parser("validate")
    live = sub.add_parser("live", help="incrementally process new telemetry events")
    live.add_argument("--once", action="store_true", help="process one bounded batch and exit")
    reconcile = sub.add_parser("reconcile", help="rebuild live aggregates for a recent window")
    reconcile.add_argument("--hours", type=int, default=2)
    sub.add_parser("maintenance", help="apply retention and storage quota policy")
    migrate = sub.add_parser("migrate", help="create a separate telemetry SQLite v2 copy")
    migrate.add_argument("--destination")
    args = parser.parse_args(argv)
    config = load_config(config_override or args.config, ROOT)
    lock = lock_path_for_command(config, args.command)
    with run_lock(lock):
        if args.command == "live":
            live_analyzer = LiveAnalyzer(config, ROOT)
            try:
                if args.once:
                    print(json.dumps({"processed": live_analyzer.run_once()}))
                    return 0
                return live_analyzer.run()
            finally:
                live_analyzer.close()
        if args.command == "reconcile":
            live_analyzer = LiveAnalyzer(config, ROOT)
            try:
                print(json.dumps({"processed": live_analyzer.reconcile(args.hours)}))
                return 0
            finally:
                live_analyzer.close()
        if args.command == "maintenance":
            print(json.dumps(run_maintenance(config, ROOT), ensure_ascii=False))
            return 0
        if args.command == "migrate":
            destination = Path(args.destination) if args.destination else Path(config.telemetry_database).with_name("sfd_telemetry_v2.sqlite3")
            print(json.dumps(migrate_telemetry(config.telemetry_database, destination, ROOT / "sql" / "schema.sql"), ensure_ascii=False))
            return 0
        if args.command == "validate":
            telemetry = open_telemetry(config.telemetry_database)
            analytics = open_analytics(config.analytics_database, ROOT / "analyzer" / "schema.sql")
            try:
                telemetry_tables = {row[0] for row in telemetry.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                analytics_tables = {row[0] for row in analytics.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                needed_telemetry = {"events", "player_sessions", "rounds", "network_samples", "state_samples", "chat_messages", "combat_events", "player_stat_snapshots"}
                needed_analytics = {"analysis_runs", "daily_server_metrics", "daily_player_metrics", "daily_map_metrics", "round_metrics", "daily_weapon_metrics", "daily_pair_metrics", "player_style_profiles", "chat_annotations", "candidate_windows", "pattern_catalog", "pattern_matches", "llm_batches", "llm_requests", "daily_reports", "processing_checkpoints", "agg_server_minute", "episode_catalog", "pattern_candidates", "llm_jobs", "llm_results"}
                if needed_telemetry - telemetry_tables or needed_analytics - analytics_tables:
                    raise RuntimeError("Required analytics or telemetry tables are missing")
                for report in Path(config.report_directory).glob("*.json"):
                    validate_report(json.loads(report.read_text(encoding="utf-8")))
            finally:
                telemetry.close(); analytics.close()
            print("validation: ok")
            return 0
        if args.command == "sync-llm":
            analytics = open_analytics(config.analytics_database, ROOT / "analyzer" / "schema.sql")
            try: print(json.dumps({"imported": sync_batches(analytics, config.report_directory)}))
            finally: analytics.close()
            return 0
        if args.command == "dashboard":
            print(build_dashboard(config.report_directory, args.date, config.telemetry_database, int(args.episode) if args.episode else None))
            return 0
        if args.command == "submit-llm":
            analytics = open_analytics(config.analytics_database, ROOT / "analyzer" / "schema.sql")
            try: print(json.dumps({"status": submit_pending_batches(analytics, config)}))
            finally: analytics.close()
            return 0
        if args.command == "export":
            analytics = open_analytics(config.analytics_database, ROOT / "analyzer" / "schema.sql")
            try:
                row = analytics.execute("SELECT report_json FROM daily_reports WHERE report_date=? AND metric_version=?", (args.date, METRIC_VERSION)).fetchone()
            finally:
                analytics.close()
            if not row: raise RuntimeError(f"No report for {args.date}")
            print(row[0]); return 0
        if args.command == "rebuild":
            current, finish = parse_day(args.start), parse_day(args.end)
            while current <= finish:
                print(analyze_day(config, current, "rebuild"))
                current += timedelta(days=1)
            return 0
        zone = ZoneInfo(config.timezone)
        if args.command == "hourly":
            requested = parse_day(args.date) if args.date else datetime.now(zone).date()
            print(analyze_day(config, requested, "hourly"))
            return 0
        raise RuntimeError(f"Unsupported analyzer command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"analyzer failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
