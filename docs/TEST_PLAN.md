# Test plan

Automated checks:

```bat
py -3 -m unittest discover -s tests -v
pwsh -File tools\compile_sfd_script.ps1
```

Manual server matrix:

| case | expected records/check |
|---|---|
| 1 human | one `user_join/present`, session, ping/state samples |
| 1 human + bot | bot has session-only identity; counts are 1 human/1 bot |
| 2 humans | separate player sessions and directional combat pairs |
| 8 humans theoretical | 1 Hz baseline + 4 Hz RAM ring; no sustained dropped count |
| join mid-round | `round_players.late_join=1` |
| leave mid-round | exact `Left` or `ConnectionLost`, final state/stats |
| spectating | spectator flags without invented player state |
| death | death/stats plus deduplicated high-resolution window |
| respawn/new round | new round UUID; connected player session remains stable |
| map change | lifecycle closes old round and starts new map GUID/name |
| server restart | new server session UUID and sequence beginning at 1 |
| collector restart | no duplicate `(server_session, sequence)` rows |
| Unicode name/chat | byte-identical UTF-8 after Base64 transport |
| very long chat | one valid JSON event or visible drop/gap; collector stays alive |
| rapid actions | bounded queue; `telemetry_health.dropped_event_count` visible |
| high ping | raw samples and offline percentiles/threshold durations |
| bot users | no `players` identity row; gameplay session remains queryable |
| Team Rotation 2 | telemetry only observes team changes and never writes teams/rules |
| malformed telemetry | entry in `data/malformed_events.jsonl`; valid neighbors ingest |
| duplicate telemetry | ignored by unique server-session/sequence key |
| storage rewrite/rotation | collector reads replacement snapshot and continues sequence |

For live callback verification, join a private test server, send one public chat
message, deal damage, then leave. Query:

```sql
SELECT sequence, event_type, player_session_id, utc_timestamp
FROM events
WHERE event_type IN ('user_join','chat_message','player_damage','user_leave')
ORDER BY event_id;
```
