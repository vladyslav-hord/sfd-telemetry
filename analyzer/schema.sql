PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS analytics_schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
INSERT OR IGNORE INTO analytics_schema_version(version) VALUES(1);
CREATE TABLE IF NOT EXISTS analysis_runs(analysis_run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, error_text TEXT);
CREATE TABLE IF NOT EXISTS analysis_watermarks(source_name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_server_metrics(report_date TEXT NOT NULL, metric_version INTEGER NOT NULL, metrics_json TEXT NOT NULL, source_coverage REAL, quality TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(report_date,metric_version));
CREATE TABLE IF NOT EXISTS daily_player_metrics(report_date TEXT NOT NULL, player_identity_id TEXT NOT NULL, metric_version INTEGER NOT NULL, is_bot INTEGER NOT NULL, metrics_json TEXT NOT NULL, source_coverage REAL, quality TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(report_date,player_identity_id,metric_version));
CREATE TABLE IF NOT EXISTS daily_map_metrics(report_date TEXT NOT NULL, map_name TEXT NOT NULL, metric_version INTEGER NOT NULL, metrics_json TEXT NOT NULL, source_coverage REAL, quality TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(report_date,map_name,metric_version));
CREATE TABLE IF NOT EXISTS round_metrics(round_id TEXT NOT NULL, metric_version INTEGER NOT NULL, metrics_json TEXT NOT NULL, source_coverage REAL, quality TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(round_id,metric_version));
CREATE TABLE IF NOT EXISTS daily_weapon_metrics(report_date TEXT NOT NULL, weapon TEXT NOT NULL, metric_version INTEGER NOT NULL, metrics_json TEXT NOT NULL, source_coverage REAL, quality TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(report_date,weapon,metric_version));
CREATE TABLE IF NOT EXISTS daily_pair_metrics(report_date TEXT NOT NULL, player_a_id TEXT NOT NULL, player_b_id TEXT NOT NULL, metric_version INTEGER NOT NULL, metrics_json TEXT NOT NULL, source_coverage REAL, quality TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(report_date,player_a_id,player_b_id,metric_version));
CREATE TABLE IF NOT EXISTS player_style_profiles(player_identity_id TEXT NOT NULL, metric_version INTEGER NOT NULL, features_json TEXT NOT NULL, sample_rounds INTEGER NOT NULL, confidence REAL NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(player_identity_id,metric_version));
CREATE TABLE IF NOT EXISTS chat_annotations(chat_id INTEGER NOT NULL, prompt_version TEXT NOT NULL, model TEXT NOT NULL, annotation_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(chat_id,prompt_version,model));
CREATE TABLE IF NOT EXISTS candidate_windows(window_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, source_window_id TEXT NOT NULL, features_json TEXT NOT NULL, coverage REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(source_window_id));
CREATE TABLE IF NOT EXISTS pattern_catalog(pattern_id TEXT PRIMARY KEY, state TEXT NOT NULL, feature_centroid_json TEXT, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pattern_matches(pattern_id TEXT NOT NULL, source_window_id TEXT NOT NULL, confidence REAL NOT NULL, quality TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(pattern_id,source_window_id));
CREATE TABLE IF NOT EXISTS llm_batches(batch_id TEXT PRIMARY KEY, remote_batch_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT, metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_requests(request_hash TEXT PRIMARY KEY, batch_id TEXT REFERENCES llm_batches(batch_id), source_type TEXT NOT NULL, source_id TEXT NOT NULL, prompt_version TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, response_json TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(source_type,source_id,prompt_version,model));
CREATE TABLE IF NOT EXISTS daily_reports(report_date TEXT NOT NULL, metric_version INTEGER NOT NULL, report_json TEXT NOT NULL, llm_status TEXT NOT NULL, generated_at TEXT NOT NULL, analysis_run_id TEXT NOT NULL, PRIMARY KEY(report_date,metric_version));
CREATE INDEX IF NOT EXISTS idx_llm_requests_status ON llm_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_candidate_windows_date ON candidate_windows(report_date);

CREATE TABLE IF NOT EXISTS processing_checkpoints (
  consumer_name TEXT PRIMARY KEY,
  last_event_id INTEGER NOT NULL DEFAULT 0,
  last_sequences_json TEXT NOT NULL DEFAULT '{}',
  processed_at TEXT NOT NULL,
  algorithm_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_segments (
  raw_segment_id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  server_session_id TEXT,
  first_sequence INTEGER,
  last_sequence INTEGER,
  sha256 TEXT,
  compression_status TEXT NOT NULL DEFAULT 'active',
  processing_status TEXT NOT NULL DEFAULT 'available',
  retention_priority INTEGER NOT NULL DEFAULT 5,
  safe_delete_after TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS storage_health (
  component TEXT PRIMARY KEY,
  used_bytes INTEGER NOT NULL DEFAULT 0,
  max_bytes INTEGER NOT NULL DEFAULT 0,
  watermark REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'ok',
  dropped_count INTEGER NOT NULL DEFAULT 0,
  malformed_count INTEGER NOT NULL DEFAULT 0,
  gap_count INTEGER NOT NULL DEFAULT 0,
  details_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agg_server_minute (
  minute_start TEXT NOT NULL,
  server_session_id TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, server_session_id)
);
CREATE TABLE IF NOT EXISTS agg_player_minute (
  minute_start TEXT NOT NULL,
  player_session_id TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, player_session_id)
);
CREATE TABLE IF NOT EXISTS agg_network_minute (
  minute_start TEXT NOT NULL,
  player_session_id TEXT NOT NULL,
  ping_count INTEGER NOT NULL DEFAULT 0,
  ping_sum REAL NOT NULL DEFAULT 0,
  ping_min REAL,
  ping_max REAL,
  ping_sum_sq REAL NOT NULL DEFAULT 0,
  histogram_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, player_session_id)
);
CREATE TABLE IF NOT EXISTS agg_weapon_minute (
  minute_start TEXT NOT NULL,
  server_session_id TEXT NOT NULL,
  weapon TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, server_session_id, weapon)
);
CREATE TABLE IF NOT EXISTS agg_map_minute (
  minute_start TEXT NOT NULL,
  server_session_id TEXT NOT NULL,
  map_name TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, server_session_id, map_name)
);
CREATE TABLE IF NOT EXISTS agg_pair_minute (
  minute_start TEXT NOT NULL,
  player_a_session_id TEXT NOT NULL,
  player_b_session_id TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, player_a_session_id, player_b_session_id)
);
CREATE TABLE IF NOT EXISTS agg_scene_minute (
  minute_start TEXT NOT NULL,
  round_id TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(minute_start, round_id)
);

