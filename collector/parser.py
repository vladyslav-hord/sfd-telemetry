from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Iterable

PREFIX = "SFDTELEMETRY_V1|"


class TelemetryParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedEvent:
    envelope: dict[str, Any]
    raw_line: str


def _validate(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise TelemetryParseError("envelope is not an object")
    required = {
        "v": int,
        "seq": int,
        "ts": str,
        "type": str,
        "server_session": str,
        "round": str,
        "data": dict,
    }
    for key, expected in required.items():
        value = envelope.get(key)
        if isinstance(value, bool) or not isinstance(value, expected):
            raise TelemetryParseError(f"invalid {key}")
    if envelope["v"] != 1 or envelope["seq"] < 1:
        raise TelemetryParseError("unsupported version or sequence")
    if not envelope["type"] or not envelope["server_session"]:
        raise TelemetryParseError("empty type or server_session")
    player = envelope.get("player")
    if player is not None and not isinstance(player, str):
        raise TelemetryParseError("invalid player")
    game_ms = envelope.get("game_ms")
    if game_ms is not None and not isinstance(game_ms, (int, float)):
        raise TelemetryParseError("invalid game_ms")
    return envelope


def parse_telemetry_line(line: str) -> ParsedEvent:
    line = line.strip("\r\n")
    if not line.startswith(PREFIX):
        raise TelemetryParseError("prefix mismatch")
    try:
        payload = json.loads(line[len(PREFIX) :])
    except json.JSONDecodeError as exc:
        raise TelemetryParseError(f"invalid JSON: {exc.msg}") from exc
    return ParsedEvent(_validate(payload), line)


def parse_shared_storage(text: str) -> tuple[list[ParsedEvent], list[str]]:
    """Decode SFD string[] spool entries.

    Telemetry values are Base64, so SFD's own backslash escaping cannot alter the
    JSON. A full storage snapshot may contain duplicate rotating slots.
    """
    events: list[ParsedEvent] = []
    malformed: list[str] = []
    for storage_line in text.splitlines():
        if not storage_line.startswith("string[]|slot_"):
            continue
        parts = storage_line.split("|")
        for encoded in parts[2:]:
            if not encoded or encoded == r"\0\0":
                continue
            try:
                raw = base64.b64decode(encoded, validate=True).decode("utf-8")
                events.append(parse_telemetry_line(raw))
            except (binascii.Error, UnicodeDecodeError, TelemetryParseError) as exc:
                malformed.append(f"{type(exc).__name__}: {exc}; value={encoded[:256]}")
    events.sort(key=lambda item: (item.envelope["server_session"], item.envelope["seq"]))
    return events, malformed


def unique_events(events: Iterable[ParsedEvent]) -> list[ParsedEvent]:
    seen: set[tuple[str, int]] = set()
    result: list[ParsedEvent] = []
    for item in events:
        key = (item.envelope["server_session"], item.envelope["seq"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

