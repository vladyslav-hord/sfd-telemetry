from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

from .prompts import CHAT_MODERATION_SCHEMA, CHAT_PROMPT, CHAT_PROMPT_VERSION, CHAT_SCHEMA, GAMEPLAY_PROMPT, GAMEPLAY_PROMPT_VERSION, GAMEPLAY_SCHEMA, LEGACY_GAMEPLAY_PROMPT_VERSION, LIVE_MODERATION_PROMPT, LIVE_MODERATION_PROMPT_VERSION, LIVE_SCENE_PROMPT, LIVE_SCENE_PROMPT_VERSION, NARRATIVE_PROMPT, NARRATIVE_PROMPT_VERSION, NARRATIVE_SCHEMA, SCENE_CANDIDATE_SCHEMA


TERMINAL_REMOTE_STATUSES = {"completed", "failed", "cancelled", "incomplete", "expired"}
DEFAULT_ANALYSIS_VERSION = "llm-v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens(value: object) -> int:
    """Conservative preflight estimate used only for admission/budget checks."""
    encoded = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return max(1, math.ceil(len(encoded) / 4))


def _model_for_job(job, config) -> str:
    stored_model = job["model"] if hasattr(job, "keys") and "model" in job.keys() else job.get("model") if isinstance(job, dict) else None
    return str(stored_model or getattr(config, "live_llm_model", None) or config.openai_model)


def _prompt_cache_key(kind: str, prompt_version: str, model: str, config) -> str:
    analysis_version = getattr(config, "llm_analysis_version", DEFAULT_ANALYSIS_VERSION)
    return f"sfd:{analysis_version}:{kind}:{prompt_version}:{model}"


def _analysis_version(config) -> str:
    return str(getattr(config, "llm_analysis_version", DEFAULT_ANALYSIS_VERSION))


def _versioned_storage_name(name: str, config) -> str:
    """Avoid legacy SQLite unique constraints hiding an explicit re-analysis."""
    version = _analysis_version(config)
    return name if version == DEFAULT_ANALYSIS_VERSION else f"{name}@{version}"


def _base_storage_name(name: str) -> str:
    return name.rsplit("@", 1)[0]


def _request_analysis_version(request, config=None) -> str:
    """Recover the version stored with a request, including legacy rows."""
    if request is None:
        return DEFAULT_ANALYSIS_VERSION
    prompt_version = str(request["prompt_version"] or "")
    if "@" in prompt_version:
        return prompt_version.rsplit("@", 1)[1]
    return DEFAULT_ANALYSIS_VERSION


def _job_analysis_version(job, config=None) -> str:
    """Recover a live job version from its stored kind; legacy jobs are v1."""
    job_kind = str(job["job_kind"] or "") if job is not None else ""
    return job_kind.rsplit("@", 1)[1] if "@" in job_kind else DEFAULT_ANALYSIS_VERSION


def _usage(response: dict | None) -> dict[str, int]:
    if not isinstance(response, dict):
        return {}
    body = response.get("response", {}).get("body", response)
    value = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(value, dict):
        return {}
    input_details = value.get("input_tokens_details") or {}
    output_details = value.get("output_tokens_details") or {}
    keys = {
        "input_tokens": value.get("input_tokens"),
        "output_tokens": value.get("output_tokens"),
        "cached_tokens": value.get("cached_tokens", input_details.get("cached_tokens")),
        "cache_write_tokens": value.get("cache_write_tokens", input_details.get("cache_write_tokens")),
        "reasoning_tokens": value.get("reasoning_tokens", output_details.get("reasoning_tokens")),
        "total_tokens": value.get("total_tokens"),
    }
    return {key: int(item) for key, item in keys.items() if isinstance(item, (int, float))}


def _response_dump(response) -> dict:
    if isinstance(response, dict):
        return response
    return response.model_dump() if hasattr(response, "model_dump") else {}


def _compact_response(response: dict, usage: dict[str, int]) -> dict:
    body = response.get("response", {}).get("body", response)
    compact = {
        "id": response.get("id") or body.get("id"),
        "status": response.get("status") or body.get("status"),
        "model": response.get("model") or body.get("model"),
        "usage": usage or None,
    }
    parsed = extract_json(response)
    if parsed is not None:
        compact["output"] = parsed
    return {key: value for key, value in compact.items() if value is not None}


