"""Tests for the heuristic name-aliasing pipeline."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from elo import aliases as A  # noqa: E402


class TestNameCompat(unittest.TestCase):
    def test_exact(self):
        ok, why = A._first_name_compatible("Max", "max")
        self.assertTrue(ok); self.assertEqual(why, "exact")

    def test_prefix_at_least_3(self):
        ok, why = A._first_name_compatible("Max", "Maximilian")
        self.assertTrue(ok); self.assertEqual(why, "prefix")

    def test_prefix_too_short(self):
        ok, _ = A._first_name_compatible("Jo", "Johannes")
        self.assertFalse(ok)

    def test_mismatch(self):
        ok, _ = A._first_name_compatible("Tom", "Lukas")
        self.assertFalse(ok)


class TestCountryCompat(unittest.TestCase):
    def test_dvv_de_alias(self):
        self.assertTrue(A._country_compatible("Germany", "GER"))
        self.assertTrue(A._country_compatible("Germany", "Deutschland"))

    def test_exact(self):
        self.assertTrue(A._country_compatible("Brazil", "Brazil"))

    def test_blank_passes(self):
        self.assertTrue(A._country_compatible("", "Brazil"))

    def test_mismatch(self):
        self.assertFalse(A._country_compatible("Germany", "Brazil"))


class TestBuildMerges(unittest.TestCase):
    def test_prefix_country_merge(self):
        cands = [
            {"src": "dvv", "first": "Max",        "last": "Just",
             "country": "Germany", "birthdate": None, "n_played": 5},
            {"src": "fivb", "first": "Maximilian", "last": "Just",
             "country": "Germany", "birthdate": "1998-01-01", "n_played": 12},
        ]
        doc = A.build_merges(cands)
        self.assertEqual(len(doc["merges"]), 1)
        m = doc["merges"][0]
        # Maximilian wins (longer firstname)
        self.assertEqual(m["canonical"], "just_maximilian")
        self.assertIn("just_max", m["alternatives"])
        self.assertEqual(m["confidence"], "medium")

    def test_birthdate_merge_high_confidence(self):
        cands = [
            {"src": "fivb", "first": "Anders", "last": "Mol",
             "country": "Norway", "birthdate": "1997-08-01", "n_played": 50},
            {"src": "fivb", "first": "A.",     "last": "Mol",
             "country": "Norway", "birthdate": "1997-08-01", "n_played": 2},
        ]
        doc = A.build_merges(cands)
        self.assertEqual(len(doc["merges"]), 1)
        self.assertEqual(doc["merges"][0]["confidence"], "high")

    def test_no_merge_on_country_mismatch(self):
        cands = [
            {"src": "dvv", "first": "Max",        "last": "Just",
             "country": "Germany", "birthdate": None, "n_played": 5},
            {"src": "fivb", "first": "Maximilian", "last": "Just",
             "country": "Brazil",  "birthdate": None, "n_played": 5},
        ]
        doc = A.build_merges(cands)
        self.assertEqual(len(doc["merges"]), 0)
        self.assertTrue(any(i["reason"] == "country_mismatch"
                            for i in doc["ignored_collisions"]))

    def test_single_carrier_no_alias(self):
        cands = [{"src": "dvv", "first": "Solo", "last": "Person",
                  "country": "Germany", "birthdate": None, "n_played": 1}]
        doc = A.build_merges(cands)
        self.assertEqual(len(doc["merges"]), 0)


class TestApplyAliases(unittest.TestCase):
    def test_remap_player_ids_and_team_ids(self):
        from elo import elo as elo_math
        records = [{
            "player1a": "just_max", "player1b": "kraft_paul",
            "player2a": "smith_john", "player2b": "doe_jane",
            "team1_id": elo_math.team_key("just_max", "kraft_paul"),
            "team2_id": elo_math.team_key("smith_john", "doe_jane"),
        }]
        mapping = {"just_max": "just_maximilian"}
        n = A.apply_aliases(records, mapping)
        self.assertEqual(n, 1)
        self.assertEqual(records[0]["player1a"], "just_maximilian")
        # team1_id must reflect the remapped player id
        expect = elo_math.team_key("just_maximilian", "kraft_paul")
        self.assertEqual(records[0]["team1_id"], expect)


if __name__ == "__main__":
    unittest.main()
