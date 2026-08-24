from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone


def open_telemetry(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def open_analytics(path: str, schema_path: Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    return conn


def file_bytes(path: Path) -> int:
    """Include SQLite sidecars when accounting for the working quota."""
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except FileNotFoundError:
            pass
    return total


def directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def quota_status(used: int, maximum: int, high: float = .85, critical: float = .95) -> dict:
    ratio = used / maximum if maximum else 0.0
    if ratio >= 1:
        state = "full"
    elif ratio >= critical:
        state = "critical"
    elif ratio >= high:
        state = "high"
    else:
        state = "ok"
    return {"used_bytes": used, "max_bytes": maximum, "watermark": ratio, "state": state}


def record_storage_health(connection: sqlite3.Connection, component: str, used: int, maximum: int, *,
                          high: float = .85, critical: float = .95, dropped: int = 0,
                          malformed: int = 0, gaps: int = 0, details: dict | None = None) -> None:
    status = quota_status(used, maximum, high, critical)
    connection.execute(
        """INSERT INTO storage_health(component,used_bytes,max_bytes,watermark,state,
           dropped_count,malformed_count,gap_count,details_json,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(component) DO UPDATE SET used_bytes=excluded.used_bytes,
           max_bytes=excluded.max_bytes,watermark=excluded.watermark,state=excluded.state,
           dropped_count=storage_health.dropped_count+excluded.dropped_count,
           malformed_count=storage_health.malformed_count+excluded.malformed_count,
           gap_count=storage_health.gap_count+excluded.gap_count,
           details_json=excluded.details_json,updated_at=excluded.updated_at""",
        (component, used, maximum, status["watermark"], status["state"], dropped,
         malformed, gaps, json.dumps(details or {}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )


def sqlite_quota(paths: list[str | Path], maximum: int, high: float = .85, critical: float = .95) -> dict:
    used = sum(file_bytes(Path(path)) for path in paths)
    return quota_status(used, maximum, high, critical)


def raw_quota(raw_directory: str | Path, malformed_path: str | Path | None, maximum: int,
              high: float = .85, critical: float = .95) -> dict:
    used = directory_bytes(Path(raw_directory))
    if malformed_path:
        used += file_bytes(Path(malformed_path))
    return quota_status(used, maximum, high, critical)


def prune_dashboard_cache(path: str | Path, maximum: int, keep: int = 50) -> int:
    """Apply a recoverable LRU-like policy to generated episode HTML."""
    root = Path(path)
    files = sorted((item for item in root.glob("*.html") if item.is_file()), key=lambda item: item.stat().st_mtime)
    removed = 0
    while len(files) > keep or directory_bytes(root) > maximum:
        if not files:
            break
        files.pop(0).unlink(missing_ok=True)
        removed += 1
    return removed