def _body_for_request(row, config) -> dict:
    if row["source_type"] == "chat":
        prompt, schema, name = CHAT_PROMPT, CHAT_SCHEMA, "chat_annotations"
        max_output = int(getattr(config, "max_chat_output_tokens_per_request", 256))
    elif row["source_type"] == "gameplay":
        prompt, schema, name = GAMEPLAY_PROMPT, GAMEPLAY_SCHEMA, "gameplay_pattern"
        max_output = int(getattr(config, "max_gameplay_output_tokens_per_request", 256))
    else:
        prompt, schema, name = NARRATIVE_PROMPT, NARRATIVE_SCHEMA, "daily_narrative"
        max_output = int(getattr(config, "max_narrative_output_tokens_per_request", 600))
    model = str(row["model"] or config.openai_model)
    content = row["request_json"]
    return {
        "model": model,
        "store": False,
        "reasoning": {"effort": getattr(config, "openai_reasoning_effort", "minimal")},
        "max_output_tokens": max_output,
        "prompt_cache_key": _prompt_cache_key(row["source_type"], row["prompt_version"], model, config),
        "input": [{"role": "developer", "content": prompt}, {"role": "user", "content": content}],
        "text": {"verbosity": getattr(config, "openai_verbosity", "low"), "format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
    }


def _logical_key(source_type: str, source_id: str, kind: str, config) -> str:
    version = _analysis_version(config)
    return f"request|{source_type}|{source_id}|{kind}|{version}"


def _claim_logical_request(analytics, source_type: str, source_id: str, kind: str, request_hash_value: str, now: str, config) -> bool:
    """Claim one logical source independently of model; safe across restarts."""
    key = _logical_key(source_type, source_id, kind, config)
    cursor = analytics.execute(
        "INSERT OR IGNORE INTO llm_logical_keys(logical_key,source_type,source_id,kind,analysis_version,request_hash,created_at) VALUES(?,?,?,?,?,?,?)",
        (key, source_type, source_id, kind, _analysis_version(config), request_hash_value, now),
    )
    return bool(cursor.rowcount)


def _upsert_ledger(analytics, *, ledger_key: str, source_type: str, source_id: str, kind: str, analysis_version: str, model: str, status: str, created_at: str, updated_at: str, request_hash_value: str | None = None, job_id: str | None = None, remote_id: str | None = None, remote_status: str | None = None, prompt_cache_key: str | None = None, payload_bytes: int = 0, estimated_input_tokens: int = 0, estimated_output_tokens: int = 0, input_tokens: int | None = None, output_tokens: int | None = None, cached_tokens: int | None = None, cache_write_tokens: int | None = None, reasoning_tokens: int | None = None, total_tokens: int | None = None, retry_count: int = 0, dedupe_hit: int = 0) -> None:
    analytics.execute(
        """INSERT INTO llm_cost_ledger(ledger_key,request_hash,job_id,source_type,source_id,kind,analysis_version,model,status,remote_id,remote_status,prompt_cache_key,payload_bytes,estimated_input_tokens,estimated_output_tokens,input_tokens,output_tokens,cached_tokens,cache_write_tokens,reasoning_tokens,total_tokens,retry_count,dedupe_hit,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ledger_key) DO UPDATE SET status=excluded.status,model=excluded.model,remote_id=COALESCE(excluded.remote_id,llm_cost_ledger.remote_id),remote_status=COALESCE(excluded.remote_status,llm_cost_ledger.remote_status),prompt_cache_key=COALESCE(excluded.prompt_cache_key,llm_cost_ledger.prompt_cache_key),payload_bytes=MAX(llm_cost_ledger.payload_bytes,excluded.payload_bytes),estimated_input_tokens=MAX(llm_cost_ledger.estimated_input_tokens,excluded.estimated_input_tokens),estimated_output_tokens=MAX(llm_cost_ledger.estimated_output_tokens,excluded.estimated_output_tokens),input_tokens=COALESCE(excluded.input_tokens,llm_cost_ledger.input_tokens),output_tokens=COALESCE(excluded.output_tokens,llm_cost_ledger.output_tokens),cached_tokens=COALESCE(excluded.cached_tokens,llm_cost_ledger.cached_tokens),cache_write_tokens=COALESCE(excluded.cache_write_tokens,llm_cost_ledger.cache_write_tokens),reasoning_tokens=COALESCE(excluded.reasoning_tokens,llm_cost_ledger.reasoning_tokens),total_tokens=COALESCE(excluded.total_tokens,llm_cost_ledger.total_tokens),retry_count=MAX(llm_cost_ledger.retry_count,excluded.retry_count),dedupe_hit=MAX(llm_cost_ledger.dedupe_hit,excluded.dedupe_hit),updated_at=excluded.updated_at""",
        (ledger_key, request_hash_value, job_id, source_type, source_id, kind, analysis_version, model, status, remote_id, remote_status, prompt_cache_key, payload_bytes, estimated_input_tokens, estimated_output_tokens, input_tokens, output_tokens, cached_tokens, cache_write_tokens, reasoning_tokens, total_tokens, retry_count, dedupe_hit, created_at, updated_at),
    )


def request_hash(source_type: str, source_id: str, prompt_version: str, model: str, payload: dict) -> str:
    raw = json.dumps([source_type, source_id, prompt_version, model, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _compact_value(value, *, depth: int = 0, max_items: int = 10, max_string: int = 600):
    if isinstance(value, str):
        return value[:max_string]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return None if isinstance(value, (dict, list)) else value
    if isinstance(value, list):
        return [_compact_value(item, depth=depth + 1, max_items=max_items, max_string=max_string) for item in value[:max_items]]
    if isinstance(value, dict):
        return {str(key): _compact_value(item, depth=depth + 1, max_items=max_items, max_string=max_string) for key, item in list(value.items())[:max_items]}
    return str(value)[:max_string]


def compact_narrative_payload(report_date: str, aggregates: dict, config) -> dict:
    """Keep narrative evidence bounded and remove raw/high-cardinality tails."""
    max_items = max(1, int(getattr(config, "max_narrative_items_per_section", 10)))
    compact = {"report_date": report_date, "aggregates": _compact_value(aggregates, max_items=max_items)}
    byte_limit = max(1000, int(getattr(config, "max_narrative_payload_bytes", 100000)))
    token_limit = max(250, int(getattr(config, "max_narrative_input_tokens", 25000)))
    while len(_json(compact).encode("utf-8")) > byte_limit or estimate_tokens(_json(compact)) > token_limit:
        if max_items > 2:
            max_items = max(2, max_items // 2)
            compact = {"report_date": report_date, "aggregates": _compact_value(aggregates, max_items=max_items, max_string=max(120, max_items * 60))}
            continue
        # A valid, bounded summary is preferable to silently uploading a raw report.
        compact = {"report_date": report_date, "aggregates": {"truncated": True, "sections": sorted(str(key) for key in aggregates)[:max_items]}}
        break
    return compact


def queue_chat_requests(analytics, telemetry, day_start: str, day_end: str, config) -> int:
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    rows = telemetry.execute("SELECT chat_id, player_session_id, round_id, utc_timestamp, account_name, message FROM chat_messages WHERE utc_timestamp>=? AND utc_timestamp<? ORDER BY chat_id", (day_start, day_end)).fetchall()
    if rows:
        ids = [int(row["chat_id"]) for row in rows]
        marks = ",".join("?" for _ in ids)
        annotated = {int(row[0]) for row in analytics.execute(f"SELECT chat_id FROM chat_annotations WHERE status='complete' AND chat_id IN ({marks})", ids)}
        rows = [row for row in rows if int(row["chat_id"]) not in annotated]
    if not rows:
        return 0
    # Live moderation is the canonical moderation path when explicitly enabled.
    # The legacy /moderations call otherwise re-processes the same chat stream.
    flagged = set() if getattr(config, "live_llm_enabled", False) else moderate_chat(analytics, rows, config)
    rows = sorted(rows, key=lambda row: (row["chat_id"] not in flagged, row["chat_id"]))
    now = datetime.now(timezone.utc).isoformat()
    queued = 0
    stored_prompt_version = _versioned_storage_name(CHAT_PROMPT_VERSION, config)
    for core, context in chat_windows(rows, config):
        payload = {"core_messages": [chat_payload(row, config) for row in core], "context_messages": [chat_payload(row, config) for row in context]}
        source_id = ",".join(str(row["chat_id"]) for row in core)
        key = request_hash("chat", source_id, stored_prompt_version, config.openai_model, payload)
        if not _claim_logical_request(analytics, "chat", source_id, CHAT_PROMPT_VERSION, key, now, config):
            _upsert_ledger(analytics, ledger_key=key, source_type="chat", source_id=source_id, kind=CHAT_PROMPT_VERSION, analysis_version=getattr(config, "llm_analysis_version", "llm-v1"), model=config.openai_model, status="deduped", created_at=now, updated_at=now, request_hash_value=key, payload_bytes=len(_json(payload).encode("utf-8")), estimated_input_tokens=estimate_tokens(_json(payload)), dedupe_hit=1)
            continue
        content = _json(payload)
        cursor = analytics.execute("INSERT OR IGNORE INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (key, "chat", source_id, stored_prompt_version, config.openai_model, content, now, now))
        _upsert_ledger(analytics, ledger_key=key, source_type="chat", source_id=source_id, kind=CHAT_PROMPT_VERSION, analysis_version=getattr(config, "llm_analysis_version", "llm-v1"), model=config.openai_model, status="queued", created_at=now, updated_at=now, request_hash_value=key, payload_bytes=len(content.encode("utf-8")), estimated_input_tokens=estimate_tokens(content), estimated_output_tokens=int(getattr(config, "max_chat_output_tokens_per_request", 256)))
        queued += cursor.rowcount
    analytics.commit()
    return queued


def chat_payload(row, config) -> dict:
    return {"chat_id": row["chat_id"], "actor_key": row["player_session_id"], "round_id": row["round_id"], "text": row["message"], "name": row["account_name"] if config.send_public_names else None}


def chat_windows(rows, config):
    """Core IDs never overlap; adjacent context is read-only and capped at 30 messages."""
    ordered = sorted(rows, key=lambda row: row["chat_id"])
    core_size = min(20, config.max_chat_messages_per_request)
    for start in range(0, len(ordered), core_size):
        core = ordered[start:start + core_size]
        context = ordered[max(0, start - 5):start] + ordered[start + len(core):start + len(core) + 5]
        while len(core) + len(context) > 30:
            context.pop()
        # Reject overly long windows before any remote request, preserving it for a later smaller run.
        if sum(len(row["message"] or "") for row in core + context) > config.max_input_chars_per_request:
            core = core[:1]
            context = []
        yield core, context


def reconcile_chat_annotations(analytics, telemetry) -> int:
    """Analytics must not retain an annotation once its telemetry chat row is gone."""
    source_ids = {row[0] for row in telemetry.execute("SELECT chat_id FROM chat_messages")}
    stale = [row[0] for row in analytics.execute("SELECT DISTINCT chat_id FROM chat_annotations") if row[0] not in source_ids]
    if stale:
        marks = ",".join("?" for _ in stale)
        analytics.execute(f"DELETE FROM chat_annotations WHERE chat_id IN ({marks})", stale)
        analytics.commit()
    return len(stale)


def _moderation_chunk_rows(rows, config=None):
    """Yield bounded moderation inputs without dropping unannotated messages."""
    message_limit = max(1, int(getattr(config, "max_moderation_messages_per_request", getattr(config, "max_chat_messages_per_request", 30))))
    byte_limit = max(1, int(getattr(config, "max_moderation_input_bytes_per_request", 20000)))
    token_limit = max(1, int(getattr(config, "max_moderation_input_tokens_per_request", 5000)))
    char_limit = max(1, int(getattr(config, "max_input_chars_per_request", 30000)))
    current_rows: list = []
    current: list[str] = []

    def fits(values: list[str]) -> bool:
        encoded = _json(values).encode("utf-8")
        return len(encoded) <= byte_limit and estimate_tokens(encoded) <= token_limit

    for row in rows:
        message = str(row["message"] or "")[:char_limit]
        while message and not fits([message]):
            message = message[:max(1, len(message) // 2)]
        if current and (len(current) >= message_limit or not fits(current + [message])):
            yield current_rows, current
            current_rows, current = [], []
        current_rows.append(row)
        current.append(message)
    if current:
        yield current_rows, current


def moderate_chat(analytics, rows, config=None) -> set[int]:
    """Moderation labels are analytic metadata, never an enforcement action."""
    if not rows or not os.getenv("OPENAI_API_KEY"):
        return set()
    try:
        ids = [int(row["chat_id"]) for row in rows]
        marks = ",".join("?" for _ in ids)
        annotated = {int(row[0]) for row in analytics.execute(f"SELECT chat_id FROM chat_annotations WHERE status='complete' AND chat_id IN ({marks})", ids)}
        rows = [row for row in rows if int(row["chat_id"]) not in annotated]
        if not rows:
            return set()
        from openai import OpenAI
        client = OpenAI()
        flagged: set[int] = set()
        now = datetime.now(timezone.utc).isoformat()
        for chunk_rows, messages in _moderation_chunk_rows(rows, config):
            response = client.moderations.create(model="omni-moderation-latest", input=messages)
            for row, result in zip(chunk_rows, getattr(response, "results", [])):
                values = {"flagged": bool(result.flagged), "categories": result.categories.model_dump() if hasattr(result.categories, "model_dump") else dict(result.categories), "category_scores": result.category_scores.model_dump() if hasattr(result.category_scores, "model_dump") else dict(result.category_scores)}
                if values["flagged"]: flagged.add(row["chat_id"])
                analytics.execute("INSERT INTO chat_annotations(chat_id,prompt_version,model,annotation_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id,prompt_version,model) DO UPDATE SET annotation_json=excluded.annotation_json,status=excluded.status,updated_at=excluded.updated_at", (row["chat_id"], "moderation_v1", "omni-moderation-latest", json.dumps(values), "complete", now, now))
        analytics.commit()
        return flagged
    except Exception:
        return set()


def queue_narrative_request(analytics, report_date: str, aggregates: dict, config) -> int:
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    payload = compact_narrative_payload(report_date, aggregates, config)
    stored_prompt_version = _versioned_storage_name(NARRATIVE_PROMPT_VERSION, config)
    key = request_hash("narrative", report_date, stored_prompt_version, config.openai_model, payload)
    now = datetime.now(timezone.utc).isoformat()
    if not _claim_logical_request(analytics, "narrative", report_date, NARRATIVE_PROMPT_VERSION, key, now, config):
        content = _json(payload)
        _upsert_ledger(analytics, ledger_key=key, source_type="narrative", source_id=report_date, kind=NARRATIVE_PROMPT_VERSION, analysis_version=getattr(config, "llm_analysis_version", "llm-v1"), model=config.openai_model, status="deduped", created_at=now, updated_at=now, request_hash_value=key, payload_bytes=len(content.encode("utf-8")), estimated_input_tokens=estimate_tokens(content), dedupe_hit=1)
        return 0
    content = _json(payload)
    cursor = analytics.execute("INSERT OR IGNORE INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (key, "narrative", report_date, stored_prompt_version, config.openai_model, content, now, now))
    _upsert_ledger(analytics, ledger_key=key, source_type="narrative", source_id=report_date, kind=NARRATIVE_PROMPT_VERSION, analysis_version=getattr(config, "llm_analysis_version", "llm-v1"), model=config.openai_model, status="queued", created_at=now, updated_at=now, request_hash_value=key, payload_bytes=len(content.encode("utf-8")), estimated_input_tokens=estimate_tokens(content), estimated_output_tokens=int(getattr(config, "max_narrative_output_tokens_per_request", 600)))
    analytics.commit()
    return cursor.rowcount


_GAMEPLAY_IDENTIFIER_KEYS = {
    "player_session_id", "player_identity_id", "session_id", "actor_key", "account_id",
    "steam_id", "persistent_id", "persistent_identifier", "display_name", "account_name",
    "object_id", "target_entity_id", "scene_interaction_id", "interaction_id",
}
_GAMEPLAY_RAW_KEYS = {"raw_json", "data_json", "payload_json", "state_json"}


def _sanitize_gameplay_value(value, *, depth: int = 0):
    if depth > 3:
        return None
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, list):
        return [_sanitize_gameplay_value(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key): _sanitize_gameplay_value(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in _GAMEPLAY_IDENTIFIER_KEYS
            and str(key).lower() not in _GAMEPLAY_RAW_KEYS
            and "session" not in str(key).lower()
            and "identity" not in str(key).lower()
        }
    return str(value)[:240]


def _gameplay_payload(item: dict, source_id: str, report_date: str) -> dict:
    evidence = item.get("source_event_ids", item.get("evidence_event_ids", []))
    evidence = [value for value in evidence if isinstance(value, int)][:20] if isinstance(evidence, list) else []
    window = {"window_id": source_id, "player_label": "Player 1", "evidence_event_ids": evidence}
    for key, value in item.items():
        normalized = str(key).lower()
        if normalized in _GAMEPLAY_IDENTIFIER_KEYS or normalized in _GAMEPLAY_RAW_KEYS:
            continue
        if "session" in normalized or "identity" in normalized or key in {"source_window_id", "source_event_ids", "evidence_event_ids"}:
            continue
        window[str(key)] = _sanitize_gameplay_value(value)
    return {"report_date": report_date, "window": window}


def _contains_gameplay_identifier(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _GAMEPLAY_IDENTIFIER_KEYS or normalized in _GAMEPLAY_RAW_KEYS or "session" in normalized or "identity" in normalized:
                return True
            if _contains_gameplay_identifier(item):
                return True
    elif isinstance(value, list):
        return any(_contains_gameplay_identifier(item) for item in value[:100])
    return False


def supersede_legacy_gameplay(analytics) -> int:
    """Retire only unsent legacy gameplay payloads; accepted remote work is immutable."""
    now = datetime.now(timezone.utc).isoformat()
    superseded = 0
    for row in analytics.execute("SELECT * FROM llm_requests WHERE source_type='gameplay' AND status='queued'").fetchall():
        try:
            payload = json.loads(row["request_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        legacy = _base_storage_name(str(row["prompt_version"] or "")) == LEGACY_GAMEPLAY_PROMPT_VERSION
        if not legacy and not _contains_gameplay_identifier(payload):
            continue
        updated = analytics.execute("UPDATE llm_requests SET status='superseded',response_json=?,updated_at=? WHERE request_hash=? AND status='queued'", (_json({"status": "superseded", "reason": "legacy_gameplay_payload"}), now, row["request_hash"]))
        if not updated.rowcount:
            continue
        _upsert_ledger(analytics, ledger_key=row["request_hash"], source_type="gameplay", source_id=row["source_id"], kind=row["prompt_version"], analysis_version=_request_analysis_version(row), model=row["model"], status="superseded", created_at=row["created_at"], updated_at=now, request_hash_value=row["request_hash"], payload_bytes=len((row["request_json"] or "").encode("utf-8")), estimated_input_tokens=estimate_tokens(row["request_json"] or ""))
        superseded += 1
    for row in analytics.execute("SELECT * FROM llm_jobs WHERE source_type='gameplay' AND status='queued'").fetchall():
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        legacy = _base_storage_name(str(row["job_kind"] or "")) == LEGACY_GAMEPLAY_PROMPT_VERSION
        if not legacy and not _contains_gameplay_identifier(payload):
            continue
        updated = analytics.execute("UPDATE llm_jobs SET status='superseded',next_attempt_at=NULL,updated_at=? WHERE job_id=? AND status='queued'", (now, row["job_id"]))
        if not updated.rowcount:
            continue
        _upsert_ledger(analytics, ledger_key=f"job:{row['job_id']}", source_type="gameplay", source_id=row["source_id"], kind=row["job_kind"], analysis_version=_job_analysis_version(row), model=row["model"], status="superseded", created_at=row["created_at"], updated_at=now, job_id=row["job_id"], payload_bytes=len((row["payload_json"] or "").encode("utf-8")), estimated_input_tokens=estimate_tokens(row["payload_json"] or ""))
        superseded += 1
    analytics.commit()
    return superseded


def queue_gameplay_requests(analytics, report_date: str, windows: list[dict], config) -> int:
    """Queue compact high-coverage candidates only; raw matches and full telemetry never leave SQLite."""
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    supersede_legacy_gameplay(analytics)
    now, queued = datetime.now(timezone.utc).isoformat(), 0
    stored_prompt_version = _versioned_storage_name(GAMEPLAY_PROMPT_VERSION, config)
    eligible = [item for item in windows if item.get("coverage", item.get("features", {}).get("coverage", 0)) >= config.anomaly_min_sample_coverage]
    eligible.sort(key=lambda item: abs(item.get("robust_z") or 0), reverse=True)
    for item in eligible[:config.max_llm_anomaly_windows_per_day]:
        source_id = str(item["source_window_id"])
        payload = _gameplay_payload(item, source_id, report_date)
        key = request_hash("gameplay", source_id, stored_prompt_version, config.openai_model, payload)
        if not _claim_logical_request(analytics, "gameplay", source_id, GAMEPLAY_PROMPT_VERSION, key, now, config):
            content = _json(payload)
            _upsert_ledger(analytics, ledger_key=key, source_type="gameplay", source_id=source_id, kind=GAMEPLAY_PROMPT_VERSION, analysis_version=getattr(config, "llm_analysis_version", "llm-v1"), model=config.openai_model, status="deduped", created_at=now, updated_at=now, request_hash_value=key, payload_bytes=len(content.encode("utf-8")), estimated_input_tokens=estimate_tokens(content), dedupe_hit=1)
            continue
        content = _json(payload)
        cursor = analytics.execute("INSERT OR IGNORE INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (key, "gameplay", source_id, stored_prompt_version, config.openai_model, content, now, now))
        _upsert_ledger(analytics, ledger_key=key, source_type="gameplay", source_id=source_id, kind=GAMEPLAY_PROMPT_VERSION, analysis_version=getattr(config, "llm_analysis_version", "llm-v1"), model=config.openai_model, status="queued", created_at=now, updated_at=now, request_hash_value=key, payload_bytes=len(content.encode("utf-8")), estimated_input_tokens=estimate_tokens(content), estimated_output_tokens=int(getattr(config, "max_gameplay_output_tokens_per_request", 256)))
        queued += cursor.rowcount
    analytics.commit()
    return queued


def submit_pending_batches(analytics, config) -> str:
    supersede_legacy_gameplay(analytics)
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return "disabled"
    pending = analytics.execute("SELECT * FROM llm_requests WHERE status='queued' ORDER BY created_at").fetchall()
    if not pending:
        return "complete"
    now = datetime.now(timezone.utc)
    day_start = now.date().isoformat()
    used_input = used_output = used_requests = 0
    for existing in analytics.execute("SELECT * FROM llm_requests WHERE created_at>=? AND status IN ('submitted','complete')", (day_start,)).fetchall():
        body = _body_for_request(existing, config)
        used_input += estimate_tokens(_json(body))
        used_output += int(getattr(config, "max_chat_output_tokens_per_request", 256) if existing["source_type"] == "chat" else getattr(config, "max_gameplay_output_tokens_per_request", 256) if existing["source_type"] == "gameplay" else getattr(config, "max_narrative_output_tokens_per_request", 600))
        used_requests += 1
    max_requests = max(1, int(getattr(config, "max_batch_requests_per_run", 50)))
    max_input_tokens = max(1, int(getattr(config, "max_batch_input_tokens_per_day", 500000)))
    max_output_tokens = max(1, int(getattr(config, "max_batch_output_tokens_per_day", 100000)))
    max_batch_bytes = max(10000, int(getattr(config, "max_batch_input_bytes_per_batch", 8000000)))
    selected, lines = [], []
    total_bytes = 0
    selected_model = None
    for row in pending:
        if used_requests + len(selected) >= max_requests:
            break
        if selected_model is not None and row["model"] != selected_model:
            continue
        body = _body_for_request(row, config)
        line = _json({"custom_id": row["request_hash"], "method": "POST", "url": "/v1/responses", "body": body})
        input_tokens = estimate_tokens(_json(body))
        output_tokens = int(getattr(config, "max_chat_output_tokens_per_request", 256) if row["source_type"] == "chat" else getattr(config, "max_gameplay_output_tokens_per_request", 256) if row["source_type"] == "gameplay" else getattr(config, "max_narrative_output_tokens_per_request", 600))
        if used_input + sum(estimate_tokens(_json(_body_for_request(item, config))) for item in selected) + input_tokens > max_input_tokens:
            continue
        if used_output + sum(int(getattr(config, "max_chat_output_tokens_per_request", 256) if item["source_type"] == "chat" else getattr(config, "max_gameplay_output_tokens_per_request", 256) if item["source_type"] == "gameplay" else getattr(config, "max_narrative_output_tokens_per_request", 600)) for item in selected) + output_tokens > max_output_tokens:
            continue
        if total_bytes + len(line.encode("utf-8")) + 1 > max_batch_bytes:
            continue
        selected_model = selected_model or row["model"]
        selected.append(row)
        lines.append(line)
        total_bytes += len(line.encode("utf-8")) + 1
    if not selected:
        return "budget_exceeded"
    try:
        from openai import OpenAI
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as file:
            file.write("\n".join(lines) + "\n")
            name = file.name
        try:
            client = OpenAI()
            with open(name, "rb") as upload_file:
                uploaded = client.files.create(file=upload_file, purpose="batch")
            remote = client.batches.create(input_file_id=uploaded.id, endpoint="/v1/responses", completion_window="24h")
        finally:
            os.unlink(name)
        batch_id, now = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
        analytics.execute("INSERT INTO llm_batches(batch_id,remote_batch_id,status,created_at,metadata_json) VALUES(?,?,?,?,?)", (batch_id, remote.id, remote.status, now, json.dumps({"input_file_id": uploaded.id})))
        analytics.executemany("UPDATE llm_requests SET batch_id=?,status='submitted',updated_at=?,attempts=attempts+1 WHERE request_hash=? AND status='queued'", [(batch_id, now, row["request_hash"]) for row in selected])
        for row in selected:
            body = _body_for_request(row, config)
            _upsert_ledger(analytics, ledger_key=row["request_hash"], source_type=row["source_type"], source_id=row["source_id"], kind=row["prompt_version"], analysis_version=_request_analysis_version(row, config), model=row["model"], status="submitted", created_at=row["created_at"], updated_at=now, request_hash_value=row["request_hash"], payload_bytes=len(row["request_json"].encode("utf-8")), estimated_input_tokens=estimate_tokens(_json(body)), estimated_output_tokens=int(getattr(config, "max_chat_output_tokens_per_request", 256) if row["source_type"] == "chat" else getattr(config, "max_gameplay_output_tokens_per_request", 256) if row["source_type"] == "gameplay" else getattr(config, "max_narrative_output_tokens_per_request", 600)))
        analytics.commit()
        return "pending"
    except Exception:
        # Deterministic reports remain successful; queue is retained for a later run.
        return "partial"


def sync_batches(analytics, report_directory: str | None = None) -> int:
    if not os.getenv("OPENAI_API_KEY"):
        return 0
    try:
        from openai import OpenAI
        client, imported = OpenAI(), 0
        for batch in analytics.execute("""SELECT DISTINCT b.* FROM llm_batches b
            LEFT JOIN llm_requests r ON r.batch_id=b.batch_id
            WHERE b.status NOT IN ('completed','failed','cancelled')
               OR r.status='submitted'""").fetchall():
            remote = client.batches.retrieve(batch["remote_batch_id"])
            analytics.execute("UPDATE llm_batches SET status=?,completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END WHERE batch_id=?", (remote.status, remote.status, datetime.now(timezone.utc).isoformat(), batch["batch_id"]))
            if remote.status != "completed" or not remote.output_file_id:
                continue
            content = client.files.content(remote.output_file_id).text
            for line in content.splitlines():
                result = json.loads(line)
                key = result.get("custom_id")
                if key:
                    usage = _usage(result)
                    compact_result = _compact_response(result, usage)
                    annotations = extract_annotations(result)
                    request = analytics.execute("SELECT source_type,source_id,prompt_version,model,attempts,batch_id FROM llm_requests WHERE request_hash=?", (key,)).fetchone()
                    analysis_version = _request_analysis_version(request)
                    if request is not None and request["batch_id"] != batch["batch_id"]:
                        continue
                    if request is None or (request["source_type"] == "chat" and annotations is None):
                        updated = datetime.now(timezone.utc).isoformat()
                        analytics.execute("UPDATE llm_requests SET status='malformed',response_json=?,updated_at=? WHERE request_hash=?", (_json(compact_result), updated, key))
                        if request is not None:
                            _upsert_ledger(analytics, ledger_key=key, source_type=request["source_type"], source_id=request["source_id"], kind=request["prompt_version"], analysis_version=analysis_version, model=request["model"], status="malformed", created_at=updated, updated_at=updated, request_hash_value=key, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"), reasoning_tokens=usage.get("reasoning_tokens"), total_tokens=usage.get("total_tokens"))
                        continue
                    if request["source_type"] == "chat" and valid_chat_annotations(annotations, {int(value) for value in request["source_id"].split(",")}):
                        for annotation in annotations:
                            analytics.execute("INSERT INTO chat_annotations(chat_id,prompt_version,model,annotation_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id,prompt_version,model) DO UPDATE SET annotation_json=excluded.annotation_json,status=excluded.status,updated_at=excluded.updated_at", (annotation["chat_id"], request["prompt_version"], request["model"], json.dumps(annotation, ensure_ascii=False), "complete", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
                        updated = datetime.now(timezone.utc).isoformat()
                        analytics.execute("UPDATE llm_requests SET status='complete',response_json=?,updated_at=? WHERE request_hash=?", (_json(compact_result), updated, key))
                        _upsert_ledger(analytics, ledger_key=key, source_type=request["source_type"], source_id=request["source_id"], kind=request["prompt_version"], analysis_version=analysis_version, model=request["model"], status="complete", created_at=updated, updated_at=updated, request_hash_value=key, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"), reasoning_tokens=usage.get("reasoning_tokens"), total_tokens=usage.get("total_tokens"))
                        imported += 1
                    elif request["source_type"] == "narrative" and (narrative := extract_json(result)) is not None and valid_narrative(narrative):
                        report = analytics.execute("SELECT metric_version,report_json FROM daily_reports WHERE report_date=? ORDER BY metric_version DESC LIMIT 1", (request["source_id"],)).fetchone()
                        if report:
                            data = json.loads(report["report_json"]); data["narrative"] = narrative; data["status"]["llm"] = "complete"
                            analytics.execute("UPDATE daily_reports SET report_json=?,llm_status='complete',generated_at=? WHERE report_date=? AND metric_version=?", (json.dumps(data, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), request["source_id"], report["metric_version"]))
                            if report_directory:
                                from .report import write_report
                                write_report(report_directory, request["source_id"], data)
                        updated = datetime.now(timezone.utc).isoformat()
                        analytics.execute("UPDATE llm_requests SET status='complete',response_json=?,updated_at=? WHERE request_hash=?", (_json(compact_result), updated, key))
                        _upsert_ledger(analytics, ledger_key=key, source_type=request["source_type"], source_id=request["source_id"], kind=request["prompt_version"], analysis_version=analysis_version, model=request["model"], status="complete", created_at=updated, updated_at=updated, request_hash_value=key, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"), reasoning_tokens=usage.get("reasoning_tokens"), total_tokens=usage.get("total_tokens")); imported += 1
                    elif request["source_type"] == "gameplay" and (gameplay := extract_json(result)) is not None and valid_gameplay(gameplay, request["source_id"]):
                        updated = datetime.now(timezone.utc).isoformat()
                        analytics.execute("UPDATE llm_requests SET status='complete',response_json=?,updated_at=? WHERE request_hash=?", (_json(compact_result), updated, key))
                        _upsert_ledger(analytics, ledger_key=key, source_type=request["source_type"], source_id=request["source_id"], kind=request["prompt_version"], analysis_version=analysis_version, model=request["model"], status="complete", created_at=updated, updated_at=updated, request_hash_value=key, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"), reasoning_tokens=usage.get("reasoning_tokens"), total_tokens=usage.get("total_tokens")); imported += 1
                    else:
                        status = "queued" if request["attempts"] < 3 else "malformed"
                        updated = datetime.now(timezone.utc).isoformat()
                        analytics.execute("UPDATE llm_requests SET status=?,response_json=?,updated_at=? WHERE request_hash=?", (status, _json(compact_result), updated, key))
                        _upsert_ledger(analytics, ledger_key=key, source_type=request["source_type"], source_id=request["source_id"], kind=request["prompt_version"], analysis_version=analysis_version, model=request["model"], status=status, created_at=updated, updated_at=updated, request_hash_value=key, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"), reasoning_tokens=usage.get("reasoning_tokens"), total_tokens=usage.get("total_tokens"))
        analytics.commit()
        return imported
    except Exception:
        return 0


def extract_annotation(batch_result: dict) -> dict | None:
    """Handle the Responses Batch envelope without trusting model-shaped data."""
    try:
        parsed = extract_json(batch_result)
        if parsed is None:
            return None
        annotations = parsed.get("annotations")
        return annotations[0] if isinstance(annotations, list) and len(annotations) == 1 else None
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def extract_annotations(batch_result: dict) -> list[dict] | None:
    parsed = extract_json(batch_result)
    annotations = parsed.get("annotations") if parsed else None
    return annotations if isinstance(annotations, list) else None


def extract_json(batch_result: dict) -> dict | None:
    try:
        body = batch_result.get("response", {}).get("body", batch_result)
        text = body.get("output_text")
        if not text:
            text = next(content["text"] for item in body.get("output", []) for content in item.get("content", []) if content.get("type") == "output_text")
        return json.loads(text)
    except (KeyError, StopIteration, TypeError, json.JSONDecodeError):
        return None


def valid_chat_annotation(value: dict, expected_chat_id: int) -> bool:
    required = {"chat_id", "language", "intent", "sentiment", "topics", "toxicity", "target_actor_keys", "reply_to_chat_id", "explicit_leave_reason", "confidence", "evidence_chat_ids"}
    toxicity = {"score", "profanity", "harassment", "threat", "hate", "sexual", "spam", "targeted"}
    return set(value) == required and value.get("chat_id") == expected_chat_id and isinstance(value["topics"], list) and isinstance(value["target_actor_keys"], list) and isinstance(value["evidence_chat_ids"], list) and set(value["toxicity"]) == toxicity and set(value["explicit_leave_reason"]) == {"present", "category"} and 0 <= float(value["confidence"]) <= 1


def valid_chat_annotations(values: list[dict], expected_ids: set[int]) -> bool:
    return {item.get("chat_id") for item in values} == expected_ids and len(values) == len(expected_ids) and all(valid_chat_annotation(item, item["chat_id"]) for item in values)


def valid_narrative(value: dict) -> bool:
    keys = {"headline", "server_health", "player_experience", "map_findings", "network_findings", "player_highlights", "pattern_findings", "chat_findings", "possible_factors", "limitations"}
    return set(value) == keys and isinstance(value["headline"], str) and all(isinstance(value[key], list) and all(isinstance(item, str) for item in value[key]) for key in keys - {"headline"})


def valid_gameplay(value: dict, expected_window_id: str) -> bool:
    required = {"window_id", "classification", "mechanic_family", "novelty_score", "advantage_observed", "advantage_description", "observations", "known_pattern_id", "candidate_signature_features", "confidence", "evidence_event_ids", "should_create_candidate"}
    return set(value) == required and value.get("window_id") == expected_window_id and 0 <= float(value["confidence"]) <= 1 and 0 <= float(value["novelty_score"]) <= 1 and isinstance(value["observations"], list) and isinstance(value["evidence_event_ids"], list)


def near_live_request_body(job, config) -> dict:
    """Build the background Responses request without including raw telemetry frames."""
    kind = _base_storage_name(job["job_kind"])
    moderation = kind == "moderation"
    schema = CHAT_MODERATION_SCHEMA if moderation else SCENE_CANDIDATE_SCHEMA
    prompt = LIVE_MODERATION_PROMPT if moderation else LIVE_SCENE_PROMPT
    prompt_version = LIVE_MODERATION_PROMPT_VERSION if moderation else LIVE_SCENE_PROMPT_VERSION
    model = str(getattr(config, "live_llm_model", None) or config.openai_model)
    return {
        "model": model,
        "background": True,
        "store": False,
        "max_output_tokens": int(getattr(config, "live_llm_max_output_tokens", 128)),
        "prompt_cache_key": _prompt_cache_key(kind, prompt_version, model, config),
        "input": [{"role": "developer", "content": prompt}, {"role": "user", "content": job["payload_json"]}],
        "reasoning": {"effort": getattr(config, "openai_reasoning_effort", "minimal")},
        "text": {"verbosity": getattr(config, "openai_verbosity", "low"), "format": {"type": "json_schema", "name": "sfd_live_result", "strict": True, "schema": schema}},
    }


def _live_job_kind_version(job_kind: str) -> str:
    return LIVE_MODERATION_PROMPT_VERSION if _base_storage_name(job_kind) == "moderation" else LIVE_SCENE_PROMPT_VERSION


def reconcile_near_live_jobs(analytics, config, client=None) -> int:
    """Poll submitted Responses jobs and persist terminal state plus compact usage."""
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI()
        rows = analytics.execute("SELECT * FROM llm_jobs WHERE status='submitted' ORDER BY updated_at LIMIT 100").fetchall()
        completed = 0
        for job in rows:
            ledger = analytics.execute("SELECT remote_id FROM llm_cost_ledger WHERE job_id=? ORDER BY updated_at DESC LIMIT 1", (job["job_id"],)).fetchone()
            remote_id = ledger["remote_id"] if ledger else None
            if not remote_id:
                previous = analytics.execute("SELECT result_json FROM llm_results WHERE job_id=? ORDER BY result_id DESC LIMIT 1", (job["job_id"],)).fetchone()
                if previous:
                    try:
                        remote_id = json.loads(previous["result_json"]).get("id")
                    except (TypeError, json.JSONDecodeError):
                        remote_id = None
            if not remote_id:
                continue
            response = client.responses.retrieve(remote_id)
            dumped = _response_dump(response)
            remote_status = str(dumped.get("status") or "unknown")
            usage = _usage(dumped)
            compact = _compact_response(dumped, usage)
            now = datetime.now(timezone.utc).isoformat()
            terminal = remote_status in TERMINAL_REMOTE_STATUSES
            job_status = "complete" if remote_status == "completed" else remote_status if terminal else "submitted"
            with analytics:
                analytics.execute("UPDATE llm_jobs SET status=?,next_attempt_at=NULL,updated_at=? WHERE job_id=? AND status='submitted'", (job_status, now, job["job_id"]))
                changed = analytics.execute("UPDATE llm_results SET model=?,status=?,result_json=?,created_at=? WHERE result_id=(SELECT result_id FROM llm_results WHERE job_id=? ORDER BY result_id DESC LIMIT 1)", (dumped.get("model") or _model_for_job(job, config), job_status, _json(compact), now, job["job_id"]))
                if not changed.rowcount:
                    analytics.execute("INSERT INTO llm_results(job_id,model,status,result_json,created_at) VALUES(?,?,?,?,?)", (job["job_id"], dumped.get("model") or _model_for_job(job, config), job_status, _json(compact), now))
                body = near_live_request_body(job, config)
                _upsert_ledger(analytics, ledger_key=f"job:{job['job_id']}", source_type=job["source_type"], source_id=job["source_id"], kind=job["job_kind"], analysis_version=_job_analysis_version(job, config), model=dumped.get("model") or body["model"], status=job_status, created_at=job["created_at"], updated_at=now, job_id=job["job_id"], remote_id=remote_id, remote_status=remote_status, prompt_cache_key=body.get("prompt_cache_key"), payload_bytes=len(job["payload_json"].encode("utf-8")), estimated_input_tokens=estimate_tokens(_json(body)), estimated_output_tokens=int(body.get("max_output_tokens", 0)), input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_tokens"), cache_write_tokens=usage.get("cache_write_tokens"), reasoning_tokens=usage.get("reasoning_tokens"), total_tokens=usage.get("total_tokens"), retry_count=job["attempts"])
                if terminal and _base_storage_name(job["job_kind"]) == "moderation":
                    parsed = extract_json(dumped) or {}
                    evidence = parsed.get("evidence_message_ids") if isinstance(parsed, dict) else []
                    if isinstance(evidence, list):
                        for chat_id in evidence:
                            if isinstance(chat_id, int):
                                analytics.execute("INSERT INTO chat_annotations(chat_id,prompt_version,model,annotation_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id,prompt_version,model) DO UPDATE SET annotation_json=excluded.annotation_json,status=excluded.status,updated_at=excluded.updated_at", (chat_id, LIVE_MODERATION_PROMPT_VERSION, dumped.get("model") or body["model"], _json(parsed), "complete", now, now))
            completed += int(terminal)
        return completed
    except Exception:
        analytics.rollback()
        return 0


def submit_near_live_jobs(analytics, config) -> int:
    """Submit bounded live jobs and never resubmit an accepted background response."""
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    try:
        reconcile_near_live_jobs(analytics, config)
    except Exception:
        analytics.rollback()
    if not getattr(config, "live_llm_enabled", False):
        return 0
    # Retire pre-v2 gameplay rows before the live selector can see them.
    # Submitted/completed rows are intentionally outside this migration.
    supersede_legacy_gameplay(analytics)
    now = datetime.now(timezone.utc)
    rows = analytics.execute(
        """SELECT * FROM llm_jobs
           WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
           ORDER BY created_at LIMIT 100""", (now.isoformat(),)
    ).fetchall()
    if not rows:
        return 0
    hour_cutoff = (now - timedelta(hours=1)).isoformat()
    day_cutoff = now.date().isoformat()
    used_hour_requests = used_day_requests = used_hour_tokens = used_day_tokens = 0
    for row in analytics.execute("SELECT * FROM llm_jobs WHERE created_at>=? AND status IN ('submitted','complete')", (day_cutoff,)).fetchall():
        body = near_live_request_body(row, config)
        tokens = estimate_tokens(_json(body))
        used_day_requests += 1
        used_day_tokens += tokens
        if row["created_at"] >= hour_cutoff:
            used_hour_requests += 1
            used_hour_tokens += tokens
    max_hour_requests = max(1, int(getattr(config, "live_llm_max_requests_per_hour", 24)))
    max_day_requests = max(1, int(getattr(config, "live_llm_max_requests_per_day", 240)))
    max_hour_tokens = max(1, int(getattr(config, "live_llm_max_estimated_tokens_per_hour", 12000)))
    max_day_tokens = max(1, int(getattr(config, "live_llm_max_estimated_tokens_per_day", 100000)))
    try:
        from openai import OpenAI
        client = OpenAI()
        submitted = 0
        for row in rows:
            body = near_live_request_body(row, config)
            estimated = estimate_tokens(_json(body))
            if used_hour_requests >= max_hour_requests or used_day_requests >= max_day_requests or used_hour_tokens + estimated > max_hour_tokens or used_day_tokens + estimated > max_day_tokens:
                break
            try:
                response = client.responses.create(**body)
                completed_at = datetime.now(timezone.utc).isoformat()
                dumped = _response_dump(response)
                remote_id = dumped.get("id")
                remote_status = str(dumped.get("status") or "queued")
                actual_model = dumped.get("model") or body["model"]
                with analytics:
                    analytics.execute("UPDATE llm_jobs SET model=?,status='submitted',attempts=attempts+1,next_attempt_at=NULL,updated_at=? WHERE job_id=? AND status='queued'", (actual_model, completed_at, row["job_id"]))
                    analytics.execute("INSERT INTO llm_results(job_id,model,status,result_json,created_at) VALUES(?,?,?,?,?)", (row["job_id"], actual_model, "submitted", _json(_compact_response(dumped, _usage(dumped))), completed_at))
                    _upsert_ledger(analytics, ledger_key=f"job:{row['job_id']}", source_type=row["source_type"], source_id=row["source_id"], kind=row["job_kind"], analysis_version=_job_analysis_version(row, config), model=actual_model, status="submitted", created_at=row["created_at"], updated_at=completed_at, job_id=row["job_id"], remote_id=remote_id, remote_status=remote_status, prompt_cache_key=body.get("prompt_cache_key"), payload_bytes=len(row["payload_json"].encode("utf-8")), estimated_input_tokens=estimated, estimated_output_tokens=int(body.get("max_output_tokens", 0)), retry_count=row["attempts"] + 1)
                submitted += 1
                used_hour_requests += 1
                used_day_requests += 1
                used_hour_tokens += estimated
                used_day_tokens += estimated
            except Exception:
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
                try:
                    with analytics:
                        analytics.execute("UPDATE llm_jobs SET attempts=attempts+1,next_attempt_at=?,updated_at=? WHERE job_id=? AND status='queued'", (retry_at, datetime.now(timezone.utc).isoformat(), row["job_id"]))
                except Exception:
                    analytics.rollback()
        return submitted
    except Exception:
        analytics.rollback()
        return 0
