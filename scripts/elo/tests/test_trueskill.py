"""Sanity tests for the TrueSkill adapter."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from elo.trueskill_model import TrueSkillConfig, TrueSkillModel  # noqa: E402


def _match(winner=1):
    return {
        "date": "2026-01-15",
        "player1a": "a", "player1b": "b",
        "player2a": "c", "player2b": "d",
        "team1_id": "a|b", "team2_id": "c|d",
        "winner": winner, "sets_won_1": 2, "sets_won_2": 0,
        "set_scores": [(21, 18), (21, 17)],
        "round_kind": "main", "source": "dvv",
        "team1_country": "DE", "team2_country": "DE", "gender": "m",
    }


class TestModelBasics(unittest.TestCase):
    def test_initial_state(self):
        m = TrueSkillModel.from_overrides({})
        s = m._get("p1")
        # Default TrueSkill: mu=25, sigma=25/3
        self.assertAlmostEqual(s.mu,    25.0)
        self.assertAlmostEqual(s.sigma, 25.0 / 3.0, places=4)

    def test_winner_mu_increases(self):
        m = TrueSkillModel.from_overrides({})
        m.process_match(_match(winner=1))
        self.assertGreater(m._state["a"].mu, 25.0)
        self.assertGreater(m._state["b"].mu, 25.0)
        self.assertLess(m._state["c"].mu, 25.0)
        self.assertLess(m._state["d"].mu, 25.0)

    def test_sigma_decreases_with_play(self):
        m = TrueSkillModel.from_overrides({})
        # 30 ish matches involving "a" — σ should shrink notably
        s0 = m._get("a")
        sigma_before = s0.sigma
        for i in range(30):
            mm = _match(winner=1 if i % 2 == 0 else 2)
            mm["player2a"] = f"opp{i}_x"; mm["player2b"] = f"opp{i}_y"
            mm["team2_id"] = f"opp{i}_x|opp{i}_y"
            m.process_match(mm)
        self.assertLess(m._state["a"].sigma, sigma_before * 0.85)

    def test_predicted_p1_within_bounds(self):
        m = TrueSkillModel.from_overrides({})
        upd = m.process_match(_match(winner=1))
        self.assertGreater(upd.predicted_p1, 0.0)
        self.assertLess(upd.predicted_p1, 1.0)


class TestSliderSpec(unittest.TestCase):
    def test_spec_has_required_keys(self):
        spec = TrueSkillModel.slider_spec()
        required = {"key", "label", "min", "max", "step", "default", "fmt"}
        for entry in spec:
            self.assertTrue(required.issubset(entry.keys()))


if __name__ == "__main__":
    unittest.main()
