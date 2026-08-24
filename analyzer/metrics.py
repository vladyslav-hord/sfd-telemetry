from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import json
from itertools import groupby

from .features import movement_features, state_durations
from .patterns import detect_windows
from .scene import scene_overview


STAT_KEYS = ("TotalBlockedAttacks", "TotalDamageTaken", "TotalDives", "TotalEmptyGunsFireAttempts", "TotalExplosionDamageTaken", "TotalFallDamageTaken", "TotalFireDamageTaken", "TotalGrabbedPlayers", "TotalGrabCharges", "TotalItemsThrown", "TotalJumps", "TotalKickHits", "TotalKickSwings", "TotalMeleeAttackHits", "TotalMeleeAttackSwings", "TotalMeleeDamageTaken", "TotalOtherDamageTaken", "TotalPlayersThrown", "TotalProjectileDamageTaken", "TotalProjectilesHitBy", "TotalReloads", "TotalRolls", "TotalShotsFired")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return float(ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ping_metrics(samples: list[tuple[str, int]]) -> dict:
    if not samples:
        return {"samples": 0, "coverage": 0.0}
    values = [float(ping) for _, ping in samples]
    deltas = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    above = {str(t): 0.0 for t in (100, 150, 200, 250)}
    longest = current = 0.0
    for i in range(1, len(samples)):
        seconds = min(30.0, max(0.0, (parse_time(samples[i][0]) - parse_time(samples[i - 1][0])).total_seconds()))
        if values[i - 1] > 100:
            current += seconds
            longest = max(longest, current)
        else:
            current = 0.0
        for threshold in (100, 150, 200, 250):
            if values[i - 1] > threshold:
                above[str(threshold)] += seconds
    return {"samples": len(values), "mean": statistics.fmean(values), "median": statistics.median(values), "min": min(values), "max": max(values), "stddev": statistics.pstdev(values), "p50": percentile(values, .5), "p90": percentile(values, .9), "p95": percentile(values, .95), "p99": percentile(values, .99), "estimated_jitter": statistics.fmean(deltas) if deltas else 0.0, "spikes": {str(t): sum(delta >= t for delta in deltas) for t in (50, 100, 150)}, "seconds_above": above, "longest_high_ping_interval": longest, "coverage": 1.0}


def day_bounds(day: str, zone) -> tuple[str, str]:
    from datetime import timedelta
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))


def data_quality(conn, start: str, end: str) -> dict:
    count = conn.execute("SELECT COUNT(*) FROM events WHERE utc_timestamp>=? AND utc_timestamp<?", (start, end)).fetchone()[0]
    gaps = conn.execute("SELECT COUNT(*) FROM telemetry_gaps WHERE detected_at>=? AND detected_at<?", (start, end)).fetchone()[0]
    missing = conn.execute("SELECT COALESCE(SUM(observed_sequence-expected_sequence),0) FROM telemetry_gaps WHERE detected_at>=? AND detected_at<?", (start, end)).fetchone()[0]
    incomplete = conn.execute("SELECT COUNT(*) FROM player_sessions WHERE joined_at<? AND (left_at IS NULL OR left_at>=?)", (end, end)).fetchone()[0]
    combat = conn.execute("SELECT COUNT(*) FROM combat_events c JOIN events e ON e.event_id=c.event_id WHERE e.utc_timestamp>=? AND e.utc_timestamp<?", (start, end)).fetchone()[0]
    resolved = conn.execute("SELECT COUNT(*) FROM combat_events c JOIN events e ON e.event_id=c.event_id WHERE e.utc_timestamp>=? AND e.utc_timestamp<? AND c.attacker_session_id IS NOT NULL AND c.victim_session_id IS NOT NULL", (start, end)).fetchone()[0]
    state = conn.execute("SELECT COUNT(*) FROM state_samples WHERE utc_timestamp>=? AND utc_timestamp<?", (start, end)).fetchone()[0]
    highres = conn.execute("SELECT COUNT(*) FROM state_samples WHERE utc_timestamp>=? AND utc_timestamp<? AND resolution_hz>=4", (start, end)).fetchone()[0]
    windows = conn.execute("SELECT COUNT(*) FROM state_windows WHERE utc_timestamp>=? AND utc_timestamp<?", (start, end)).fetchone()[0]
    ping = conn.execute("SELECT COUNT(*) FROM network_samples WHERE utc_timestamp>=? AND utc_timestamp<?", (start, end)).fetchone()[0]
    finished_rounds = conn.execute("SELECT COUNT(*) FROM rounds WHERE started_at>=? AND started_at<? AND ended_at IS NOT NULL", (start, end)).fetchone()[0]
    rounds = conn.execute("SELECT COUNT(*) FROM rounds WHERE started_at>=? AND started_at<?", (start, end)).fetchone()[0]
    stable = conn.execute("SELECT COUNT(*) FROM player_sessions WHERE joined_at>=? AND joined_at<? AND player_identity_id IS NOT NULL", (start, end)).fetchone()[0]
    total_sessions = conn.execute("SELECT COUNT(*) FROM player_sessions WHERE joined_at>=? AND joined_at<?", (start, end)).fetchone()[0]
    scene_frames = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='scene_frame_batch' AND utc_timestamp>=? AND utc_timestamp<?", (start, end)).fetchone()[0]
    scene_manifests = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='scene_manifest_batch' AND utc_timestamp>=? AND utc_timestamp<?", (start, end)).fetchone()[0]
    return {"event_count": count, "sequence_gaps": gaps, "sequence_missing_events": missing, "source_coverage": count / (count + missing) if count + missing else 0.0, "incomplete_player_sessions": incomplete, "state_samples": state, "highres_samples": highres, "highres_windows": windows, "ping_samples": ping, "combat_events": combat, "scene_frame_batches": scene_frames, "scene_manifest_batches": scene_manifests, "scene_available": bool(scene_frames or scene_manifests), "combat_identity_coverage": resolved / combat if combat else None, "round_completion_coverage": finished_rounds / rounds if rounds else None, "stable_identity_coverage": stable / total_sessions if total_sessions else None}


