"""
Glicko-2 implementation, faithful to Mark Glickman's reference paper:
    "Example of the Glicko-2 system" (2013)
    http://www.glicko.net/glicko/glicko2.pdf

Per-player state is (μ, φ, σ):
    μ   — skill estimate (centered around 1500 in Glicko-1 scale)
    φ   — rating deviation (uncertainty); ~ 350 for new players, ~ 30 for veterans
    σ   — rating volatility; expected fluctuation rate over time

The key algorithmic distinction vs ELO:
  1. Updates happen per "rating period" (a batch of matches), not per match.
     We batch by calendar week — typically 3-5 matches per player per week
     during the season, which is what Glickman recommends.
  2. New players have huge φ and so their μ moves fast over the first few
     matches; veterans have small φ and barely move. No artificial K-factor
     scaling needed.
  3. The volatility σ adapts per player: streaky players get higher σ,
     consistent ones lower σ, automatically.

We adapt Glicko-2 to 2v2 beach matches as follows:
  - Each player's per-period opponent list is the OTHER team's two players
    (counted twice — once per opposing player), with the appropriate score
    (1.0 if your team won, 0.0 if you lost).
  - Team ratings are tracked as a parallel weighted-average of the two
    players' (μ, φ), updated at flush time — purely a display layer, NOT
    an independent Glicko-2 entity (avoids double-counting).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace as _dc_replace
from datetime import date as _date
from typing import Optional

from elo.models import MatchUpdate


# ── Constants ────────────────────────────────────────────────────────────────

# Conversion between Glicko-1 (μ around 1500) and Glicko-2 (around 0) scale.
_SCALE = 173.7178


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Glicko2Config:
    initial_mu:    float = 1500.0   # Glicko-1 scale
    initial_phi:   float = 200.0    # Glicko-1 scale (tuned from grid 2026-06)
    initial_sigma: float = 0.06     # always in Glicko-2 scale
    tau:           float = 0.3      # system volatility constraint (tuned)
    rating_period_days: int = 7     # batch matches by calendar week

    # Inactivity: between periods, φ inflates toward initial.  Glickman's
    # formula `φ' = sqrt(φ² + σ²)` is applied at the end of every empty
    # rating period the player skips.
    epsilon: float = 0.000001       # convergence threshold for σ update


# ── Per-player state ─────────────────────────────────────────────────────────

@dataclass
class _G2State:
    mu:    float          # Glicko-1 scale (1500-based)
    phi:   float          # Glicko-1 scale
    sigma: float          # Glicko-2 scale (always)
    # Pending opponents for the current rating period:
    # [(opponent_mu_g2, opponent_phi_g2, score), ...]
    pending: list = field(default_factory=list)
    last_period: Optional[int] = None    # period index of last update


# ── Helpers (operate in Glicko-2 scale) ──────────────────────────────────────

def _to_g2(mu_g1: float, phi_g1: float) -> tuple[float, float]:
    return (mu_g1 - 1500.0) / _SCALE, phi_g1 / _SCALE


def _from_g2(mu_g2: float, phi_g2: float) -> tuple[float, float]:
    return 1500.0 + _SCALE * mu_g2, _SCALE * phi_g2


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _solve_volatility(sigma: float, phi: float, v: float, delta: float,
                      tau: float, eps: float) -> float:
    """Glickman's illinois-algorithm volatility update (step 5)."""
    a = math.log(sigma * sigma)
    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau

    fA, fB = f(A), f(B)
    while abs(B - A) > eps:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB < 0:
            A, fA = B, fB
        else:
            fA = fA / 2.0
        B, fB = C, fC

    return math.exp(A / 2.0)


# ── The Model ────────────────────────────────────────────────────────────────

