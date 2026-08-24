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
    openai_model: str = "gpt-5-nano-2025-08-07"
    openai_mode: str = "batch"
    openai_reasoning_effort: str = "minimal"
    send_public_names: bool = True
    send_persistent_identifiers: bool = False
    max_batch_input_tokens_per_day: int = 500000
    max_batch_output_tokens_per_day: int = 100000
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
