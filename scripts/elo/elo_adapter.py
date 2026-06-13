"""
RatingModel adapter for the classical ELO (existing `elo.elo.process_match`).

The original `EloConfig` and `process_match` stay unchanged in `elo.py` so all
existing tests keep passing.  This file only provides a thin wrapper that
makes ELO conform to the `RatingModel` Protocol used by the new model-agnostic
runner.

Default config values were tuned via the grid search in scripts/elo/grid_search.py.
The joint best on rank-sum of (accuracy, calibration) lies around:
  k_base=20, blend_individual_weight=0.8, decay_pull=0.10,
  team_min_matches_for_blend=10, provisional_multiplier=2.0
On 2025+/OOS this gave acc=64.0%, calib_err=0.010 — vs the historical
default (k=40, blend=0.6) of acc=64.1%, calib_err=0.030.
"""
from __future__ import annotations

from dataclasses import replace as _dc_replace

from elo import elo as elo_math
from elo.models import MatchUpdate


class EloModel:
    name = "elo"

    def __init__(self, cfg: elo_math.EloConfig):
        self.cfg = cfg
        self._state_indiv: dict[str, float] = {}
        self._state_team:  dict[str, float] = {}
        self._n_played_ind:  dict[str, int] = {}
        self._n_played_team: dict[str, int] = {}
        self._priors: dict[str, dict] = {}

    def set_priors(self, priors: dict[str, dict]) -> None:
        self._priors = priors or {}

    def _ensure_init(self, pid: str) -> None:
        if pid in self._state_indiv or pid not in self._priors:
            return
        self._state_indiv[pid] = float(self._priors[pid]["rating_g1"])

    # ── State accessors ──
    @property
    def state_indiv(self) -> dict[str, float]: return self._state_indiv
    @property
    def state_team(self)  -> dict[str, float]: return self._state_team
    @property
    def n_played_ind(self)  -> dict[str, int]: return self._n_played_ind
    @property
    def n_played_team(self) -> dict[str, int]: return self._n_played_team

    # ── Per-year seasonal decay ──
    def decay_year(self) -> None:
        pull = self.cfg.decay_pull
        tgt  = self.cfg.decay_target
        for pid, rating in list(self._state_indiv.items()):
            self._state_indiv[pid] = rating + pull * (tgt - rating)

    # ── Per-match update ──
    def process_match(self, match: dict) -> MatchUpdate:
        r = match
        for pid in (r["player1a"], r["player1b"], r["player2a"], r["player2b"]):
            self._ensure_init(pid)
        upd = elo_math.process_match(
            cfg=self.cfg,
            p1a=r["player1a"], p1b=r["player1b"],
            p2a=r["player2a"], p2b=r["player2b"],
            team1_id=r["team1_id"], team2_id=r["team2_id"],
            elo_indiv=self._state_indiv, elo_team=self._state_team,
            n_played_ind=self._n_played_ind, n_played_team=self._n_played_team,
            winner=r["winner"],
            sets_won_1=r["sets_won_1"], sets_lost_1=r["sets_won_2"],
            set_scores=r["set_scores"] or None,
            round_kind=r["round_kind"], source=r["source"],
            category_tier=r.get("category_tier", "top"),
        )

        # Apply the updates back into our own state dicts (runner used to do
        # this directly on EloRun.elo_indiv; we own it now).
        for pid, new_r in upd.new_indiv.items():
            self._state_indiv[pid] = new_r
            self._n_played_ind[pid] = self._n_played_ind.get(pid, 0) + 1
        for tid, new_r in upd.new_team.items():
            self._state_team[tid] = new_r
            self._n_played_team[tid] = self._n_played_team.get(tid, 0) + 1

        return MatchUpdate(
            predicted_p1=upd.predicted_p1,
            pre_indiv=dict(upd.pre_indiv),
            pre_team =dict(upd.pre_team),
            new_indiv=dict(upd.new_indiv),
            new_team =dict(upd.new_team),
        )

    def flush_pending(self) -> None:
        pass   # ELO has no batched updates

    # ── UI exposure ──
    def display_indiv(self, pid: str) -> float:
        if pid in self._state_indiv:
            return self._state_indiv[pid]
        if pid in self._priors:
            return float(self._priors[pid]["rating_g1"])
        return self.cfg.start
    def display_team(self, tid: str) -> float:
        return self._state_team.get(tid, self.cfg.start)
    def display_offset(self) -> float:
        return self.cfg.start

    # ── Slider spec for the tuning UI ──
    @classmethod
    def slider_spec(cls) -> list[dict]:
        return [
            {"key": "k_base",                     "label": "K-Faktor (Basis)",
             "min": 5,    "max": 100, "step": 1,    "default": 20,  "fmt": "int"},
            {"key": "blend_individual_weight",    "label": "Blend: Anteil Einzel-ELO",
             "min": 0.3,  "max": 1.0, "step": 0.05, "default": 0.8, "fmt": "f2"},
            {"key": "decay_pull",                 "label": "Jährlicher Decay",
             "min": 0.0,  "max": 0.4, "step": 0.01, "default": 0.10, "fmt": "f2"},
            {"key": "team_min_matches_for_blend", "label": "Team-Blend Schwelle",
             "min": 1,    "max": 30,  "step": 1,    "default": 10,  "fmt": "int"},
            {"key": "provisional_multiplier",     "label": "Provisional-K (×)",
             "min": 1.0,  "max": 3.0, "step": 0.1,  "default": 2.0, "fmt": "f1"},
            {"key": "importance_quali",           "label": "Wichtigkeit Quali",
             "min": 0.3,  "max": 1.2, "step": 0.05, "default": 0.75, "fmt": "f2"},
            {"key": "importance_final",           "label": "Wichtigkeit Finals",
             "min": 1.0,  "max": 2.0, "step": 0.05, "default": 1.25, "fmt": "f2"},
            {"key": "mov_strength",               "label": "Margin-of-Victory Stärke",
             "min": 0.0,  "max": 1.5, "step": 0.05, "default": 1.0, "fmt": "f2"},
            {"key": "source_weight_dvv",          "label": "Quelle DVV (Gewicht)",
             "min": 0.0,  "max": 2.0, "step": 0.05, "default": 1.0, "fmt": "f2"},
            {"key": "source_weight_fivb",         "label": "Quelle FIVB (Gewicht)",
             "min": 0.0,  "max": 2.0, "step": 0.05, "default": 1.0, "fmt": "f2"},
            {"key": "source_weight_bvb",          "label": "Quelle bvbinfo (Gewicht)",
             "min": 0.0,  "max": 2.0, "step": 0.05, "default": 1.0, "fmt": "f2"},
            {"key": "tier_weight_challenger",     "label": "DVV Challenger-Format (Gewicht)",
             "min": 0.0,  "max": 1.5, "step": 0.05, "default": 0.5, "fmt": "f2"},
            {"key": "tier_weight_qualifier",      "label": "DVV Qualifier-only (Gewicht)",
             "min": 0.0,  "max": 1.5, "step": 0.05, "default": 0.3, "fmt": "f2"},
        ]

    @classmethod
    def from_overrides(cls, overrides: dict) -> "EloModel":
        # Build a config from the new grid-tuned defaults, then apply overrides.
        base = elo_math.EloConfig(
            k_base=30,
            blend_individual_weight=0.8,
            blend_team_weight=0.2,
            decay_pull=0.10,
            team_min_matches_for_blend=10,
            provisional_multiplier=2.0,
            importance_quali=0.75,
            importance_final=1.25,
        )
        allowed = set(base.__dataclass_fields__)
        valid = {k: v for k, v in overrides.items() if k in allowed}
        if "blend_individual_weight" in valid:
            valid.setdefault("blend_team_weight",
                             1.0 - valid["blend_individual_weight"])
        cfg = _dc_replace(base, **valid)
        return cls(cfg)
