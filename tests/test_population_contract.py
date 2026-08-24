import sqlite3
import unittest

from analyzer.metrics import _ping_metrics_by_session, _population_contract, round_metric_rows


START = "2026-08-24T00:00:00Z"
END = "2026-08-25T00:00:00Z"


class PopulationContractTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE player_sessions(
                player_session_id TEXT PRIMARY KEY,
                player_identity_id TEXT,
                joined_at TEXT NOT NULL,
                left_at TEXT,
                is_bot INTEGER NOT NULL
            )"""
        )
        self.connection.executemany(
            "INSERT INTO player_sessions VALUES(?,?,?,?,?)",
            [
                ("h1-a", "human-1", START, "2026-08-24T01:00:00Z", 0),
                ("h1-b", "human-1", "2026-08-24T02:00:00Z", "2026-08-24T03:00:00Z", 0),
                ("h2", "human-2", "2026-08-24T03:00:00Z", None, 0),
                ("h-null", None, "2026-08-24T01:00:00Z", "2026-08-24T04:00:00Z", 0),
                ("b-null-a", None, START, "2026-08-24T00:10:00Z", 1),
                ("b-null-b", None, "2026-08-24T02:00:00Z", END, 1),
                ("b1", "bot-1", "2026-08-24T02:00:00Z", None, 1),
                # Joined exactly at the exclusive end must not enter the window.
                ("outside-end", "human-3", END, None, 0),
                # A session that left before the window must not enter it.
                ("outside-left", "human-4", "2026-08-23T20:00:00Z", "2026-08-23T23:59:59Z", 0),
            ],
        )

    def tearDown(self):
        self.connection.close()

    def test_population_counts_distinct_identities_and_observed_null_entities(self):
        result = _population_contract(self.connection, START, END)

        self.assertEqual(result["sessions"], 7)
        self.assertEqual(result["human_sessions"], 4)
        self.assertEqual(result["bot_sessions"], 3)
        self.assertEqual(result["human_players"], 2)
        self.assertEqual(result["bot_players"], 1)
        self.assertEqual(result["identified_entities"], 3)
        self.assertEqual(result["identified_human_sessions"], 3)
        self.assertEqual(result["identified_bot_sessions"], 1)
        self.assertEqual(result["null_identity_sessions"], 3)
        self.assertEqual(result["unidentified_human_sessions"], 1)
        self.assertEqual(result["unidentified_bot_sessions"], 2)
        self.assertEqual(result["player_entities"], 6)
        self.assertAlmostEqual(result["identity_quality"]["human_identity_coverage"], .75)
        self.assertAlmostEqual(result["identity_quality"]["bot_identity_coverage"], 1 / 3)

    def test_round_rows_expose_slots_and_unique_population(self):
        self.connection.executescript(
            """CREATE TABLE rounds(
                round_id TEXT PRIMARY KEY, started_at TEXT, duration_ms REAL,
                player_count INTEGER, human_count INTEGER, bot_count INTEGER,
                winner_json TEXT, result_source TEXT
            );
            CREATE TABLE round_players(
                round_id TEXT, player_session_id TEXT, late_join INTEGER
            );"""
        )
        self.connection.execute(
            "INSERT INTO rounds VALUES('r1', ?, 120000, 5, 3, 2, '{}', 'exact')",
            (START,),
        )
        self.connection.executemany(
            "INSERT INTO round_players VALUES('r1',?,?)",
            [("h1-a", 0), ("h1-b", 1), ("h-null", 0), ("b-null-a", 0), ("b1", 0)],
        )

        result = round_metric_rows(self.connection, START, END)[0]

        self.assertEqual(result["player_slots"], 5)
        self.assertEqual(result["human_slots"], 3)
        self.assertEqual(result["bot_slots"], 2)
        self.assertEqual(result["joined_session_count"], 5)
        self.assertEqual(result["unique_players"], 2)
        self.assertEqual(result["unique_human_players"], 1)
        self.assertEqual(result["unique_bot_players"], 1)
        self.assertEqual(result["unidentified_player_sessions"], 2)
        self.assertEqual(result["bot_sessions"], 2)


class NetworkIdentityMappingTests(unittest.TestCase):
    def test_network_has_session_identity_mapping_and_player_rollup(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """CREATE TABLE player_sessions(
                    player_session_id TEXT PRIMARY KEY,
                    player_identity_id TEXT,
                    is_bot INTEGER NOT NULL
                );
                CREATE TABLE network_samples(
                    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_session_id TEXT,
                    utc_timestamp TEXT,
                    ping_ms REAL
                );"""
            )
            connection.executemany(
                "INSERT INTO player_sessions VALUES(?,?,?)",
                [("h1-a", "human-1", 0), ("h1-b", "human-1", 0), ("b-null", None, 1)],
            )
            connection.executemany(
                "INSERT INTO network_samples(player_session_id,utc_timestamp,ping_ms) VALUES(?,?,?)",
                [
                    ("h1-a", START, 100),
                    ("h1-a", "2026-08-24T00:00:10Z", 200),
                    ("h1-b", "2026-08-24T00:00:05Z", 150),
                    ("h1-b", "2026-08-24T00:00:15Z", 250),
                    ("b-null", START, 90),
                    # End is exclusive and must not alter the 4-sample rollup.
                    ("h1-a", END, 999),
                ],
            )

            sessions, players = _ping_metrics_by_session(connection, START, END, include_players=True)

        self.assertEqual({row["player_session_id"] for row in sessions}, {"h1-a", "h1-b", "b-null"})
        by_session = {row["player_session_id"]: row for row in sessions}
        self.assertEqual(by_session["h1-a"]["player_identity_id"], "human-1")
        self.assertEqual(by_session["h1-a"]["identity_quality"], "identified")
        self.assertEqual(by_session["b-null"]["identity_quality"], "unidentified_bot_session")

        human = next(row for row in players if row["player_identity_id"] == "human-1")
        self.assertEqual(human["session_count"], 2)
        self.assertEqual(human["samples"], 4)
        self.assertEqual(human["p50"], 175)
        self.assertAlmostEqual(human["p95"], 242.5)
        unknown_bot = next(row for row in players if row["player_session_id"] == "b-null")
        self.assertEqual(unknown_bot["player_identity_id"], None)
        self.assertEqual(unknown_bot["identity_quality"], "unidentified_bot_session")


if __name__ == "__main__":
    unittest.main()
