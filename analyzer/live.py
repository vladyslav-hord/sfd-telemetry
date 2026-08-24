from __future__ import annotations

import json
import gzip
import hashlib
import os
import queue
import signal
import shutil
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .dashboard import build_live_dashboard
from .llm import _versioned_storage_name
from .storage import open_analytics, open_telemetry, record_storage_health, raw_quota, sqlite_quota


CRITICAL_TRIGGERS = {
    "player_death", "player_kill", "player_damage", "object_damage", "object_damage_batch",
    "projectile_hit", "explosion_hit", "scene_window_complete", "round_end",
}

AGGREGATE_KEYS = {
    "agg_server_minute": ("minute_start", "server_session_id"),
    "agg_player_minute": ("minute_start", "player_session_id"),
    "agg_weapon_minute": ("minute_start", "server_session_id", "weapon"),
    "agg_map_minute": ("minute_start", "server_session_id", "map_name"),
    "agg_pair_minute": ("minute_start", "player_a_session_id", "player_b_session_id"),
    "agg_scene_minute": ("minute_start", "round_id"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def minute_bucket(value: str) -> str:
    return value[:16] + ":00Z"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _number(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


class LiveAnalyzer:
    """Incremental consumer for telemetry events.

    The checkpoint is committed in the same transaction as all derived writes. A
    restart therefore either repeats no batch or repeats an idempotent transaction.
    """

    def __init__(self, config, root: Path | None = None):
        self.config = config
        self.root = root or Path(__file__).resolve().parents[1]
        self.telemetry = open_telemetry(config.telemetry_database)
        self.analytics = open_analytics(config.analytics_database, self.root / "analyzer" / "schema.sql")
        batch_size = min(2000, max(500, int(config.live_batch_size)))
        self.batch_size = batch_size
        self.event_queue: queue.Queue[list[sqlite3.Row]] = queue.Queue(maxsize=max(1, int(config.live_queue_size)))
        self.stop_requested = False
        self.last_dashboard_at = 0.0
        self.last_microbatch_at = time.monotonic()
        self.previous_state: dict[str, tuple[float, float, float, float, float]] = {}
        self.previous_objects: dict[tuple, tuple[float, float, float, float, float]] = {}
        self._scene_context: tuple[str | None, str | None] | None = None
        self._scene_state_cache_limit = max(1000, int(getattr(config, "live_scene_state_cache_limit", 50000)))
        self.scene_ring = deque(maxlen=60)  # 15 seconds at the target 4 Hz resolution.
        self._pending_json: dict[tuple[str, tuple], dict[str, float]] = {}
        self._pending_network: dict[tuple[str, str], dict] = {}
        self._raw_segment_sequences: dict[str, int] = {}
        self._raw_lock = threading.Lock()
        self._raw_stop = threading.Event()
        self._raw_wakeup = threading.Event()
        self._last_raw_mark_at = 0.0
        self._raw_mark_interval = max(1.0, float(getattr(config, "live_raw_mark_interval_seconds", 15)))
        self._raw_busy_timeout_ms = max(25, int(getattr(config, "live_raw_mark_busy_timeout_ms", 250)))
        self._raw_retry_at = 0.0
        self._raw_retry_delay = 0.25
        self._raw_worker = threading.Thread(target=self._raw_mark_worker, name="sfd-live-raw-ack", daemon=True)
        self._raw_worker.start()
        self._storage_cache: tuple[dict, dict, list[dict]] | None = None
        self._storage_cache_at = 0.0
        self._storage_check_interval = max(1.0, float(getattr(config, "live_storage_check_interval_seconds", 30)))
        self._llm_stop = threading.Event()
        self._llm_wakeup = threading.Event()
        self._llm_worker: threading.Thread | None = None
        # Collection remains independent of the API. The worker may reconcile
        # already accepted responses, but new near-live jobs are opt-in.
        if config.openai_enabled and os.getenv("OPENAI_API_KEY"):
            self._llm_worker = threading.Thread(target=self._near_live_worker, name="sfd-live-llm", daemon=True)
            self._llm_worker.start()

    def close(self) -> None:
        self._raw_stop.set()
        self._raw_wakeup.set()
        self._raw_worker.join(timeout=1.0)
        self._llm_stop.set()
        self._llm_wakeup.set()
        if self._llm_worker is not None:
            self._llm_worker.join(timeout=1.0)
        try:
            self._mark_raw_segments([], force=True)
        except sqlite3.Error:
            pass
        self.telemetry.close()
        self.analytics.close()

    def _near_live_worker(self) -> None:
        """Submit queued near-live jobs off the ingestion thread."""
        from .llm import submit_near_live_jobs

        connection = None
        try:
            connection = open_analytics(self.config.analytics_database, self.root / "analyzer" / "schema.sql")
            interval = max(1.0, float(getattr(self.config, "live_llm_interval_seconds", 15)))
            next_submit_at = 0.0
            while not self._llm_stop.is_set():
                now = time.monotonic()
                timeout = interval if next_submit_at <= now else next_submit_at - now
                self._llm_wakeup.wait(timeout)
                self._llm_wakeup.clear()
                if self._llm_stop.is_set():
                    break
                if next_submit_at > time.monotonic():
                    continue
                try:
                    submit_near_live_jobs(connection, self.config)
                except sqlite3.OperationalError:
                    connection.rollback()
                next_submit_at = time.monotonic() + interval
        finally:
            if connection is not None:
                connection.close()

    def _raw_mark_worker(self) -> None:
        interval = self._raw_mark_interval
        while not self._raw_stop.is_set():
            self._raw_wakeup.wait(interval)
            self._raw_wakeup.clear()
            if self._raw_stop.is_set():
                break
            self._mark_raw_segments()

    def request_stop(self, *_: object) -> None:
        self.stop_requested = True

    def _checkpoint(self, consumer: str = "live_analyzer") -> sqlite3.Row | None:
        return self.analytics.execute("SELECT * FROM processing_checkpoints WHERE consumer_name=?", (consumer,)).fetchone()

    def _next_rows(self, consumer: str = "live_analyzer") -> list[sqlite3.Row]:
        row = self._checkpoint(consumer)
        last_event_id = int(row["last_event_id"]) if row else 0
        return self.telemetry.execute(
            """SELECT event_id,event_type,server_session_id,round_id,player_session_id,
               sequence,utc_timestamp,game_ms,data_json FROM events
               WHERE event_id>? ORDER BY event_id LIMIT ?""", (last_event_id, self.batch_size)
        ).fetchall()

    def run_once(self) -> int:
        rows = self._next_rows()
        if not rows:
            self._raw_wakeup.set()
            self._maybe_microbatch()
            self._maybe_dashboard()
            return 0
        try:
            self.event_queue.put_nowait(rows)
        except queue.Full:
            # Keep ingestion independent from analytics; high-volume scene frames
            # are the first data allowed to wait/drop when the bounded queue is full.
            reduced = [row for row in rows if row["event_type"] not in {"scene_highres_batch", "highres_state_sample"}]
            if not reduced:
                self._record_drop(len(rows))
                return 0
            try:
                self.event_queue.put_nowait(reduced)
            except queue.Full:
                self._record_drop(len(rows))
                return 0
            self._record_drop(len(rows) - len(reduced))
        batch = self.event_queue.get_nowait()
        self._process_rows(batch, "live_analyzer")
        self._queue_raw_segments(batch)
        self._maybe_microbatch()
        self._maybe_dashboard()
        return len(batch)

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)
        print(f"live analyzer started; batch={self.batch_size} poll={self.config.live_poll_interval_seconds}s")
        processed = 0
        while not self.stop_requested:
            try:
                count = self.run_once()
            except sqlite3.OperationalError as exc:
                # A concurrent collector/maintenance transaction can briefly hold
                # the source SQLite lock. Keep the live consumer alive and retry.
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                print(f"live analyzer retry after SQLite lock: {exc}", flush=True)
                time.sleep(1.0)
                continue
            processed += count
            if not count:
                time.sleep(max(.1, float(self.config.live_poll_interval_seconds)))
        print(f"live analyzer stopped cleanly; processed={processed}")
        return 0

    def reconcile(self, hours: int = 2) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat().replace("+00:00", "Z")
        rows = self.telemetry.execute(
            """SELECT event_id,event_type,server_session_id,round_id,player_session_id,
               sequence,utc_timestamp,game_ms,data_json FROM events WHERE utc_timestamp>=? ORDER BY event_id""", (cutoff,)
        ).fetchall()
        affected = sorted({minute_bucket(row["utc_timestamp"]) for row in rows})
        with self.analytics:
            for bucket in affected:
                for table in ("agg_server_minute", "agg_player_minute", "agg_network_minute", "agg_weapon_minute", "agg_map_minute", "agg_pair_minute", "agg_scene_minute"):
                    self.analytics.execute(f"DELETE FROM {table} WHERE minute_start=?", (bucket,))
        for start in range(0, len(rows), self.batch_size):
            self._process_rows(rows[start:start + self.batch_size], "reconcile")
        return len(rows)

    def _process_rows(self, rows: list[sqlite3.Row], consumer: str) -> None:
        now = utc_now()
        previous_checkpoint = self._checkpoint(consumer)
        try:
            sequence_state: dict[str, int] = json.loads(previous_checkpoint["last_sequences_json"]) if previous_checkpoint else {}
        except (TypeError, json.JSONDecodeError):
            sequence_state = {}
        self._pending_json = {}
        self._pending_network = {}
        raw, sqlite, collector_health, storage_refreshed = self._storage_state()
        try:
            with self.analytics:
                for row in rows:
                    data = self._data(row)
                    if row["event_type"] in {"state_sample", "highres_state_sample", "scene_frame_batch", "scene_highres_batch"}:
                        self.scene_ring.append({"event_id": row["event_id"], "utc_timestamp": row["utc_timestamp"], "data": data})
                    sequence_state[row["server_session_id"]] = max(sequence_state.get(row["server_session_id"], 0), int(row["sequence"]))
                    self._aggregate(row, data, now)
                    if consumer == "live_analyzer":
                        self._promote_episode(row, data, now)
                        self._candidate(row, data, now)
                if consumer == "live_analyzer":
                    self._queue_chat_windows(rows, now)
                self._flush_aggregates(now)
                last_id = int(rows[-1]["event_id"])
                self.analytics.execute(
                    """INSERT INTO processing_checkpoints(consumer_name,last_event_id,last_sequences_json,processed_at,algorithm_version)
                       VALUES(?,?,?,?,?) ON CONFLICT(consumer_name) DO UPDATE SET last_event_id=excluded.last_event_id,
                       last_sequences_json=excluded.last_sequences_json,processed_at=excluded.processed_at,
                       algorithm_version=excluded.algorithm_version""",
                    (consumer, last_id, _json(sequence_state), now, self.config.live_algorithm_version),
                )
                if storage_refreshed:
                    record_storage_health(self.analytics, "raw", raw["used_bytes"], raw["max_bytes"], high=self.config.raw_high_watermark, critical=self.config.raw_critical_watermark, details={"collector": collector_health})
                    source_raw = next((item for item in collector_health if item.get("component") == "raw"), {})
                    self.analytics.execute(
                        """UPDATE storage_health SET dropped_count=?,malformed_count=?,gap_count=?,details_json=?
                           WHERE component='raw'""",
                        (int(source_raw.get("dropped_count", 0)), int(source_raw.get("malformed_count", 0)), int(source_raw.get("gap_count", 0)), _json({"collector": collector_health})),
                    )
                    record_storage_health(self.analytics, "sqlite", sqlite["used_bytes"], sqlite["max_bytes"], high=self.config.sqlite_high_watermark, critical=self.config.sqlite_chunk_watermark)
        except Exception:
            self.analytics.rollback()
            raise
        if consumer == "live_analyzer" and self._llm_worker is not None:
            self._llm_wakeup.set()

    def _storage_state(self) -> tuple[dict, dict, list[dict], bool]:
        now = time.monotonic()
        if self._storage_cache is not None and now - self._storage_cache_at < self._storage_check_interval:
            raw, sqlite, health = self._storage_cache
            return raw, sqlite, health, False
        raw = raw_quota(Path(self.config.report_directory).parent / "raw", None, self.config.raw_archive_max_bytes, self.config.raw_high_watermark, self.config.raw_critical_watermark)
        sqlite = sqlite_quota([self.config.telemetry_database, self.config.analytics_database], self.config.sqlite_max_bytes, self.config.sqlite_high_watermark, self.config.sqlite_chunk_watermark)
        health = [dict(item) for item in self.telemetry.execute("SELECT * FROM storage_health").fetchall()]
        self._storage_cache = (raw, sqlite, health)
        self._storage_cache_at = now
        return raw, sqlite, health, True

    @staticmethod
    def _data(row: sqlite3.Row) -> dict:
        try:
            value = json.loads(row["data_json"] or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _aggregate(self, row: sqlite3.Row, data: dict, now: str) -> None:
        event_type = row["event_type"]
        bucket = minute_bucket(row["utc_timestamp"])
        metrics: dict[str, float] = {"events": 1, event_type: 1}
        if event_type in {"player_damage", "object_damage", "object_damage_batch"}:
            metrics["damage"] = _number(data.get("damage"))
        if event_type in {"player_death", "player_kill"}:
            metrics["deaths" if event_type == "player_death" else "kills"] = 1
        if event_type in {"input_action", "action", "key_input", "key_input_batch"}:
            metrics["actions"] = 1
        if event_type.startswith("input") or event_type.startswith("key_input"):
            metrics["input_events"] = 1
        if event_type in {"state_sample", "highres_state_sample"} and row["player_session_id"]:
            x, y = data.get("x"), data.get("y")
            vx, vy = data.get("vx", data.get("velocity_x")), data.get("vy", data.get("velocity_y"))
            game_ms = _number(row["game_ms"], 0)
            if all(isinstance(value, (int, float)) for value in (x, y, vx, vy)):
                previous = self.previous_state.get(row["player_session_id"])
                if previous:
                    dt = max((game_ms - previous[4]) / 1000, .001)
                    metrics["movement_distance"] = ((x - previous[0]) ** 2 + (y - previous[1]) ** 2) ** .5
                    metrics["movement_intensity"] = (vx * vx + vy * vy) ** .5
                    metrics["acceleration"] = (((vx - previous[2]) ** 2 + (vy - previous[3]) ** 2) ** .5) / dt
                self.previous_state[row["player_session_id"]] = (float(x), float(y), float(vx), float(vy), game_ms)
            metrics["nearest_players"] = len(data.get("nearest_players", [])) if isinstance(data.get("nearest_players"), list) else 0
            metrics["nearest_objects"] = len(data.get("nearest_objects", [])) if isinstance(data.get("nearest_objects"), list) else 0
        self._merge_json("agg_server_minute", (bucket, row["server_session_id"]), metrics, now)
        if row["player_session_id"]:
            self._merge_json("agg_player_minute", (bucket, row["player_session_id"]), metrics, now)
        ping = data.get("ping_ms")
        if event_type == "network_sample" and isinstance(ping, (int, float)):
            self._merge_network(bucket, row["player_session_id"] or "unknown", float(ping), now)
        weapon = data.get("weapon")
        if not weapon and isinstance(data.get("attacker"), dict):
            weapon = data["attacker"].get("weapon")
        if not weapon and isinstance(data.get("projectile"), dict):
            weapon = data["projectile"].get("projectile")
        if weapon and event_type in CRITICAL_TRIGGERS | {"weapon_fire", "melee_action", "projectile_hit"}:
            self._merge_json("agg_weapon_minute", (bucket, row["server_session_id"], str(weapon)), metrics, now)
        map_name = data.get("map_name") or "unknown"
        if event_type in {"round_start", "round_end"} or data.get("map_name"):
            self._merge_json("agg_map_minute", (bucket, row["server_session_id"], str(map_name)), metrics, now)
        attacker, victim = data.get("attacker_session_id"), data.get("victim_session_id")
        if attacker and victim and attacker != victim:
            first, second = sorted((str(attacker), str(victim)))
            self._merge_json("agg_pair_minute", (bucket, first, second), metrics, now)
        if row["round_id"] and (event_type.startswith("scene_") or event_type.startswith("object_") or event_type in {"projectile_hit", "explosion_hit"}):
            entities = data.get("entities", [])
            if isinstance(entities, list):
                scene_context = (row["server_session_id"], row["round_id"])
                if scene_context != self._scene_context:
                    self.previous_objects.clear()
                    self._scene_context = scene_context
                if len(self.previous_objects) >= self._scene_state_cache_limit:
                    self.previous_objects.clear()
                speed_sum = acceleration_sum = 0.0
                samples = 0
                for entity in entities:
                    if not isinstance(entity, dict) or not all(isinstance(entity.get(key), (int, float)) for key in ("x", "y")):
                        continue
                    vx, vy = _number(entity.get("vx")), _number(entity.get("vy"))
                    key = (row["server_session_id"], row["round_id"], entity.get("kind", "object"), entity.get("object_id", entity.get("instance_id")))
                    current = (float(entity["x"]), float(entity["y"]), vx, vy, _number(entity.get("game_ms"), _number(row["game_ms"])))
                    previous = self.previous_objects.get(key)
                    speed_sum += (vx * vx + vy * vy) ** .5
                    if previous:
                        dt = max((current[4] - previous[4]) / 1000, .001)
                        acceleration_sum += (((vx - previous[2]) ** 2 + (vy - previous[3]) ** 2) ** .5) / dt
                    self.previous_objects[key] = current
                    samples += 1
                if samples:
                    metrics["object_samples"] = samples
                    metrics["object_speed_sum"] = speed_sum
                    metrics["object_acceleration_sum"] = acceleration_sum
            self._merge_json("agg_scene_minute", (bucket, row["round_id"]), metrics, now)

    def _merge_json(self, table: str, keys: tuple, metrics: dict, now: str) -> None:
        merged = self._pending_json.setdefault((table, keys), {})
        for name, value in metrics.items():
            if name.endswith("_min"):
                merged[name] = min(merged.get(name, value), value)
            elif name.endswith("_max"):
                merged[name] = max(merged.get(name, value), value)
            else:
                merged[name] = merged.get(name, 0) + value

    def _flush_aggregates(self, now: str) -> None:
        for (table, keys), metrics in self._pending_json.items():
            key_names = AGGREGATE_KEYS[table]
            existing = self.analytics.execute(f"SELECT metrics_json FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in key_names), keys).fetchone()
            merged = json.loads(existing[0]) if existing else {}
            for name, value in metrics.items():
                if name.endswith("_min"):
                    merged[name] = min(merged.get(name, value), value)
                elif name.endswith("_max"):
                    merged[name] = max(merged.get(name, value), value)
                else:
                    merged[name] = merged.get(name, 0) + value
            columns = ",".join((*key_names, "metrics_json", "updated_at"))
            placeholders = ",".join("?" for _ in (*keys, "metrics_json", "updated_at"))
            self.analytics.execute(
                f"INSERT INTO {table}({columns}) VALUES({placeholders}) ON CONFLICT DO UPDATE SET metrics_json=excluded.metrics_json,updated_at=excluded.updated_at",
                (*keys, _json(merged), now),
            )
        for (bucket, player), pending in self._pending_network.items():
            row = self.analytics.execute("SELECT * FROM agg_network_minute WHERE minute_start=? AND player_session_id=?", (bucket, player)).fetchone()
            histogram = json.loads(row["histogram_json"]) if row else {}
            for band, count in pending["histogram"].items():
                histogram[band] = histogram.get(band, 0) + count
            if row:
                self.analytics.execute(
                    """UPDATE agg_network_minute SET ping_count=?,ping_sum=?,ping_min=?,ping_max=?,ping_sum_sq=?,histogram_json=?,updated_at=?
                       WHERE minute_start=? AND player_session_id=?""",
                    (row["ping_count"] + pending["count"], row["ping_sum"] + pending["sum"], min(row["ping_min"], pending["min"]) if row["ping_min"] is not None else pending["min"], max(row["ping_max"], pending["max"]) if row["ping_max"] is not None else pending["max"], row["ping_sum_sq"] + pending["sum_sq"], _json(histogram), now, bucket, player),
                )
            else:
                self.analytics.execute(
                    """INSERT INTO agg_network_minute(minute_start,player_session_id,ping_count,ping_sum,ping_min,ping_max,ping_sum_sq,histogram_json,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (bucket, player, pending["count"], pending["sum"], pending["min"], pending["max"], pending["sum_sq"], _json(histogram), now),
                )
        self._pending_json.clear()
        self._pending_network.clear()

    def _merge_network(self, bucket: str, player: str, ping: float, now: str) -> None:
        pending = self._pending_network.setdefault((bucket, player), {"count": 0, "sum": 0.0, "min": ping, "max": ping, "sum_sq": 0.0, "histogram": {}})
        pending["count"] += 1
        pending["sum"] += ping
        pending["min"] = min(pending["min"], ping)
        pending["max"] = max(pending["max"], ping)
        pending["sum_sq"] += ping * ping
        band = str(int(ping // 10) * 10)
        pending["histogram"][band] = pending["histogram"].get(band, 0) + 1

    def _promote_episode(self, row: sqlite3.Row, data: dict, now: str) -> None:
        event_type = row["event_type"]
        ordinary = event_type in {"scene_highres_batch", "highres_state_sample"}
        if event_type not in CRITICAL_TRIGGERS and not ordinary:
            return
        if ordinary and (int(row["event_id"]) * 2654435761) % 100 >= 1:
            return
        _, quota, _, _ = self._storage_state()
        if ordinary and quota["watermark"] >= self.config.sqlite_episode_watermark:
            return
        if not ordinary and row["game_ms"] is not None:
            nearby = self.analytics.execute(
                """SELECT episode_id FROM episode_catalog WHERE server_session_id=? AND round_id IS ?
                   AND trigger_game_ms IS NOT NULL AND ABS(trigger_game_ms-?)<1000 LIMIT 1""",
                (row["server_session_id"], row["round_id"], row["game_ms"]),
            ).fetchone()
            if nearby:
                return
        reason = "critical_event" if not ordinary else "deterministic_one_percent_sample"
        coverage = _number(data.get("coverage"), 1.0 if event_type in CRITICAL_TRIGGERS else 0.0)
        cursor = self.analytics.execute(
            """INSERT OR IGNORE INTO episode_catalog(source_event_id,server_session_id,round_id,trigger,trigger_game_ms,
               status,selection_reason,coverage,source_window_id,created_at,closed_at)
               VALUES(?,?,?,?,?,'selected',?,?,?,?,?)""",
            (row["event_id"], row["server_session_id"], row["round_id"], event_type, row["game_ms"], reason,
             coverage, str(row["event_id"]), now, now if event_type == "scene_window_complete" else None),
        )
        if cursor.rowcount:
            episode = self.analytics.execute("SELECT episode_id FROM episode_catalog WHERE source_event_id=?", (row["event_id"],)).fetchone()
            self.analytics.execute("INSERT OR REPLACE INTO episode_features(episode_id,features_json,extracted_at) VALUES(?,?,?)", (episode[0], _json({"event_type": event_type, "coverage": coverage, "feature_source": "live", "ring_frame_count": len(self.scene_ring), "window_before_ms": 5000, "window_after_ms": 5000}), now))

    def _candidate(self, row: sqlite3.Row, data: dict, now: str) -> None:
        if row["event_type"] not in CRITICAL_TRIGGERS:
            return
        signature = f"event:{row['event_type']}"
        family = "combat" if "damage" in row["event_type"] or "death" in row["event_type"] else "scene"
        self.analytics.execute(
            """INSERT INTO pattern_candidates(signature,pattern_family,state,confidence,supported_by_direct_event,evidence_event_ids_json,features_json,occurrences,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(signature) DO UPDATE SET occurrences=pattern_candidates.occurrences+1,
               confidence=MAX(pattern_candidates.confidence,excluded.confidence),supported_by_direct_event=MAX(pattern_candidates.supported_by_direct_event,excluded.supported_by_direct_event),
               last_seen_at=excluded.last_seen_at,evidence_event_ids_json=excluded.evidence_event_ids_json""",
            (signature, family, "candidate", 1.0 if row["event_type"] in {"player_death", "player_kill", "scene_window_complete"} else .5,
             int(row["event_type"] in CRITICAL_TRIGGERS), _json([row["event_id"]]), _json({"event_type": row["event_type"], "data_keys": sorted(data)[:20]}), 1, now, now),
        )

    def _queue_chat_windows(self, rows: list[sqlite3.Row], now: str) -> None:
        if not getattr(self.config, "live_llm_enabled", False):
            return
        messages: defaultdict[int, list[dict]] = defaultdict(list)
        window_seconds = max(60, int(getattr(self.config, "live_llm_window_seconds", 300)))
        for row in rows:
            if row["event_type"] != "chat_message":
                continue
            data = self._data(row)
            if not isinstance(data.get("message"), str):
                continue
            try:
                epoch = int(datetime.fromisoformat(row["utc_timestamp"].replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
            messages[epoch // window_seconds].append({"event_id": row["event_id"], "player_session_id": row["player_session_id"], "text": data["message"][:4000]})
        if not self.config.openai_enabled:
            return
        for window, values in messages.items():
            values = values[:max(1, int(getattr(self.config, "live_llm_max_messages_per_window", 20)))]
            min_messages = max(1, int(getattr(self.config, "live_llm_min_messages", 2)))
            distinct_actors = {item.get("player_session_id") for item in values}
            high_signal = len(values) >= min_messages or len(distinct_actors) >= 2 or any(len(item.get("text") or "") >= 32 for item in values)
            if not high_signal:
                continue
            source_id = f"chat:{window_seconds}:{window}"
            logical_key = f"job|chat|{source_id}|moderation|{getattr(self.config, 'llm_analysis_version', 'llm-v1')}"
            stored_job_kind = _versioned_storage_name("moderation", self.config)
            claimed = self.analytics.execute(
                "INSERT OR IGNORE INTO llm_logical_keys(logical_key,source_type,source_id,kind,analysis_version,job_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (logical_key, "chat", source_id, "moderation", getattr(self.config, "llm_analysis_version", "llm-v1"), str(uuid.uuid5(uuid.NAMESPACE_URL, logical_key)), now),
            )
            if not claimed.rowcount:
                continue
            self.analytics.execute(
                """INSERT OR IGNORE INTO llm_jobs(job_id,source_type,source_id,job_kind,model,status,payload_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,'queued',?,?,?)""",
                (str(uuid.uuid5(uuid.NAMESPACE_URL, logical_key)), "chat", source_id, stored_job_kind, getattr(self.config, "live_llm_model", self.config.openai_model), _json({"messages": values}), now, now),
            )

    def _record_drop(self, count: int) -> None:
        with self.analytics:
            record_storage_health(self.analytics, "live_queue", 0, 1, dropped=count, details={"reason": "bounded_queue"})

    def _queue_raw_segments(self, rows: list[sqlite3.Row]) -> None:
        if rows:
            with self._raw_lock:
                for row in rows:
                    server = row["server_session_id"]
                    self._raw_segment_sequences[server] = max(self._raw_segment_sequences.get(server, 0), int(row["sequence"]))
        self._raw_wakeup.set()

    def _mark_raw_segments(self, rows: list[sqlite3.Row] | None = None, force: bool = False) -> None:
        """Acknowledge closed raw segments off the live ingestion transaction."""
        if rows:
            self._queue_raw_segments(rows)
        now = time.monotonic()
        if not force and now < self._raw_retry_at:
            return
        with self._raw_lock:
            if not self._raw_segment_sequences:
                return
            if not force and now - self._last_raw_mark_at < self._raw_mark_interval:
                return
            pending = dict(self._raw_segment_sequences)

        read_uri = Path(self.config.telemetry_database).resolve().as_uri() + "?mode=ro"
        readable = sqlite3.connect(read_uri, uri=True, timeout=self._raw_busy_timeout_ms / 1000)
        try:
            matches = {}
            for server, sequence in pending.items():
                if server is None:
                    continue
                row = readable.execute(
                    """SELECT 1 FROM raw_segments WHERE server_session_id=? AND first_sequence IS NOT NULL
                       AND last_sequence IS NOT NULL AND last_sequence<=? AND compression_status='gzip'
                       AND processing_status='available' LIMIT 1""", (server, sequence)
                ).fetchone()
                if row:
                    matches[server] = sequence
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            self._raw_retry_at = now + self._raw_retry_delay
            self._raw_retry_delay = min(5.0, self._raw_retry_delay * 2)
            return
        finally:
            readable.close()

        if not matches:
            with self._raw_lock:
                for server in pending:
                    if server is None:
                        self._raw_segment_sequences.pop(server, None)
            self._last_raw_mark_at = now
            self._raw_retry_at = now + self._raw_mark_interval
            return

        connection = sqlite3.connect(self.config.telemetry_database, timeout=self._raw_busy_timeout_ms / 1000)
        try:
            connection.execute(f"PRAGMA busy_timeout={self._raw_busy_timeout_ms}")
            with connection:
                for server, sequence in matches.items():
                    connection.execute(
                        """UPDATE raw_segments SET processing_status='processed' WHERE server_session_id=?
                           AND first_sequence IS NOT NULL AND last_sequence<=? AND compression_status='gzip'
                           AND processing_status='available'""", (server, sequence)
                    )
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            self._raw_retry_at = now + self._raw_retry_delay
            self._raw_retry_delay = min(5.0, self._raw_retry_delay * 2)
            return
        finally:
            connection.close()

        with self._raw_lock:
            for server in pending:
                if server is None or server in matches:
                    self._raw_segment_sequences.pop(server, None)
        self._last_raw_mark_at = now
        self._raw_retry_at = 0.0
        self._raw_retry_delay = 0.25

    def _maybe_dashboard(self) -> None:
        now = time.monotonic()
        if now - self.last_dashboard_at < max(1, int(self.config.live_dashboard_interval_seconds)):
            return
        build_live_dashboard(self.config, self.analytics)
        self.last_dashboard_at = now

    def _maybe_microbatch(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self.last_microbatch_at < max(1, int(self.config.live_microbatch_seconds)):
            return
        now = utc_now()
        with self.analytics:
            self.analytics.execute("UPDATE episode_catalog SET closed_at=COALESCE(closed_at,?) WHERE status='selected' AND created_at<?", (now, now))
            self.analytics.execute("UPDATE pattern_candidates SET state='confirmed' WHERE occurrences>=?", (max(2, int(self.config.pattern_confirmation_occurrences)),))
        self.last_microbatch_at = now_monotonic


def run_maintenance(config, root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parents[1]
    analytics = open_analytics(config.analytics_database, root / "analyzer" / "schema.sql")
    result = {}
    try:
        _apply_telemetry_retention(config, analytics)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=config.minute_aggregate_retention_days)).isoformat().replace("+00:00", "Z")
        with analytics:
            for table in ("agg_server_minute", "agg_player_minute", "agg_network_minute", "agg_weapon_minute", "agg_map_minute", "agg_pair_minute", "agg_scene_minute"):
                analytics.execute(f"DELETE FROM {table} WHERE minute_start<?", (cutoff,))
            episode_cutoff = (datetime.now(timezone.utc) - timedelta(days=config.selected_episode_retention_days)).isoformat().replace("+00:00", "Z")
            analytics.execute("DELETE FROM episode_catalog WHERE created_at<? AND selection_reason!='critical_event'", (episode_cutoff,))
            raw = raw_quota(Path(config.report_directory).parent / "raw", None, config.raw_archive_max_bytes, config.raw_high_watermark, config.raw_critical_watermark)
            sqlite = sqlite_quota([config.telemetry_database, config.analytics_database], config.sqlite_max_bytes, config.sqlite_high_watermark, config.sqlite_chunk_watermark)
            raw_directory = Path(config.report_directory).parent / "raw"
            has_unbounded_segment = any(
                path.is_file() and (
                    path.suffix == ".jsonl" or
                    (path.name.endswith(".jsonl.gz") and path.stat().st_size > config.raw_segment_max_bytes)
                )
                for path in raw_directory.rglob("*")
            ) if raw_directory.exists() else False
            if raw["watermark"] >= config.raw_high_watermark or has_unbounded_segment:
                raw = _maintain_raw(config)
            if sqlite["watermark"] >= config.sqlite_cleanup_watermark:
                _clean_transient_aggregates(analytics)
                sqlite = sqlite_quota([config.telemetry_database, config.analytics_database], config.sqlite_max_bytes, config.sqlite_high_watermark, config.sqlite_chunk_watermark)
            if sqlite["watermark"] >= config.sqlite_episode_watermark:
                _drop_low_priority_episodes(analytics, config.telemetry_database)
                sqlite = sqlite_quota([config.telemetry_database, config.analytics_database], config.sqlite_max_bytes, config.sqlite_high_watermark, config.sqlite_chunk_watermark)
            chunk_usage = _episode_chunk_bytes(config.telemetry_database)
            if chunk_usage > config.episode_chunk_max_bytes:
                _enforce_episode_chunk_quota(analytics, config.telemetry_database, config.episode_chunk_max_bytes)
                chunk_usage = _episode_chunk_bytes(config.telemetry_database)
            _enforce_llm_queue_quota(analytics, config.llm_queue_max_bytes)
            record_storage_health(analytics, "raw", raw["used_bytes"], raw["max_bytes"], high=config.raw_high_watermark, critical=config.raw_critical_watermark)
            record_storage_health(analytics, "sqlite", sqlite["used_bytes"], sqlite["max_bytes"], high=config.sqlite_cleanup_watermark, critical=config.sqlite_chunk_watermark)
            result = {"raw": raw, "sqlite": sqlite, "episode_chunk_bytes": chunk_usage}
    finally:
        analytics.close()
    return result


def _maintain_raw(config) -> dict:
    raw_directory = Path(config.report_directory).parent / "raw"
    raw_directory.mkdir(parents=True, exist_ok=True)
    telemetry = sqlite3.connect(config.telemetry_database, timeout=2)
    try:
        telemetry.execute("PRAGMA busy_timeout=2000")
        for path in raw_directory.rglob("*.jsonl"):
            target = Path(str(path) + ".gz")
            if target.exists():
                suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
                target = path.with_name(f"{path.stem}-closed-{suffix}.jsonl.gz")
            with path.open("rb") as source, gzip.open(target, "wb", compresslevel=max(1, min(9, int(config.raw_gzip_level)))) as destination:
                shutil.copyfileobj(source, destination)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            telemetry.execute("UPDATE raw_segments SET path=?,size_bytes=?,sha256=?,compression_status='gzip',closed_at=? WHERE path=?", (str(target), target.stat().st_size, checksum, utc_now(), str(path)))
            path.unlink()
        for path in list(raw_directory.rglob("*.jsonl.gz")):
            if path.stat().st_size > config.raw_segment_max_bytes:
                _split_large_gzip_segment(path, config.raw_segment_max_bytes, int(config.raw_gzip_level), telemetry)
        telemetry.commit()
        used = sum(path.stat().st_size for path in raw_directory.rglob("*") if path.is_file())
        if used > config.raw_archive_max_bytes:
            rows = telemetry.execute("SELECT raw_segment_id,path FROM raw_segments WHERE processing_status='processed' ORDER BY retention_priority DESC,created_at").fetchall()
            for row in rows:
                if used <= config.raw_archive_max_bytes:
                    break
                path = Path(row[1])
                size = path.stat().st_size if path.exists() else 0
                path.unlink(missing_ok=True)
                telemetry.execute("DELETE FROM raw_segments WHERE raw_segment_id=?", (row[0],))
                used -= size
            telemetry.commit()
        return raw_quota(raw_directory, None, config.raw_archive_max_bytes, config.raw_high_watermark, config.raw_critical_watermark)
    finally:
        telemetry.close()


def _split_large_gzip_segment(path: Path, maximum: int, gzip_level: int, telemetry: sqlite3.Connection) -> None:
    """Convert a pre-segmentation archive into bounded gzip members without losing lines."""
    targets: list[Path] = []
    part = 1
    handle = None
    raw_written = 0
    try:
        with gzip.open(path, "rb") as source:
            for line in source:
                if handle is None or raw_written + len(line) > maximum:
                    if handle is not None:
                        handle.close()
                    target = path.with_name(f"{path.stem.replace('.jsonl', '')}-part-{part:06d}.jsonl.gz")
                    handle = gzip.open(target, "wb", compresslevel=max(1, min(9, gzip_level)))
                    targets.append(target)
                    part += 1
                    raw_written = 0
                handle.write(line)
                raw_written += len(line)
        if handle is not None:
            handle.close()
        for target in targets:
            telemetry.execute(
                """INSERT OR IGNORE INTO raw_segments(path,size_bytes,compression_status,processing_status,retention_priority,created_at)
                   VALUES(?,?, 'gzip','available',5,?)""", (str(target), target.stat().st_size, utc_now())
            )
        path.unlink()
    except Exception:
        if handle is not None:
            handle.close()
        for target in targets:
            target.unlink(missing_ok=True)
        raise


def _apply_telemetry_retention(config, analytics: sqlite3.Connection) -> None:
    """Drop only derived transient rows; event envelopes and critical lifecycle stay intact."""
    connection = sqlite3.connect(config.telemetry_database, timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        now = datetime.now(timezone.utc)
        highres_cutoff = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        baseline_cutoff = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        network_cutoff = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        chat_cutoff = (now - timedelta(days=config.chat_annotation_retention_days)).isoformat().replace("+00:00", "Z")
        selected = [row[0] for row in analytics.execute("SELECT source_event_id FROM episode_catalog WHERE selection_reason IN ('critical_event','deterministic_one_percent_sample')").fetchall()]
        selected_sql = ",".join("?" for _ in selected) or "NULL"
        connection.execute("DELETE FROM network_samples WHERE utc_timestamp<?", (network_cutoff,))
        connection.execute("DELETE FROM chat_messages WHERE utc_timestamp<?", (chat_cutoff,))
        connection.execute("DELETE FROM state_samples WHERE utc_timestamp<? AND event_id IN (SELECT event_id FROM events WHERE event_type='state_sample')", (baseline_cutoff,))
        connection.execute(f"DELETE FROM scene_samples WHERE event_id IN (SELECT event_id FROM events WHERE utc_timestamp<?) AND event_id NOT IN ({selected_sql})", (highres_cutoff, *selected))
        connection.execute(f"DELETE FROM scene_chunks WHERE event_id IN (SELECT event_id FROM events WHERE utc_timestamp<?) AND event_id NOT IN ({selected_sql})", (highres_cutoff, *selected))
        connection.execute(f"DELETE FROM events WHERE event_type IN ('highres_state_sample','scene_highres_batch','scene_frame_batch') AND utc_timestamp<? AND event_id NOT IN ({selected_sql})", (highres_cutoff, *selected))
        connection.commit()
    finally:
        connection.close()


def _clean_transient_aggregates(analytics: sqlite3.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    for table in ("agg_server_minute", "agg_player_minute", "agg_network_minute", "agg_weapon_minute", "agg_map_minute", "agg_pair_minute", "agg_scene_minute"):
        analytics.execute(f"DELETE FROM {table} WHERE minute_start<?", (cutoff,))


def _drop_low_priority_episodes(analytics: sqlite3.Connection, telemetry_path: str) -> None:
    rows = analytics.execute("SELECT source_event_id FROM episode_catalog WHERE selection_reason='deterministic_one_percent_sample' ORDER BY created_at LIMIT 1000").fetchall()
    if not rows:
        return
    event_ids = [row[0] for row in rows]
    placeholders = ",".join("?" for _ in event_ids)
    telemetry = sqlite3.connect(telemetry_path, timeout=2)
    try:
        telemetry.execute("PRAGMA busy_timeout=2000")
        telemetry.execute(f"DELETE FROM scene_chunks WHERE event_id IN ({placeholders})", event_ids)
        telemetry.execute(f"DELETE FROM scene_samples WHERE event_id IN ({placeholders})", event_ids)
        telemetry.commit()
    finally:
        telemetry.close()
    analytics.execute(f"DELETE FROM episode_catalog WHERE source_event_id IN ({placeholders})", event_ids)


def _episode_chunk_bytes(telemetry_path: str) -> int:
    connection = sqlite3.connect(telemetry_path, timeout=2)
    try:
        return int(connection.execute("SELECT COALESCE(SUM(compressed_bytes),0) FROM scene_chunks").fetchone()[0])
    finally:
        connection.close()


def _enforce_episode_chunk_quota(analytics: sqlite3.Connection, telemetry_path: str, maximum: int) -> None:
    telemetry = sqlite3.connect(telemetry_path, timeout=2)
    try:
        telemetry.execute("PRAGMA busy_timeout=2000")
        while int(telemetry.execute("SELECT COALESCE(SUM(compressed_bytes),0) FROM scene_chunks").fetchone()[0]) > maximum:
            row = analytics.execute("SELECT source_event_id FROM episode_catalog WHERE selection_reason='deterministic_one_percent_sample' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                break
            event_id = row[0]
            telemetry.execute("DELETE FROM scene_chunks WHERE event_id=?", (event_id,))
            telemetry.execute("DELETE FROM scene_samples WHERE event_id=?", (event_id,))
            analytics.execute("DELETE FROM episode_catalog WHERE source_event_id=?", (event_id,))
        telemetry.commit()
    finally:
        telemetry.close()


def _enforce_llm_queue_quota(analytics: sqlite3.Connection, maximum: int) -> None:
    size = analytics.execute("SELECT COALESCE(SUM(length(payload_json)),0) FROM llm_jobs WHERE status IN ('queued','submitted')").fetchone()[0]
    if size <= maximum:
        return
    analytics.execute("DELETE FROM llm_jobs WHERE status IN ('complete','failed','cancelled') AND job_id IN (SELECT job_id FROM llm_jobs ORDER BY updated_at LIMIT 1000)")
