from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    telemetry_database: str = "data/sfd_telemetry.sqlite3"
    analytics_database: str = "data/sfd_analytics.sqlite3"
    report_directory: str = "data/analysis"
    timezone: str = "Europe/Warsaw"
    reconciliation_days: int = 3
    human_player_reports: bool = True
    individual_bot_reports: bool = False
    openai_enabled: bool = True
    openai_model: str = "gpt-5-nano"
    openai_mode: str = "batch"
    openai_reasoning_effort: str = "minimal"
    openai_verbosity: str = "low"
    llm_analysis_version: str = "llm-v1"
    live_llm_enabled: bool = False
    live_llm_model: str = "gpt-5-nano"
    live_llm_window_seconds: int = 300
    live_llm_min_messages: int = 2
    live_llm_max_messages_per_window: int = 20
    live_llm_max_output_tokens: int = 128
    live_llm_max_requests_per_hour: int = 24
    live_llm_max_requests_per_day: int = 240
    live_llm_max_estimated_tokens_per_hour: int = 12000
    live_llm_max_estimated_tokens_per_day: int = 100000
    send_public_names: bool = True
    send_persistent_identifiers: bool = False
    max_batch_input_tokens_per_day: int = 500000
    max_batch_output_tokens_per_day: int = 100000
    max_batch_requests_per_run: int = 50
    max_batch_input_bytes_per_batch: int = 8000000
    max_batch_output_tokens_per_request: int = 600
    max_chat_output_tokens_per_request: int = 256
    max_moderation_messages_per_request: int = 30
    max_moderation_input_bytes_per_request: int = 20000
    max_moderation_input_tokens_per_request: int = 5000
    max_gameplay_output_tokens_per_request: int = 256
    max_narrative_output_tokens_per_request: int = 600
    max_narrative_payload_bytes: int = 100000
    max_narrative_input_tokens: int = 25000
    max_narrative_items_per_section: int = 10
    max_llm_anomaly_windows_per_day: int = 50
    max_chat_messages_per_request: int = 30
    max_input_chars_per_request: int = 30000
    pattern_audit_rate: float = 0.05
    pattern_confirmation_occurrences: int = 12
    pattern_confirmation_players: int = 3
    pattern_confirmation_rounds: int = 3
    anomaly_min_sample_coverage: float = 0.7
    anomaly_robust_z_threshold: float = 4.0
    heatmap_cell_size: int = 50
    static_speed_threshold: float = 1.0
    chat_annotation_retention_days: int = 30
    live_poll_interval_seconds: float = 1.0
    live_batch_size: int = 1000
    live_queue_size: int = 4
    live_algorithm_version: str = "live-v1"
    live_dashboard_interval_seconds: int = 60
    live_microbatch_seconds: int = 300
    live_storage_check_interval_seconds: int = 30
    live_raw_mark_interval_seconds: int = 15
    live_raw_mark_busy_timeout_ms: int = 250
    live_scene_state_cache_limit: int = 50000
    live_llm_interval_seconds: int = 15
    raw_archive_max_bytes: int = 1073741824
    raw_segment_max_bytes: int = 33554432
    raw_gzip_level: int = 1
    raw_high_watermark: float = 0.85
    raw_critical_watermark: float = 0.95
    sqlite_max_bytes: int = 16106127360
    sqlite_high_watermark: float = 0.80
    sqlite_cleanup_watermark: float = 0.90
    sqlite_episode_watermark: float = 0.95
    sqlite_chunk_watermark: float = 0.98
    episode_chunk_max_bytes: int = 4294967296
    dashboard_cache_max_bytes: int = 104857600
    llm_queue_max_bytes: int = 104857600
    raw_retention_days: int = 7
    minute_aggregate_retention_days: int = 180
    round_summary_retention_days: int = 365
    selected_episode_retention_days: int = 90

    def resolved(self, root: Path) -> "Config":
        values = asdict(self)
        for key in ("telemetry_database", "analytics_database", "report_directory"):
            path = Path(values[key])
            values[key] = str(path if path.is_absolute() else root / path)
        return Config(**values)


def load_config(path: str | Path | None, root: Path) -> Config:
    if not path:
        return Config().resolved(root)
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    values = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = set(Config.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    return Config(**values).resolved(root)
