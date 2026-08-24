from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


_ATOMIC_RETRIES = 4


def _fallback_path(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return target.with_name(f"{target.stem}.fallback-{stamp}-{uuid.uuid4().hex[:10]}{target.suffix}")


def _atomic_write_text(target: Path, content: str) -> Path:
    """Write a complete artifact, retaining the old file when Windows locks it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(_ATOMIC_RETRIES):
            try:
                os.replace(temporary, target)
                return target
            except PermissionError:
                if attempt + 1 == _ATOMIC_RETRIES:
                    break
                time.sleep(0.05 * (attempt + 1))
        fallback = _fallback_path(target)
        os.replace(temporary, fallback)
        return fallback
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_report(directory: str, report_date: str, payload: dict) -> Path:
    target = Path(directory) / f"{report_date}.json"
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    return _atomic_write_text(target, content)


def validate_report(payload: dict) -> None:
    # Schema v1 reports predating scene telemetry remain readable; absence means unavailable,
    # never that the old match had an empty scene.
    if "environment" not in payload:
        payload["environment"] = {"available": False, "episodes": [], "reason": "scene telemetry unavailable for this historical report"}
    required = {"schema_version", "report_date", "timezone", "generated_at", "data_cutoff", "analysis_run_id", "status", "data_quality", "server", "retention", "network", "maps", "rounds", "players", "weapons", "interactions", "environment", "chat", "patterns", "narrative", "limitations"}
    missing = required - set(payload)
    if missing or payload.get("schema_version") != 1:
        raise ValueError(f"Invalid daily report: missing={sorted(missing)}")
    if "raw_messages" in payload["chat"] or "messages" in payload["chat"]:
        raise ValueError("Daily report must not contain raw chat")


def base_report(day: str, timezone_name: str, run_id: str, cutoff: str, server: dict, maps: list, players: list, weapons: list, network: list, llm_status: str, interactions: list, rounds: list, patterns: list) -> dict:
    # The report keeps the public top-level groups, while avoiding three exact
    # copies of large derived objects inside server.
    server_view = dict(server)
    data_quality = server_view.pop("data_quality", {})
    retention = server_view.pop("retention", {})
    environment = server_view.pop("environment", {})
    return {"schema_version": 1, "report_date": day, "timezone": timezone_name, "generated_at": datetime.now(timezone.utc).isoformat(), "data_cutoff": cutoff, "analysis_run_id": run_id, "status": {"deterministic": "complete", "llm": llm_status}, "data_quality": data_quality, "server": server_view, "retention": retention, "network": {"sessions": network}, "maps": maps, "rounds": rounds, "players": players, "weapons": weapons, "interactions": interactions, "environment": environment, "chat": {"raw_messages_included": False}, "patterns": patterns, "narrative": None, "limitations": ["ConnectionIP, SteamID and packet loss are unavailable.", "Killer, assist and round results may be inferred or unknown; this report does not claim cheating or violations."]}
