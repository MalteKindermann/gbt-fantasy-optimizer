"""
Pure ELO computation — no I/O, no globals, fully unit-testable.

All functions are deterministic and side-effect free.  The builder
(`build_ratings.py`) holds state (per-entity rating dicts, match counters)
and calls into here for math.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EloConfig:
    start: float = 1500.0
    k_base: float = 40.0

    # Provisional period: first N matches use boosted K
    provisional_matches: int = 20
    provisional_multiplier: float = 1.5

    # Round-importance multipliers on K
    importance_quali: float = 0.75
    importance_main:  float = 1.0
    importance_final: float = 1.25

    # Seasonal decay: each season-end, pull active players toward start
    decay_pull: float = 0.10
    decay_target: float = 1500.0
    decay_min_matches: int = 3

    # Team-ELO blending into the predictive "combined" rating
    team_min_matches_for_blend: int = 5
    blend_individual_weight: float = 0.6
    blend_team_weight: float = 0.4

    # Cross-source weight (FIVB matches count half — different competition)
    source_weight_dvv: float = 1.0
    source_weight_fivb: float = 1.0
    source_weight_bvb: float = 1.0

    # Per-DVV-category-tier weight. Stacks multiplicatively with source weight.
    # tier_top covers GBT + DM + historic top-tour names + Corona-2020 specials
    # (always full weight). tier_challenger covers ROCK the BEACH / smart beach
    # cup / Urlaubsguru / etc. tier_qualifier covers Qualifier-only events.
    tier_weight_top:        float = 1.0
    tier_weight_challenger: float = 0.5
    tier_weight_qualifier:  float = 0.3

    # Margin-of-victory strength.  0.0 = MoV fully disabled (every win counts
    # the same, classic ELO).  1.0 = full MoV per `mov_multiplier`.  Values
    # in between linearly interpolate.  Tuneable via the UI slider.
    mov_strength: float = 1.0


# ── Round classification (string → importance multiplier key) ────────────────

def classify_round(label: str | None) -> str:
    """
    Returns one of 'quali', 'final', 'main'.

    The DVV Spielplan uses German round labels; FIVB CSVs use English.
    """
    if not label:
        return "main"
    s = label.lower().strip()
    if "quali" in s:
        return "quali"
    # Medal matches (bvbinfo English labels): always finals-tier.
    if ("gold medal" in s or "bronze medal" in s
            or "platz 3" in s or "3rd place" in s):
        return "final"
    # Tricky: German "Achtel/Viertel/Halb-finale" all contain "final" as a
    # substring but are MAIN draw, not the gold-medal match. Treat as "final"
    # only when "final" / "finale" / "finals" appears as its own word
    # (boundary-delimited; "Semi-Finals" qualifies because it's hyphen-split).
    import re as _re
    if _re.search(r"\bfinal(?:s|e)?\b", s):
        return "final"
    return "main"


def importance_factor(cfg: EloConfig, round_kind: str) -> float:
    return {
        "quali": cfg.importance_quali,
        "main":  cfg.importance_main,
        "final": cfg.importance_final,
    }.get(round_kind, cfg.importance_main)


# ── Core ELO math ────────────────────────────────────────────────────────────

def expected(rating_a: float, rating_b: float) -> float:
    """Standard logistic: P(A beats B)."""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def k_factor(cfg: EloConfig, round_kind: str, matches_played: int,
             source: str = "dvv", category_tier: str = "top") -> float:
    k = cfg.k_base * importance_factor(cfg, round_kind)
    if matches_played < cfg.provisional_matches:
        k *= cfg.provisional_multiplier
    if source == "fivb":
        k *= cfg.source_weight_fivb
    elif source == "dvv":
        k *= cfg.source_weight_dvv
    elif source == "bvb":
        k *= cfg.source_weight_bvb
    # Per-DVV-tier multiplier (FIVB/bvb always 'top')
    if category_tier == "challenger":
        k *= cfg.tier_weight_challenger
    elif category_tier == "qualifier":
        k *= cfg.tier_weight_qualifier
    return k


def mov_multiplier(sets_won: int, sets_lost: int,
                   set_scores: Iterable[tuple[int, int]] | None = None) -> float:
    """
    Margin-of-victory multiplier on the rating delta.

      * If set_scores available → log-based: 1 + ln(1 + |point_diff|/10)
        where point_diff = sum(winner_points) - sum(loser_points) across all sets.
      * Else fall back to discrete 2:0 → 1.2 / 2:1 → 0.9.

    Always returns a value ≥ 1 for clear wins; the 2:1 case (0.9) is
    intentionally < 1 to dampen close matches.
    """
    if set_scores:
        wpts = 0
        lpts = 0
        for a, b in set_scores:
            # winner of the set = higher
            if a >= b:
                wpts += a
                lpts += b
            else:
                wpts += b
                lpts += a
        diff = abs(wpts - lpts)
        return 1.0 + math.log(1.0 + diff / 10.0)
    if sets_won == 2 and sets_lost == 0:
        return 1.2
    if sets_won == 2 and sets_lost == 1:
        return 0.9
    return 1.0


def update(rating_a: float, rating_b: float, score_a: float,
           k: float, mov: float = 1.0) -> tuple[float, float]:
    """
    Apply one ELO update. `score_a` = 1 if A won, 0 if A lost (no draws in BV).

    Returns (new_rating_a, new_rating_b).
    """
    exp_a = expected(rating_a, rating_b)
    delta = k * mov * (score_a - exp_a)
    return rating_a + delta, rating_b - delta


def blended(elo_ind_a: float, elo_ind_b: float, elo_team: float | None,
            team_matches: int, cfg: EloConfig) -> float:
    """
    Predictive "combined" rating for a 2-player team.

    Falls back to pure individual mean when the partnership has fewer than
    `team_min_matches_for_blend` matches together (no signal in team ELO yet).
    """
    avg_ind = 0.5 * (elo_ind_a + elo_ind_b)
    if elo_team is None or team_matches < cfg.team_min_matches_for_blend:
        return avg_ind
    return (cfg.blend_individual_weight * avg_ind
            + cfg.blend_team_weight * elo_team)


def apply_seasonal_decay(rating: float, matches_in_season: int,
                         cfg: EloConfig) -> float:
    """
    Pull rating `decay_pull` of the way toward `decay_target`, but only for
    entities that played enough matches to "exist" this season.
    """
    if matches_in_season < cfg.decay_min_matches:
        return rating
    return rating + cfg.decay_pull * (cfg.decay_target - rating)


# ── Match-level orchestration helper (still I/O free) ─────────────────────────

@dataclass
class MatchUpdate:
    """Result of a single match — what the builder should persist."""
    # New ratings, per entity
    new_indiv: dict[str, float] = field(default_factory=dict)
    new_team:  dict[str, float] = field(default_factory=dict)
    # Pre-match snapshots for ELO-history rows
    pre_indiv: dict[str, float] = field(default_factory=dict)
    pre_team:  dict[str, float] = field(default_factory=dict)
    # Implied win prob of team 1 (before the match), purely informational
    predicted_p1: float = 0.5


def process_match(
    *,
    cfg: EloConfig,
    # Identities
    p1a: str, p1b: str, p2a: str, p2b: str,
    team1_id: str, team2_id: str,
    # Current ratings
    elo_indiv: dict[str, float],          # mutated read-only here
    elo_team:  dict[str, float],
    n_played_ind:  dict[str, int],
    n_played_team: dict[str, int],
    # Outcome
    winner: int,                          # 1 or 2
    sets_won_1: int, sets_lost_1: int,
    set_scores: list[tuple[int, int]] | None,
    round_kind: str,
    source: str,
    category_tier: str = "top",
) -> MatchUpdate:
    """
    Compute new ratings for one match. Pure: does not write `elo_indiv` /
    `elo_team` — returns the new values for the caller to persist.

    Strategy:
      * Each individual is updated against the OPPONENT TEAM's blended rating
        (so team context informs individuals without double-counting).
      * The team-vs-team update is a separate, independent ELO event using
        the two team ratings directly.
    """
    out = MatchUpdate()

    r1a = elo_indiv.get(p1a, cfg.start)
    r1b = elo_indiv.get(p1b, cfg.start)
    r2a = elo_indiv.get(p2a, cfg.start)
    r2b = elo_indiv.get(p2b, cfg.start)
    rt1 = elo_team.get(team1_id, cfg.start)
    rt2 = elo_team.get(team2_id, cfg.start)
    nt1 = n_played_team.get(team1_id, 0)
    nt2 = n_played_team.get(team2_id, 0)

    out.pre_indiv = {p1a: r1a, p1b: r1b, p2a: r2a, p2b: r2b}
    out.pre_team  = {team1_id: rt1, team2_id: rt2}

    # Predictive prob (for output / backtest), uses blended
    team1_blend = blended(r1a, r1b, rt1, nt1, cfg)
    team2_blend = blended(r2a, r2b, rt2, nt2, cfg)
    out.predicted_p1 = expected(team1_blend, team2_blend)

    mov_raw = mov_multiplier(sets_won_1 if winner == 1 else sets_lost_1,
                             sets_lost_1 if winner == 1 else sets_won_1,
                             set_scores)
    # Interpolate strength: strength=0 → 1.0 (no MoV), strength=1 → full MoV
    mov = 1.0 + (mov_raw - 1.0) * cfg.mov_strength
    score1 = 1.0 if winner == 1 else 0.0

    # Individual updates — each player updated against opponent team blend
    def upd_ind(pid: str, opp_blend: float, score: float) -> float:
        r = elo_indiv.get(pid, cfg.start)
        n = n_played_ind.get(pid, 0)
        k = k_factor(cfg, round_kind, n, source, category_tier)
        new_r, _ = update(r, opp_blend, score, k, mov)
        return new_r

    out.new_indiv[p1a] = upd_ind(p1a, team2_blend, score1)
    out.new_indiv[p1b] = upd_ind(p1b, team2_blend, score1)
    out.new_indiv[p2a] = upd_ind(p2a, team1_blend, 1.0 - score1)
    out.new_indiv[p2b] = upd_ind(p2b, team1_blend, 1.0 - score1)

    # Team update — direct team-vs-team
    k_t = k_factor(cfg, round_kind, min(nt1, nt2), source, category_tier)
    new_rt1, new_rt2 = update(rt1, rt2, score1, k_t, mov)
    out.new_team[team1_id] = new_rt1
    out.new_team[team2_id] = new_rt2

    return out


# ── Helpers for prediction / display ──────────────────────────────────────────

def team_key(player_a: str, player_b: str) -> str:
    """Stable team-id from two player-ids (sort so order doesn't matter)."""
    a, b = sorted([player_a, player_b])
    return f"{a}|{b}"
