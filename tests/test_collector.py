from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.db import TelemetryDB
from collector.main import Collector
from collector.parser import PREFIX, TelemetryParseError, parse_shared_storage, parse_telemetry_line


def event(sequence: int, event_type: str = "test", player: str | None = None, data: dict | None = None) -> str:
    envelope = {
        "v": 1,
        "seq": sequence,
        "ts": f"2026-08-24T00:00:{sequence:02d}.000Z",
        "type": event_type,
        "server_session": "server-1",
        "round": "round-1",
        "player": player,
        "game_ms": sequence * 10,
        "data": data or {},
    }
    return PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def storage(*raw_lines: str) -> str:
    encoded = [base64.b64encode(line.encode("utf-8")).decode("ascii") for line in raw_lines]
    return "v.1.6.0.1|UTF8|header\nstring[]|slot_0|" + "|".join(encoded) + "\n"


class ParserTests(unittest.TestCase):
    def test_unicode_and_json_escaping_round_trip(self) -> None:
        raw = event(1, "chat_message", "player-1", {"message": "Привет | \\n \"SFD\""})
        parsed, malformed = parse_shared_storage(storage(raw))
        self.assertEqual(malformed, [])
        self.assertEqual(parsed[0].envelope["data"]["message"], "Привет | \\n \"SFD\"")
        self.assertEqual(parsed[0].raw_line, raw)

    def test_malformed_value_does_not_hide_valid_event(self) -> None:
        text = storage(event(1)) + "string[]|slot_1|not-base64!\n"
        parsed, malformed = parse_shared_storage(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(malformed), 1)

    def test_rejects_invalid_schema(self) -> None:
        with self.assertRaises(TelemetryParseError):
            parse_telemetry_line(PREFIX + '{"v":1}')


