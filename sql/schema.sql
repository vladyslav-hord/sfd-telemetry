PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
INSERT OR IGNORE INTO schema_version(version) VALUES(1);

CREATE TABLE IF NOT EXISTS server_sessions (
  server_session_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  first_event_at TEXT NOT NULL,
  last_event_at TEXT NOT NULL,
  script_version TEXT,
  sfd_build TEXT,
  transport TEXT,
  map_name TEXT
);

CREATE TABLE IF NOT EXISTS players (
  player_identity_id TEXT PRIMARY KEY,
  identity_confidence TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_sessions (
  player_session_id TEXT PRIMARY KEY,
  server_session_id TEXT NOT NULL REFERENCES server_sessions(server_session_id),
  player_identity_id TEXT REFERENCES players(player_identity_id),
  joined_at TEXT NOT NULL,
  left_at TEXT,
  duration_ms REAL,
  leave_reason TEXT,
  user_identifier INTEGER,
  legacy_user_id INTEGER,
  local_user_index INTEGER,
  game_slot_index INTEGER,
  account_name TEXT,
  character_name TEXT,
  is_user INTEGER NOT NULL,
  is_bot INTEGER NOT NULL,
  is_host INTEGER NOT NULL,
  is_moderator INTEGER NOT NULL,
  joined_as_spectator INTEGER NOT NULL,
  initial_spectating INTEGER NOT NULL,
  initial_team TEXT,
  initial_ping INTEGER,
  final_ping INTEGER,
  final_state_json TEXT
);

CREATE TABLE IF NOT EXISTS player_aliases (
  alias_id INTEGER PRIMARY KEY,
  player_identity_id TEXT REFERENCES players(player_identity_id),
  player_session_id TEXT NOT NULL REFERENCES player_sessions(player_session_id),
  alias_type TEXT NOT NULL,
  alias TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  UNIQUE(player_session_id, alias_type, alias)
);

CREATE TABLE IF NOT EXISTS profiles (
  profile_id INTEGER PRIMARY KEY,
  player_session_id TEXT NOT NULL REFERENCES player_sessions(player_session_id),
  player_identity_id TEXT REFERENCES players(player_identity_id),
  captured_at TEXT NOT NULL,
  profile_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
  round_id TEXT PRIMARY KEY,
  server_session_id TEXT NOT NULL REFERENCES server_sessions(server_session_id),
  map_name TEXT NOT NULL,
  map_guid TEXT,
  map_original_guid TEXT,
  map_author TEXT,
  map_round INTEGER,
  map_type TEXT,
  game_type TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  start_game_ms REAL,
  end_game_ms REAL,
  duration_ms REAL,
  time_limit_seconds INTEGER,
  sudden_death_enabled INTEGER,
  sudden_death_active INTEGER,
  player_count INTEGER,
  human_count INTEGER,
  bot_count INTEGER,
  winner_json TEXT,
  draw INTEGER,
  result_source TEXT
);

CREATE TABLE IF NOT EXISTS round_players (
  round_id TEXT NOT NULL REFERENCES rounds(round_id),
  player_session_id TEXT NOT NULL REFERENCES player_sessions(player_session_id),
  team TEXT,
  joined_game_ms REAL,
  left_game_ms REAL,
  late_join INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  PRIMARY KEY(round_id, player_session_id)
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY,
  schema_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  server_session_id TEXT NOT NULL REFERENCES server_sessions(server_session_id),
  round_id TEXT,
  player_session_id TEXT,
  sequence INTEGER NOT NULL,
  utc_timestamp TEXT NOT NULL,
  game_ms REAL,
  data_json TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  UNIQUE(server_session_id, sequence)
);

CREATE TABLE IF NOT EXISTS combat_events (
  event_id INTEGER PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
  attacker_session_id TEXT,
  victim_session_id TEXT,
  damage REAL,
  damage_type TEXT,
  weapon TEXT,
  distance REAL,
  context_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS combat_hit_details (
  event_id INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  hit_index INTEGER NOT NULL,
  attacker_session_id TEXT,
  victim_session_id TEXT,
  damage REAL,
  damage_type TEXT,
  weapon TEXT,
  is_player INTEGER NOT NULL,
  context_json TEXT NOT NULL,
  PRIMARY KEY(event_id, hit_index)
);

CREATE TABLE IF NOT EXISTS scene_entities (
  scene_entity_id INTEGER PRIMARY KEY,
  server_session_id TEXT NOT NULL REFERENCES server_sessions(server_session_id),
  round_id TEXT NOT NULL,
  entity_kind TEXT NOT NULL CHECK(entity_kind IN ('object','projectile')),
  engine_id INTEGER NOT NULL,
  name TEXT,
  manifest_json TEXT NOT NULL,
  created_event_id INTEGER REFERENCES events(event_id),
  terminated_event_id INTEGER REFERENCES events(event_id),
  UNIQUE(server_session_id, round_id, entity_kind, engine_id)
);

CREATE TABLE IF NOT EXISTS scene_chunks (
  scene_chunk_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  codec TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  raw_bytes INTEGER NOT NULL,
  compressed_bytes INTEGER NOT NULL,
  payload BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_samples (
  scene_sample_id INTEGER PRIMARY KEY,
  scene_entity_id INTEGER NOT NULL REFERENCES scene_entities(scene_entity_id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  round_id TEXT NOT NULL,
  game_ms REAL NOT NULL,
  x REAL, y REAL, velocity_x REAL, velocity_y REAL, angle REAL, angular_velocity REAL,
  health REAL, max_health REAL, is_missile INTEGER, state_json TEXT NOT NULL,
  UNIQUE(scene_entity_id, event_id, game_ms)
);

CREATE TABLE IF NOT EXISTS scene_interactions (
  scene_interaction_id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  round_id TEXT,
  utc_timestamp TEXT NOT NULL,
  game_ms REAL,
  interaction_type TEXT NOT NULL,
  source_quality TEXT NOT NULL,
  player_session_id TEXT,
  actor_entity_id INTEGER REFERENCES scene_entities(scene_entity_id),
  target_entity_id INTEGER REFERENCES scene_entities(scene_entity_id),
  target_player_session_id TEXT,
  x REAL, y REAL, damage REAL, distance REAL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_windows (
  scene_window_id INTEGER PRIMARY KEY,
  source_event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  round_id TEXT NOT NULL,
  trigger_game_ms REAL NOT NULL,
  trigger TEXT NOT NULL,
  coverage REAL NOT NULL,
  entities_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_samples (
  sample_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  player_session_id TEXT NOT NULL,
  round_id TEXT,
  utc_timestamp TEXT NOT NULL,
  game_ms REAL,
  resolution_hz REAL NOT NULL,
  x REAL, y REAL, velocity_x REAL, velocity_y REAL,
  hp REAL, energy REAL, team TEXT,
  state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_windows (
  window_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  player_session_id TEXT NOT NULL,
  round_id TEXT,
  utc_timestamp TEXT NOT NULL,
  trigger TEXT NOT NULL,
  window_before_ms REAL,
  samples_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS network_samples (
  sample_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  player_session_id TEXT NOT NULL,
  round_id TEXT,
  utc_timestamp TEXT NOT NULL,
  ping_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
  chat_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  player_session_id TEXT,
  round_id TEXT,
  utc_timestamp TEXT NOT NULL,
  account_name TEXT,
  character_name TEXT,
  message TEXT NOT NULL,
  is_command INTEGER NOT NULL,
  command_name TEXT,
  command_arguments TEXT,
  map_name TEXT,
  player_count INTEGER,
  state_json TEXT
);

CREATE TABLE IF NOT EXISTS player_stat_snapshots (
  snapshot_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  player_session_id TEXT NOT NULL,
  round_id TEXT,
  utc_timestamp TEXT NOT NULL,
  checkpoint TEXT NOT NULL,
  stats_json TEXT NOT NULL,
  delta_json TEXT
);

CREATE TABLE IF NOT EXISTS moderation_events (
  moderation_id INTEGER PRIMARY KEY,
  event_id INTEGER UNIQUE REFERENCES events(event_id),
  action TEXT NOT NULL,
  target_player_session_id TEXT,
  reason TEXT,
  source TEXT NOT NULL,
  details_json TEXT
);

CREATE TABLE IF NOT EXISTS collector_state (
  source_path TEXT PRIMARY KEY,
  last_mtime_ns INTEGER,
  last_size INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_gaps (
  gap_id INTEGER PRIMARY KEY,
  server_session_id TEXT NOT NULL,
  expected_sequence INTEGER NOT NULL,
  observed_sequence INTEGER NOT NULL,
  detected_at TEXT NOT NULL,
  UNIQUE(server_session_id, expected_sequence, observed_sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_player_time ON events(player_session_id, utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_events_round ON events(round_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_events_utc_time ON events(utc_timestamp, event_id);
CREATE INDEX IF NOT EXISTS idx_player_sessions_identity ON player_sessions(player_identity_id, joined_at);
CREATE INDEX IF NOT EXISTS idx_player_sessions_join_leave ON player_sessions(joined_at, left_at);
CREATE INDEX IF NOT EXISTS idx_rounds_map_time ON rounds(map_name, started_at);
CREATE INDEX IF NOT EXISTS idx_rounds_started ON rounds(started_at, round_id);
CREATE INDEX IF NOT EXISTS idx_chat_sender_time ON chat_messages(player_session_id, utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_network_player_time ON network_samples(player_session_id, utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_network_utc_time ON network_samples(utc_timestamp, player_session_id, sample_id);
CREATE INDEX IF NOT EXISTS idx_state_player_time ON state_samples(player_session_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_state_round_time ON state_samples(round_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_state_utc_time ON state_samples(utc_timestamp, player_session_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_state_windows_player_time ON state_windows(player_session_id, utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_state_windows_utc_time ON state_windows(utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_combat_hit_details_victim ON combat_hit_details(victim_session_id);
CREATE INDEX IF NOT EXISTS idx_scene_entities_round ON scene_entities(round_id, entity_kind, engine_id);
CREATE INDEX IF NOT EXISTS idx_scene_samples_entity_time ON scene_samples(scene_entity_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_scene_samples_round_time ON scene_samples(round_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_scene_interactions_player_time ON scene_interactions(player_session_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_scene_interactions_target ON scene_interactions(target_entity_id, game_ms);
CREATE INDEX IF NOT EXISTS idx_scene_interactions_type_time ON scene_interactions(interaction_type, utc_timestamp);
CREATE INDEX IF NOT EXISTS idx_scene_interactions_utc_time ON scene_interactions(utc_timestamp, scene_interaction_id);
CREATE INDEX IF NOT EXISTS idx_scene_windows_round_time ON scene_windows(round_id, trigger_game_ms);
CREATE INDEX IF NOT EXISTS idx_telemetry_gaps_detected ON telemetry_gaps(detected_at);

-- Live retention and storage accounting. These tables are deliberately local to
-- telemetry so the collector can report degradation even when the analyzer is down.
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

CREATE INDEX IF NOT EXISTS idx_raw_segments_status ON raw_segments(processing_status, retention_priority, created_at);
