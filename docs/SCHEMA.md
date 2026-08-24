# Database schema

The durable database is `data/sfd_telemetry.sqlite3`. SQLite runs with WAL,
foreign keys, a 5-second busy timeout, `synchronous=NORMAL`, and batch
transactions.

| table | purpose |
|---|---|
| `server_sessions` | one telemetry producer lifetime and last observed event |
| `players` | host-scoped persistent `S0...` account hashes only |
| `player_aliases` | account/character name history |
| `player_sessions` | one connected local player/user session |
| `profiles` | complete avatar/outfit snapshots |
| `rounds` | map, mode, timing and explicitly sourced result |
| `round_players` | team composition, late joins and mid-round leaves |
| `events` | lossless validated envelopes and original JSON line |
| `combat_events` | queryable attacker/victim/damage fields plus full context JSON |
| `state_samples` | normalized 1 Hz and post-trigger 4 Hz state samples |
| `state_windows` | deduplicated 10-second pre-event 4 Hz ring dumps |
| `network_samples` | raw ping samples |
| `chat_messages` | independently deletable public callback messages |
| `player_stat_snapshots` | all canonical 1.6 counters plus delta from round-start baseline |
| `moderation_events` | reserved for explicit sources; ScriptAPI currently cannot fill it |
| `collector_state` | source snapshot checkpoint |
| `telemetry_gaps` | visible monotonic-sequence discontinuities |

Large or evolving callback payloads remain in `events.data_json`. This avoids a
schema migration every time a harmless context field is added. Chat content is
duplicated only into its dedicated table so it can be deleted independently.

Prepared views:

- `v_session_summary`: duration, rounds and mean/min/max ping per session;
- `v_player_retention`: first/last seen, sessions and playtime;
- `v_ping_distribution`: lightweight SQL overview;
- `v_map_quality`: round duration/population/leaves by map;
- `v_combat_pairs`: directional damage/event counts between sessions.

Exact median, percentiles, standard deviation, jitter and time above ping
thresholds are calculated offline by `tools/analyze.py ping`.
