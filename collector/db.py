from __future__ import annotations

import json
import sqlite3
import hashlib
import zlib
from pathlib import Path
from typing import Any, Iterable

from .parser import ParsedEvent


class TelemetryDB:
    def __init__(self, path: Path, schema_path: Path, views_path: Path, busy_timeout_ms: int = 5000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(schema_path.read_text(encoding="utf-8"))
        self.connection.executescript(views_path.read_text(encoding="utf-8"))
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(player_stat_snapshots)")}
        if "delta_json" not in columns:
            self.connection.execute("ALTER TABLE player_stat_snapshots ADD COLUMN delta_json TEXT")
        self.connection.execute("UPDATE storage_health SET gap_count=(SELECT COUNT(*) FROM telemetry_gaps) WHERE component='raw'")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def insert_batch(self, items: Iterable[ParsedEvent]) -> tuple[int, int]:
        inserted = duplicates = 0
        with self.connection:
            for item in items:
                if self._insert_event(item):
                    inserted += 1
                    self._dispatch(item.envelope)
                else:
                    duplicates += 1
        return inserted, duplicates

    def _ensure_server(self, event: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO server_sessions(server_session_id, started_at, first_event_at, last_event_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(server_session_id) DO UPDATE SET last_event_at=excluded.last_event_at""",
            (event["server_session"], event["ts"], event["ts"], event["ts"]),
        )

    def _insert_event(self, item: ParsedEvent) -> bool:
        e = item.envelope
        self._ensure_server(e)
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO events(
                 schema_version, event_type, server_session_id, round_id,
                 player_session_id, sequence, utc_timestamp, game_ms, data_json, raw_json)
               VALUES(?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, ?, ?, ?)""",
            (
                e["v"], e["type"], e["server_session"], e.get("round", ""),
                e.get("player", ""), e["seq"], e["ts"], e.get("game_ms"),
                json.dumps(e["data"], ensure_ascii=False, separators=(",", ":")), item.raw_line,
            ),
        )
        return cursor.rowcount == 1

    def _dispatch(self, e: dict[str, Any]) -> None:
        event_type, data = e["type"], e["data"]
        event_id = self.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        if event_type == "script_start":
            self.connection.execute(
                """UPDATE server_sessions SET script_version=?, sfd_build=?, transport=?,
                   map_name=?, ended_at=NULL WHERE server_session_id=?""",
                (data.get("script_version"), data.get("sfd_build"), data.get("transport"),
                 data.get("map_name"), e["server_session"]),
            )
        elif event_type == "script_shutdown":
            self.connection.execute(
                "UPDATE server_sessions SET ended_at=? WHERE server_session_id=?",
                (e["ts"], e["server_session"]),
            )
        elif event_type == "round_start":
            self.connection.execute(
                """INSERT OR REPLACE INTO rounds(
                     round_id, server_session_id, map_name, map_guid, map_original_guid,
                     map_author, map_round, map_type, game_type, started_at, start_game_ms,
                     time_limit_seconds, sudden_death_enabled, player_count, human_count, bot_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e["round"], e["server_session"], data.get("map_name"), data.get("map_guid"),
                 data.get("map_original_guid"), data.get("map_author"), data.get("map_round"),
                 data.get("map_type"), data.get("game_type"), e["ts"], e.get("game_ms"),
                 data.get("time_limit_seconds"), int(bool(data.get("sudden_death_enabled"))),
                 data.get("player_count"), data.get("human_count"), data.get("bot_count")),
            )
            for player in data.get("composition", []):
                player_session_id = player.get("player_session_id")
                if player_session_id:
                    self.connection.execute(
                        """INSERT OR IGNORE INTO round_players(round_id, player_session_id, team,
                           joined_game_ms, late_join) VALUES(?,?,?,?,0)""",
                        (e["round"], player_session_id, player.get("team"), e.get("game_ms")),
                    )
        elif event_type == "round_end":
            self.connection.execute(
                """UPDATE rounds SET ended_at=?, end_game_ms=?, duration_ms=?, winner_json=?,
                   draw=?, result_source=?, sudden_death_active=? WHERE round_id=?""",
                (e["ts"], e.get("game_ms"), data.get("duration_ms"),
                json.dumps(data.get("winners"), ensure_ascii=False), int(bool(data.get("draw"))),
                 data.get("result_source"), int(bool(data.get("sudden_death_active"))), e["round"]),
            )
            winners = set(data.get("winners") or [])
            for row in self.connection.execute(
                "SELECT player_session_id FROM round_players WHERE round_id=?", (e["round"],)
            ).fetchall():
                result = {
                    "winner": row[0] in winners,
                    "draw": bool(data.get("draw")),
                    "result_source": data.get("result_source"),
                }
                self.connection.execute(
                    "UPDATE round_players SET result_json=? WHERE round_id=? AND player_session_id=?",
                    (json.dumps(result, separators=(",", ":")), e["round"], row[0]),
                )
        elif event_type in {"user_join", "user_present"}:
            self._upsert_player_session(e)
            if event_type == "user_join" and e.get("round"):
                self.connection.execute(
                    """INSERT OR IGNORE INTO round_players(round_id, player_session_id, team,
                       joined_game_ms, late_join) SELECT ?,?,?,?,1 WHERE EXISTS
                       (SELECT 1 FROM rounds WHERE round_id=?)""",
                    (e["round"], e.get("player"), data.get("team"), e.get("game_ms"), e["round"]),
                )
        elif event_type == "user_leave":
            self.connection.execute(
                """UPDATE player_sessions SET left_at=?, duration_ms=?, leave_reason=?, final_ping=?,
                   final_state_json=? WHERE player_session_id=?""",
                (e["ts"], data.get("duration_ms"), data.get("reason"), data.get("ping"),
                json.dumps(data.get("state"), ensure_ascii=False), e.get("player")),
            )
            self.connection.execute(
                "UPDATE round_players SET left_game_ms=? WHERE round_id=? AND player_session_id=?",
                (e.get("game_ms"), e.get("round"), e.get("player")),
            )
        elif event_type == "network_sample":
            self.connection.execute(
                "INSERT INTO network_samples(event_id, player_session_id, round_id, utc_timestamp, ping_ms) "
                "VALUES(last_insert_rowid(),?,?,?,?)",
                (e.get("player"), e.get("round") or None, e["ts"], data.get("ping_ms")),
            )
        elif event_type in {"state_sample", "highres_state_sample"}:
            if not e.get("player"):
                return
            self.connection.execute(
                """INSERT INTO state_samples(event_id, player_session_id, round_id, utc_timestamp,
                   game_ms, resolution_hz, x, y, velocity_x, velocity_y, hp, energy, team, state_json)
                   VALUES(last_insert_rowid(),?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e.get("player"), e.get("round") or None, e["ts"], e.get("game_ms"),
                 data.get("resolution_hz"), data.get("x"), data.get("y"), data.get("vx"),
                 data.get("vy"), data.get("hp"), data.get("energy"), data.get("team"),
                 json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
            )
        elif event_type == "highres_state_window":
            self.connection.execute(
                """INSERT INTO state_windows(event_id, player_session_id, round_id, utc_timestamp,
                   trigger, window_before_ms, samples_json)
                   VALUES(last_insert_rowid(),?,?,?,?,?,?)""",
                (e.get("player"), e.get("round") or None, e["ts"], data.get("trigger"),
                 data.get("window_before_ms"),
                 json.dumps(data.get("samples", []), ensure_ascii=False, separators=(",", ":"))),
            )
        elif event_type == "chat_message":
            self.connection.execute(
                """INSERT INTO chat_messages(event_id, player_session_id, round_id, utc_timestamp,
                   account_name, character_name, message, is_command, command_name, command_arguments,
                   map_name, player_count, state_json) VALUES(last_insert_rowid(),?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e.get("player"), e.get("round") or None, e["ts"], data.get("account_name"),
                 data.get("character_name"), data.get("message"), int(bool(data.get("is_command"))),
                 data.get("command"), data.get("command_arguments"), data.get("map_name"),
                 data.get("player_count"), json.dumps(data.get("state"), ensure_ascii=False)),
            )
        elif event_type == "stats_snapshot":
            if not e.get("player"):
                return
            stats = data.get("stats", {})
            baseline_row = self.connection.execute(
                """SELECT stats_json FROM player_stat_snapshots
                   WHERE player_session_id=? AND round_id=? AND checkpoint='round_start'
                   ORDER BY snapshot_id DESC LIMIT 1""",
                (e.get("player"), e.get("round") or None),
            ).fetchone()
            delta = None
            if baseline_row:
                baseline = json.loads(baseline_row[0])
                delta = {
                    key: value - baseline.get(key, 0)
                    for key, value in stats.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
            self.connection.execute(
                """INSERT INTO player_stat_snapshots(event_id, player_session_id, round_id,
                   utc_timestamp, checkpoint, stats_json, delta_json)
                   VALUES(last_insert_rowid(),?,?,?,?,?,?)""",
                (e.get("player"), e.get("round") or None, e["ts"], data.get("checkpoint"),
                 json.dumps(stats, ensure_ascii=False, separators=(",", ":")),
                 json.dumps(delta, ensure_ascii=False, separators=(",", ":")) if delta is not None else None),
            )
        elif event_type in {"scene_manifest_batch", "scene_frame_batch", "scene_highres_batch"}:
            self._store_scene_chunk(event_id, data)
            self._store_scene_batch(event_id, event_type, e, data)
        elif event_type == "scene_window_complete":
            self._store_scene_chunk(event_id, data)
            self.connection.execute(
                "INSERT OR REPLACE INTO scene_windows(source_event_id,round_id,trigger_game_ms,trigger,coverage,entities_json) VALUES(?,?,?,?,?,?)",
                (event_id, e.get("round") or "", e.get("game_ms") or 0, data.get("trigger", "unknown"),
                 float(data.get("coverage", 0)), json.dumps(data.get("entities", []), ensure_ascii=False, separators=(",", ":"))),
            )
        elif event_type in {"object_created", "object_damage", "object_terminated"}:
            snapshot = data.get("object") if isinstance(data.get("object"), dict) else data
            entity_id = self._scene_entity(e, event_id, "object", snapshot.get("object_id"), snapshot)
            if entity_id is not None and event_type == "object_terminated":
                self.connection.execute("UPDATE scene_entities SET terminated_event_id=? WHERE scene_entity_id=?", (event_id, entity_id))
            if event_type == "object_damage":
                self._store_object_damage(event_id, e, data)
        elif event_type == "object_damage_batch":
            records = data.get("records", [])
            if data.get("field_map") == "object_damage_v1":
                records = [self._object_damage_v1(record) for record in records if isinstance(record, list)]
            for record in records:
                if isinstance(record, dict):
                    self._store_object_damage(event_id, e, record)
        elif event_type == "melee_action":
            self._store_combat_event(event_id, event_type, data, e)
            for hit in data.get("hits", []):
                if isinstance(hit, dict) and not hit.get("is_player"):
                    target = self._scene_entity(e, event_id, "object", hit.get("object_id"), {})
                    interaction = "player_kick_object" if data.get("action") == "kick" else "player_melee_object"
                    self._insert_scene_interaction(event_id, e, interaction, "exact", data.get("attacker_session_id"), None, target, None, hit)
        elif event_type == "projectile_hit" and not data.get("is_player"):
            self._store_combat_event(event_id, event_type, data, e)
            projectile = data.get("projectile") if isinstance(data.get("projectile"), dict) else {}
            actor = self._scene_entity(e, event_id, "projectile", projectile.get("instance_id"), projectile)
            target = self._scene_entity(e, event_id, "object", data.get("hit_object_id"), {})
            self._insert_scene_interaction(event_id, e, "projectile_hit_object", "exact", data.get("attacker_session_id"), actor, target, None, data)
        elif event_type == "explosion_hit":
            self._store_combat_event(event_id, event_type, data, e)
            for hit in data.get("hits", []):
                if isinstance(hit, dict) and not hit.get("is_player"):
                    target = self._scene_entity(e, event_id, "object", hit.get("object_id"), {})
                    self._insert_scene_interaction(event_id, e, "explosion_hit_object", "exact", None, None, target, None, hit)
        elif event_type.startswith(("player_damage", "player_death", "melee_", "projectile_", "explosion_", "weapon_")):
            self._store_combat_event(event_id, event_type, data, e)

    def _store_combat_event(self, event_id: int, event_type: str, data: dict[str, Any], e: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO combat_events(event_id, attacker_session_id, victim_session_id,
               damage, damage_type, weapon, distance, context_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, data.get("attacker_session_id"), data.get("victim_session_id") or e.get("player"),
             data.get("damage"), data.get("damage_type"), data.get("weapon"), data.get("distance"),
             json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
        )
        self._insert_combat_hit_details(event_id, event_type, data, e)

    def _scene_entity(self, e: dict[str, Any], event_id: int, kind: str, engine_id: Any, snapshot: dict[str, Any]) -> int | None:
        try:
            engine_id = int(engine_id)
        except (TypeError, ValueError):
            return None
        self.connection.execute(
            """INSERT INTO scene_entities(server_session_id,round_id,entity_kind,engine_id,name,manifest_json,created_event_id)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(server_session_id,round_id,entity_kind,engine_id) DO UPDATE SET
               name=COALESCE(excluded.name,scene_entities.name), manifest_json=CASE WHEN excluded.manifest_json!='{}' THEN excluded.manifest_json ELSE scene_entities.manifest_json END""",
            (e["server_session"], e.get("round") or "", kind, engine_id, snapshot.get("name"),
             json.dumps(snapshot or {}, ensure_ascii=False, separators=(",", ":")), event_id),
        )
        return self.connection.execute("SELECT scene_entity_id FROM scene_entities WHERE server_session_id=? AND round_id=? AND entity_kind=? AND engine_id=?", (e["server_session"], e.get("round") or "", kind, engine_id)).fetchone()[0]

    def _store_scene_chunk(self, event_id: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = zlib.compress(raw, level=6)
        self.connection.execute("INSERT OR REPLACE INTO scene_chunks(event_id,codec,sha256,raw_bytes,compressed_bytes,payload) VALUES(?,?,?,?,?,?)", (event_id, "zlib", hashlib.sha256(raw).hexdigest(), len(raw), len(compressed), compressed))

    def _store_scene_batch(self, event_id: int, event_type: str, e: dict[str, Any], data: dict[str, Any]) -> None:
        entities = data.get("entities", data.get("objects", []))
        if not isinstance(entities, list):
            return
        for item in entities:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind", "object")
            entity_id = self._scene_entity(e, event_id, kind, item.get("object_id", item.get("instance_id")), item)
            if entity_id is None or event_type not in {"scene_frame_batch", "scene_highres_batch"}:
                continue
            self.connection.execute(
                """INSERT OR IGNORE INTO scene_samples(scene_entity_id,event_id,round_id,game_ms,x,y,velocity_x,velocity_y,angle,angular_velocity,health,max_health,is_missile,state_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entity_id, event_id, e.get("round") or "", item.get("game_ms", e.get("game_ms") or 0), item.get("x"), item.get("y"), item.get("vx"), item.get("vy"), item.get("angle"), item.get("angular_velocity"), item.get("health"), item.get("max_health"), int(bool(item.get("is_missile"))), json.dumps(item, ensure_ascii=False, separators=(",", ":"))),
            )

    @staticmethod
    def _object_damage_type(data: dict[str, Any]) -> str:
        value = str(data.get("damage_type", "unknown")).lower()
        if "explosion" in value:
            return "object_damage_explosion"
        if "projectile" in value:
            return "object_damage_projectile"
        return "object_damage_player" if data.get("source_is_player") else "object_impact_object"

    def _store_object_damage(self, event_id: int, e: dict[str, Any], data: dict[str, Any]) -> None:
        snapshot = data.get("object") if isinstance(data.get("object"), dict) else data
        entity_id = self._scene_entity(e, event_id, "object", snapshot.get("object_id"), snapshot)
        if entity_id is None:
            return
        interaction_type = self._object_damage_type(data)
        source_kind = "projectile" if interaction_type == "object_damage_projectile" else "object"
        actor_id = None if data.get("source_is_player") else self._scene_entity(e, event_id, source_kind, data.get("source_id"), {})
        event = {**e, "game_ms": data.get("game_ms", e.get("game_ms"))}
        self._insert_scene_interaction(event_id, event, interaction_type, "exact", data.get("source_player_session_id") or e.get("player"), actor_id, entity_id, None, data)

    @staticmethod
    def _object_damage_v1(record: list[Any]) -> dict[str, Any]:
        if len(record) != 14:
            return {}
        return {
            "object_id": record[0], "x": record[1], "y": record[2], "vx": record[3], "vy": record[4],
            "health": record[5], "max_health": record[6], "is_missile": record[7], "damage": record[8],
            "damage_type": record[9], "source_id": record[10], "source_is_player": record[11],
            "source_player_session_id": record[12], "game_ms": record[13],
        }

    def _insert_scene_interaction(self, event_id: int, e: dict[str, Any], interaction_type: str, quality: str, player_id: str | None, actor_id: int | None, target_id: int | None, target_player_id: str | None, details: dict[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO scene_interactions(event_id,round_id,utc_timestamp,game_ms,interaction_type,source_quality,player_session_id,actor_entity_id,target_entity_id,target_player_session_id,x,y,damage,distance,details_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, e.get("round") or None, e["ts"], e.get("game_ms"), interaction_type, quality, player_id, actor_id, target_id, target_player_id, details.get("x", details.get("hit_x")), details.get("y", details.get("hit_y")), details.get("damage"), details.get("distance"), json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
        )

    def _insert_combat_hit_details(self, event_id: int, event_type: str, data: dict[str, Any], e: dict[str, Any]) -> None:
        attacker = data.get("attacker_session_id")
        weapon = data.get("weapon")
        attacker_context = data.get("attacker")
        if not weapon and isinstance(attacker_context, dict):
            weapon = attacker_context.get("weapon")
        projectile = data.get("projectile")
        if not weapon and isinstance(projectile, dict):
            weapon = projectile.get("projectile")
        if event_type in {"player_damage", "projectile_hit"}:
            hits = [data]
        elif event_type in {"melee_action", "explosion_hit"}:
            hits = data.get("hits", [])
        else:
            return
        if not isinstance(hits, list):
            return
        for index, hit in enumerate(hits):
            if not isinstance(hit, dict):
                continue
            damage = hit.get("damage") if event_type != "melee_action" else hit.get("damage", hit.get("hit_damage"))
            damage_type = data.get("damage_type") or ("Melee" if event_type == "melee_action" else "Explosion" if event_type == "explosion_hit" else "Projectile")
            self.connection.execute(
                """INSERT OR REPLACE INTO combat_hit_details(event_id, hit_index, attacker_session_id,
                   victim_session_id, damage, damage_type, weapon, is_player, context_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (event_id, index, attacker, hit.get("victim_session_id") or data.get("victim_session_id") or e.get("player"),
                 damage, damage_type, weapon, int(bool(hit.get("is_player", data.get("is_player", True)))),
                 json.dumps(hit, ensure_ascii=False, separators=(",", ":"))),
            )

    def _upsert_player_session(self, e: dict[str, Any]) -> None:
        data, session_id = e["data"], e.get("player")
        identity = data.get("player_identity_id") or None
        if identity:
            self.connection.execute(
                """INSERT INTO players(player_identity_id, identity_confidence, first_seen_at, last_seen_at)
                   VALUES(?,?,?,?) ON CONFLICT(player_identity_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (identity, data.get("identity_confidence"), e["ts"], e["ts"]),
            )
        self.connection.execute(
            """INSERT INTO player_sessions(
                 player_session_id, server_session_id, player_identity_id, joined_at,
                 user_identifier, legacy_user_id, local_user_index, game_slot_index,
                 account_name, character_name, is_user, is_bot, is_host, is_moderator,
                 joined_as_spectator, initial_spectating, initial_team, initial_ping)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(player_session_id) DO UPDATE SET
                 account_name=excluded.account_name, character_name=excluded.character_name,
                 initial_team=excluded.initial_team""",
            (session_id, e["server_session"], identity, data.get("joined_at", e["ts"]),
             data.get("user_identifier"), data.get("legacy_user_id"), data.get("local_user_index"),
             data.get("game_slot_index"), data.get("account_name"), data.get("character_name"),
             int(bool(data.get("is_user"))), int(bool(data.get("is_bot"))), int(bool(data.get("is_host"))),
             int(bool(data.get("is_moderator"))), int(bool(data.get("joined_as_spectator"))),
             int(bool(data.get("spectating"))), data.get("team"), data.get("ping")),
        )
        for kind, value in (("account_name", data.get("account_name")), ("character_name", data.get("character_name"))):
            if value:
                self.connection.execute(
                    "INSERT OR IGNORE INTO player_aliases(player_identity_id, player_session_id, alias_type, alias, first_seen_at) VALUES(?,?,?,?,?)",
                    (identity, session_id, kind, value, e["ts"]),
                )
        if data.get("profile"):
            self.connection.execute(
                "INSERT INTO profiles(player_session_id, player_identity_id, captured_at, profile_json) VALUES(?,?,?,?)",
                (session_id, identity, e["ts"], json.dumps(data["profile"], ensure_ascii=False, separators=(",", ":"))),
            )

    def record_gap(self, server_session: str, expected: int, observed: int, detected_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO telemetry_gaps(server_session_id, expected_sequence, observed_sequence, detected_at) VALUES(?,?,?,?)",
                (server_session, expected, observed, detected_at),
            )
            self.connection.execute(
                "UPDATE storage_health SET gap_count=(SELECT COUNT(*) FROM telemetry_gaps),updated_at=? WHERE component='raw'",
                (detected_at,),
            )

    def last_sequences(self) -> dict[str, int]:
        return {row[0]: row[1] for row in self.connection.execute(
            "SELECT server_session_id, MAX(sequence) FROM events GROUP BY server_session_id"
        )}