class DatabaseTests(unittest.TestCase):
    def test_duplicate_is_idempotent_and_specialized_rows_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = TelemetryDB(Path(directory) / "test.sqlite3", ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                join_data = {
                    "player_identity_id": "host-account:S0HASH:0",
                    "identity_confidence": "host_scoped_account_hash",
                    "user_identifier": 7,
                    "legacy_user_id": 7,
                    "local_user_index": 0,
                    "game_slot_index": 0,
                    "account_name": "Account",
                    "character_name": "Игрок",
                    "is_user": True,
                    "is_bot": False,
                    "is_host": False,
                    "is_moderator": False,
                    "joined_as_spectator": False,
                    "spectating": False,
                    "team": "Independent",
                    "ping": 42,
                    "profile": {"avatar_gender": "Male"},
                }
                item = parse_telemetry_line(event(1, "user_join", "player-1", join_data))
                self.assertEqual(db.insert_batch([item]), (1, 0))
                self.assertEqual(db.insert_batch([item]), (0, 1))
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                self.assertEqual(db.connection.execute("SELECT character_name FROM player_sessions").fetchone()[0], "Игрок")
                ping = parse_telemetry_line(event(2, "network_sample", "player-1", {"ping_ms": 123}))
                self.assertEqual(db.insert_batch([ping]), (1, 0))
                self.assertEqual(db.connection.execute("SELECT ping_ms FROM network_samples").fetchone()[0], 123)
            finally:
                db.close()

    def test_join_chat_damage_leave_poc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = TelemetryDB(Path(directory) / "poc.sqlite3", ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                join = {
                    "player_identity_id": "", "identity_confidence": "session_only",
                    "user_identifier": 2, "legacy_user_id": 2, "local_user_index": 0,
                    "game_slot_index": 1, "account_name": "A", "character_name": "Юникод",
                    "is_user": True, "is_bot": False, "is_host": False, "is_moderator": False,
                    "joined_as_spectator": False, "spectating": False, "team": "Independent", "ping": 50,
                }
                lines = [
                    event(1, "user_join", "poc-player", join),
                    event(2, "chat_message", "poc-player", {"message": "Привет", "is_command": False}),
                    event(3, "player_damage", "poc-player", {"victim_session_id": "poc-player", "damage": 12.5, "damage_type": "Melee"}),
                    event(4, "user_leave", "poc-player", {"reason": "Left", "duration_ms": 1000, "ping": 55}),
                ]
                parsed = [parse_telemetry_line(line) for line in lines]
                self.assertEqual(db.insert_batch(parsed), (4, 0))
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], 1)
                self.assertEqual(db.connection.execute("SELECT damage FROM combat_events").fetchone()[0], 12.5)
                self.assertEqual(db.connection.execute("SELECT leave_reason FROM player_sessions").fetchone()[0], "Left")
            finally:
                db.close()

    def test_normalizes_nested_combat_hits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = TelemetryDB(Path(directory) / "hits.sqlite3", ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                melee = parse_telemetry_line(event(1, "melee_action", "attacker", {
                    "attacker_session_id": "attacker", "attacker": {"weapon": "BAT"},
                    "hits": [{"victim_session_id": "victim-a", "damage": 12, "is_player": True},
                             {"victim_session_id": "victim-b", "damage": 8, "is_player": True}],
                }))
                projectile = parse_telemetry_line(event(2, "projectile_hit", "attacker", {
                    "attacker_session_id": "attacker", "victim_session_id": "victim-c", "damage": 15,
                    "is_player": True, "projectile": {"projectile": "UZI"},
                }))
                self.assertEqual(db.insert_batch([melee, projectile]), (2, 0))
                rows = db.connection.execute("SELECT victim_session_id,damage_type,weapon FROM combat_hit_details ORDER BY event_id,hit_index").fetchall()
                self.assertEqual([tuple(row) for row in rows], [("victim-a", "Melee", "BAT"), ("victim-b", "Melee", "BAT"), ("victim-c", "Projectile", "UZI")])
            finally:
                db.close()

    def test_incomplete_stats_snapshot_does_not_stop_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = TelemetryDB(Path(directory) / "stats.sqlite3", ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                item = parse_telemetry_line(event(1, "stats_snapshot", None, {"checkpoint": "death", "stats": {"TotalDives": 1}}))
                self.assertEqual(db.insert_batch([item]), (1, 0))
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM player_stat_snapshots").fetchone()[0], 0)
            finally:
                db.close()

    def test_scene_batches_are_compressed_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = TelemetryDB(Path(directory) / "scene.sqlite3", ROOT / "sql/schema.sql", ROOT / "sql/views.sql")
            try:
                manifest = parse_telemetry_line(event(1, "scene_manifest_batch", None, {"entities": [{"kind": "object", "object_id": 44, "name": "Barrel", "x": 1, "y": 2}]}))
                frame = parse_telemetry_line(event(2, "scene_frame_batch", None, {"entities": [{"kind": "object", "object_id": 44, "x": 3, "y": 2, "vx": 4, "vy": 0, "game_ms": 20}]}))
                melee = parse_telemetry_line(event(3, "melee_action", "p", {"attacker_session_id": "p", "action": "kick", "hits": [{"object_id": 44, "is_player": False, "damage": 4, "x": 3, "y": 2}]}))
                self.assertEqual(db.insert_batch([manifest, frame, melee]), (3, 0))
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM scene_chunks").fetchone()[0], 2)
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM scene_samples").fetchone()[0], 1)
                self.assertEqual(tuple(db.connection.execute("SELECT interaction_type,source_quality FROM scene_interactions").fetchone()), ("player_kick_object", "exact"))
            finally:
                db.close()


class CollectorTests(unittest.TestCase):
    def test_snapshot_replacement_restart_dedupe_and_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            spool = temp / "sfdtelemetry_v1.txt"
            config = {
                "_config_source": "test",
                "shared_storage_path": str(spool),
                "database_path": str(temp / "telemetry.sqlite3"),
                "raw_archive_enabled": True,
                "raw_archive_dir": str(temp / "raw"),
                "malformed_log_path": str(temp / "malformed.jsonl"),
                "batch_size": 2,
            }
            spool.write_text(storage(event(1), event(2)), encoding="utf-8")
            collector = Collector(config)
            try:
                self.assertEqual(collector.process_once(force=True)[:2], (2, 0))
                self.assertEqual(collector.process_once(force=True)[:2], (0, 0))
                spool.write_text(storage(event(2), event(4)), encoding="utf-8")
                self.assertEqual(collector.process_once(force=True)[:2], (1, 0))
                gap = collector.db.connection.execute(
                    "SELECT expected_sequence, observed_sequence FROM telemetry_gaps"
                ).fetchone()
                self.assertEqual(tuple(gap), (3, 4))
            finally:
                collector.close()
            restarted = Collector(config)
            try:
                self.assertEqual(restarted.process_once(force=True)[:2], (0, 0))
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
