from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json

from .features import movement_features, state_durations
from .patterns import detect_windows


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
    rows = conn.execute(f"SELECT player_session_id, stats_json FROM player_stat_snapshots WHERE player_session_id IN ({marks}) AND utc_timestamp<? ORDER BY player_session_id,utc_timestamp", (*session_ids, end)).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        try: grouped[row[0]].append(json.loads(row[1]))
        except json.JSONDecodeError: pass
    for session_id, snapshots in grouped.items():
        if len(snapshots) < 2: continue
        first, last = snapshots[0], snapshots[-1]
        result[session_id] = {key: max(0.0, float(last.get(key, 0)) - float(first.get(key, 0))) for key in STAT_KEYS}
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
    return {"active_players": len(active_today), "new_players": new, "returning_players": returning, "rolling_active": {"1d": active_since(1), "7d": active_since(7), "30d": active_since(30)}, "calendar_retention": {"D1": retained(1), "D7": retained(7), "D30": retained(30)}}


def overlap_seconds(joined_at: str, left_at: str | None, start: str, end: str) -> float:
    joined, left = parse_time(joined_at), parse_time(left_at) if left_at else parse_time(end)
    return max(0.0, (min(left, parse_time(end)) - max(joined, parse_time(start))).total_seconds())


def leave_context(conn, sessions) -> dict[str, dict]:
    result = {}
    for session in sessions:
        if not session["left_at"]:
            continue
        left = parse_time(session["left_at"])
        death = conn.execute("SELECT MAX(e.utc_timestamp) FROM events e JOIN combat_events c ON c.event_id=e.event_id WHERE c.victim_session_id=? AND e.event_type LIKE '%death%' AND e.utc_timestamp<=?", (session["player_session_id"], session["left_at"])).fetchone()[0]
        death_seconds = (left - parse_time(death)).total_seconds() if death else None
        ping = conn.execute("SELECT ping_ms FROM network_samples WHERE player_session_id=? AND utc_timestamp<=? ORDER BY utc_timestamp DESC LIMIT 1", (session["player_session_id"], session["left_at"])).fetchone()
        result[session["player_session_id"]] = {"leave_after_death_seconds": death_seconds, "leave_after_death_10s": death_seconds is not None and death_seconds <= 10, "leave_after_death_30s": death_seconds is not None and death_seconds <= 30, "leave_after_death_60s": death_seconds is not None and death_seconds <= 60, "ping_before_leave": ping[0] if ping else None, "leave_after_ping_spike": bool(ping and ping[0] >= 150)}
    return result


def input_metrics(conn, start: str, end: str) -> dict[str, dict]:
    grouped: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for row in conn.execute("SELECT player_session_id,utc_timestamp,event_type,data_json FROM events WHERE event_type IN ('key_input','key_input_batch') AND utc_timestamp>=? AND utc_timestamp<? ORDER BY player_session_id,utc_timestamp", (start, end)):
        try:
            data = json.loads(row[3])
        except json.JSONDecodeError:
            continue
        transitions = data.get("transitions") if row[2] == "key_input_batch" else None
        if isinstance(transitions, list):
            grouped[row[0]].extend((parse_time(row[1]), item) for item in transitions if isinstance(item, dict))
        else:
            grouped[row[0]].append((parse_time(row[1]), data))
    result = {}
    for session, items in grouped.items():
        actions = Counter(item.get("key", "unknown") for _, item in items)
        transitions = sum(1 for _, item in items if item.get("event") in {"Pressed", "Released"})
        bursts = right = 0
        for index, (at, _) in enumerate(items):
            right = max(right, index)
            while right < len(items) and (items[right][0] - at).total_seconds() <= 1:
                right += 1
            if right - index >= 5: bursts += 1
        minutes = max((items[-1][0] - items[0][0]).total_seconds() / 60, 1 / 60)
        result[session] = {"events": len(items), "transitions": transitions, "actions_per_minute": len(items) / minutes, "input_burst_starts": bursts, "keys": dict(actions)}
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
    for row in conn.execute("SELECT r.*, COUNT(rp.player_session_id) joined_players, SUM(CASE WHEN rp.late_join=1 THEN 1 ELSE 0 END) late_joins FROM rounds r LEFT JOIN round_players rp ON rp.round_id=r.round_id WHERE r.started_at>=? AND r.started_at<? GROUP BY r.round_id", (start, end)):
        values = dict(row)
        values["duration_seconds"] = (values.pop("duration_ms") or 0) / 1000
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


