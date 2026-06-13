"""
EnsembleModel — averages predictions from ELO, Glicko-2, TrueSkill.

Implements the RatingModel protocol so it slots into the existing runner /
build / API plumbing without special-casing. Internally it owns three child
models and runs them in lockstep on each match. Predictions are the
configurable weighted mean of the three children; display ratings are
normalised onto the ELO scale (1500-base) for the ranking UI.

Default weights (1, 1, 1) → equal-weight mean. Set any weight to 0 to drop
that child from the ensemble; useful for A/B-comparing 2-of-3 combinations
via the tuning UI.

Why this is worth ~+0.3-1.0pp accuracy: the three models have systematically
different failure modes (ELO smooth-but-stale, Glicko-2 well-calibrated but
slow to react to streaks, TrueSkill team-aware but Halo-tuned defaults
overshoot). Averaging the predictions cancels uncorrelated errors.
"""
from __future__ import annotations

from dataclasses import dataclass, replace as _dc_replace
from typing import Any

from elo.models import MatchUpdate
from elo.elo_adapter import EloModel
from elo.glicko2 import Glicko2Model
from elo.trueskill_model import TrueSkillModel


@dataclass(frozen=True)
class EnsembleConfig:
    weight_elo:       float = 1.0
    weight_glicko2:   float = 1.0
    weight_trueskill: float = 1.0


def _normalise_to_elo(rating: float, model_offset: float) -> float:
    """Convert a model's display rating to an ELO-1500-equivalent. Each model's
    `display_offset` is its 'average player' value (ELO: 1500, Glicko-2: 1500,
    TrueSkill: ~700 with our scale/offset)."""
    return 1500.0 + (rating - model_offset)


class EnsembleModel:
    name = "ensemble"

    def __init__(self, cfg: EnsembleConfig):
        self.cfg = cfg
        self.children: dict[str, Any] = {
            "elo":       EloModel.from_overrides({}),
            "glicko2":   Glicko2Model.from_overrides({}),
            "trueskill": TrueSkillModel.from_overrides({}),
        }
        self._weights = {
            "elo":       max(0.0, cfg.weight_elo),
            "glicko2":   max(0.0, cfg.weight_glicko2),
            "trueskill": max(0.0, cfg.weight_trueskill),
        }
        # Combined-state mirrors for runner's snapshot needs
        self._state_indiv: dict[str, float] = {}
        self._state_team:  dict[str, float] = {}
        self._n_played_ind:  dict[str, int] = {}
        self._n_played_team: dict[str, int] = {}

    # ── Priors fan-out ──
    def set_priors(self, priors: dict[str, dict]) -> None:
        for child in self.children.values():
            child.set_priors(priors)

    # ── State views ──
    # Plain attribute dicts updated incrementally during process_match.
    # Values are placeholders — the runner only does membership checks
    # (`tid in model.state_team`) and `for pid in model.state_indiv`.
    # Actual display ratings come from display_indiv / display_team.

    @property
    def state_indiv(self) -> dict[str, float]:
        return self._state_indiv

    @property
    def state_team(self) -> dict[str, float]:
        return self._state_team

    @property
    def n_played_ind(self) -> dict[str, int]:
        return self._n_played_ind

    @property
    def n_played_team(self) -> dict[str, int]:
        return self._n_played_team

    # ── Per-match ──
    def decay_year(self) -> None:
        for c in self.children.values():
            c.decay_year()

    def process_match(self, match: dict) -> MatchUpdate:
        updates = {name: c.process_match(match) for name, c in self.children.items()}
        # Mirror keys into our state dicts so the runner's downstream
        # `pid in model.state_indiv` / `tid in model.state_team` is O(1).
        for pid in (match["player1a"], match["player1b"],
                    match["player2a"], match["player2b"]):
            if pid:
                self._state_indiv[pid] = 0.0
                self._n_played_ind[pid] = self._n_played_ind.get(pid, 0) + 1
        for tid in (match["team1_id"], match["team2_id"]):
            if tid:
                self._state_team[tid] = 0.0
                self._n_played_team[tid] = self._n_played_team.get(tid, 0) + 1
        total_w = sum(self._weights.values()) or 1.0
        pred = sum(self._weights[name] * u.predicted_p1
                   for name, u in updates.items()) / total_w

        # Pre/new dicts: aggregate per pid using ELO-normalised display values
        def _disp(c, pid):
            return _normalise_to_elo(c.display_indiv(pid), c.display_offset())

        pids = set()
        for u in updates.values():
            pids.update(u.pre_indiv.keys())
        tids = set()
        for u in updates.values():
            tids.update(u.pre_team.keys())

        pre_indiv = {pid: sum(self._weights[name] *
                              _normalise_to_elo(
                                  updates[name].pre_indiv.get(pid,
                                      self.children[name].display_offset()),
                                  self.children[name].display_offset())
                              for name in self.children) / total_w
                     for pid in pids}
        new_indiv = {pid: _disp(self, pid) for pid in pids}

        def _team_disp_pre(name, tid):
            v = updates[name].pre_team.get(tid)
            if v is None:
                v = self.children[name].display_offset()
            return _normalise_to_elo(v, self.children[name].display_offset())

        pre_team = {tid: sum(self._weights[name] * _team_disp_pre(name, tid)
                             for name in self.children) / total_w
                    for tid in tids}
        new_team = {tid: self.display_team(tid) for tid in tids}

        return MatchUpdate(
            predicted_p1=pred,
            pre_indiv=pre_indiv, pre_team=pre_team,
            new_indiv=new_indiv, new_team=new_team,
        )

    def flush_pending(self) -> None:
        for c in self.children.values():
            c.flush_pending()

    # ── UI display ──
    def display_indiv(self, pid: str) -> float:
        total_w = sum(self._weights.values()) or 1.0
        s = 0.0
        for name, c in self.children.items():
            w = self._weights[name]
            if w <= 0.0:
                continue
            s += w * _normalise_to_elo(c.display_indiv(pid), c.display_offset())
        return s / total_w

    def display_team(self, tid: str) -> float:
        total_w = sum(self._weights.values()) or 1.0
        s = 0.0
        for name, c in self.children.items():
            w = self._weights[name]
            if w <= 0.0:
                continue
            s += w * _normalise_to_elo(c.display_team(tid), c.display_offset())
        return s / total_w

    def display_offset(self) -> float:
        return 1500.0

    # ── Tuning UI ──
    @classmethod
    def slider_spec(cls) -> list[dict]:
        return [
            {"key": "weight_elo",       "label": "Gewicht ELO",
             "min": 0.0, "max": 2.0, "step": 0.1, "default": 1.0, "fmt": "f1"},
            {"key": "weight_glicko2",   "label": "Gewicht Glicko-2",
             "min": 0.0, "max": 2.0, "step": 0.1, "default": 1.0, "fmt": "f1"},
            {"key": "weight_trueskill", "label": "Gewicht TrueSkill",
             "min": 0.0, "max": 2.0, "step": 0.1, "default": 1.0, "fmt": "f1"},
        ]

    @classmethod
    def from_overrides(cls, overrides: dict) -> "EnsembleModel":
        base = EnsembleConfig()
        allowed = set(base.__dataclass_fields__)
        valid = {k: v for k, v in overrides.items() if k in allowed}
        cfg = _dc_replace(base, **valid)
        return cls(cfg)
