"""Tests for the Glicko-2 implementation.

The headline test reproduces the reference example from Glickman's
"Example of the Glicko-2 system" (2013), section 'Step-by-step example'.

  Player has initial (μ=1500, φ=200, σ=0.06).
  Plays three matches in one rating period:
    - vs (1400, 30) → win
    - vs (1550, 100) → loss
    - vs (1700, 300) → loss

  After the update he should be at approximately
    μ ≈ 1464.05, φ ≈ 151.52, σ ≈ 0.05999.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from elo.glicko2 import (  # noqa: E402
    Glicko2Config, Glicko2Model, _solve_volatility,
    _to_g2, _g, _E,
)


class TestGlickmanPaperExample(unittest.TestCase):
    """Verbatim reproduction of Glickman's worked example."""

    def test_step_by_step(self):
        # Build a custom config: tau = 0.5 per Glickman example
        cfg = Glicko2Config(initial_mu=1500, initial_phi=200,
                            initial_sigma=0.06, tau=0.5)

        mu, phi = _to_g2(1500.0, 200.0)
        sigma   = 0.06

        opponents_g1 = [(1400, 30, 1), (1550, 100, 0), (1700, 300, 0)]
        v_inv = 0.0
        delta_sum = 0.0
        for mu_j_g1, phi_j_g1, score in opponents_g1:
            mu_j_g2, phi_j_g2 = _to_g2(mu_j_g1, phi_j_g1)
            E_j = _E(mu, mu_j_g2, phi_j_g2)
            g_j = _g(phi_j_g2)
            v_inv += g_j * g_j * E_j * (1.0 - E_j)
            delta_sum += g_j * (score - E_j)
        v = 1.0 / v_inv
        delta = v * delta_sum

        new_sigma = _solve_volatility(sigma, phi, v, delta, cfg.tau, cfg.epsilon)
        phi_star = math.sqrt(phi * phi + new_sigma * new_sigma)
        new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
        new_mu  = mu + new_phi * new_phi * delta_sum

        # Back to Glicko-1 scale
        final_mu  = 1500.0 + 173.7178 * new_mu
        final_phi = 173.7178 * new_phi

        self.assertAlmostEqual(final_mu,  1464.06, places=1)
        self.assertAlmostEqual(final_phi, 151.52, places=1)
        self.assertAlmostEqual(new_sigma,  0.05999, places=4)


class TestModelBasics(unittest.TestCase):
    def test_initial_state(self):
        m = Glicko2Model.from_overrides({})
        s = m._get("test_player")
        self.assertEqual(s.mu, 1500.0)
        self.assertEqual(s.phi, m.cfg.initial_phi)
        self.assertEqual(s.sigma, 0.06)

    def test_winner_rating_increases(self):
        m = Glicko2Model.from_overrides({})
        match = {
            "date": "2026-01-15",
            "player1a": "a", "player1b": "b",
            "player2a": "c", "player2b": "d",
            "team1_id": "a|b", "team2_id": "c|d",
            "winner": 1,
            "sets_won_1": 2, "sets_won_2": 0,
            "set_scores": [(21, 18), (21, 17)],
            "round_kind": "main", "source": "dvv",
            "team1_country": "DE", "team2_country": "DE",
            "gender": "m",
        }
        m.process_match(match)
        # Force the period to flush by stepping to the next week
        future = dict(match); future["date"] = "2026-02-15"
        future["player1a"] = "e"; future["player1b"] = "f"
        future["player2a"] = "g"; future["player2b"] = "h"
        future["team1_id"] = "e|f"; future["team2_id"] = "g|h"
        m.process_match(future)
        m.flush_pending()
        # Players a/b were winners → μ > 1500, c/d losers → μ < 1500
        self.assertGreater(m._state["a"].mu, 1500.0)
        self.assertGreater(m._state["b"].mu, 1500.0)
        self.assertLess(m._state["c"].mu, 1500.0)
        self.assertLess(m._state["d"].mu, 1500.0)

    def test_phi_decreases_with_play(self):
        m = Glicko2Model.from_overrides({})
        s_before = m._get("p1")
        phi_before = s_before.phi
        # Many matches in one period
        for i in range(15):
            match = {
                "date": "2026-01-15",
                "player1a": "p1", "player1b": "p2",
                "player2a": f"o{i}a", "player2b": f"o{i}b",
                "team1_id": "p1|p2", "team2_id": f"o{i}a|o{i}b",
                "winner": 1, "sets_won_1": 2, "sets_won_2": 0,
                "set_scores": [(21, 15), (21, 15)],
                "round_kind": "main", "source": "dvv",
                "team1_country": "DE", "team2_country": "DE",
                "gender": "m",
            }
            m.process_match(match)
        # Trigger flush by stepping to a new period
        m._flush_period(m._current_period + 1)
        self.assertLess(m._state["p1"].phi, phi_before)


class TestSliderSpec(unittest.TestCase):
    def test_spec_has_required_keys(self):
        spec = Glicko2Model.slider_spec()
        required = {"key", "label", "min", "max", "step", "default", "fmt"}
        for entry in spec:
            self.assertTrue(required.issubset(entry.keys()))


if __name__ == "__main__":
    unittest.main()
