"""
Generic rating-model runner.

Walks the chronologically-sorted `records` list once, delegates the per-match
math to a `RatingModel`, and accumulates metadata (last-active, country,
gender, team membership) plus backtest stats on the side.

The same loop drives ELO, Glicko-2, TrueSkill — only the injected `model`
differs.  Calendar-year boundaries trigger `model.decay_year()`, and we
respect `train_end_date` for honest out-of-sample evaluation.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from elo.models import RatingModel


@dataclass
class ModelRun:
    model: RatingModel = None   # type: ignore[assignment]

    # Side-tables (model-independent)
    last_active_ind:  dict[str, str] = field(default_factory=dict)
    last_active_team: dict[str, str] = field(default_factory=dict)
    teams_for_player: dict[str, set] = field(default_factory=dict)
    country_ctr: dict[str, Counter]  = field(default_factory=dict)
    gender_ctr:  dict[str, Counter]  = field(default_factory=dict)

    # Backtest accumulators
    in_sample_total:   int = 0
    in_sample_correct: int = 0
    in_sample_calib:   dict[int, list[int]] = field(
        default_factory=lambda: {i: [] for i in range(5, 10)})
    oos_total:   int = 0
    oos_correct: int = 0
    oos_calib:   dict[int, list[int]] = field(
        default_factory=lambda: {i: [] for i in range(5, 10)})
    train_end_date: Optional[str] = None

    match_predictions: list[float] = field(default_factory=list)
    history_rows:      list[tuple] = field(default_factory=list)


def run_model(
    records: list[dict],
    model: RatingModel,
    *,
    train_end_date: Optional[str] = None,
    collect_history: bool = False,
) -> ModelRun:
    """Walk `records` chronologically driving the given rating model."""
    out = ModelRun(model=model, train_end_date=train_end_date)
    current_year: Optional[int] = None

    for r in records:
        date_str = r.get("date") or ""
        is_train = (train_end_date is None
                    or (date_str and date_str <= train_end_date))

        # ── Calendar-year decay ──────────────────────────────────────────────
        if len(date_str) >= 4 and date_str[:4].isdigit():
            r_year = int(date_str[:4])
            if current_year is not None and r_year != current_year:
                steps = max(1, r_year - current_year)
                for _ in range(steps):
                    model.decay_year()
            current_year = r_year

        # ── Predict + update via the injected model ──────────────────────────
        upd = model.process_match(r)
        out.match_predictions.append(upd.predicted_p1)

        # ── Backtest tallies ─────────────────────────────────────────────────
        pred_winner = 1 if upd.predicted_p1 >= 0.5 else 2
        p_for_bucket = (upd.predicted_p1 if upd.predicted_p1 >= 0.5
                        else 1.0 - upd.predicted_p1)
        bucket = min(9, max(5, int(p_for_bucket * 10)))
        is_correct = 1 if pred_winner == r["winner"] else 0

        if r["source"] == "dvv" and (r["date"] or "") >= "2025-01-01":
            out.in_sample_total += 1
            out.in_sample_correct += is_correct
            out.in_sample_calib[bucket].append(is_correct)

        if train_end_date and not is_train:
            out.oos_total += 1
            out.oos_correct += is_correct
            out.oos_calib[bucket].append(is_correct)

        # ── Side-tables (always populated; cheap, helps the UI export) ──────
        for pid, country in (
            (r["player1a"], r.get("team1_country") or ""),
            (r["player1b"], r.get("team1_country") or ""),
            (r["player2a"], r.get("team2_country") or ""),
            (r["player2b"], r.get("team2_country") or ""),
        ):
            if not pid:
                continue
            if country:
                out.country_ctr.setdefault(pid, Counter())[country] += 1
            g = r.get("gender") or "?"
            if g in ("m", "f"):
                out.gender_ctr.setdefault(pid, Counter())[g] += 1
        out.teams_for_player.setdefault(r["player1a"], set()).add(r["team1_id"])
        out.teams_for_player.setdefault(r["player1b"], set()).add(r["team1_id"])
        out.teams_for_player.setdefault(r["player2a"], set()).add(r["team2_id"])
        out.teams_for_player.setdefault(r["player2b"], set()).add(r["team2_id"])

        if is_train:
            # last-active is always the most recent training match
            for pid in (r["player1a"], r["player1b"], r["player2a"], r["player2b"]):
                if pid:
                    out.last_active_ind[pid] = r["date"]
            out.last_active_team[r["team1_id"]] = r["date"]
            out.last_active_team[r["team2_id"]] = r["date"]

        # History rows (only if requested + only on training matches)
        if collect_history and is_train:
            for pid, new_v in upd.new_indiv.items():
                old_v = upd.pre_indiv.get(pid)
                if old_v is None:
                    old_v = model.display_offset()
                out.history_rows.append((pid, "individual", r["date"],
                                         r["tournament_id"], r["match_id"],
                                         old_v, new_v, new_v - old_v))
            for tid, new_v in upd.new_team.items():
                old_v = upd.pre_team.get(tid)
                if old_v is None:
                    old_v = model.display_offset()
                out.history_rows.append((tid, "team", r["date"],
                                         r["tournament_id"], r["match_id"],
                                         old_v, new_v, new_v - old_v))

    # Final flush (Glicko-2 needs this to apply queued period updates)
    model.flush_pending()
    return out


# ── Player-export helper (used by both phase_build and the API) ──────────────

def build_player_export(run: ModelRun) -> list[dict]:
    """
    Construct the per-player UI payload from a finished ModelRun.  Uses
    `model.display_indiv` / `model.display_team` so the JSON schema stays
    identical across rating models (the ranking UI doesn't care which model
    produced the numbers).
    """
    model = run.model
    def _display_from_id(pid: str) -> str:
        last, _, first = pid.partition("_")
        def _cap(s):
            return " ".join(w.capitalize() for w in s.split())
        return f"{_cap(first)} {_cap(last)}" if first else _cap(last)

    players: list[dict] = []
    n_played_ind  = model.n_played_ind
    n_played_team = model.n_played_team

    for pid in model.state_indiv:
        n = n_played_ind.get(pid, 0)
        if n < 1:
            continue
        country = "?"
        if pid in run.country_ctr and run.country_ctr[pid]:
            country = run.country_ctr[pid].most_common(1)[0][0]
        gender = "?"
        if pid in run.gender_ctr and run.gender_ctr[pid]:
            gender = run.gender_ctr[pid].most_common(1)[0][0]

        # Current partnership — same logic as before, now using model.display_team
        best_team_id: Optional[str] = None
        best_team_n = 0
        best_recent_date = ""
        most_played_tid: Optional[str] = None
        most_played_n = 0
        for tid in run.teams_for_player.get(pid, ()):
            n_team = n_played_team.get(tid, 0)
            if tid not in model.state_team:
                continue
            if n_team > most_played_n:
                most_played_tid = tid
                most_played_n = n_team
            if n_team < 5:
                continue
            la = run.last_active_team.get(tid, "")
            if la > best_recent_date:
                best_recent_date = la
                best_team_id = tid
                best_team_n = n_team
        if best_team_id is None and most_played_tid is not None:
            best_team_id = most_played_tid
            best_team_n = most_played_n

        indiv_rating = model.display_indiv(pid)
        team_rating = (model.display_team(best_team_id)
                       if best_team_id is not None else None)

        # Combined: 60% individual + 40% team if there's a stable partnership.
        # We keep this exact formula for ALL models so the UI display "feels"
        # the same across them.  TrueSkill's team rating is itself already a
        # smart aggregate, so the blend amounts to a light shrinkage toward
        # the partner's number.
        if team_rating is not None and best_team_n >= 5:
            elo_combined = 0.6 * indiv_rating + 0.4 * team_rating
        else:
            elo_combined = indiv_rating

        players.append({
            "id":             pid,
            "name":           _display_from_id(pid),
            "elo_individual": round(indiv_rating, 1),
            "elo_combined":   round(elo_combined, 1),
            "matches":        n,
            "last_active":    run.last_active_ind.get(pid, ""),
            "country":        country,
            "gender":         gender,
            "best_team_elo":  (round(team_rating, 1)
                               if team_rating is not None else None),
            "best_team_matches": best_team_n,
        })
    players.sort(key=lambda p: p["elo_individual"], reverse=True)
    return players


# ── Backwards-compatibility shims ────────────────────────────────────────────
#
# build_ratings.phase_build used to call run_elo(records, cfg, ...).  We keep
# a wrapper so existing call sites compile, but they should migrate to
# run_model(records, model, ...).

def run_elo(records, cfg, *, train_end_date=None, collect_history=False):
    from elo.elo_adapter import EloModel
    model = EloModel(cfg)
    run = run_model(records, model,
                    train_end_date=train_end_date,
                    collect_history=collect_history)
    # Mimic the old EloRun-shaped facade for any callers that read attributes
    # directly (build_player_export used to consume run.elo_indiv etc.).
    setattr(run, "elo_indiv",  dict(model.state_indiv))
    setattr(run, "elo_team",   dict(model.state_team))
    setattr(run, "n_played_ind",  dict(model.n_played_ind))
    setattr(run, "n_played_team", dict(model.n_played_team))
    return run
