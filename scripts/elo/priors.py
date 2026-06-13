"""
Cold-start rating priors from DVV ranking points.

Players new to our match history would otherwise start at the configured
default (ELO 1500, Glicko-2 μ=1500 φ=350, TrueSkill μ=25 σ=8.33). Most
"new" players in fact already have DVV ranking points — they just hadn't
played yet at the bvbinfo/FIVB level we track. Using their DVV points as
a prior cuts the cold-start error on the first ~5-10 matches per player.

Output: a `{player_id: {"rating_g1": float, "source": str, "points": int}}`
mapping. `rating_g1` is in Glicko-1 / ELO scale (centered around 1500).
Each rating model converts to its own native scale on read.

Linear scale:
    rating = 1400 + (dvv_points / 3000) * 400
    clamped to [1400, 1800]   — 0 pts → 1400, 3000+ pts → 1800.
"""
from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir  # noqa: E402

DATA = data_dir()

PRIOR_FLOOR = 1400.0
PRIOR_CEIL  = 1800.0
PRIOR_SCALE_POINTS = 3000.0   # 3000 DVV pts → top-of-scale prior


def _normalise(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def player_id_from_name(first: str, last: str) -> str:
    return f"{_normalise(last)}_{_normalise(first)}".strip("_")


def points_to_g1_rating(points: int) -> float:
    """Map DVV ranking points to a Glicko-1 / ELO scale prior."""
    if points <= 0:
        return PRIOR_FLOOR
    r = PRIOR_FLOOR + (points / PRIOR_SCALE_POINTS) * (PRIOR_CEIL - PRIOR_FLOOR)
    return max(PRIOR_FLOOR, min(PRIOR_CEIL, r))


def _load_season_overlay(path: Path) -> dict[str, dict]:
    """Read a Firestore-typed season doc and return {pid: {fn, ln, g}}."""
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    fields = doc.get("fields") or {}
    pl = fields.get("pl") or {}
    pl = (pl.get("mapValue") or {}).get("fields") or {}
    out: dict[str, dict] = {}
    for pid, node in pl.items():
        f = (node.get("mapValue") or {}).get("fields") or {}
        fn = (f.get("fn") or {}).get("stringValue", "")
        ln = (f.get("ln") or {}).get("stringValue", "")
        g  = (f.get("g")  or {}).get("stringValue", "")
        if not ln:
            continue
        out[str(pid)] = {"fn": fn, "ln": ln, "g": g}
    return out


def _collect_dvv_players() -> list[tuple[str, str, str]]:
    """Returns [(firstName, lastName, gender_lower), ...] from all overlays."""
    seen: dict[str, tuple[str, str, str]] = {}
    for p in sorted(DATA.glob("players_season_*.json")):
        for pid, info in _load_season_overlay(p).items():
            g_raw = (info.get("g") or "").upper()
            g = "m" if g_raw == "M" else "f" if g_raw == "W" else "?"
            seen[pid] = (info.get("fn") or "", info.get("ln") or "", g)
    # Legacy single-file alias as last resort
    legacy = DATA / "players_season.json"
    if legacy.exists():
        for pid, info in _load_season_overlay(legacy).items():
            if pid not in seen:
                g_raw = (info.get("g") or "").upper()
                g = "m" if g_raw == "M" else "f" if g_raw == "W" else "?"
                seen[pid] = (info.get("fn") or "", info.get("ln") or "", g)
    return list(seen.values())


def build_priors(force_refresh: bool = False) -> dict[str, dict]:
    """Build the {player_id: prior} mapping. Network call for DVV rankings
    (cached upstream by simulate_tournament for 1 h)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import simulate_tournament as sim  # noqa: E402
    except Exception as e:
        print(f"[priors] could not import simulate_tournament: {e}", file=sys.stderr)
        return {}

    rankings: dict[str, dict[str, int]] = {}
    for gender in ("m", "f"):
        try:
            rankings[gender] = sim.fetch_dvv_rankings(gender, force=force_refresh) or {}
        except Exception as e:
            print(f"[priors] DVV fetch failed for {gender}: {e}", file=sys.stderr)
            rankings[gender] = {}

    if not any(rankings.values()):
        return {}

    priors: dict[str, dict] = {}
    for first, last, gender in _collect_dvv_players():
        if not last:
            continue
        pool = rankings.get(gender, {}) or rankings.get("m", {})
        pts: Optional[int] = None
        # Try "Lastname, Firstname" first (most specific in DVV indiv tables),
        # then fall back to last-name-only (DVV does this key too)
        if first:
            key_full = f"{last}, {first}"
            if key_full in pool:
                pts = pool[key_full]
        if pts is None and last in pool:
            pts = pool[last]
        if pts is None:
            continue
        pid = player_id_from_name(first, last)
        if not pid:
            continue
        # Keep highest if collision (same pid surfaces twice from different overlays)
        prev = priors.get(pid)
        if prev is not None and prev["points"] >= pts:
            continue
        priors[pid] = {
            "rating_g1": points_to_g1_rating(pts),
            "source": "dvv_points",
            "points": int(pts),
        }
    return priors


def build_for_model(model_name: str, force_refresh: bool = False) -> dict[str, dict]:
    """Same as build_priors — model adapters convert the g1 rating to their own
    scale internally. Returning the canonical g1 form here keeps the priors
    file model-agnostic so a single network fetch serves all three models."""
    return build_priors(force_refresh=force_refresh)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    p = build_priors(force_refresh=args.force)
    print(f"[priors] built {len(p)} priors")
    if args.print:
        top = sorted(p.items(), key=lambda kv: -kv[1]["points"])[:20]
        for pid, info in top:
            print(f"  {pid:<30}  pts={info['points']:>4}  prior={info['rating_g1']:.0f}")