def aggregate_day(conn, start: str, end: str, include_bots: bool, static_speed_threshold: float = 1.0, z_threshold: float = 4.0, min_coverage: float = .7) -> tuple[dict, list[dict], list[dict], list[dict], list[dict], list[dict], list[dict], list[dict]]:
    quality = data_quality(conn, start, end)
    sessions = conn.execute("SELECT * FROM player_sessions WHERE joined_at<? AND COALESCE(left_at, ?)>=?", (end, end, start)).fetchall()
    leave_data = leave_context(conn, sessions)
    inputs = input_metrics(conn, start, end)
    rounds = conn.execute("SELECT * FROM rounds WHERE started_at>=? AND started_at<?", (start, end)).fetchall()
    events = conn.execute("SELECT event_type, COUNT(*) AS n FROM events WHERE utc_timestamp>=? AND utc_timestamp<? GROUP BY event_type", (start, end)).fetchall()
    combat, pairs, weapons = combat_metrics(conn, start, end)
    round_rows = round_metric_rows(conn, start, end)
    server = {"sessions": len(sessions), "rounds": len(rounds), "events_by_type": {r[0]: r[1] for r in events}, "humans": sum(not r["is_bot"] for r in sessions), "bots": sum(bool(r["is_bot"]) for r in sessions), "data_quality": quality, "retention": retention_metrics(conn, start, end), "combat": {"damage": sum(x["damage_dealt"] for x in combat.values()), "inferred_kills": sum(x["inferred_kill_credit"] for x in combat.values()), "inferred_kills_high_confidence": sum(x["inferred_kill_high_confidence"] for x in combat.values()), "inferred_kills_medium_confidence": sum(x["inferred_kill_medium_confidence"] for x in combat.values()), "unattributed_deaths": sum(x["unattributed_death"] for x in combat.values())}}
    player_rows = []
    grouped = defaultdict(list)
    for row in sessions:
        if row["player_identity_id"] and (include_bots or not row["is_bot"]):
            grouped[row["player_identity_id"]].append(row)
    deltas = stat_deltas(conn, [row["player_session_id"] for row in sessions], start, end)
    state_rows = defaultdict(list)
    for row in conn.execute("SELECT player_session_id,game_ms,x,y,velocity_x,velocity_y,state_json FROM state_samples WHERE utc_timestamp>=? AND utc_timestamp<? ORDER BY player_session_id,game_ms", (start, end)):
        state_rows[row["player_session_id"]].append(dict(row))
    for identity, rows in grouped.items():
        visits = stitch_visits(rows, start, end)
        durations = [visit["duration_seconds"] for visit in visits]
        completed_durations = [visit["duration_seconds"] for visit in visits if not visit["censored"]]
        player_stats = {key: sum(deltas.get(row["player_session_id"], {}).get(key, 0) for row in rows) for key in STAT_KEYS}
        movement = [movement_features(state_rows[row["player_session_id"]], static_speed_threshold) for row in rows if state_rows[row["player_session_id"]]]
        combat_data = {key: sum(combat.get(row["player_session_id"], {}).get(key, 0) for row in rows) for key in ("damage_dealt", "damage_received", "combat_events", "inferred_kill_credit", "inferred_kill_high_confidence", "inferred_kill_medium_confidence", "inferred_assist_credit", "inferred_death", "unattributed_death")}
        leaves = [leave_data.get(r["player_session_id"], {}) for r in rows]
        input_rows = [inputs.get(r["player_session_id"], {}) for r in rows]
        display_name = next((row["character_name"] or row["account_name"] for row in rows if row["character_name"] or row["account_name"]), "Player " + identity[-6:])
        player_rows.append({"player_identity_id": identity, "display_name": display_name, "is_bot": bool(rows[0]["is_bot"]), "sessions": len(rows), "visits": len(visits), "censored_sessions": sum(r["left_at"] is None for r in rows), "censored_visits": sum(visit["censored"] for visit in visits), "playtime_seconds": sum(durations), "mean_session_seconds": statistics.fmean(completed_durations) if completed_durations else None, "p50_session_seconds": percentile(completed_durations, .5), "p90_session_seconds": percentile(completed_durations, .9), "leave_reasons": dict(Counter(r["leave_reason"] or "censored" for r in rows)), "leave_context": {"after_death_10s": sum(x.get("leave_after_death_10s", False) for x in leaves), "after_death_30s": sum(x.get("leave_after_death_30s", False) for x in leaves), "after_death_60s": sum(x.get("leave_after_death_60s", False) for x in leaves), "after_ping_spike": sum(x.get("leave_after_ping_spike", False) for x in leaves)}, "input": {"events": sum(x.get("events", 0) for x in input_rows), "transitions": sum(x.get("transitions", 0) for x in input_rows), "actions_per_minute": statistics.fmean(x["actions_per_minute"] for x in input_rows if "actions_per_minute" in x) if any("actions_per_minute" in x for x in input_rows) else 0.0, "input_burst_starts": sum(x.get("input_burst_starts", 0) for x in input_rows)}, "statistics": player_stats, "combat": combat_data, "movement": {"distance": sum(x["distance"] for x in movement), "active_distance": sum(x["active_distance"] for x in movement), "static_seconds": sum(x["static_seconds"] for x in movement), "coverage": sum(x["coverage"] for x in movement) / len(movement) if movement else 0.0}})
    maps = []
    for row in conn.execute("SELECT map_name, COUNT(*) rounds, AVG(duration_ms)/1000 duration_seconds FROM rounds WHERE started_at>=? AND started_at<? GROUP BY map_name", (start, end)):
        maps.append(dict(row))
    pings = defaultdict(list)
    for row in conn.execute("SELECT n.player_session_id,n.utc_timestamp,n.ping_ms FROM network_samples n WHERE n.utc_timestamp>=? AND n.utc_timestamp<? ORDER BY n.player_session_id,n.utc_timestamp", (start, end)):
        pings[row[0]].append((row[1], row[2]))
    network = [{"player_session_id": key, **ping_metrics(value)} for key, value in pings.items()]
    build_style_profiles(player_rows)
    return server, player_rows, maps, network, weapons, pairs, round_rows, anomaly_windows(conn, start, end, z_threshold, min_coverage, static_speed_threshold)
