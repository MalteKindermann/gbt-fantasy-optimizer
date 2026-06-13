"""
RatingModel protocol + factory dispatch.

Three rating systems run side-by-side over the same match record list:

  * EloModel        - the existing classical ELO (cfg = elo.EloConfig)
  * Glicko2Model    - Glickman 2012 with rating periods
  * TrueSkillModel  - Microsoft Research TrueSkill via the `trueskill` package

Each model owns its own per-player and per-team state.  The runner loop
(`scripts/elo/runner.py`) is generic — it only calls model.decay_year(),
model.predict_p1(), and model.update() and accumulates metadata
(country / gender / last-active) on the side.

For UI display each model exposes a single-number `display_indiv` /
`display_team` so the existing ranking JSON schema stays identical
across models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


# ── Shared return shape for per-match updates ────────────────────────────────

@dataclass
class MatchUpdate:
    """What a model produces when it processes one match."""
    predicted_p1: float
    # Pre-match raw state (for history persistence)
    pre_indiv: dict[str, Any]
    pre_team:  dict[str, Any]
    # Post-match raw state (will be stored back into the model)
    new_indiv: dict[str, Any]
    new_team:  dict[str, Any]


# ── Protocol every concrete model implements ─────────────────────────────────

class RatingModel(Protocol):
    name: str

    # ── State accessors used by the runner for snapshotting ──
    @property
    def state_indiv(self) -> dict[str, Any]: ...
    @property
    def state_team(self) -> dict[str, Any]: ...
    @property
    def n_played_ind(self) -> dict[str, int]: ...
    @property
    def n_played_team(self) -> dict[str, int]: ...

    # ── Per-match operations ──
    def set_priors(self, priors: dict[str, dict]) -> None:
        """Inject `{player_id: {"rating_g1": float, ...}}` cold-start priors.
        Each model converts the g1-scale rating to its native parameterisation
        on first state lookup for that player."""
    def decay_year(self) -> None:
        """Apply one year of seasonal decay/inflation, in place."""
    def process_match(self, match: dict) -> MatchUpdate:
        """Predict & update in one go.  The model decides internally how to
        gate the update (e.g. Glicko-2 may queue and apply at period end)."""
    def flush_pending(self) -> None:
        """Apply any queued updates (Glicko-2 period boundary, etc.).  Called
        once after the last match in the input list."""

    # ── UI exposure ──
    def display_indiv(self, pid: str) -> float:
        """Convert raw state to the single-number display rating."""
    def display_team(self, tid: str) -> float: ...
    def display_offset(self) -> float:
        """For UI: the value of an 'average' player.  ELO uses 1500, Glicko-2
        also 1500, TrueSkill (with default mu=25) uses 25.  Lets the ranking
        UI fall back sanely when a player has no team."""

    @classmethod
    def slider_spec(cls) -> list[dict]:
        """List of slider definitions for the tuning UI.  Each entry:
        {key, label, min, max, step, default, fmt}."""
    @classmethod
    def from_overrides(cls, overrides: dict) -> "RatingModel":
        """Construct a fresh model instance with the given config overrides
        applied on top of defaults."""


# ── Factory ──────────────────────────────────────────────────────────────────

def make_model(model_id: str, overrides: Optional[dict] = None) -> RatingModel:
    overrides = overrides or {}
    if model_id == "elo":
        from elo.elo_adapter import EloModel
        return EloModel.from_overrides(overrides)
    if model_id == "glicko2":
        from elo.glicko2 import Glicko2Model
        return Glicko2Model.from_overrides(overrides)
    if model_id == "trueskill":
        from elo.trueskill_model import TrueSkillModel
        return TrueSkillModel.from_overrides(overrides)
    if model_id == "ensemble":
        from elo.ensemble import EnsembleModel
        return EnsembleModel.from_overrides(overrides)
    raise ValueError(f"Unknown rating model: {model_id!r}")


def available_models() -> list[dict]:
    """List of (id, display_name) for the UI dropdown."""
    return [
        {"id": "elo",       "name": "ELO (klassisch)"},
        {"id": "glicko2",   "name": "Glicko-2"},
        {"id": "trueskill", "name": "TrueSkill"},
        {"id": "ensemble",  "name": "Ensemble (3-Modell-Mittel)"},
    ]
