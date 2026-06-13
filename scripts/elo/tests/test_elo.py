"""Unit tests for the pure ELO logic (no I/O)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from elo.elo import (  # noqa: E402
    EloConfig, apply_seasonal_decay, blended, classify_round, expected,
    k_factor, mov_multiplier, process_match, team_key, update,
)


CFG = EloConfig()


class TestExpected(unittest.TestCase):
    def test_equal_ratings(self):
        self.assertAlmostEqual(expected(1500, 1500), 0.5)

    def test_symmetry(self):
        for a, b in [(1500, 1700), (1900, 1400), (1234, 1567)]:
            self.assertAlmostEqual(expected(a, b) + expected(b, a), 1.0)

    def test_higher_rating_wins_more(self):
        self.assertGreater(expected(1800, 1500), 0.5)
        self.assertLess(expected(1200, 1500), 0.5)


class TestUpdate(unittest.TestCase):
    def test_winner_gains_loser_loses_equal(self):
        a, b = update(1500, 1500, 1.0, k=40)
        self.assertGreater(a, 1500)
        self.assertLess(b, 1500)
        self.assertAlmostEqual(a - 1500, 1500 - b)

    def test_upset_bigger_delta(self):
        # Weak player (1300) beats strong (1700) → larger delta than even matchup
        weak_new, _ = update(1300, 1700, 1.0, k=40)
        even_new, _ = update(1500, 1500, 1.0, k=40)
        self.assertGreater(weak_new - 1300, even_new - 1500)


class TestMoV(unittest.TestCase):
    def test_two_zero_more_than_two_one(self):
        self.assertGreater(mov_multiplier(2, 0), mov_multiplier(2, 1))

    def test_log_with_scores(self):
        # 2:0 21-15, 21-17 → diff = 10 → mov = 1 + ln(2) ≈ 1.693
        m = mov_multiplier(2, 0, [(21, 15), (21, 17)])
        self.assertGreater(m, 1.5)
        self.assertLess(m, 2.0)

    def test_close_match_smaller_mov(self):
        close = mov_multiplier(2, 1, [(21, 19), (19, 21), (15, 13)])
        blowout = mov_multiplier(2, 0, [(21, 10), (21, 8)])
        self.assertLess(close, blowout)


class TestKFactor(unittest.TestCase):
    def test_provisional_higher(self):
        prov = k_factor(CFG, "main", matches_played=5)
        reg  = k_factor(CFG, "main", matches_played=50)
        self.assertGreater(prov, reg)

    def test_final_higher_than_quali(self):
        f = k_factor(CFG, "final", matches_played=50)
        q = k_factor(CFG, "quali", matches_played=50)
        self.assertGreater(f, q)

    def test_sources_equally_weighted(self):
        # We deliberately weight DVV/FIVB/bvb equally now — the per-source
        # competition-level adjustment is handled by the opponent ratings
        # themselves, not by an extra K multiplier.
        dvv  = k_factor(CFG, "main", matches_played=50, source="dvv")
        fivb = k_factor(CFG, "main", matches_played=50, source="fivb")
        bvb  = k_factor(CFG, "main", matches_played=50, source="bvb")
        self.assertEqual(dvv, fivb)
        self.assertEqual(dvv, bvb)


class TestBlend(unittest.TestCase):
    def test_pure_individual_when_team_unknown(self):
        b = blended(1600, 1600, None, 0, CFG)
        self.assertAlmostEqual(b, 1600)

    def test_pure_individual_when_team_below_threshold(self):
        b = blended(1600, 1600, 1800, team_matches=2, cfg=CFG)
        self.assertAlmostEqual(b, 1600)

    def test_team_mixed_in_after_threshold(self):
        b = blended(1600, 1600, 1800, team_matches=10, cfg=CFG)
        # 0.6 * 1600 + 0.4 * 1800 = 1680
        self.assertAlmostEqual(b, 1680)


class TestDecay(unittest.TestCase):
    def test_active_player_pulled_toward_target(self):
        new = apply_seasonal_decay(1700, matches_in_season=10, cfg=CFG)
        # 10% pull toward 1500 → 1680
        self.assertAlmostEqual(new, 1680)

    def test_inactive_skipped(self):
        new = apply_seasonal_decay(1700, matches_in_season=1, cfg=CFG)
        self.assertEqual(new, 1700)


class TestClassifyRound(unittest.TestCase):
    def test_german_finals(self):
        self.assertEqual(classify_round("Finale"), "final")
        self.assertEqual(classify_round("Spiel um Platz 3"), "final")
    def test_quali(self):
        self.assertEqual(classify_round("Qualifikation Runde 1"), "quali")
    def test_main(self):
        self.assertEqual(classify_round("Achtelfinale"), "main")
        self.assertEqual(classify_round("Viertelfinale"), "main")
        self.assertEqual(classify_round("Pool A"), "main")
        self.assertEqual(classify_round("Round of 16"), "main")

    def test_bvbinfo_finals(self):
        self.assertEqual(classify_round("Gold Medal"), "final")
        self.assertEqual(classify_round("Bronze Medal"), "final")
        self.assertEqual(classify_round("Semi-Finals"), "final")
        self.assertEqual(classify_round("Finals"), "final")


class TestProcessMatch(unittest.TestCase):
    def test_basic_match_updates(self):
        elo_ind = {}
        elo_team = {}
        n_ind = {}
        n_team = {}
        upd = process_match(
            cfg=CFG,
            p1a="a", p1b="b", p2a="c", p2b="d",
            team1_id=team_key("a", "b"), team2_id=team_key("c", "d"),
            elo_indiv=elo_ind, elo_team=elo_team,
            n_played_ind=n_ind, n_played_team=n_team,
            winner=1, sets_won_1=2, sets_lost_1=0,
            set_scores=[(21, 18), (21, 15)],
            round_kind="main", source="dvv",
        )
        # Winners go up, losers go down
        self.assertGreater(upd.new_indiv["a"], 1500)
        self.assertGreater(upd.new_indiv["b"], 1500)
        self.assertLess(upd.new_indiv["c"], 1500)
        self.assertLess(upd.new_indiv["d"], 1500)
        # Team rating tracks together
        self.assertGreater(upd.new_team[team_key("a", "b")], 1500)
        self.assertLess(upd.new_team[team_key("c", "d")], 1500)
        # Predicted prob was even (all unknown players)
        self.assertAlmostEqual(upd.predicted_p1, 0.5)


class TestTeamKey(unittest.TestCase):
    def test_order_invariant(self):
        self.assertEqual(team_key("x", "y"), team_key("y", "x"))


if __name__ == "__main__":
    unittest.main()
