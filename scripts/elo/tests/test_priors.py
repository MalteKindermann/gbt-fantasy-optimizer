"""Tests for cold-start priors injection across all three rating models."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from elo import priors as elo_priors  # noqa: E402
from elo.elo_adapter import EloModel  # noqa: E402
from elo.glicko2 import Glicko2Model  # noqa: E402
from elo.trueskill_model import TrueSkillModel  # noqa: E402


SAMPLE_PRIORS = {
    "wickler_clemens": {"rating_g1": 1800.0, "source": "dvv_points", "points": 3000},
    "kraft_paul":      {"rating_g1": 1500.0, "source": "dvv_points", "points": 750},
}


class TestPointsToRating(unittest.TestCase):
    def test_zero_points_is_floor(self):
        self.assertEqual(elo_priors.points_to_g1_rating(0), elo_priors.PRIOR_FLOOR)

    def test_high_points_clamps_to_ceil(self):
        self.assertEqual(elo_priors.points_to_g1_rating(99999), elo_priors.PRIOR_CEIL)

    def test_linear_midpoint(self):
        # 1500 pts → halfway between 1400 and 1800 → 1600
        r = elo_priors.points_to_g1_rating(1500)
        self.assertAlmostEqual(r, 1600.0, places=1)


class TestEloPriors(unittest.TestCase):
    def test_display_reflects_prior_before_any_match(self):
        m = EloModel.from_overrides({})
        m.set_priors(SAMPLE_PRIORS)
        self.assertAlmostEqual(m.display_indiv("wickler_clemens"), 1800.0, places=1)
        self.assertAlmostEqual(m.display_indiv("kraft_paul"),      1500.0, places=1)
        # Unknown player falls back to default
        self.assertAlmostEqual(m.display_indiv("nobody_at_all"),   m.cfg.start, places=1)

    def test_first_match_uses_prior_as_starting_state(self):
        m = EloModel.from_overrides({})
        m.set_priors(SAMPLE_PRIORS)
        match = {
            "player1a": "wickler_clemens", "player1b": "kraft_paul",
            "player2a": "nobody_one",       "player2b": "nobody_two",
            "team1_id": "T1", "team2_id": "T2",
            "winner": 1, "sets_won_1": 2, "sets_won_2": 0,
            "set_scores": [(21, 18), (21, 15)],
            "round_kind": "main", "source": "dvv",
        }
        upd = m.process_match(match)
        # Pre-match state must reflect the prior, not 1500.
        self.assertAlmostEqual(upd.pre_indiv["wickler_clemens"], 1800.0, places=1)


class TestGlickoPriors(unittest.TestCase):
    def test_prior_lowers_phi_and_sets_mu(self):
        m = Glicko2Model.from_overrides({})
        m.set_priors(SAMPLE_PRIORS)
        s = m._get("wickler_clemens")
        self.assertAlmostEqual(s.mu, 1800.0, places=1)
        self.assertLessEqual(s.phi, m.cfg.initial_phi)
        self.assertLessEqual(s.phi, m.PRIOR_PHI + 1e-6)


class TestTrueSkillPriors(unittest.TestCase):
    def test_prior_lifts_mu_above_default(self):
        m = TrueSkillModel.from_overrides({})
        m.set_priors(SAMPLE_PRIORS)
        r = m._get("wickler_clemens")
        # 1800 g1 -> mu = 25 + (300 * 5/400) = 25 + 3.75
        self.assertAlmostEqual(r.mu, 25.0 + 300.0 * (5.0 / 400.0), places=3)
        self.assertLess(r.sigma, m.cfg.sigma0)


if __name__ == "__main__":
    unittest.main()
