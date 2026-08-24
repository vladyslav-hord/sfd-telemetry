from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any


BARREL_MARKERS = ("barrel", "drum", "oilbarrel")


def object_category(name: str | None) -> str:
    value = (name or "unknown").lower()
    if any(marker in value for marker in BARREL_MARKERS):
        return "barrel"
    if any(marker in value for marker in ("crate", "box", "case")):
        return "crate"
    if any(marker in value for marker in ("chair", "table", "sofa", "furniture")):
        return "furniture"
    if any(marker in value for marker in ("platform", "elevator", "lift")):
        return "platform"
    if any(marker in value for marker in ("mine", "grenade", "bomb", "explosive")):
        return "explosive"
    return "other"


def _nearest(rows: list[dict], target: float, before: bool) -> dict | None:
    eligible = [row for row in rows if (row["game_ms"] <= target if before else row["game_ms"] >= target)]
    if not eligible:
        return None
    return max(eligible, key=lambda row: row["game_ms"]) if before else min(eligible, key=lambda row: row["game_ms"])


def _speed(row: dict | None) -> float:
    if not row:
        return 0.0
    return math.hypot(float(row.get("velocity_x", row.get("vx", 0)) or 0), float(row.get("velocity_y", row.get("vy", 0)) or 0))


def _barrel_candidates(conn, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        """SELECT i.scene_interaction_id,i.event_id,i.round_id,i.game_ms,i.player_session_id,
                  i.target_entity_id,e.name
           FROM scene_interactions i JOIN scene_entities e ON e.scene_entity_id=i.target_entity_id
           WHERE i.interaction_type IN ('player_kick_object','player_melee_object')
             AND i.utc_timestamp>=? AND i.utc_timestamp<?""",
        (start, end),
    ).fetchall()
    result = []
    for row in rows:
        if object_category(row["name"]) != "barrel" or not row["player_session_id"] or row["game_ms"] is None:
            continue
        at = float(row["game_ms"])
        object_rows = [dict(item) for item in conn.execute(
            "SELECT game_ms,velocity_x,velocity_y,x,y FROM scene_samples WHERE scene_entity_id=? AND round_id=? AND game_ms BETWEEN ? AND ? ORDER BY game_ms",
            (row["target_entity_id"], row["round_id"], at - 150, at + 150),
        )]
        player_rows = [dict(item) for item in conn.execute(
            "SELECT game_ms,velocity_x,velocity_y,x,y FROM state_samples WHERE player_session_id=? AND round_id=? AND game_ms BETWEEN ? AND ? ORDER BY game_ms",
            (row["player_session_id"], row["round_id"], at - 150, at + 600),
        )]
        object_before, object_after = _nearest(object_rows, at, True), _nearest(object_rows, at, False)
        player_before, player_after = _nearest(player_rows, at, True), _nearest(player_rows, at + 500, False)
        coverage = sum(value is not None for value in (object_before, object_after, player_before, player_after)) / 4
        impulse = _speed(object_after) - _speed(object_before)
        speed_gain = _speed(player_after) - _speed(player_before)
        displacement = math.hypot(float((player_after or {}).get("x", 0) or 0) - float((player_before or {}).get("x", 0) or 0), float((player_after or {}).get("y", 0) or 0) - float((player_before or {}).get("y", 0) or 0)) if player_before and player_after else 0.0
        if coverage < .7:
            confidence = "insufficient"
        elif impulse > .5 and speed_gain > .5 and displacement > .25:
            confidence = "high"
        elif impulse > .25 and (speed_gain > .25 or displacement > .25):
            confidence = "medium"
        else:
            confidence = "low"
        result.append({
            "source_window_id": "scene:" + str(row["event_id"]), "source_event_ids": [row["event_id"]],
            "pattern_type": "object_assisted_movement", "subtype": "barrel_boost_candidate",
            "player_session_id": row["player_session_id"], "round_id": row["round_id"], "object_id": row["target_entity_id"],
            "object_name": row["name"], "interaction_id": row["scene_interaction_id"], "coverage": coverage,
            "confidence": confidence, "object_speed_gain": impulse, "player_speed_gain": speed_gain,
            "player_displacement": displacement, "observed_advantage": max(speed_gain, displacement),
        })
    return result


def _motifs(conn, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        """SELECT player_session_id,round_id,game_ms,interaction_type,target_entity_id
           FROM scene_interactions WHERE player_session_id IS NOT NULL AND utc_timestamp>=? AND utc_timestamp<?
           ORDER BY player_session_id,round_id,game_ms""",
        (start, end),
    ).fetchall()
    sequences: Counter[tuple[str, str, str]] = Counter()
    for first, second in zip(rows, rows[1:]):
        if first["player_session_id"] != second["player_session_id"] or first["round_id"] != second["round_id"]:
            continue
        if first["game_ms"] is None or second["game_ms"] is None or second["game_ms"] - first["game_ms"] > 3000:
            continue
        sequences[(first["interaction_type"], second["interaction_type"], str(first["target_entity_id"] == second["target_entity_id"]))] += 1
    return [{"motif": ">".join(key[:2]), "same_target": key[2] == "True", "occurrences": value} for key, value in sequences.most_common(50)]


