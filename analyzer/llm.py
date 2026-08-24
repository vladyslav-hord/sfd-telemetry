from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone

from .prompts import CHAT_PROMPT, CHAT_PROMPT_VERSION, CHAT_SCHEMA, GAMEPLAY_PROMPT, GAMEPLAY_PROMPT_VERSION, GAMEPLAY_SCHEMA, NARRATIVE_PROMPT, NARRATIVE_PROMPT_VERSION, NARRATIVE_SCHEMA


def request_hash(source_type: str, source_id: str, prompt_version: str, model: str, payload: dict) -> str:
    raw = json.dumps([source_type, source_id, prompt_version, model, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def queue_chat_requests(analytics, telemetry, day_start: str, day_end: str, config) -> int:
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    rows = telemetry.execute("SELECT chat_id, player_session_id, round_id, utc_timestamp, account_name, message FROM chat_messages WHERE utc_timestamp>=? AND utc_timestamp<? ORDER BY chat_id", (day_start, day_end)).fetchall()
    flagged = moderate_chat(analytics, rows)
    rows = sorted(rows, key=lambda row: (row["chat_id"] not in flagged, row["chat_id"]))
    now = datetime.now(timezone.utc).isoformat()
    queued = 0
    for core, context in chat_windows(rows, config):
        payload = {"core_messages": [chat_payload(row, config) for row in core], "context_messages": [chat_payload(row, config) for row in context]}
        source_id = ",".join(str(row["chat_id"]) for row in core)
        key = request_hash("chat", source_id, CHAT_PROMPT_VERSION, config.openai_model, payload)
        cursor = analytics.execute("INSERT OR IGNORE INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (key, "chat", source_id, CHAT_PROMPT_VERSION, config.openai_model, json.dumps(payload, ensure_ascii=False), now, now))
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


def moderate_chat(analytics, rows) -> set[int]:
    """Moderation labels are analytic metadata, never an enforcement action."""
    if not rows or not os.getenv("OPENAI_API_KEY"):
        return set()
    try:
        from openai import OpenAI
        response = OpenAI().moderations.create(model="omni-moderation-latest", input=[row["message"] for row in rows])
        results = response.results
        flagged: set[int] = set()
        now = datetime.now(timezone.utc).isoformat()
        for row, result in zip(rows, results):
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
    payload = {"report_date": report_date, "aggregates": aggregates}
    key = request_hash("narrative", report_date, NARRATIVE_PROMPT_VERSION, config.openai_model, payload)
    now = datetime.now(timezone.utc).isoformat()
    cursor = analytics.execute("INSERT OR IGNORE INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (key, "narrative", report_date, NARRATIVE_PROMPT_VERSION, config.openai_model, json.dumps(payload, ensure_ascii=False), now, now))
    analytics.commit()
    return cursor.rowcount


def queue_gameplay_requests(analytics, report_date: str, windows: list[dict], config) -> int:
    """Queue compact high-coverage candidates only; raw matches and full telemetry never leave SQLite."""
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return 0
    now, queued = datetime.now(timezone.utc).isoformat(), 0
    eligible = [item for item in windows if item.get("coverage", item.get("features", {}).get("coverage", 0)) >= config.anomaly_min_sample_coverage]
    eligible.sort(key=lambda item: abs(item.get("robust_z") or 0), reverse=True)
    for item in eligible[:config.max_llm_anomaly_windows_per_day]:
        source_id = str(item["source_window_id"])
        payload = {"report_date": report_date, "window": {key: value for key, value in item.items() if key not in {"player_identity_id", "display_name"}}}
        key = request_hash("gameplay", source_id, GAMEPLAY_PROMPT_VERSION, config.openai_model, payload)
        cursor = analytics.execute("INSERT OR IGNORE INTO llm_requests(request_hash,source_type,source_id,prompt_version,model,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (key, "gameplay", source_id, GAMEPLAY_PROMPT_VERSION, config.openai_model, json.dumps(payload, ensure_ascii=False), now, now))
        queued += cursor.rowcount
    analytics.commit()
    return queued


def submit_pending_batches(analytics, config) -> str:
    if not config.openai_enabled or not os.getenv("OPENAI_API_KEY"):
        return "disabled"
    rows = analytics.execute("SELECT * FROM llm_requests WHERE status='queued' ORDER BY created_at LIMIT ?", (config.max_chat_messages_per_request,)).fetchall()
    if not rows:
        return "complete"
    try:
        from openai import OpenAI
        lines = []
        for row in rows:
            if row["source_type"] == "chat":
                prompt, schema, name = CHAT_PROMPT, CHAT_SCHEMA, "chat_annotations"
            elif row["source_type"] == "gameplay":
                prompt, schema, name = GAMEPLAY_PROMPT, GAMEPLAY_SCHEMA, "gameplay_pattern"
            else:
                prompt, schema, name = NARRATIVE_PROMPT, NARRATIVE_SCHEMA, "daily_narrative"
            content = row["request_json"]
            body = {"model": config.openai_model, "store": False, "reasoning": {"effort": config.openai_reasoning_effort}, "input": [{"role": "developer", "content": prompt}, {"role": "user", "content": content}], "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}}}
            lines.append(json.dumps({"custom_id": row["request_hash"], "method": "POST", "url": "/v1/responses", "body": body}, ensure_ascii=False))
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
        analytics.executemany("UPDATE llm_requests SET batch_id=?,status='submitted',updated_at=?,attempts=attempts+1 WHERE request_hash=?", [(batch_id, now, row["request_hash"]) for row in rows])
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
                    annotations = extract_annotations(result)
                    request = analytics.execute("SELECT source_type,source_id,prompt_version,model,attempts,batch_id FROM llm_requests WHERE request_hash=?", (key,)).fetchone()
                    if request is not None and request["batch_id"] != batch["batch_id"]:
                        continue
                    if request is None or (request["source_type"] == "chat" and annotations is None):
                        analytics.execute("UPDATE llm_requests SET status='malformed',response_json=?,updated_at=? WHERE request_hash=?", (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key))
                        continue
                    if request["source_type"] == "chat" and valid_chat_annotations(annotations, {int(value) for value in request["source_id"].split(",")}):
                        for annotation in annotations:
                            analytics.execute("INSERT INTO chat_annotations(chat_id,prompt_version,model,annotation_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id,prompt_version,model) DO UPDATE SET annotation_json=excluded.annotation_json,status=excluded.status,updated_at=excluded.updated_at", (annotation["chat_id"], request["prompt_version"], request["model"], json.dumps(annotation, ensure_ascii=False), "complete", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
                        analytics.execute("UPDATE llm_requests SET status='complete',response_json=?,updated_at=? WHERE request_hash=?", (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key))
                        imported += 1
                    elif request["source_type"] == "narrative" and (narrative := extract_json(result)) is not None and valid_narrative(narrative):
                        report = analytics.execute("SELECT report_json FROM daily_reports WHERE report_date=?", (request["source_id"],)).fetchone()
                        if report:
                            data = json.loads(report[0]); data["narrative"] = narrative; data["status"]["llm"] = "complete"
                            analytics.execute("UPDATE daily_reports SET report_json=?,llm_status='complete',generated_at=? WHERE report_date=?", (json.dumps(data, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), request["source_id"]))
                            if report_directory:
                                from .report import write_report
                                write_report(report_directory, request["source_id"], data)
                        analytics.execute("UPDATE llm_requests SET status='complete',response_json=?,updated_at=? WHERE request_hash=?", (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key)); imported += 1
                    elif request["source_type"] == "gameplay" and (gameplay := extract_json(result)) is not None and valid_gameplay(gameplay, request["source_id"]):
                        analytics.execute("UPDATE llm_requests SET status='complete',response_json=?,updated_at=? WHERE request_hash=?", (json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key)); imported += 1
                    else:
                        status = "queued" if request["attempts"] < 3 else "malformed"
                        analytics.execute("UPDATE llm_requests SET status=?,response_json=?,updated_at=? WHERE request_hash=?", (status, json.dumps(result, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), key))
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
        body = batch_result["response"]["body"]
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
