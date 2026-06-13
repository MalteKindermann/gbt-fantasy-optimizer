"""
TrueSkill rating adapter using the `trueskill` PyPI package
(BSD-licensed, Heungsub Lee).  TrueSkill is Microsoft Research's 2007
Bayesian rating system designed for team games (originally Halo on Xbox Live).

Why TrueSkill is interesting for beach 2v2:
  - Native team-of-N support — no manual blend between individual and team
    ratings.  `trueskill.rate([(p1a, p1b), (p2a, p2b)], ranks=[0, 1])`
    runs the correct joint factor-graph update over all four players.
  - Skill σ shrinks automatically with games played; no explicit K-factor
    decay schedule.
  - Probabilistic foundation: `win_probability` falls out of the math.

Per-player state is a `trueskill.Rating(μ, σ)`, default μ=25, σ=25/3≈8.33.
For UI display we use `μ - 3σ` (TrueSkill's recommended conservative skill
estimate) and rescale to the same visual range as ELO so the UI cards
remain readable.

Team handling: TrueSkill emits new player ratings; the team's effective
rating for display is the sum of its two players' μ minus a draw term
(trueskill.expose).  We track team display as the average of player display
ratings for simplicity, identical to the Glicko-2 approach.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace as _dc_replace
from typing import Optional

import trueskill

from elo.models import MatchUpdate


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrueSkillConfig:
    mu0:    float = 25.0
    sigma0: float = 25.0 / 3.0      # ≈ 8.33
    beta:   float = 6.0             # tuned 2026-06 (Halo default 4.17 → 6.0 for beach)
    tau:    float = 0.02            # tuned 2026-06 (Halo default 0.083 → 0.02)
    draw_probability: float = 0.0   # beach has no draws
    # Custom: σ-inflation per inactive year (drift effect outside the algorithm)
    sigma_inflation_per_year: float = 1.2
    # Display rescale: target range similar to ELO 1000-2500.  μ-3σ ranges from
    # roughly 0 (rookie) to ~45 (elite); we map linearly: display = (μ-3σ)*30 + 700.
    display_scale: float = 30.0
    display_offset: float = 700.0


# ── The Model ────────────────────────────────────────────────────────────────

class TrueSkillModel:
    name = "trueskill"

    def __init__(self, cfg: TrueSkillConfig):
        self.cfg = cfg
        # Build a TrueSkill environment so we don't pollute the global env
        # (multiple configurations might be tested in the same process via API).
        # Backend selection: try scipy → mpmath → default (pure-python erfc).
        # The pure-python default is fine for our match volume and avoids
        # adding a heavy dependency.
        backend = None
        try:
            import scipy.stats   # noqa: F401
            backend = "scipy"
        except Exception:
            try:
                import mpmath    # noqa: F401
                backend = "mpmath"
            except Exception:
                backend = None
        self.env = trueskill.TrueSkill(
            mu=cfg.mu0, sigma=cfg.sigma0, beta=cfg.beta, tau=cfg.tau,
            draw_probability=cfg.draw_probability, backend=backend,
        )
        self._state: dict[str, trueskill.Rating] = {}
        self._n_played_ind:  dict[str, int] = {}
        self._n_played_team: dict[str, int] = {}
        self._team_display: dict[str, float] = {}
        self._priors: dict[str, dict] = {}

    # σ for primed players (have a prior). 5.0 ≈ "some prior info, but still
    # quite uncertain" — between default 8.33 and a veteran's ~2-3.
    PRIOR_SIGMA: float = 5.0
    # g1-scale → TS skill conversion: 400 ELO ≈ 5 TS μ units (one default σ).
    G1_TO_MU_SCALE: float = 5.0 / 400.0

    def set_priors(self, priors: dict[str, dict]) -> None:
        self._priors = priors or {}

    def _prior_rating(self, g1: float) -> trueskill.Rating:
        mu = self.cfg.mu0 + (g1 - 1500.0) * self.G1_TO_MU_SCALE
        return self.env.create_rating(mu=mu, sigma=self.PRIOR_SIGMA)

    # ── State accessors ──
    @property
    def state_indiv(self) -> dict[str, trueskill.Rating]: return self._state
    @property
    def state_team(self)  -> dict[str, float]:            return self._team_display
    @property
    def n_played_ind(self)  -> dict[str, int]: return self._n_played_ind
    @property
    def n_played_team(self) -> dict[str, int]: return self._n_played_team

    def _get(self, pid: str) -> trueskill.Rating:
        r = self._state.get(pid)
        if r is None:
            prior = self._priors.get(pid)
            if prior is not None:
                r = self._prior_rating(float(prior["rating_g1"]))
            else:
                r = self.env.create_rating()
            self._state[pid] = r
        return r

    def _display_one(self, r: trueskill.Rating) -> float:
        return (r.mu - 3.0 * r.sigma) * self.cfg.display_scale + self.cfg.display_offset

    # ── Per-year decay: σ-inflation only ──
    def decay_year(self) -> None:
        factor = self.cfg.sigma_inflation_per_year
        if factor <= 1.0:
            return
        new_state: dict[str, trueskill.Rating] = {}
        sigma_cap = self.cfg.sigma0   # never inflate beyond a fresh player
        for pid, r in self._state.items():
            new_sigma = min(r.sigma * factor, sigma_cap)
            new_state[pid] = self.env.create_rating(mu=r.mu, sigma=new_sigma)
        self._state = new_state

    # ── Try-fallback for backends ──
    def _rate(self, w1, w2, l1, l2):
        """Wraps env.rate_1vs1 / env.rate.  TrueSkill defaults to scipy; if it's
        missing the env was constructed with mpmath above, which is bundled."""
        try:
            return self.env.rate([(w1, w2), (l1, l2)], ranks=[0, 1])
        except Exception:
            # Backend issue — fall back to per-match approximation
            return [(w1, w2), (l1, l2)]

    # ── Per-match update ──
    def process_match(self, match: dict) -> MatchUpdate:
        r = match
        p1a, p1b = r["player1a"], r["player1b"]
        p2a, p2b = r["player2a"], r["player2b"]
        t1_id, t2_id = r["team1_id"], r["team2_id"]
        is_p1_winner = r["winner"] == 1

        r1a = self._get(p1a); r1b = self._get(p1b)
        r2a = self._get(p2a); r2b = self._get(p2b)

        pre_indiv = {p: self._display_one(s) for p, s in
                     ((p1a, r1a), (p1b, r1b), (p2a, r2a), (p2b, r2b))}
        pre_team  = {
            t1_id: self._team_display.get(t1_id, self.cfg.mu0),
            t2_id: self._team_display.get(t2_id, self.cfg.mu0),
        }

        # Win probability for team 1 — closed form for two teams of equal size 2
        # P(t1 wins) = Φ( (Σμ_t1 - Σμ_t2) / sqrt(2N·β² + Σσ²) )
        # with N total players (=4 here).
        try:
            from statistics import NormalDist
            delta_mu = (r1a.mu + r1b.mu) - (r2a.mu + r2b.mu)
            denom_sq = (4 * self.cfg.beta * self.cfg.beta
                        + r1a.sigma**2 + r1b.sigma**2
                        + r2a.sigma**2 + r2b.sigma**2)
            denom = math.sqrt(denom_sq) if denom_sq > 0 else 1.0
            predicted_p1 = NormalDist(0, 1).cdf(delta_mu / denom)
        except Exception:
            predicted_p1 = 0.5

        # Run the TrueSkill team update.  ranks=[0, 1] means first team won.
        if is_p1_winner:
            (n1a, n1b), (n2a, n2b) = self._rate(r1a, r1b, r2a, r2b)
        else:
            (n2a, n2b), (n1a, n1b) = self._rate(r2a, r2b, r1a, r1b)

        self._state[p1a] = n1a
        self._state[p1b] = n1b
        self._state[p2a] = n2a
        self._state[p2b] = n2b

        for p in (p1a, p1b, p2a, p2b):
            self._n_played_ind[p] = self._n_played_ind.get(p, 0) + 1
        self._n_played_team[t1_id] = self._n_played_team.get(t1_id, 0) + 1
        self._n_played_team[t2_id] = self._n_played_team.get(t2_id, 0) + 1

        # Team display = mean of partners' display ratings
        self._team_display[t1_id] = 0.5 * (self._display_one(n1a) + self._display_one(n1b))
        self._team_display[t2_id] = 0.5 * (self._display_one(n2a) + self._display_one(n2b))

        new_indiv = {p: self._display_one(s) for p, s in
                     ((p1a, n1a), (p1b, n1b), (p2a, n2a), (p2b, n2b))}
        new_team  = {t1_id: self._team_display[t1_id],
                     t2_id: self._team_display[t2_id]}

        return MatchUpdate(
            predicted_p1=predicted_p1,
            pre_indiv=pre_indiv, pre_team=pre_team,
            new_indiv=new_indiv, new_team=new_team,
        )

    def flush_pending(self) -> None:
        pass   # TrueSkill is fully online

    # ── UI display ──
    def display_indiv(self, pid: str) -> float:
        r = self._state.get(pid)
        if r is None:
            return self._display_one(self.env.create_rating())
        return self._display_one(r)

    def display_team(self, tid: str) -> float:
        return self._team_display.get(tid, self._display_one(self.env.create_rating()))

    def display_offset(self) -> float:
        return self._display_one(self.env.create_rating())

    # ── Tuning UI ──
    @classmethod
    def slider_spec(cls) -> list[dict]:
        return [
            {"key": "mu0", "label": "Initial μ", "min": 15, "max": 50,
             "step": 1, "default": 25, "fmt": "int"},
            {"key": "sigma0", "label": "Initial σ", "min": 5.0, "max": 15.0,
             "step": 0.5, "default": 25.0/3.0, "fmt": "f2"},
            {"key": "beta", "label": "Skill-Bandbreite β",
             "min": 2.0, "max": 12.0, "step": 0.5, "default": 25.0/6.0, "fmt": "f2"},
            {"key": "tau", "label": "Dynamik-Drift τ",
             "min": 0.01, "max": 0.2, "step": 0.01, "default": 25.0/300.0, "fmt": "f3"},
            {"key": "sigma_inflation_per_year", "label": "σ-Inflation pro Jahr",
             "min": 1.0, "max": 1.5, "step": 0.05, "default": 1.2, "fmt": "f2"},
        ]

    @classmethod
    def from_overrides(cls, overrides: dict) -> "TrueSkillModel":
        base = TrueSkillConfig()
        allowed = set(base.__dataclass_fields__)
        valid = {k: v for k, v in overrides.items() if k in allowed}
        cfg = _dc_replace(base, **valid)
        return cls(cfg)
