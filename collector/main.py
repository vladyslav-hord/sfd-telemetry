from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from collector.db import TelemetryDB
    from collector.parser import parse_shared_storage, unique_events
else:
    from .db import TelemetryDB
    from .parser import parse_shared_storage, unique_events

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "collector" / "config.example.json"


@contextmanager
def collector_lock(path: Path):
    """Allow only one collector writer per telemetry database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another collector process is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another collector process is already running") from exc
        locked = True
        try:
            handle.seek(0)
            handle.write("0")
            handle.flush()
        except OSError as exc:
            locked = False
            raise RuntimeError("Another collector process is already running") from exc
        yield
    finally:
        if locked and os.name == "nt":
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        elif locked:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            handle.close()
        except OSError:
            pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_config(path: Path | None) -> dict[str, Any]:
    source = path if path and path.exists() else DEFAULT_CONFIG
    config = json.loads(source.read_text(encoding="utf-8"))
    config["_config_source"] = str(source)
    return config


def resolve_path(value: str) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    return expanded if expanded.is_absolute() else ROOT / expanded


def storage_candidates(config: dict[str, Any]) -> list[Path]:
    configured = config.get("shared_storage_path")
    if configured:
        return [resolve_path(configured)]
    home = Path.home()
    relative = Path("Superfighters Deluxe/Cache/ScriptData/Shared/sfdtelemetry_v1.txt")
    candidates = [home / "Documents" / relative]
    one_drive = os.environ.get("OneDrive")
    if one_drive:
        candidates.append(Path(one_drive) / "Documents" / relative)
    return list(dict.fromkeys(candidates))


class Collector:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.storage_paths = storage_candidates(config)
        self.db = TelemetryDB(
            resolve_path(config["database_path"]), ROOT / "sql" / "schema.sql",
            ROOT / "sql" / "views.sql", int(config.get("busy_timeout_ms", 5000)),
        )
        self.last_stat: dict[Path, tuple[int, int]] = {}
        self.last_sequences = self.db.last_sequences()
        self.stop_requested = False
        self.malformed_path = resolve_path(config.get("malformed_log_path", "data/malformed_events.jsonl"))
        self.malformed_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_enabled = bool(config.get("raw_archive_enabled", True))
        self.archive_dir = resolve_path(config.get("raw_archive_dir", "data/raw"))
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.raw_max_bytes = max(1, int(config.get("raw_archive_max_bytes", 1073741824)))
        self.raw_segment_max_bytes = max(1024, int(config.get("raw_segment_max_bytes", 33554432)))
        self.raw_gzip_level = min(9, max(1, int(config.get("raw_gzip_level", 1))))
        self.raw_high_watermark = float(config.get("raw_high_watermark", .85))
        self.raw_critical_watermark = float(config.get("raw_critical_watermark", .95))

    def close(self) -> None:
        if self.archive_enabled:
            self._close_active_segments()
        self.db.close()

    def request_stop(self, *_: object) -> None:
        self.stop_requested = True

    def discover(self) -> Path | None:
        existing = [path for path in self.storage_paths if path.exists()]
        return max(existing, key=lambda path: path.stat().st_mtime_ns) if existing else None

    def process_once(self, force: bool = False) -> tuple[int, int, int]:
        path = self.discover()
        if path is None:
            return 0, 0, 0
        try:
            stat = path.stat()
            marker = (stat.st_mtime_ns, stat.st_size)
            if not force and self.last_stat.get(path) == marker:
                return 0, 0, 0
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self._write_malformed(f"storage read failed: {exc}")
            return 0, 0, 1

        parsed, malformed = parse_shared_storage(text)
        for message in malformed:
            self._write_malformed(message)
        candidates = unique_events(parsed)
        fresh = []
        per_session: dict[str, list[int]] = {}
        for item in candidates:
            session, sequence = item.envelope["server_session"], item.envelope["seq"]
            if sequence > self.last_sequences.get(session, 0):
                fresh.append(item)
                per_session.setdefault(session, []).append(sequence)
        for session, sequences in per_session.items():
            expected = self.last_sequences.get(session, 0) + 1
            for observed in sorted(sequences):
                if observed > expected:
                    self.db.record_gap(session, expected, observed, utc_now())
                expected = observed + 1
        batch_size = max(1, int(self.config.get("batch_size", 250)))
        inserted = duplicates = 0
        for index in range(0, len(fresh), batch_size):
            batch = fresh[index : index + batch_size]
            added, dupes = self.db.insert_batch(batch)
            inserted += added
            duplicates += dupes
        if fresh:
            for item in fresh:
                session, sequence = item.envelope["server_session"], item.envelope["seq"]
                self.last_sequences[session] = max(sequence, self.last_sequences.get(session, 0))
            if self.archive_enabled:
                self._archive(fresh)
        self.last_stat[path] = marker
        with self.db.connection:
            self.db.connection.execute(
                """INSERT INTO collector_state(source_path,last_mtime_ns,last_size,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(source_path) DO UPDATE SET
                   last_mtime_ns=excluded.last_mtime_ns,last_size=excluded.last_size,updated_at=excluded.updated_at""",
                (str(path), marker[0], marker[1], utc_now()),
            )
        return inserted, duplicates, len(malformed)

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)
        interval = max(0.1, float(self.config.get("poll_interval_seconds", 0.5)))
        print(f"collector started; config={self.config['_config_source']}")
        print("storage candidates=" + "; ".join(map(str, self.storage_paths)))
        while not self.stop_requested:
            inserted, duplicates, malformed = self.process_once()
            if inserted or malformed:
                print(f"{utc_now()} inserted={inserted} duplicates={duplicates} malformed={malformed}")
            self.stop_requested or time.sleep(interval)
        self.process_once(force=True)
        print("collector stopped cleanly")
        return 0

    def _write_malformed(self, message: str) -> None:
        if self._raw_usage() >= self.raw_max_bytes:
            self._update_raw_health(malformed=1, dropped=1, state="full")
            return
        with self.malformed_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"ts": utc_now(), "error": message}, ensure_ascii=False) + "\n")
        self._update_raw_health(malformed=1)

    def _archive(self, items: list[Any]) -> None:
        if not items or not self.archive_enabled:
            return
        for item in items:
            raw = (item.raw_line + "\n").encode("utf-8")
            if len(raw) > self.raw_segment_max_bytes:
                self._update_raw_health(dropped=1, state="degraded")
                continue
            event = item.envelope
            path = self._active_path()
            segment = self.db.connection.execute(
                "SELECT server_session_id FROM raw_segments WHERE path=? AND compression_status='active'",
                (str(path),),
            ).fetchone()
            if segment and segment["server_session_id"] != event.get("server_session"):
                self._close_segment(path)
                path = self._active_path()
            size = path.stat().st_size if path.exists() else 0
            if size and size + len(raw) > self.raw_segment_max_bytes:
                self._close_segment(path)
                path = self._active_path()
                size = 0
            if self._raw_usage() + len(raw) > self.raw_max_bytes:
                self._evict_processed_segments()
            if self._raw_usage() + len(raw) > self.raw_max_bytes:
                self._update_raw_health(dropped=1, state="full")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                handle.write(raw)
            self.db.connection.execute(
                """INSERT INTO raw_segments(path,size_bytes,server_session_id,first_sequence,last_sequence,
                   compression_status,processing_status,retention_priority,created_at)
                   VALUES(?,?,?,?,?,'active','available',?,?)
                   ON CONFLICT(path) DO UPDATE SET size_bytes=excluded.size_bytes,
                   server_session_id=CASE WHEN raw_segments.server_session_id=excluded.server_session_id THEN raw_segments.server_session_id ELSE NULL END,
                   first_sequence=MIN(raw_segments.first_sequence,excluded.first_sequence),
                   last_sequence=MAX(raw_segments.last_sequence,excluded.last_sequence)""",
                (str(path), path.stat().st_size, event.get("server_session"), event.get("seq"), event.get("seq"),
                 self._raw_priority(event.get("type")), utc_now()),
            )
        self.db.connection.commit()
        self._update_raw_health()
        if self._raw_usage() >= self.raw_high_watermark * self.raw_max_bytes:
            self._close_active_segments()
            self._evict_processed_segments()
            self._update_raw_health()

    def _active_path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        directory = self.archive_dir / day
        existing = sorted(directory.glob("telemetry-*.jsonl")) if directory.exists() else []
        if existing:
            return existing[-1]
        known = sorted(directory.glob("telemetry-*.jsonl*")) if directory.exists() else []
        number = 1
        for path in known:
            token = path.name.removeprefix("telemetry-").split(".", 1)[0]
            if token.isdigit():
                number = max(number, int(token) + 1)
        return directory / f"telemetry-{number:06d}.jsonl"

    def _close_active_segments(self) -> None:
        for path in self.archive_dir.rglob("*.jsonl"):
            self._close_segment(path)

    def _close_segment(self, path: Path) -> None:
        if not path.exists() or path.stat().st_size == 0:
            return
        target = Path(str(path) + ".gz")
        if target.exists():
            suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            target = path.with_name(f"{path.stem}-closed-{suffix}.jsonl.gz")
        with path.open("rb") as source, gzip.open(target, "wb", compresslevel=self.raw_gzip_level) as destination:
            shutil.copyfileobj(source, destination)
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        row = self.db.connection.execute("SELECT first_sequence,last_sequence,server_session_id,retention_priority FROM raw_segments WHERE path=?", (str(path),)).fetchone()
        if row:
            self.db.connection.execute(
                """UPDATE raw_segments SET path=?,size_bytes=?,sha256=?,compression_status='gzip',
                   closed_at=?,safe_delete_after=? WHERE path=?""",
                (str(target), target.stat().st_size, checksum, utc_now(), utc_now(), str(path)),
            )
            self.db.connection.commit()
        path.unlink()

    def _raw_usage(self) -> int:
        total = sum(path.stat().st_size for path in self.archive_dir.rglob("*") if path.is_file())
        if self.malformed_path.exists():
            total += self.malformed_path.stat().st_size
        return total

    def _raw_priority(self, event_type: str | None) -> int:
        if event_type in {"script_start", "script_shutdown", "user_join", "user_leave", "round_start", "round_end", "player_death"}:
            return 1
        if event_type in {"scene_window_complete", "object_damage", "object_terminated", "projectile_hit", "explosion_hit"}:
            return 2
        if event_type in {"chat_message", "player_damage", "melee_action"}:
            return 3
        if event_type in {"network_sample", "state_sample", "input_action"}:
            return 4
        return 5

    def _evict_processed_segments(self) -> None:
        rows = self.db.connection.execute(
            "SELECT raw_segment_id,path FROM raw_segments WHERE processing_status='processed' ORDER BY retention_priority DESC,created_at"
        ).fetchall()
        for row in rows:
            if self._raw_usage() < self.raw_critical_watermark * self.raw_max_bytes:
                break
            Path(row[1]).unlink(missing_ok=True)
            self.db.connection.execute("DELETE FROM raw_segments WHERE raw_segment_id=?", (row[0],))
        self.db.connection.commit()

    def _update_raw_health(self, *, dropped: int = 0, malformed: int = 0, state: str | None = None) -> None:
        used = self._raw_usage()
        watermark = used / self.raw_max_bytes
        resolved = state or ("full" if watermark >= 1 else "critical" if watermark >= self.raw_critical_watermark else "high" if watermark >= self.raw_high_watermark else "ok")
        self.db.connection.execute(
            """INSERT INTO storage_health(component,used_bytes,max_bytes,watermark,state,dropped_count,malformed_count,gap_count,updated_at)
               VALUES('raw',?,?,?,?,?,?,(SELECT COUNT(*) FROM telemetry_gaps),?) ON CONFLICT(component) DO UPDATE SET used_bytes=excluded.used_bytes,
               max_bytes=excluded.max_bytes,watermark=excluded.watermark,state=excluded.state,
               dropped_count=storage_health.dropped_count+excluded.dropped_count,
               malformed_count=storage_health.malformed_count+excluded.malformed_count,
               gap_count=(SELECT COUNT(*) FROM telemetry_gaps),updated_at=excluded.updated_at""",
            (used, self.raw_max_bytes, watermark, resolved, dropped, malformed, utc_now()),
        )
        self.db.connection.commit()


def maintenance(config: dict[str, Any], vacuum: bool) -> int:
    db_path = resolve_path(config["database_path"])
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    connection = sqlite3.connect(db_path)
    try:
        now = datetime.now(timezone.utc)
        gameplay_days = int(config.get("gameplay_retention_days", 0))
        chat_days = int(config.get("chat_retention_days", 30))
        scene_days = int(config.get("scene_raw_retention_days", 90))
        with connection:
            if chat_days > 0:
                cutoff = (now - timedelta(days=chat_days)).isoformat().replace("+00:00", "Z")
                connection.execute("DELETE FROM chat_messages WHERE utc_timestamp < ?", (cutoff,))
            if gameplay_days > 0:
                cutoff = (now - timedelta(days=gameplay_days)).isoformat().replace("+00:00", "Z")
                connection.execute("DELETE FROM events WHERE utc_timestamp < ?", (cutoff,))
            if scene_days > 0:
                cutoff = (now - timedelta(days=scene_days)).isoformat().replace("+00:00", "Z")
                preserved = "SELECT source_event_id FROM scene_windows"
                connection.execute(f"DELETE FROM scene_samples WHERE event_id IN (SELECT event_id FROM events WHERE utc_timestamp<? AND event_id NOT IN ({preserved}))", (cutoff,))
                connection.execute(f"DELETE FROM scene_chunks WHERE event_id IN (SELECT event_id FROM events WHERE utc_timestamp<? AND event_id NOT IN ({preserved}))", (cutoff,))
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if vacuum:
            connection.execute("VACUUM")
    finally:
        connection.close()
    raw_dir = resolve_path(config.get("raw_archive_dir", "data/raw"))
    gzip_after = int(config.get("gzip_raw_after_days", 2))
    cutoff_ts = time.time() - gzip_after * 86400
    if raw_dir.exists():
        for path in raw_dir.glob("*.jsonl"):
            if path.stat().st_mtime < cutoff_ts:
                target = path.with_suffix(path.suffix + ".gz")
                with path.open("rb") as source, gzip.open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)
                path.unlink()
    print("maintenance complete")
    return 0


def backup(config: dict[str, Any], destination: Path | None) -> int:
    source = resolve_path(config["database_path"])
    if not source.exists():
        print(f"database not found: {source}", file=sys.stderr)
        return 2
    if destination is None:
        backup_dir = resolve_path(config.get("backup_dir", "data/backups"))
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"sfd-telemetry-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    print(destination)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SFD 1.6 telemetry collector")
    parser.add_argument("--config", type=Path, help="collector JSON config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="follow the SFD shared-storage spool")
    sub.add_parser("once", help="process one storage snapshot")
    maintenance_parser = sub.add_parser("maintenance", help="apply retention and gzip old raw files")
    maintenance_parser.add_argument("--vacuum", action="store_true", help="VACUUM; run only while collector/server are idle")
    backup_parser = sub.add_parser("backup", help="create a consistent SQLite backup")
    backup_parser.add_argument("destination", nargs="?", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "maintenance":
        return maintenance(config, args.vacuum)
    if args.command == "backup":
        return backup(config, args.destination)
    database_path = resolve_path(config["database_path"])
    lock_path = database_path.with_suffix(".collector.lock")
    with collector_lock(lock_path):
        collector = Collector(config)
        try:
            if args.command == "once":
                result = collector.process_once(force=True)
                print(f"inserted={result[0]} duplicates={result[1]} malformed={result[2]}")
                return 0
            return collector.run()
        finally:
            collector.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"collector failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
