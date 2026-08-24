from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PERMANENT_TABLES = {
    "server_sessions", "players", "player_sessions", "player_aliases", "rounds", "round_players",
    "events", "combat_events", "combat_hit_details", "scene_entities", "scene_interactions",
    "scene_windows", "state_windows", "chat_messages", "player_stat_snapshots", "moderation_events",
    "telemetry_gaps",
}


def migrate_telemetry(source_path: str | Path, destination_path: str | Path, schema_path: Path) -> dict:
    """Create a rollback-safe v2 copy without mutating or vacuuming the source."""
    source_path, destination_path = Path(source_path), Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("Migration destination must differ from source")
    if destination_path.exists():
        raise FileExistsError(f"Migration destination already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.execute("PRAGMA busy_timeout=10000")
        destination.execute("PRAGMA busy_timeout=10000")
        source.backup(destination)
        destination.executescript(schema_path.read_text(encoding="utf-8"))
        destination.execute("CREATE TABLE IF NOT EXISTS migration_checks(name TEXT PRIMARY KEY,value TEXT NOT NULL,checked_at TEXT NOT NULL)")
        checks = {}
        for table in sorted(PERMANENT_TABLES & {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}):
            checks[table] = destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        checks["event_max_id"] = destination.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]
        checks["sequence_ranges"] = destination.execute("SELECT COUNT(*) FROM telemetry_gaps").fetchone()[0]
        checked_at = datetime.now(timezone.utc).isoformat()
        destination.executemany("INSERT OR REPLACE INTO migration_checks(name,value,checked_at) VALUES(?,?,?)", [(key, str(value), checked_at) for key, value in checks.items()])
        destination.commit()
        return {"source": str(source_path), "destination": str(destination_path), "checks": checks}
    finally:
        source.close()
        destination.close()