def scene_overview(conn, start: str, end: str) -> dict[str, Any]:
    interactions = conn.execute(
        """SELECT i.interaction_type,i.source_quality,i.x,i.y,e.name
           FROM scene_interactions i LEFT JOIN scene_entities e ON e.scene_entity_id=i.target_entity_id
           WHERE i.utc_timestamp>=? AND i.utc_timestamp<?""", (start, end)
    ).fetchall()
    categories, types, heatmap, graph_edges, object_usage = Counter(), Counter(), Counter(), Counter(), Counter()
    for row in interactions:
        types[row["interaction_type"]] += 1
        category = object_category(row["name"])
        categories[category] += 1
        object_usage[(row["name"] or "unknown", category)] += 1
        source = "player:" + row["player_session_id"] if row["player_session_id"] else "scene_source"
        graph_edges[(source, row["interaction_type"], category)] += 1
        if row["x"] is not None and row["y"] is not None:
            heatmap[(round(float(row["x"]) / 50) * 50, round(float(row["y"]) / 50) * 50)] += 1
    episodes = [dict(row) for row in conn.execute(
        """SELECT w.source_event_id,w.round_id,w.trigger_game_ms,w.trigger,w.coverage,e.utc_timestamp,
                  json_array_length(w.entities_json) entity_count
           FROM scene_windows w JOIN events e ON e.event_id=w.source_event_id
           WHERE e.utc_timestamp>=? AND e.utc_timestamp<? ORDER BY e.utc_timestamp DESC LIMIT 200""", (start, end)
    )]
    barrels = _barrel_candidates(conn, start, end)
    lifecycle = {row[0]: row[1] for row in conn.execute(
        "SELECT event_type,COUNT(*) FROM events WHERE event_type IN ('object_created','object_terminated','projectile_created','projectile_hit','explosion_hit') AND utc_timestamp>=? AND utc_timestamp<? GROUP BY event_type", (start, end)
    )}
    samples = conn.execute("SELECT COUNT(*) FROM scene_samples s JOIN events e ON e.event_id=s.event_id WHERE e.utc_timestamp>=? AND e.utc_timestamp<?", (start, end)).fetchone()[0]
    return {
        "available": bool(interactions), "interactions_by_type": dict(types), "object_categories": dict(categories),
        "interaction_heatmap": [{"x": x, "y": y, "count": count} for (x, y), count in heatmap.most_common(500)],
        "object_usage": [{"name": name, "category": category, "interactions": count} for (name, category), count in object_usage.most_common(50)],
        "interaction_graph": [{"from": source, "relation": relation, "to": target, "count": count} for (source, relation, target), count in graph_edges.most_common(100)],
        "lifecycle": lifecycle, "scene_samples": samples,
        "environmental_damage": sum(count for kind, count in types.items() if kind in {"object_damage_player", "object_damage_explosion", "object_impact_object"}),
        "barrel_boost_candidates": barrels, "motifs": _motifs(conn, start, end), "episodes": episodes,
    }


def load_episode(conn, source_event_id: int) -> dict[str, Any] | None:
    window = conn.execute(
        """SELECT w.*,e.utc_timestamp FROM scene_windows w JOIN events e ON e.event_id=w.source_event_id
           WHERE w.source_event_id=?""", (source_event_id,)
    ).fetchone()
    if not window:
        return None
    entities = json.loads(window["entities_json"])
    engine_ids = [item.get("object_id") for item in entities if isinstance(item, dict) and item.get("object_id") is not None]
    placeholders = ",".join("?" for _ in engine_ids) or "NULL"
    entity_rows = conn.execute(
        f"SELECT scene_entity_id,engine_id,name,entity_kind,manifest_json FROM scene_entities WHERE round_id=? AND engine_id IN ({placeholders})",
        [window["round_id"], *engine_ids],
    ).fetchall() if engine_ids else []
    entity_ids = [row["scene_entity_id"] for row in entity_rows]
    sample_placeholders = ",".join("?" for _ in entity_ids) or "NULL"
    start_ms, end_ms = window["trigger_game_ms"] - 5000, window["trigger_game_ms"] + 2000
    samples = [dict(row) for row in conn.execute(
        f"SELECT scene_entity_id,game_ms,x,y,velocity_x,velocity_y,angle,health,is_missile FROM scene_samples WHERE scene_entity_id IN ({sample_placeholders}) AND game_ms BETWEEN ? AND ? ORDER BY game_ms LIMIT 1200",
        [*entity_ids, start_ms, end_ms],
    )] if entity_ids else []
    players = [dict(row) for row in conn.execute(
        "SELECT player_session_id,game_ms,x,y,velocity_x,velocity_y,hp,team FROM state_samples WHERE round_id=? AND game_ms BETWEEN ? AND ? ORDER BY game_ms LIMIT 1200",
        (window["round_id"], start_ms, end_ms),
    )]
    interactions = [dict(row) for row in conn.execute(
        "SELECT interaction_type,source_quality,player_session_id,target_entity_id,game_ms,x,y,damage FROM scene_interactions WHERE round_id=? AND game_ms BETWEEN ? AND ? ORDER BY game_ms",
        (window["round_id"], start_ms, end_ms),
    )]
    explosions = []
    for row in conn.execute("SELECT game_ms,data_json FROM events WHERE round_id=? AND event_type='explosion_hit' AND game_ms BETWEEN ? AND ? ORDER BY game_ms", (window["round_id"], start_ms, end_ms)):
        try:
            data = json.loads(row["data_json"])
            explosions.append({"game_ms": row["game_ms"], "x": data.get("x"), "y": data.get("y"), "radius": data.get("radius"), "max_damage": data.get("max_damage")})
        except (TypeError, json.JSONDecodeError):
            continue
    return {"source_event_id": source_event_id, "round_id": window["round_id"], "trigger": window["trigger"], "trigger_game_ms": window["trigger_game_ms"], "coverage": window["coverage"], "utc_timestamp": window["utc_timestamp"], "entities": [dict(row) for row in entity_rows], "samples": samples, "players": players, "interactions": interactions, "explosions": explosions, "limitations": ["States between samples are interpolated.", "Unobserved SFD physics collisions are not reconstructed."]}
