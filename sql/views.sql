CREATE VIEW IF NOT EXISTS v_session_summary AS
SELECT ps.player_session_id, ps.player_identity_id, ps.account_name, ps.character_name,
       ps.joined_at, ps.left_at,
       COALESCE(ps.duration_ms,
         (julianday(COALESCE(ps.left_at, ss.last_event_at)) - julianday(ps.joined_at)) * 86400000.0
       ) AS duration_ms,
       ps.leave_reason, ps.is_bot,
       COUNT(DISTINCT e.round_id) AS rounds_seen,
       AVG(ns.ping_ms) AS mean_ping_ms,
       MIN(ns.ping_ms) AS min_ping_ms,
       MAX(ns.ping_ms) AS max_ping_ms
FROM player_sessions ps
JOIN server_sessions ss USING(server_session_id)
LEFT JOIN events e USING(player_session_id)
LEFT JOIN network_samples ns USING(player_session_id)
GROUP BY ps.player_session_id;

CREATE VIEW IF NOT EXISTS v_player_retention AS
SELECT p.player_identity_id, p.identity_confidence, p.first_seen_at, p.last_seen_at,
       COUNT(DISTINCT ps.player_session_id) AS sessions,
       SUM(COALESCE(ps.duration_ms, 0)) / 1000.0 AS total_playtime_seconds,
       CAST(julianday(p.last_seen_at) - julianday(p.first_seen_at) AS INTEGER) AS observed_days
FROM players p LEFT JOIN player_sessions ps USING(player_identity_id)
GROUP BY p.player_identity_id;

CREATE VIEW IF NOT EXISTS v_ping_distribution AS
WITH ranked AS (
  SELECT player_session_id, ping_ms,
         ROW_NUMBER() OVER(PARTITION BY player_session_id ORDER BY ping_ms) AS rn,
         COUNT(*) OVER(PARTITION BY player_session_id) AS n
  FROM network_samples
)
SELECT player_session_id, AVG(ping_ms) AS mean_ping_ms,
       MIN(ping_ms) AS min_ping_ms, MAX(ping_ms) AS max_ping_ms,
       AVG(CASE WHEN rn IN ((n + 1) / 2, (n + 2) / 2) THEN ping_ms END) AS median_ping_ms,
       MAX(CASE WHEN rn >= CAST(n * 0.90 AS INTEGER) THEN ping_ms END) FILTER
         (WHERE rn = CAST(n * 0.90 AS INTEGER) OR rn = n) AS p90_hint,
       SUM(ping_ms > 100) AS samples_over_100,
       SUM(ping_ms > 150) AS samples_over_150,
       SUM(ping_ms > 200) AS samples_over_200,
       SUM(ping_ms > 250) AS samples_over_250
FROM ranked GROUP BY player_session_id;

CREATE VIEW IF NOT EXISTS v_map_quality AS
SELECT r.map_name, r.map_type, COUNT(DISTINCT r.round_id) AS rounds,
       AVG(r.duration_ms) / 1000.0 AS avg_round_seconds,
       AVG(r.player_count) AS avg_players,
       SUM(CASE WHEN ps.left_at BETWEEN r.started_at AND COALESCE(r.ended_at, ps.left_at)
                THEN 1 ELSE 0 END) AS leaves_during_round
FROM rounds r LEFT JOIN player_sessions ps ON ps.server_session_id=r.server_session_id
GROUP BY r.map_name, r.map_type;

CREATE VIEW IF NOT EXISTS v_combat_pairs AS
SELECT attacker_session_id, victim_session_id,
       COUNT(*) AS events, SUM(COALESCE(damage,0)) AS damage
FROM combat_events
WHERE attacker_session_id IS NOT NULL AND victim_session_id IS NOT NULL
GROUP BY attacker_session_id, victim_session_id;