CREATE TABLE IF NOT EXISTS episode_catalog (
  episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_event_id INTEGER NOT NULL UNIQUE,
  server_session_id TEXT NOT NULL,
  round_id TEXT,
  trigger TEXT NOT NULL,
  trigger_game_ms REAL,
  window_before_ms REAL NOT NULL DEFAULT 5000,
  window_after_ms REAL NOT NULL DEFAULT 5000,
  status TEXT NOT NULL,
  selection_reason TEXT NOT NULL,
  coverage REAL NOT NULL DEFAULT 0,
  source_window_id TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS episode_features (
  episode_id INTEGER PRIMARY KEY REFERENCES episode_catalog(episode_id) ON DELETE CASCADE,
  features_json TEXT NOT NULL,
  extracted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pattern_candidates (
  pattern_candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  signature TEXT NOT NULL UNIQUE,
  pattern_family TEXT NOT NULL,
  state TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  supported_by_direct_event INTEGER NOT NULL DEFAULT 0,
  evidence_event_ids_json TEXT NOT NULL DEFAULT '[]',
  features_json TEXT NOT NULL DEFAULT '{}',
  occurrences INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_jobs (
  job_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  job_kind TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_type, source_id, job_kind)
);
CREATE TABLE IF NOT EXISTS llm_results (
  result_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES llm_jobs(job_id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Versioned logical keys keep model changes from re-submitting the same analysis.
-- The INSERT OR IGNORE backfill is safe for existing databases and collapses
-- historical model duplicates to the first known logical request.
CREATE TABLE IF NOT EXISTS llm_logical_keys (
  logical_key TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  analysis_version TEXT NOT NULL,
  request_hash TEXT,
  job_id TEXT,
  created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO llm_logical_keys(logical_key,source_type,source_id,kind,analysis_version,request_hash,created_at)
SELECT 'request|' || source_type || '|' || source_id || '|' || prompt_version || '|llm-v1',
       source_type, source_id, prompt_version, 'llm-v1', request_hash, created_at
FROM llm_requests;
INSERT OR IGNORE INTO llm_logical_keys(logical_key,source_type,source_id,kind,analysis_version,job_id,created_at)
SELECT 'job|' || source_type || '|' || source_id || '|' || job_kind || '|llm-v1',
       source_type, source_id, job_kind, 'llm-v1', job_id, created_at
FROM llm_jobs;

-- Compact operational/cost ledger. Raw prompts and model envelopes stay in
-- their existing tables; this table stores only accounting and lifecycle data.
CREATE TABLE IF NOT EXISTS llm_cost_ledger (
  ledger_key TEXT PRIMARY KEY,
  request_hash TEXT,
  job_id TEXT,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  analysis_version TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  remote_id TEXT,
  remote_status TEXT,
  prompt_cache_key TEXT,
  payload_bytes INTEGER NOT NULL DEFAULT 0,
  estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_output_tokens INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_tokens INTEGER,
  cache_write_tokens INTEGER,
  reasoning_tokens INTEGER,
  total_tokens INTEGER,
  retry_count INTEGER NOT NULL DEFAULT 0,
  dedupe_hit INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processing_checkpoints_event ON processing_checkpoints(last_event_id);
CREATE INDEX IF NOT EXISTS idx_processing_checkpoints_processed ON processing_checkpoints(processed_at);
CREATE INDEX IF NOT EXISTS idx_raw_segments_server_sequence ON raw_segments(server_session_id, last_sequence, compression_status);
CREATE INDEX IF NOT EXISTS idx_raw_segments_processing ON raw_segments(processing_status, compression_status, safe_delete_after);
CREATE INDEX IF NOT EXISTS idx_episode_catalog_status ON episode_catalog(status, created_at);
CREATE INDEX IF NOT EXISTS idx_episode_catalog_round_time ON episode_catalog(server_session_id, round_id, trigger_game_ms);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_status ON llm_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_ready ON llm_jobs(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_results_job_created ON llm_results(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_logical_keys_source ON llm_logical_keys(source_type, source_id, kind, analysis_version);
CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_status ON llm_cost_ledger(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_llm_cost_ledger_source ON llm_cost_ledger(source_type, kind, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_requests_source_status ON llm_requests(source_type, source_id, status);
CREATE INDEX IF NOT EXISTS idx_llm_batches_status_created ON llm_batches(status, created_at);
CREATE INDEX IF NOT EXISTS idx_daily_reports_date_version ON daily_reports(report_date, metric_version);