def stat_deltas(conn, session_ids: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    if not session_ids:
        return result
    marks = ",".join("?" for _ in session_ids)
    current_id = None
    first = last = None

    def finish(session_id, first_snapshot, last_snapshot):
        if session_id is not None and first_snapshot is not None and last_snapshot is not None and first_snapshot is not last_snapshot:
            result[session_id] = {key: max(0.0, float(last_snapshot.get(key, 0)) - float(first_snapshot.get(key, 0))) for key in STAT_KEYS}

    for row in conn.execute(f"SELECT player_session_id, stats_json FROM player_stat_snapshots WHERE player_session_id IN ({marks}) AND utc_timestamp<? ORDER BY player_session_id,utc_timestamp", (*session_ids, end)):
        if row[0] != current_id:
            finish(current_id, first, last)
            current_id, first, last = row[0], None, None
        try:
            snapshot = json.loads(row[1])
        except json.JSONDecodeError:
            continue
        if first is None:
            first = snapshot
        last = snapshot
    finish(current_id, first, last)
    return result


def retention_metrics(conn, start: str, end: str) -> dict:
    rows = conn.execute("SELECT player_identity_id, MIN(joined_at) first_seen, MAX(COALESCE(left_at,joined_at)) last_seen, COUNT(*) sessions, COUNT(DISTINCT substr(joined_at,1,10)) active_days FROM player_sessions WHERE player_identity_id IS NOT NULL AND joined_at<? GROUP BY player_identity_id", (end,)).fetchall()
    start_date, end_date = parse_time(start).date(), parse_time(end).date()
    active_today = {row[0] for row in conn.execute("SELECT DISTINCT player_identity_id FROM player_sessions WHERE player_identity_id IS NOT NULL AND joined_at<? AND COALESCE(left_at,?)>=?", (end, end, start))}
    def active_since(days: int) -> int:
        threshold = (parse_time(end) - __import__('datetime').timedelta(days=days)).isoformat().replace('+00:00','Z')
        return conn.execute("SELECT COUNT(DISTINCT player_identity_id) FROM player_sessions WHERE player_identity_id IS NOT NULL AND joined_at>=? AND joined_at<?", (threshold, end)).fetchone()[0]
    new = returning = 0
    for row in rows:
        if row[0] not in active_today: continue
        if parse_time(row[1]).date() == start_date: new += 1
        else: returning += 1
    def retained(days: int) -> dict:
        cohort_day = start_date - __import__('datetime').timedelta(days=days)
        cohort_start = cohort_day.isoformat() + "T00:00:00Z"
        cohort_end = (cohort_day + __import__('datetime').timedelta(days=1)).isoformat() + "T00:00:00Z"
        cohort = {row[0] for row in conn.execute("SELECT DISTINCT player_identity_id FROM player_sessions WHERE player_identity_id IS NOT NULL AND joined_at>=? AND joined_at<?", (cohort_start, cohort_end))}
        returned = len(cohort & active_today)
        return {"cohort_size": len(cohort), "returned": returned, "rate": returned / len(cohort) if cohort else None}
    return {
        "active_players": len(active_today),
        "new_players": new,
        "returning_players": returning,
        "rolling_active": {"1d": active_since(1), "7d": active_since(7), "30d": active_since(30)},
        "calendar_retention": {"D1": retained(1), "D7": retained(7), "D30": retained(30)},
        "provenance": {
            "population": "distinct non-null player_identity_id",
            "window_start": start,
            "window_end": end,
            "bot_identity_policy": "bots without player_identity_id are excluded",
        },
    }


def overlap_seconds(joined_at: str, left_at: str | None, start: str, end: str) -> float:
    joined, left = parse_time(joined_at), parse_time(left_at) if left_at else parse_time(end)
    return max(0.0, (min(left, parse_time(end)) - max(joined, parse_time(start))).total_seconds())


def leave_context(conn, sessions) -> dict[str, dict]:
    result = {}
    if not sessions:
        return result
    # The caller already selected the relevant sessions. Keep this set-based so
    # each leave context does not issue two additional queries per session.
    session_ids = {session["player_session_id"] for session in sessions if session["left_at"]}
    if not session_ids:
        return result
    marks = ",".join("?" for _ in session_ids)
    session_args = tuple(session_ids)
    deaths = {
        row[0]: row[1]
        for row in conn.execute(
            f"""SELECT c.victim_session_id,MAX(e.utc_timestamp)
               FROM combat_events c JOIN events e ON e.event_id=c.event_id
               WHERE c.victim_session_id IN ({marks})
                 AND e.event_type LIKE '%death%' AND e.utc_timestamp<=(
                   SELECT s.left_at FROM player_sessions s WHERE s.player_session_id=c.victim_session_id)
               GROUP BY c.victim_session_id""", session_args
        )
    }
    pings = {
        row[0]: row[1]
        for row in conn.execute(
            f"""WITH ranked AS (
                   SELECT n.player_session_id,n.ping_ms,
                          ROW_NUMBER() OVER (PARTITION BY n.player_session_id ORDER BY n.utc_timestamp DESC,n.sample_id DESC) AS rank_no
                   FROM network_samples n
                   JOIN player_sessions s ON s.player_session_id=n.player_session_id
                   WHERE n.player_session_id IN ({marks}) AND s.left_at IS NOT NULL AND n.utc_timestamp<=s.left_at
               )
               SELECT player_session_id,ping_ms FROM ranked WHERE rank_no=1""", session_args
        )
    }
    for session in sessions:
        if not session["left_at"]:
            continue
        left = parse_time(session["left_at"])
        death = deaths.get(session["player_session_id"])
        death_seconds = (left - parse_time(death)).total_seconds() if death else None
        ping = pings.get(session["player_session_id"])
        result[session["player_session_id"]] = {"leave_after_death_seconds": death_seconds, "leave_after_death_10s": death_seconds is not None and death_seconds <= 10, "leave_after_death_30s": death_seconds is not None and death_seconds <= 30, "leave_after_death_60s": death_seconds is not None and death_seconds <= 60, "ping_before_leave": ping, "leave_after_ping_spike": ping is not None and ping >= 150}
    return result


def input_metrics(conn, start: str, end: str) -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    for row in conn.execute("SELECT player_session_id,utc_timestamp,event_type,data_json FROM events WHERE event_type IN ('key_input','key_input_batch') AND utc_timestamp>=? AND utc_timestamp<? ORDER BY player_session_id,utc_timestamp", (start, end)):
        try:
            data = json.loads(row[3])
        except json.JSONDecodeError:
            continue
        state = grouped.setdefault(row[0], {"events": 0, "transitions": 0, "input_burst_starts": 0, "first": None, "last": None, "actions": Counter(), "window": deque()})
        transitions = data.get("transitions") if row[2] == "key_input_batch" else None
        items = transitions if isinstance(transitions, list) else [data]
        at = parse_time(row[1])
        for item in items:
            if not isinstance(item, dict):
                continue
            state["events"] += 1
            state["first"] = state["first"] or at
            state["last"] = at
            state["actions"][item.get("key", "unknown")] += 1
            if item.get("event") in {"Pressed", "Released"}:
                state["transitions"] += 1
            while state["window"] and (at - state["window"][0]).total_seconds() > 1:
                state["window"].popleft()
            state["window"].append(at)
            if len(state["window"]) >= 5:
                state["input_burst_starts"] += 1
    result = {}
    for session, state in grouped.items():
        minutes = max((state["last"] - state["first"]).total_seconds() / 60, 1 / 60)
        result[session] = {"events": state["events"], "transitions": state["transitions"], "actions_per_minute": state["events"] / minutes, "input_burst_starts": state["input_burst_starts"], "keys": dict(state["actions"])}
    return result


def inferred_weapon(row) -> str:
    if row["weapon"]:
        return str(row["weapon"])
    try:
        context = json.loads(row["context_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return "unknown"
    projectile = context.get("projectile")
    attacker = context.get("attacker")
    projectile_weapon = projectile.get("projectile") if isinstance(projectile, dict) else None
    attacker_weapon = attacker.get("weapon") if isinstance(attacker, dict) else None
    return str(context.get("weapon") or projectile_weapon or attacker_weapon or "unknown")


def infer_death_attribution(death: dict, recent_damage: list[dict]) -> tuple[str | None, str, list[str]]:
    """Infer a shooter-style final-blow killer and eligible assists; never authoritative."""
    death_time = parse_time(death["utc_timestamp"])
    victim, round_id = death["victim_session_id"], death["round_id"]
    candidates = []
    for event in recent_damage:
        attacker = event["attacker_session_id"]
        if not attacker or attacker == victim or event["round_id"] != round_id:
            continue
        age = (death_time - parse_time(event["utc_timestamp"])).total_seconds()
        if 0 <= age <= 8:
            candidates.append((event, age))
    if not candidates:
        return None, "unattributed", []
    # Shooters award a frag to the last valid damaging player. Here the temporal
    # link is inferred, so a stale final hit is deliberately not credited.
    final_hit, final_age = min(candidates, key=lambda item: (item[1], -int(item[0]["event_id"])))
    if final_age > 2.5:
        return None, "unattributed", []
    contributions: dict[str, float] = defaultdict(float)
    freshness: dict[str, float] = {}
    for event, age in candidates:
        attacker = event["attacker_session_id"]
        contributions[attacker] += float(event["damage"] or 0) * math.exp(-age / 4.0)
        freshness[attacker] = min(freshness.get(attacker, age), age)
    total = sum(contributions.values())
    killer = final_hit["attacker_session_id"]
    killer_share = contributions[killer] / total if total else 0.0
    confidence = "high" if final_age <= 0.75 and (killer_share >= 0.5 or len(contributions) == 1) else "medium"
    assists = [
        attacker for attacker, score in contributions.items()
        if attacker != killer and freshness[attacker] <= 8 and total and score / total >= 0.15
    ]
    return killer, confidence, assists


def combat_metrics(conn, start: str, end: str) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """Damage/death correlation uses a conservative final-blow model, never an authoritative kill."""
    rows = conn.execute("SELECT e.event_id,e.event_type,e.utc_timestamp,e.round_id,c.attacker_session_id,c.victim_session_id,c.damage,c.damage_type,c.weapon,c.distance,c.context_json FROM events e JOIN combat_events c ON c.event_id=e.event_id WHERE e.utc_timestamp>=? AND e.utc_timestamp<? ORDER BY e.utc_timestamp,e.event_id", (start, end)).fetchall()
    by_session: dict[str, dict] = defaultdict(lambda: {"damage_dealt": 0.0, "damage_received": 0.0, "combat_events": 0, "engagement_distances": [], "damage_by_type": defaultdict(float), "inferred_kill_credit": 0.0, "inferred_kill_high_confidence": 0, "inferred_kill_medium_confidence": 0, "inferred_assist_credit": 0.0, "inferred_death": 0, "unattributed_death": 0})
    pair: dict[tuple[str, str], dict] = defaultdict(lambda: {"damage": 0.0, "events": 0, "targeting_frequency": 0})
    weapons: dict[str, dict] = defaultdict(lambda: {"weapon": "unknown", "events": 0, "damage": 0.0, "distances": []})
    recent: dict[str, list] = defaultdict(list)
    for row in rows:
        attacker, victim, damage = row["attacker_session_id"], row["victim_session_id"], float(row["damage"] or 0)
        weapon = inferred_weapon(row)
        weapon_row = weapons[weapon]; weapon_row["weapon"] = weapon; weapon_row["events"] += 1; weapon_row["damage"] += damage
        if row["distance"] is not None: weapon_row["distances"].append(float(row["distance"]))
        if attacker:
            item = by_session[attacker]; item["damage_dealt"] += damage; item["combat_events"] += 1; item["damage_by_type"][row["damage_type"] or "unknown"] += damage
            if row["distance"] is not None: item["engagement_distances"].append(float(row["distance"]))
        if victim:
            item = by_session[victim]; item["damage_received"] += damage; item["damage_by_type"][row["damage_type"] or "unknown"] += damage
            if attacker: pair[(attacker, victim)]["damage"] += damage; pair[(attacker, victim)]["events"] += 1
            if row["event_type"] == "player_damage":
                recent[victim].append(row)
        if "death" not in row["event_type"] or not victim:
            continue
        by_session[victim]["inferred_death"] += 1
        killer, confidence, assists = infer_death_attribution(row, recent[victim])
        if not killer:
            by_session[victim]["unattributed_death"] += 1
            continue
        by_session[killer]["inferred_kill_credit"] += 1.0
        by_session[killer]["inferred_kill_" + confidence + "_confidence"] += 1
        for assistant in assists:
            by_session[assistant]["inferred_assist_credit"] += 1.0
    result = {}
    for session, item in by_session.items():
        item["damage_efficiency"] = item["damage_dealt"] / item["damage_received"] if item["damage_received"] else None
        item["median_engagement_distance"] = percentile(item.pop("engagement_distances"), .5)
        item["damage_by_type"] = dict(item["damage_by_type"])
        result[session] = item
    pairs = [{"player_a_session_id": a, "player_b_session_id": b, **metrics} for (a, b), metrics in pair.items()]
    weapon_rows = []
    for item in weapons.values():
        item["mean_distance"] = statistics.fmean(item.pop("distances")) if item["distances"] else None
        weapon_rows.append(item)
    return result, pairs, weapon_rows


def round_metric_rows(conn, start: str, end: str) -> list[dict]:
    result = []
    for row in conn.execute(
        """SELECT r.*,
                  COUNT(rp.player_session_id) AS joined_players,
                  COUNT(DISTINCT ps.player_identity_id) AS unique_player_identities,
                  COUNT(DISTINCT CASE WHEN COALESCE(ps.is_bot, 0)=0 AND ps.player_identity_id IS NOT NULL
                                      THEN ps.player_identity_id END) AS unique_human_players,
                  COUNT(DISTINCT CASE WHEN ps.is_bot=1 AND ps.player_identity_id IS NOT NULL
                                      THEN ps.player_identity_id END) AS unique_bot_players,
                  SUM(CASE WHEN rp.player_session_id IS NOT NULL AND ps.player_identity_id IS NULL
                           THEN 1 ELSE 0 END) AS unidentified_player_sessions,
                  SUM(CASE WHEN rp.player_session_id IS NOT NULL AND ps.is_bot=1
                           THEN 1 ELSE 0 END) AS bot_sessions,
                  SUM(CASE WHEN rp.late_join=1 THEN 1 ELSE 0 END) AS late_joins
             FROM rounds r
             LEFT JOIN round_players rp ON rp.round_id=r.round_id
             LEFT JOIN player_sessions ps ON ps.player_session_id=rp.player_session_id
            WHERE r.started_at>=? AND r.started_at<?
            GROUP BY r.round_id""",
        (start, end),
    ):
        values = dict(row)
        values["duration_seconds"] = (values.pop("duration_ms") or 0) / 1000
        values["player_slots"] = values.get("player_count") or 0
        values["human_slots"] = values.get("human_count") or 0
        values["bot_slots"] = values.get("bot_count") or 0
        values["unique_players"] = values.pop("unique_player_identities", 0) or 0
        values["unique_human_players"] = values.pop("unique_human_players", 0) or 0
        values["unique_bot_players"] = values.pop("unique_bot_players", 0) or 0
        values["unidentified_player_sessions"] = values.pop("unidentified_player_sessions", 0) or 0
        values["joined_session_count"] = values.get("joined_players", 0) or 0
        values["result_quality"] = "inferred" if str(values.get("result_source") or "").startswith("inferred") else ("exact" if values.get("winner_json") else "derived")
        result.append(values)
    return result


def anomaly_windows(conn, start: str, end: str, z_threshold: float, min_coverage: float, static_speed_threshold: float) -> list[dict]:
    windows = []
    for row in conn.execute("SELECT window_id,player_session_id,round_id,utc_timestamp,trigger,samples_json FROM state_windows WHERE utc_timestamp>=? AND utc_timestamp<?", (start, end)):
        try: samples = json.loads(row["samples_json"])
        except json.JSONDecodeError: continue
        normalized = []
        for item in samples:
            if not isinstance(item, dict) or item.get("game_ms") is None: continue
            normalized.append({"game_ms": item["game_ms"], "x": item.get("x"), "y": item.get("y"), "state_json": item})
        features = movement_features(normalized, static_speed_threshold)
        windows.append({"source_window_id": str(row["window_id"]), "player_session_id": row["player_session_id"], "round_id": row["round_id"], "trigger": row["trigger"], "features": features})
    return detect_windows(windows, z_threshold, min_coverage)


def build_style_profiles(players: list[dict]) -> None:
    """Percentiles are descriptive cohort values, never a single skill verdict."""
    components: dict[str, list[float]] = defaultdict(list)
    raw_by_identity: dict[str, dict[str, float]] = {}
    for player in players:
        seconds = max(float(player["playtime_seconds"] or 0), 1.0)
        stats, combat, movement = player["statistics"], player["combat"], player["movement"]
        raw = {
            "offense": combat["damage_dealt"] / seconds,
            "survival": 1 / max(combat["inferred_death"], 1),
            "damage_efficiency": combat["damage_dealt"] / max(combat["damage_received"], 1),
            "melee_precision": (stats["TotalMeleeAttackHits"] + stats["TotalKickHits"]) / max(stats["TotalMeleeAttackSwings"] + stats["TotalKickSwings"], 1),
            "ranged_projectile_effectiveness": stats["TotalProjectilesHitBy"] / max(stats["TotalShotsFired"], 1),
            "defense": stats["TotalBlockedAttacks"] / seconds,
            "movement": movement["active_distance"] / seconds,
            "consistency": 1.0 if player["sessions"] >= 2 else 0.0,
            "aggression": combat["combat_events"] / seconds,
            "staticness": movement["static_seconds"] / seconds,
            "block_roll_reliance": (stats["TotalBlockedAttacks"] + stats["TotalRolls"]) / seconds,
        }
        raw_by_identity[player["player_identity_id"]] = raw
        for key, value in raw.items(): components[key].append(value)
    for player in players:
        raw = raw_by_identity[player["player_identity_id"]]
        profile = {key: round(100 * sum(item <= value for item in components[key]) / len(components[key]), 2) for key, value in raw.items()} if len(players) >= 2 else {}
        player["skill_profile"] = {"raw_components": raw, "percentiles": profile if player["sessions"] >= 10 else {}, "sample_sessions": player["sessions"], "confidence": min(1.0, player["sessions"] / 10)}


def stitch_visits(rows, start: str, end: str) -> list[dict]:
    visits: list[dict] = []
    for row in sorted(rows, key=lambda item: item["joined_at"]):
        joined = max(parse_time(row["joined_at"]), parse_time(start))
        censored = row["left_at"] is None
        left = min(parse_time(row["left_at"]) if row["left_at"] else parse_time(end), parse_time(end))
        if visits and (joined - visits[-1]["ended_at"]).total_seconds() <= 120:
            visits[-1]["ended_at"] = max(visits[-1]["ended_at"], left)
            visits[-1]["censored"] = visits[-1]["censored"] or censored
        else:
            visits.append({"started_at": joined, "ended_at": left, "censored": censored})
    for visit in visits:
        visit["duration_seconds"] = max(0.0, (visit["ended_at"] - visit["started_at"]).total_seconds())
    return visits


def _stream_movement_features(rows, static_speed_threshold: float = 1.0) -> dict:
    """Compute movement features without retaining a session's state rows."""
    previous = None
    sample_count = 0
    distance = active = static_seconds = airtime = vertical = 0.0
    speed_sum = max_speed = max_acceleration = 0.0
    acceleration_sum = acceleration_count = 0
    direction_switches = 0
    previous_heading = previous_speed = None
    for row in rows:
        current = {"game_ms": row["game_ms"], "x": row["x"], "y": row["y"], "state_json": row["state_json"]}
        sample_count += 1
        if previous is None:
            previous = current
            continue
        dt = max(0.0, min(10.0, float(current["game_ms"] - previous["game_ms"]) / 1000))
        dx = (current["x"] or 0) - (previous["x"] or 0)
        dy = (current["y"] or 0) - (previous["y"] or 0)
        step = math.hypot(dx, dy)
        speed = step / dt if dt else 0.0
        distance += step
        speed_sum += speed
        max_speed = max(max_speed, speed)
        if previous_speed is not None:
            acceleration = abs(speed - previous_speed)
            acceleration_sum += acceleration
            max_acceleration = max(max_acceleration, acceleration)
            acceleration_count += 1
        previous_speed = speed
        if speed < static_speed_threshold:
            static_seconds += dt
        else:
            active += step
        vertical += abs(dy)
        if previous_heading is not None and dx * previous_heading[0] + dy * previous_heading[1] < 0:
            direction_switches += 1
        if step:
            previous_heading = (dx / step, dy / step)
        state = current["state_json"] or {}
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except json.JSONDecodeError:
                state = {}
        if isinstance(state, dict) and (state.get("is_airborne") or state.get("airborne")):
            airtime += dt
        previous = current
    if sample_count < 2:
        return {"sample_count": sample_count, "distance": 0.0, "active_distance": 0.0, "static_seconds": 0.0, "coverage": 0.0}
    return {"sample_count": sample_count, "distance": distance, "active_distance": active, "static_seconds": static_seconds, "mean_speed": speed_sum / (sample_count - 1), "max_speed": max_speed, "mean_acceleration": acceleration_sum / acceleration_count if acceleration_count else 0.0, "max_acceleration": max_acceleration, "direction_switches": direction_switches, "vertical_displacement": vertical, "airtime_seconds": airtime, "coverage": 1.0}


def _movement_by_session(conn, start: str, end: str, static_speed_threshold: float) -> dict[str, dict]:
    rows = conn.execute("SELECT player_session_id,game_ms,x,y,state_json FROM state_samples WHERE utc_timestamp>=? AND utc_timestamp<? ORDER BY player_session_id,game_ms", (start, end))
    return {session_id: _stream_movement_features(group, static_speed_threshold) for session_id, group in groupby(rows, key=lambda row: row["player_session_id"])}


def _ping_metrics_by_session(conn, start: str, end: str, include_players: bool = False) -> list[dict] | tuple[list[dict], list[dict]]:
    """Aggregate pings by session and, optionally, by identified player.

    Session rows retain the identity mapping so report consumers can distinguish
    repeated sessions from unique players. Unknown identities remain separate
    observed sessions and are marked instead of being merged into one player.
    """
    try:
        mapping_available = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='player_sessions'"
        ).fetchone())
    except Exception:
        mapping_available = False
    join = " LEFT JOIN player_sessions s ON s.player_session_id=n.player_session_id" if mapping_available else ""
    mapping = "s.player_identity_id AS player_identity_id,s.is_bot AS is_bot" if mapping_available else "NULL AS player_identity_id,NULL AS is_bot"
    group_expression = "COALESCE(s.player_identity_id,n.player_session_id)" if mapping_available else "n.player_session_id"

    def rows(select: str, order: str):
        return conn.execute(
            f"SELECT n.player_session_id,{mapping},{select} FROM network_samples n{join} "
            f"WHERE n.utc_timestamp>=? AND n.utc_timestamp<? ORDER BY {order}",
            (start, end),
        )

    def new_state() -> dict:
        return {
            "count": 0,
            "sum": 0.0,
            "sum_sq": 0.0,
            "min": None,
            "max": None,
            "delta_sum": 0.0,
            "delta_count": 0,
            "previous_at": None,
            "previous_value": None,
            "above": {str(t): 0.0 for t in (100, 150, 200, 250)},
            "current_high": 0.0,
            "longest_high": 0.0,
            "spikes": {str(t): 0 for t in (50, 100, 150)},
        }

    def identity_quality(identity, is_bot) -> str:
        if not mapping_available:
            return "unmapped_session"
        if identity:
            return "identified"
        if is_bot is True:
            return "unidentified_bot_session"
        if is_bot is False:
            return "unidentified_human_session"
        return "unmapped_session"

    temporal: dict[str, dict] = {}
    for row in rows("n.utc_timestamp,n.ping_ms", "n.player_session_id,n.utc_timestamp,n.sample_id"):
        session_id = row["player_session_id"]
        identity = row["player_identity_id"]
        is_bot = bool(row["is_bot"]) if row["is_bot"] is not None else None
        state = temporal.setdefault(session_id, new_state())
        state["session_id"] = session_id
        state["player_identity_id"] = identity
        state["is_bot"] = is_bot
        state["identity_quality"] = identity_quality(identity, is_bot)
        value = float(row["ping_ms"])
        at = parse_time(row["utc_timestamp"])
        state["count"] += 1
        state["sum"] += value
        state["sum_sq"] += value * value
        state["min"] = value if state["min"] is None else min(state["min"], value)
        state["max"] = value if state["max"] is None else max(state["max"], value)
        if state["previous_at"] is not None:
            seconds = min(30.0, max(0.0, (at - state["previous_at"]).total_seconds()))
            previous = state["previous_value"]
            if previous > 100:
                state["current_high"] += seconds
                state["longest_high"] = max(state["longest_high"], state["current_high"])
            else:
                state["current_high"] = 0.0
            for threshold in (100, 150, 200, 250):
                if previous > threshold:
                    state["above"][str(threshold)] += seconds
            delta = abs(value - previous)
            state["delta_sum"] += delta
            state["delta_count"] += 1
            for threshold in (50, 100, 150):
                if delta >= threshold:
                    state["spikes"][str(threshold)] += 1
        state["previous_at"], state["previous_value"] = at, value

    fractions = (.5, .9, .95, .99)

    def finish_percentiles(state: dict, count: int, captured: dict[float, dict[str, float]]) -> None:
        if not count:
            return
        for fraction in fractions:
            position = (count - 1) * fraction
            low, high = math.floor(position), math.ceil(position)
            low_value = captured[fraction]["low"]
            high_value = captured[fraction]["high"]
            state[f"p{int(fraction * 100)}"] = low_value if low == high else low_value + (high_value - low_value) * (position - low)
        mean = state["sum"] / count
        state["result"] = {
            "samples": count,
            "mean": mean,
            "median": state["p50"],
            "min": state["min"],
            "max": state["max"],
            "stddev": math.sqrt(max(0.0, state["sum_sq"] / count - mean * mean)),
            "p50": state["p50"],
            "p90": state["p90"],
            "p95": state["p95"],
            "p99": state["p99"],
            "estimated_jitter": state["delta_sum"] / state["delta_count"] if state["delta_count"] else 0.0,
            "spikes": state["spikes"],
            "seconds_above": state["above"],
            "longest_high_ping_interval": state["longest_high"],
            "coverage": 1.0,
        }

    current_id = None
    rank = count = 0
    captured: dict[float, dict[str, float]] = {}
    for row in rows("n.ping_ms,COUNT(*) OVER (PARTITION BY n.player_session_id) AS sample_count", "n.player_session_id,n.ping_ms,n.sample_id"):
        session_id = row["player_session_id"]
        if session_id != current_id:
            finish_percentiles(temporal.get(current_id), count, captured) if current_id is not None else None
            current_id = session_id
            count = int(row["sample_count"])
            rank = 0
            captured = {fraction: {} for fraction in fractions}
        value = float(row["ping_ms"])
        for fraction in fractions:
            position = (count - 1) * fraction
            if rank == math.floor(position):
                captured[fraction]["low"] = value
            if rank == math.ceil(position):
                captured[fraction]["high"] = value
        rank += 1
    if current_id is not None:
        finish_percentiles(temporal[current_id], count, captured)

    for state in temporal.values():
        state["group_key"] = state["player_identity_id"] or f"session:{state['session_id']}"

    player_temporal: dict[str, dict] = {}
    for session_id, state in temporal.items():
        group = state["group_key"]
        player = player_temporal.setdefault(group, new_state())
        if "player_identity_id" not in player:
            player["player_identity_id"] = state["player_identity_id"]
            player["is_bot"] = state["is_bot"]
            player["identity_quality"] = state["identity_quality"]
            player["session_count"] = 0
        player["session_count"] += 1
        result = state["result"]
        player["count"] += result["samples"]
        player["sum"] += state["sum"]
        player["sum_sq"] += state["sum_sq"]
        player["min"] = state["min"] if player["min"] is None else min(player["min"], state["min"])
        player["max"] = state["max"] if player["max"] is None else max(player["max"], state["max"])
        player["delta_sum"] += state["delta_sum"]
        player["delta_count"] += state["delta_count"]
        for threshold in player["above"]:
            player["above"][threshold] += state["above"][threshold]
        for threshold in player["spikes"]:
            player["spikes"][threshold] += state["spikes"][threshold]
        player["longest_high"] = max(player["longest_high"], state["longest_high"])

    if include_players and player_temporal:
        current_group = None
        rank = count = 0
        captured = {}
        player_query = rows(
            f"n.ping_ms,COUNT(*) OVER (PARTITION BY {group_expression}) AS sample_count",
            f"{group_expression},n.ping_ms,n.sample_id",
        )
        for row in player_query:
            group = row["player_identity_id"] or f"session:{row['player_session_id']}"
            if group != current_group:
                finish_percentiles(player_temporal.get(current_group), count, captured) if current_group is not None else None
                current_group = group
                count = int(row["sample_count"])
                rank = 0
                captured = {fraction: {} for fraction in fractions}
            value = float(row["ping_ms"])
            for fraction in fractions:
                position = (count - 1) * fraction
                if rank == math.floor(position):
                    captured[fraction]["low"] = value
                if rank == math.ceil(position):
                    captured[fraction]["high"] = value
            rank += 1
        if current_group is not None:
            finish_percentiles(player_temporal[current_group], count, captured)

    session_results = []
    for session_id, state in temporal.items():
        session_results.append({
            "player_session_id": session_id,
            "player_identity_id": state["player_identity_id"],
            "is_bot": state["is_bot"],
            "identity_quality": state["identity_quality"],
            **state["result"],
        })
    if not include_players:
        return session_results
    player_results = []
    for group, state in player_temporal.items():
        item = {
            "player_identity_id": state["player_identity_id"],
            "is_bot": state["is_bot"],
            "identity_quality": state["identity_quality"],
            "session_count": state["session_count"],
            **state["result"],
        }
        if state["player_identity_id"] is None:
            item["player_session_id"] = group.removeprefix("session:")
        player_results.append(item)
    return session_results, player_results


def _population_contract(conn, start: str, end: str) -> dict:
    """Return explicit player/session counts for the report contract.

    Human players are distinct non-bot identities. A bot with no identity is
    retained as an observed session entity instead of being silently dropped.
    """
    row = conn.execute(
        """SELECT
             COUNT(*) AS sessions,
             SUM(CASE WHEN COALESCE(is_bot, 0)=0 THEN 1 ELSE 0 END) AS human_sessions,
             SUM(CASE WHEN is_bot=1 THEN 1 ELSE 0 END) AS bot_sessions,
             COUNT(DISTINCT CASE WHEN COALESCE(is_bot, 0)=0 AND player_identity_id IS NOT NULL
                                 THEN player_identity_id END) AS human_players,
             COUNT(DISTINCT CASE WHEN is_bot=1 AND player_identity_id IS NOT NULL
                                 THEN player_identity_id END) AS bot_players,
             COUNT(DISTINCT player_identity_id) AS identified_entities,
             SUM(CASE WHEN COALESCE(is_bot, 0)=0 AND player_identity_id IS NOT NULL
                      THEN 1 ELSE 0 END) AS identified_human_sessions,
             SUM(CASE WHEN is_bot=1 AND player_identity_id IS NOT NULL
                      THEN 1 ELSE 0 END) AS identified_bot_sessions,
             SUM(CASE WHEN player_identity_id IS NULL THEN 1 ELSE 0 END) AS null_identity_sessions,
             SUM(CASE WHEN COALESCE(is_bot, 0)=0 AND player_identity_id IS NULL
                      THEN 1 ELSE 0 END) AS unidentified_human_sessions,
             SUM(CASE WHEN is_bot=1 AND player_identity_id IS NULL
                      THEN 1 ELSE 0 END) AS unidentified_bot_sessions
           FROM player_sessions
          WHERE joined_at<? AND COALESCE(left_at,?)>=?""",
        (end, end, start),
    ).fetchone()
    values = {key: int(row[key] or 0) for key in (
        "sessions", "human_sessions", "bot_sessions", "human_players", "bot_players",
        "identified_entities", "identified_human_sessions", "identified_bot_sessions",
        "null_identity_sessions", "unidentified_human_sessions", "unidentified_bot_sessions",
    )}
    values["player_entities"] = values["identified_entities"] + values["null_identity_sessions"]
    values["identity_quality"] = {
        "contract": "population-v1",
        "identity_scope": "distinct non-null player_identity_id",
        "human_identity_coverage": values["identified_human_sessions"] / values["human_sessions"] if values["human_sessions"] else None,
        "bot_identity_coverage": values["identified_bot_sessions"] / values["bot_sessions"] if values["bot_sessions"] else None,
        "identified_session_coverage": (values["sessions"] - values["null_identity_sessions"]) / values["sessions"] if values["sessions"] else None,
        "null_identity_sessions": values["null_identity_sessions"],
        "unidentified_human_sessions": values["unidentified_human_sessions"],
        "unidentified_bot_sessions": values["unidentified_bot_sessions"],
        "bot_identity_policy": "unidentified bot sessions count as separate observed entities",
        "window_start": start,
        "window_end": end,
    }
    return values


def aggregate_day(conn, start: str, end: str, include_bots: bool, static_speed_threshold: float = 1.0, z_threshold: float = 4.0, min_coverage: float = .7) -> tuple[dict, list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[dict]]:
    quality = data_quality(conn, start, end)
    sessions = conn.execute("SELECT player_session_id,player_identity_id,joined_at,left_at,leave_reason,account_name,character_name,is_bot FROM player_sessions WHERE joined_at<? AND COALESCE(left_at, ?)>=?", (end, end, start)).fetchall()
    leave_data = leave_context(conn, sessions)
    inputs = input_metrics(conn, start, end)
    round_count = conn.execute("SELECT COUNT(*) FROM rounds WHERE started_at>=? AND started_at<?", (start, end)).fetchone()[0]
    events = conn.execute("SELECT event_type, COUNT(*) AS n FROM events WHERE utc_timestamp>=? AND utc_timestamp<? GROUP BY event_type", (start, end)).fetchall()
    combat, pairs, weapons = combat_metrics(conn, start, end)
    round_rows = round_metric_rows(conn, start, end)
    population = _population_contract(conn, start, end)
    server = {
        "sessions": population["sessions"],
        "human_players": population["human_players"],
        "human_sessions": population["human_sessions"],
        "bot_players": population["bot_players"],
        "bot_sessions": population["bot_sessions"],
        "player_entities": population["player_entities"],
        "identified_entities": population["identified_entities"],
        "null_identity_sessions": population["null_identity_sessions"],
        "identity_quality": population["identity_quality"],
        # Compatibility aliases: these are session counts, not unique players.
        "humans": population["human_sessions"],
        "bots": population["bot_sessions"],
        "population_contract": "population-v1; humans/bots aliases mean session counts",
        "rounds": round_count,
        "events_by_type": {r[0]: r[1] for r in events},
        "data_quality": quality,
        "retention": retention_metrics(conn, start, end),
        "combat": {
            "damage": sum(x["damage_dealt"] for x in combat.values()),
            "inferred_kills": sum(x["inferred_kill_credit"] for x in combat.values()),
            "inferred_kills_high_confidence": sum(x["inferred_kill_high_confidence"] for x in combat.values()),
            "inferred_kills_medium_confidence": sum(x["inferred_kill_medium_confidence"] for x in combat.values()),
            "unattributed_deaths": sum(x["unattributed_death"] for x in combat.values()),
        },
    }
    server["environment"] = scene_overview(conn, start, end)
    player_rows = []
    grouped = defaultdict(list)
    for row in sessions:
        if row["player_identity_id"] and (include_bots or not row["is_bot"]):
            grouped[row["player_identity_id"]].append(row)
    deltas = stat_deltas(conn, [row["player_session_id"] for row in sessions], start, end)
    movement_by_session = _movement_by_session(conn, start, end, static_speed_threshold)
    for identity, rows in grouped.items():
        visits = stitch_visits(rows, start, end)
        durations = [visit["duration_seconds"] for visit in visits]
        completed_durations = [visit["duration_seconds"] for visit in visits if not visit["censored"]]
        player_stats = {key: sum(deltas.get(row["player_session_id"], {}).get(key, 0) for row in rows) for key in STAT_KEYS}
        movement = [movement_by_session[row["player_session_id"]] for row in rows if row["player_session_id"] in movement_by_session]
        combat_data = {key: sum(combat.get(row["player_session_id"], {}).get(key, 0) for row in rows) for key in ("damage_dealt", "damage_received", "combat_events", "inferred_kill_credit", "inferred_kill_high_confidence", "inferred_kill_medium_confidence", "inferred_assist_credit", "inferred_death", "unattributed_death")}
        leaves = [leave_data.get(r["player_session_id"], {}) for r in rows]
        input_rows = [inputs.get(r["player_session_id"], {}) for r in rows]
        display_name = next((row["character_name"] or row["account_name"] for row in rows if row["character_name"] or row["account_name"]), "Player " + identity[-6:])
        player_rows.append({"player_identity_id": identity, "display_name": display_name, "is_bot": bool(rows[0]["is_bot"]), "sessions": len(rows), "visits": len(visits), "censored_sessions": sum(r["left_at"] is None for r in rows), "censored_visits": sum(visit["censored"] for visit in visits), "playtime_seconds": sum(durations), "mean_session_seconds": statistics.fmean(completed_durations) if completed_durations else None, "p50_session_seconds": percentile(completed_durations, .5), "p90_session_seconds": percentile(completed_durations, .9), "leave_reasons": dict(Counter(r["leave_reason"] or "censored" for r in rows)), "leave_context": {"after_death_10s": sum(x.get("leave_after_death_10s", False) for x in leaves), "after_death_30s": sum(x.get("leave_after_death_30s", False) for x in leaves), "after_death_60s": sum(x.get("leave_after_death_60s", False) for x in leaves), "after_ping_spike": sum(x.get("leave_after_ping_spike", False) for x in leaves)}, "input": {"events": sum(x.get("events", 0) for x in input_rows), "transitions": sum(x.get("transitions", 0) for x in input_rows), "actions_per_minute": statistics.fmean(x["actions_per_minute"] for x in input_rows if "actions_per_minute" in x) if any("actions_per_minute" in x for x in input_rows) else 0.0, "input_burst_starts": sum(x.get("input_burst_starts", 0) for x in input_rows)}, "statistics": player_stats, "combat": combat_data, "movement": {"distance": sum(x["distance"] for x in movement), "active_distance": sum(x["active_distance"] for x in movement), "static_seconds": sum(x["static_seconds"] for x in movement), "coverage": sum(x["coverage"] for x in movement) / len(movement) if movement else 0.0}})
    maps = []
    for row in conn.execute("SELECT map_name, COUNT(*) rounds, AVG(duration_ms)/1000 duration_seconds FROM rounds WHERE started_at>=? AND started_at<? GROUP BY map_name", (start, end)):
        maps.append(dict(row))
    network, network_players = _ping_metrics_by_session(conn, start, end, include_players=True)
    server["network_players"] = network_players
    build_style_profiles(player_rows)
    return server, player_rows, maps, network, weapons, pairs, round_rows, anomaly_windows(conn, start, end, z_threshold, min_coverage, static_speed_threshold)
