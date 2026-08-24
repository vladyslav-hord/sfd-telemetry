from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path


def percentile(values: list[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ping_report(connection: sqlite3.Connection, player_session: str | None) -> list[dict]:
    sql = "SELECT player_session_id, utc_timestamp, ping_ms FROM network_samples"
    parameters: tuple[str, ...] = ()
    if player_session:
        sql += " WHERE player_session_id=?"
        parameters = (player_session,)
    sql += " ORDER BY player_session_id, utc_timestamp"
    grouped: dict[str, list[tuple[str, int]]] = {}
    for row in connection.execute(sql, parameters):
        grouped.setdefault(row[0], []).append((row[1], row[2]))
    result = []
    for session, samples in grouped.items():
        values = [item[1] for item in samples]
        deltas = [abs(values[index] - values[index - 1]) for index in range(1, len(values))]
        seconds_above = {threshold: 0.0 for threshold in (100, 150, 200, 250)}
        for index in range(1, len(samples)):
            duration = max(0.0, min(30.0, (parse_timestamp(samples[index][0]) - parse_timestamp(samples[index - 1][0])).total_seconds()))
            for threshold in seconds_above:
                if samples[index - 1][1] > threshold:
                    seconds_above[threshold] += duration
        result.append({
            "player_session_id": session,
            "samples": len(values),
            "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "min": min(values),
            "max": max(values),
            "stddev": statistics.pstdev(values),
            "p50": percentile(values, 0.50),
            "p90": percentile(values, 0.90),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "estimated_jitter": statistics.fmean(deltas) if deltas else 0.0,
            "spike_count_100ms_delta": sum(delta >= 100 for delta in deltas),
            "seconds_above": seconds_above,
        })
    return result


def summary(connection: sqlite3.Connection) -> dict:
    scalar = lambda sql: connection.execute(sql).fetchone()[0]
    return {
        "server_sessions": scalar("SELECT COUNT(*) FROM server_sessions"),
        "player_sessions": scalar("SELECT COUNT(*) FROM player_sessions"),
        "persistent_players": scalar("SELECT COUNT(*) FROM players"),
        "rounds": scalar("SELECT COUNT(*) FROM rounds"),
        "events": scalar("SELECT COUNT(*) FROM events"),
        "combat_events": scalar("SELECT COUNT(*) FROM combat_events"),
        "chat_messages": scalar("SELECT COUNT(*) FROM chat_messages"),
        "telemetry_gaps": scalar("SELECT COUNT(*) FROM telemetry_gaps"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline SFD telemetry analysis")
    parser.add_argument("--database", type=Path, default=Path("data/sfd_telemetry.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("summary")
    ping = sub.add_parser("ping")
    ping.add_argument("--player-session")
    args = parser.parse_args()
    with sqlite3.connect(args.database) as connection:
        data = summary(connection) if args.command == "summary" else ping_report(connection, args.player_session)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
