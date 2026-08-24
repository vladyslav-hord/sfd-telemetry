from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def write_report(directory: str, report_date: str, payload: dict) -> Path:
    target = Path(directory) / f"{report_date}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def validate_report(payload: dict) -> None:
    required = {"schema_version", "report_date", "timezone", "generated_at", "data_cutoff", "analysis_run_id", "status", "data_quality", "server", "retention", "network", "maps", "rounds", "players", "weapons", "interactions", "chat", "patterns", "narrative", "limitations"}
    missing = required - set(payload)
    if missing or payload.get("schema_version") != 1:
        raise ValueError(f"Invalid daily report: missing={sorted(missing)}")
    if "raw_messages" in payload["chat"] or "messages" in payload["chat"]:
        raise ValueError("Daily report must not contain raw chat")


def base_report(day: str, timezone_name: str, run_id: str, cutoff: str, server: dict, maps: list, players: list, weapons: list, network: list, llm_status: str, interactions: list, rounds: list, patterns: list) -> dict:
    return {"schema_version": 1, "report_date": day, "timezone": timezone_name, "generated_at": datetime.now(timezone.utc).isoformat(), "data_cutoff": cutoff, "analysis_run_id": run_id, "status": {"deterministic": "complete", "llm": llm_status}, "data_quality": server.get("data_quality", {}), "server": server, "retention": server.get("retention", {}), "network": {"sessions": network}, "maps": maps, "rounds": rounds, "players": players, "weapons": weapons, "interactions": interactions, "chat": {"raw_messages_included": False}, "patterns": patterns, "narrative": None, "limitations": ["ConnectionIP, SteamID and packet loss are unavailable.", "Killer, assist and round results may be inferred or unknown; this report does not claim cheating or violations."]}