class Glicko2Model:
    name = "glicko2"

    # φ when a prior is available — we have prior info so uncertainty is lower
    # than for a totally-new player. 200 ≈ "couple of matches played" baseline.
    PRIOR_PHI: float = 200.0

    def __init__(self, cfg: Glicko2Config):
        self.cfg = cfg
        self._state: dict[str, _G2State] = {}
        self._n_played_ind:  dict[str, int] = {}
        self._n_played_team: dict[str, int] = {}
        # Team display ratings only (no independent Glicko-2 state)
        self._team_display: dict[str, float] = {}
        self._team_display_phi: dict[str, float] = {}
        self._current_period: Optional[int] = None
        self._priors: dict[str, dict] = {}

    def set_priors(self, priors: dict[str, dict]) -> None:
        self._priors = priors or {}

    # ── State property facade ──
    @property
    def state_indiv(self) -> dict[str, _G2State]: return self._state
    @property
    def state_team(self)  -> dict[str, float]:     return self._team_display
    @property
    def n_played_ind(self)  -> dict[str, int]: return self._n_played_ind
    @property
    def n_played_team(self) -> dict[str, int]: return self._n_played_team

    # ── Helpers ──
    def _get(self, pid: str) -> _G2State:
        s = self._state.get(pid)
        if s is None:
            prior = self._priors.get(pid)
            if prior is not None:
                s = _G2State(
                    mu=float(prior["rating_g1"]),
                    phi=min(self.PRIOR_PHI, self.cfg.initial_phi),
                    sigma=self.cfg.initial_sigma,
                )
            else:
                s = _G2State(
                    mu=self.cfg.initial_mu,
                    phi=self.cfg.initial_phi,
                    sigma=self.cfg.initial_sigma,
                )
            self._state[pid] = s
        return s

    def _period_index(self, iso_date: str) -> int:
        """Map an ISO date to an integer rating-period index by configured length."""
        try:
            y, m, d = (int(x) for x in iso_date.split("-")[:3])
            day_no = _date(y, m, d).toordinal()
        except (ValueError, AttributeError):
            return 0
        return day_no // max(1, self.cfg.rating_period_days)

    def _flush_period(self, target_period: int) -> None:
        """Apply pending updates for every player whose last_period < target."""
        for pid, s in list(self._state.items()):
            # Inactive-period φ inflation per Glickman step 6
            if s.last_period is not None and s.last_period < target_period and not s.pending:
                idle = target_period - s.last_period
                if idle >= 1:
                    phi_g2 = s.phi / _SCALE
                    sig = s.sigma
                    # Apply φ' = sqrt(φ² + σ²) per skipped period
                    for _ in range(idle):
                        phi_g2 = math.sqrt(phi_g2 * phi_g2 + sig * sig)
                    s.phi = min(phi_g2 * _SCALE, self.cfg.initial_phi)
                s.last_period = target_period
                continue

            if not s.pending:
                continue

            # Active-period update per Glickman steps 3-8
            mu_g2  = (s.mu - 1500.0) / _SCALE
            phi_g2 = s.phi / _SCALE

            # Step 3-4: v and delta
            v_inv = 0.0
            delta_sum = 0.0
            for opp_mu_g2, opp_phi_g2, score in s.pending:
                E = 1.0 / (1.0 + math.exp(-_g(opp_phi_g2) * (mu_g2 - opp_mu_g2)))
                g = _g(opp_phi_g2)
                v_inv += g * g * E * (1.0 - E)
                delta_sum += g * (score - E)
            if v_inv == 0.0:
                s.pending.clear()
                s.last_period = target_period
                continue
            v = 1.0 / v_inv
            delta = v * delta_sum

            # Step 5: new volatility
            new_sigma = _solve_volatility(
                s.sigma, phi_g2, v, delta, self.cfg.tau, self.cfg.epsilon)

            # Step 6: pre-rating-period φ*
            phi_star = math.sqrt(phi_g2 * phi_g2 + new_sigma * new_sigma)

            # Step 7: new φ and μ
            new_phi_g2 = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
            new_mu_g2  = mu_g2 + new_phi_g2 * new_phi_g2 * delta_sum

            # Convert back to Glicko-1 scale
            s.mu    = 1500.0 + _SCALE * new_mu_g2
            s.phi   = _SCALE * new_phi_g2
            s.sigma = new_sigma
            s.pending.clear()
            s.last_period = target_period

    def decay_year(self) -> None:
        """Glicko-2 has no explicit yearly decay — inactivity is handled by
        φ inflation per skipped rating period.  We keep this no-op for
        protocol symmetry with the ELO model."""
        pass

    def process_match(self, match: dict) -> MatchUpdate:
        r = match
        period = self._period_index(r.get("date") or "")

        # Period boundary: flush previously-queued updates for everyone
        if self._current_period is not None and period != self._current_period:
            self._flush_period(period)
        self._current_period = period

        p1a, p1b = r["player1a"], r["player1b"]
        p2a, p2b = r["player2a"], r["player2b"]
        t1_id, t2_id = r["team1_id"], r["team2_id"]

        # Snapshot pre-match states (for the runner's history rows)
        pre_states = {p: self._get(p) for p in (p1a, p1b, p2a, p2b)}
        pre_indiv  = {p: pre_states[p].mu for p in pre_states}
        pre_team   = {
            t1_id: self._team_display.get(t1_id, self.cfg.initial_mu),
            t2_id: self._team_display.get(t2_id, self.cfg.initial_mu),
        }

        # Team-effective state in Glicko-2 scale: weighted average by 1/φ²
        def team_state(a: _G2State, b: _G2State) -> tuple[float, float]:
            ma_g2, pa_g2 = _to_g2(a.mu, a.phi)
            mb_g2, pb_g2 = _to_g2(b.mu, b.phi)
            wa = 1.0 / (pa_g2 * pa_g2)
            wb = 1.0 / (pb_g2 * pb_g2)
            mu_t  = (wa * ma_g2 + wb * mb_g2) / (wa + wb)
            phi_t = math.sqrt(1.0 / (wa + wb))   # combined uncertainty
            return mu_t, phi_t

        s1a, s1b = pre_states[p1a], pre_states[p1b]
        s2a, s2b = pre_states[p2a], pre_states[p2b]
        mu_t1, phi_t1 = team_state(s1a, s1b)
        mu_t2, phi_t2 = team_state(s2a, s2b)

        # Predict from team-effective ratings
        E1 = _E(mu_t1, mu_t2, phi_t2)
        predicted_p1 = E1

        score_p1 = 1.0 if r["winner"] == 1 else 0.0
        score_p2 = 1.0 - score_p1

        # Queue opponents for each player (NOT applied yet — happens at period flush)
        # Player 1a / 1b play against the avg of team 2
        s1a.pending.append((mu_t2, phi_t2, score_p1))
        s1b.pending.append((mu_t2, phi_t2, score_p1))
        s2a.pending.append((mu_t1, phi_t1, score_p2))
        s2b.pending.append((mu_t1, phi_t1, score_p2))

        # Bookkeeping (counts as "matches played" even though rating updates lag)
        for p in (p1a, p1b, p2a, p2b):
            self._n_played_ind[p] = self._n_played_ind.get(p, 0) + 1
        self._n_played_team[t1_id] = self._n_played_team.get(t1_id, 0) + 1
        self._n_played_team[t2_id] = self._n_played_team.get(t2_id, 0) + 1

        # Update team display (mean μ of partners) lazily — uses current state
        self._team_display[t1_id] = 0.5 * (s1a.mu + s1b.mu)
        self._team_display[t2_id] = 0.5 * (s2a.mu + s2b.mu)
        self._team_display_phi[t1_id] = math.sqrt(s1a.phi**2 + s1b.phi**2) / 2
        self._team_display_phi[t2_id] = math.sqrt(s2a.phi**2 + s2b.phi**2) / 2

        new_indiv = {p: self._get(p).mu for p in (p1a, p1b, p2a, p2b)}
        new_team  = {
            t1_id: self._team_display[t1_id],
            t2_id: self._team_display[t2_id],
        }

        return MatchUpdate(
            predicted_p1=predicted_p1,
            pre_indiv=pre_indiv, pre_team=pre_team,
            new_indiv=new_indiv, new_team=new_team,
        )

    def flush_pending(self) -> None:
        """Called after the final match: drain all queued updates."""
        if self._current_period is None:
            return
        self._flush_period(self._current_period + 1)
        # Recompute team displays from final μ-values
        # (we don't track team-membership here; phase_build derives that itself)

    # ── UI display ──
    def display_indiv(self, pid: str) -> float:
        """Conservative skill estimate: μ - 2·φ (Glickman's 95%-lower-bound convention)."""
        s = self._state.get(pid)
        if s is None:
            return self.cfg.initial_mu
        return s.mu - 2.0 * s.phi + 2.0 * 100.0  # +200 to keep numbers in ELO-like range
        # Note: pure μ-2φ would put new players ~ 800 (1500 - 700), which looks bad
        # in the UI.  Shifting by +200 keeps the visual range similar to ELO.

    def display_team(self, tid: str) -> float:
        return self._team_display.get(tid, self.cfg.initial_mu)

    def display_offset(self) -> float:
        # Match what display_indiv returns for a fresh player so callers that
        # normalise (e.g. EnsembleModel) get a consistent zero-point.
        return self.cfg.initial_mu - 2.0 * self.cfg.initial_phi + 200.0

    # ── Tuning UI ──
    @classmethod
    def slider_spec(cls) -> list[dict]:
        return [
            {"key": "tau", "label": "System-Volatilität τ",
             "min": 0.2, "max": 1.2, "step": 0.05, "default": 0.5, "fmt": "f2"},
            {"key": "initial_phi", "label": "Initial-φ (Unsicherheit)",
             "min": 100, "max": 400, "step": 10, "default": 350, "fmt": "int"},
            {"key": "initial_sigma", "label": "Initial-σ (Volatilität)",
             "min": 0.02, "max": 0.12, "step": 0.005, "default": 0.06, "fmt": "f3"},
            {"key": "rating_period_days", "label": "Rating-Period (Tage)",
             "min": 1, "max": 30, "step": 1, "default": 7, "fmt": "int"},
        ]

    @classmethod
    def from_overrides(cls, overrides: dict) -> "Glicko2Model":
        base = Glicko2Config()
        allowed = set(base.__dataclass_fields__)
        valid = {k: v for k, v in overrides.items() if k in allowed}
        # Cast ints where needed (sliders send floats)
        if "rating_period_days" in valid:
            valid["rating_period_days"] = int(valid["rating_period_days"])
        if "initial_phi" in valid:
            valid["initial_phi"] = float(valid["initial_phi"])
        cfg = _dc_replace(base, **valid)
        return cls(cfg)
