#!/usr/bin/env python3
"""
GBT Tournament Simulator
========================
Fetches DVV rankings, reads the bracket, simulates the tournament using
Monte Carlo, and outputs expected match counts per player.

Usage:
  python scripts/simulate_tournament.py --gender m
  python scripts/simulate_tournament.py --gender f
  python scripts/simulate_tournament.py --gender m --simulations 50000

Inputs  (edit before each tournament):
  data/bracket_m.json   -- men's bracket   (team names in seeding order)
  data/bracket_f.json   -- women's bracket

Output:
  data/tournament_sim.json  -- loaded by the frontend for Turnier-Prognose algorithm
"""

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT        = Path(__file__).resolve().parent.parent
PLAYERS_ALL = ROOT / "data" / "players_all.json"
PLAYERS_AVL = ROOT / "data" / "players_available.json"
SIM_OUTPUT  = ROOT / "data" / "tournament_sim.json"
CACHE_DIR   = ROOT / "data" / ".cache"

CACHE_TTL_SECONDS = 3600  # 1 hour

# ── Disk cache helpers ────────────────────────────────────────────────────────

def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.json"


def cache_get(name: str, ttl: int = CACHE_TTL_SECONDS):
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry.get("fetched_at", 0) > ttl:
            return None
        return entry.get("data")
    except Exception:
        return None


def cache_set(name: str, data) -> None:
    path = _cache_path(name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f, ensure_ascii=False)
    except Exception as e:
        print(f"  WARNING: could not write cache {name}: {e}", file=sys.stderr)

DVV_RANKING_URL = {
    "m": "https://beach.volleyball-verband.de/public/rl-show.php?id=338",
    "f": "https://beach.volleyball-verband.de/public/rl-show.php?id=339",
}

DVV_INDIVIDUAL_URL = {
    "m": "https://beach.volleyball-verband.de/public/rl-show.php?id=336",
    "f": "https://beach.volleyball-verband.de/public/rl-show.php?id=337",
}

GBT_BRACKET_URL = "https://gbt.hanski.de/rechner/data/bracket_{gender}.json"

# ── DVV Rankings ──────────────────────────────────────────────────────────────

def _fetch_dvv_table(url: str, label: str) -> dict[str, int]:
    """Fetch a generic DVV ranking table → {name: points}."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING: Could not fetch {label}: {e}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    table = None
    for t in soup.find_all("table"):
        header = t.find("tr")
        if header and "Platz" in header.get_text() and "Punkte" in header.get_text():
            table = t
            break
    if not table:
        return {}

    out: dict[str, int] = {}
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 5: continue
        name    = cells[2].get_text(strip=True)
        pts_raw = cells[4].get_text(strip=True).replace(".", "").replace(",", "").strip()
        try:
            out[name] = int(pts_raw)
        except ValueError:
            pass
    return out


def fetch_dvv_rankings(gender: str, force: bool = False) -> dict[str, int]:
    """
    Returns {name: ranking_points} merging:
      • Team rankings (id=338/339)
      • Individual rankings (id=336/337) — keyed by 'Lastname, Firstname' AND by 'Lastname' alone
    """
    cache_key = f"dvv_{gender}"
    if not force:
        cached = cache_get(cache_key)
        if cached is not None:
            print(f"DVV rankings ({gender}): using cached ({len(cached)} entries)")
            return cached

    print(f"Fetching DVV rankings ({gender})…")
    url = DVV_RANKING_URL[gender]
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        print(f"  WARNING: Could not fetch rankings: {e}", file=sys.stderr)
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    rankings = {}

    # The DVV page has multiple tables — pick the one whose header contains "Platz" and "Punkte"
    table = None
    for t in soup.find_all("table"):
        header = t.find("tr")
        if header and "Platz" in header.get_text() and "Punkte" in header.get_text():
            table = t
            break

    if not table:
        print("  WARNING: Ranking table not found.", file=sys.stderr)
        return rankings

    # Columns: 0=icon/spacer, 1=Platz, 2=Name(team), 3=Verein, 4=Punkte, [5=Trend]
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        team    = cells[2].get_text(strip=True)
        pts_raw = cells[4].get_text(strip=True).replace(".", "").replace(",", "").strip()
        try:
            rankings[team] = int(pts_raw)
        except ValueError:
            pass

    print(f"  Found {len(rankings)} teams in DVV ranking.")

    # ── Also fetch individual rankings and merge by last name ──
    indiv = _fetch_dvv_table(DVV_INDIVIDUAL_URL[gender], f"individual rankings ({gender})")
    print(f"  Found {len(indiv)} individuals.")
    for full, pts in indiv.items():
        # Format: "Lastname, Firstname"
        rankings[full] = pts                   # exact match
        if "," in full:
            last = full.split(",", 1)[0].strip()
            # If multiple players share a last name, keep the highest-rated one
            if last not in rankings or rankings[last] < pts:
                rankings[last] = pts

    cache_set(cache_key, rankings)
    return rankings


def augment_rankings_with_seedings(bracket_data: dict, rankings: dict) -> dict:
    """
    For teams in the bracket that have no DVV points (e.g. international teams),
    synthesise ranking points via linear interpolation between the nearest known
    seeds above and below.

    Example — Campos/Pedrosa at seed 2, seeds 1 and 3 have DVV data:
        pts_2 = pts_3 + 0.5 * (pts_1 - pts_3)

    General formula for unknown seed N between known seeds A (< N) and B (> N):
        pts_N = pts_B + (pts_A − pts_B) × (N − B) / (A − B)

    If only one side is known, extrapolate from the two closest known seeds on
    that side using the observed linear slope. If no known seeds exist at all,
    falls back to median(rankings) / seed.
    """
    teams_raw = bracket_data.get("teams", {})
    rules     = bracket_data.get("rules", {})
    if not teams_raw or not rules:
        return rankings

    # Only main-draw seeds actually referenced in the bracket rules
    referenced = {r["A"][1:] for r in rules.values() if r["A"].startswith("S")} | \
                 {r["B"][1:] for r in rules.values() if r["B"].startswith("S")}

    # Build seed → team_name for all real (non-placeholder) main-draw slots
    seed_to_name: dict[int, str] = {}
    for info in teams_raw.values():
        seed = str(info["seeding"])
        if seed.isdigit() and seed in referenced and not _is_placeholder(info["players"]):
            seed_to_name[int(seed)] = " - ".join(info["players"])

    if not seed_to_name:
        return rankings

    # Resolve DVV points for every known seed slot
    seed_pts: dict[int, int] = {}
    for s, name in seed_to_name.items():
        seed_pts[s] = lookup_team_points(name, rankings)

    known = {s: p for s, p in seed_pts.items() if p > 0}  # seeds WITH dvv data
    missing = {s: seed_to_name[s] for s in seed_pts if seed_pts[s] == 0}

    if not missing:
        return rankings  # nothing to augment

    known_sorted = sorted(known.items())  # [(seed, pts), ...] ascending seed

    def interpolate(seed: int) -> int:
        """Linear interpolation/extrapolation from adjacent known seeds."""
        above = [(s, p) for s, p in known_sorted if s < seed]  # better rank (lower seed #)
        below = [(s, p) for s, p in known_sorted if s > seed]  # worse  rank (higher seed #)

        if above and below:
            # Interpolate between nearest neighbours
            a_seed, a_pts = above[-1]
            b_seed, b_pts = below[0]
            return int(b_pts + (a_pts - b_pts) * (seed - b_seed) / (a_seed - b_seed))

        if above:
            # Only better seeds known — extrapolate using linear slope of last two
            if len(above) >= 2:
                a1_s, a1_p = above[-1]
                a2_s, a2_p = above[-2]
                slope = (a1_p - a2_p) / (a1_s - a2_s)  # pts per seed step (negative)
                return max(100, int(a1_p + slope * (seed - a1_s)))
            # Only one known seed — scale by ratio
            a_s, a_p = above[-1]
            return max(100, int(a_p * a_s / seed))

        if below:
            # Only worse seeds known
            if len(below) >= 2:
                b1_s, b1_p = below[0]
                b2_s, b2_p = below[1]
                slope = (b1_p - b2_p) / (b1_s - b2_s)
                return max(100, int(b1_p + slope * (seed - b1_s)))
            b_s, b_p = below[0]
            return max(100, int(b_p * b_s / seed))

        # No known seeds at all — median / seed fallback
        actual = [v for v in rankings.values() if isinstance(v, int) and v > 100]
        ref = int(statistics.median(actual)) if actual else 5000
        return max(100, ref // seed)

    global _synthetic_team_names
    augmented = dict(rankings)
    added = []
    for seed, name in sorted(missing.items()):
        synthetic = interpolate(seed)
        augmented[name] = synthetic
        _synthetic_team_names.add(name)
        # Add individual last-name entries so the 'individuals' fallback works too
        for p in re.split(r"\s+[-/,]\s+", name):
            p = p.strip()
            if p and p not in augmented:
                augmented[p] = synthetic // 2
        added.append(f"  {name} (seed {seed}) → {synthetic} pts (interpolated)")

    if added:
        print(f"  Seeding-based DVV estimate for {len(added)} team(s):")
        for line in added:
            print(line)

    return augmented


# ── H2H Data ──────────────────────────────────────────────────────────────────

H2H_TEAMS_URL = "https://gbt.hanski.de/h2h/data/teams_{gender}.json"
H2H_POST_URL  = "https://gbt.hanski.de/h2h/index.php?gender={gender}"


def fetch_team_id_map(gender: str, force: bool = False) -> dict[str, str]:
    """Fetches teams_{gender}.json and returns {team_name: team_id}."""
    cache_key = f"teamids_{gender}"
    if not force:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    url = H2H_TEAMS_URL.format(gender=gender)
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        result = {t["name"]: t["id"] for t in r.json()}
        cache_set(cache_key, result)
        return result
    except Exception as e:
        print(f"  WARNING: Could not fetch team-id map: {e}", file=sys.stderr)
        return {}


_h2h_disk_cache: dict | None = None
_h2h_cache_gender: str | None = None


def _load_h2h_cache(gender: str) -> dict:
    global _h2h_disk_cache, _h2h_cache_gender
    if _h2h_disk_cache is not None and _h2h_cache_gender == gender:
        return _h2h_disk_cache
    cached = cache_get(f"h2h_{gender}", ttl=24 * 3600) or {}
    _h2h_disk_cache = cached
    _h2h_cache_gender = gender
    return cached


def _save_h2h_cache(gender: str) -> None:
    if _h2h_disk_cache is not None:
        cache_set(f"h2h_{gender}", _h2h_disk_cache)


def _flip_individual(ind: dict) -> dict:
    """Reverse all individual H2H records: swap player order and wins/losses."""
    flipped = {}
    for k, v in ind.items():
        a, _, b = k.partition("|||")
        flipped[f"{b}|||{a}"] = {"w": v["l"], "l": v["w"]}
    return flipped


def fetch_h2h(team1_id: str, team2_id: str, gender: str, force: bool = False) -> dict | None:
    """
    Posts to the H2H page with id1/id2 and parses both the team bilanz and
    individual (Einzel-Bilanzen) stats from the HTML.

    Returns {
      "team1_wins": int, "team2_wins": int, "total": int,
      "individual": {"last_a|||last_b": {"w": int, "l": int}, ...}
    } or None.

    Individual keys: last name of team1 player ||| last name of team2 player (lowercase).
    Cached on disk per (gender, sorted-id-pair) for 24 h.
    """
    cache = _load_h2h_cache(gender)
    key = "_".join(sorted([str(team1_id), str(team2_id)]))
    if not force and key in cache:
        entry = dict(cache[key])
        entry.setdefault("individual", {})  # backward compat: old cache entries
        if str(team1_id) <= str(team2_id):
            return entry
        return {"team1_wins": entry["team2_wins"],
                "team2_wins": entry["team1_wins"],
                "total":      entry["total"],
                "individual": _flip_individual(entry["individual"])}

    url = H2H_POST_URL.format(gender=gender)
    try:
        r = requests.post(url, data={"id1": team1_id, "id2": team2_id},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # ── Team total ──
        t1w, t2w = 0, 0
        for h3 in soup.find_all("h3"):
            if "Team-Bilanz" in h3.get_text():
                score = h3.find("span", class_="score")
                if score:
                    m = re.match(r"\s*(\d+)\s*:\s*(\d+)\s*", score.get_text())
                    if m:
                        t1w, t2w = int(m.group(1)), int(m.group(2))
                        break

        # ── Einzel-Bilanzen (individual player H2H) ──
        individual: dict[str, dict] = {}
        for item in soup.select("details.bilanz-item"):
            players_div = item.find("div", class_="players")
            score_div   = item.find("div", class_="score")
            if not players_div or not score_div:
                continue
            names = [b.get_text(strip=True).lower() for b in players_div.find_all("strong")]
            if len(names) != 2:
                continue
            score_strong = score_div.find("strong")
            if not score_strong:
                continue
            parts = score_strong.get_text(strip=True).split(":")
            if len(parts) != 2:
                continue
            try:
                iw, il = int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                continue
            individual[f"{names[0]}|||{names[1]}"] = {"w": iw, "l": il}

        result = {"team1_wins": t1w, "team2_wins": t2w, "total": t1w + t2w,
                  "individual": individual}

        # Store under canonical (sorted) key
        if str(team1_id) <= str(team2_id):
            cache[key] = result
        else:
            cache[key] = {"team1_wins": t2w, "team2_wins": t1w, "total": t1w + t2w,
                          "individual": _flip_individual(individual)}
        _save_h2h_cache(gender)
        return result
    except Exception as e:
        print(f"  WARNING: H2H fetch failed for {team1_id} vs {team2_id}: {e}", file=sys.stderr)
        return None


# ── Win Probability ───────────────────────────────────────────────────────────

_player_share_cache: dict[int, dict[str, int]] = {}

# Teams whose DVV points are synthetic (seeding-based interpolation, not real DVV data).
# Populated by augment_rankings_with_seedings() so lookup_team_points_traced can mark them.
_synthetic_team_names: set[str] = set()


def team_last_names(team_name: str) -> list[str]:
    """'Henning - Pfretzschner' → ['henning', 'pfretzschner'] (lowercase)."""
    return [p.strip().lower() for p in re.split(r"\s+[-/,]\s+", team_name) if p.strip()]


def aggregate_individual_h2h(h2h_data: dict,
                               team_a_names: list[str],
                               team_b_names: list[str]) -> float | None:
    """
    Returns win probability for team_a based on Einzel-Bilanzen records,
    or None if there are fewer than 3 total individual games across all pairs.

    Uses a weighted average (total_wins / total_games) so pairings with more
    games carry more weight than single-game results.

    h2h_data["individual"] keys: "last_a|||last_b" where last_a ∈ team_a_names.
    """
    records = h2h_data.get("individual", {})
    total_wins = 0
    total_games = 0
    for pa in team_a_names:
        for pb in team_b_names:
            v = records.get(f"{pa}|||{pb}")
            if v:
                w, l = v["w"], v["l"]
            else:
                v = records.get(f"{pb}|||{pa}")
                if v:
                    w, l = v["l"], v["w"]   # reversed — b played for team_a side
                else:
                    continue
            games = w + l
            if games > 0:
                total_wins += w
                total_games += games
    if total_games < 3:
        return None
    return total_wins / total_games


def _build_player_share_index(rankings: dict) -> dict[str, int]:
    """For each individual last-name, find their best 'share' (half of best team)."""
    rid = id(rankings)
    if rid in _player_share_cache:
        return _player_share_cache[rid]

    shares: dict[str, int] = {}
    for team, pts in rankings.items():
        # If single name (already an individual entry), use full points
        if " - " not in team and "/" not in team:
            shares[team.strip()] = max(shares.get(team.strip(), 0), pts)
            continue
        # Split team into individual players
        players = re.split(r"\s+[-/,]\s+", team)
        if len(players) < 2:
            continue
        share = pts // 2
        for p in players:
            p = p.strip()
            if not p: continue
            shares[p] = max(shares.get(p, 0), share)
    _player_share_cache[rid] = shares
    return shares


def lookup_team_points(team: str, rankings: dict) -> int:
    return lookup_team_points_traced(team, rankings)[0]


def lookup_team_points_traced(team: str, rankings: dict) -> tuple[int, dict]:
    """
    Returns (points, trace). Trace = {source, breakdown: [...]}

    Resolution order:
      1. Exact team-name match → DVV team ranking (or seeding estimate if synthetic)
      2. Sum of individual DVV rankings (by last-name)
      3. Best-share fallback for players missing from individual list too
    """
    if team in rankings:
        is_synth = team in _synthetic_team_names
        source   = "seeding" if is_synth else "team"
        return rankings[team], {
            "source": source,
            "breakdown": [{"label": team, "value": rankings[team], "type": source}],
        }

    players = re.split(r"\s+[-/,]\s+", team)
    if len(players) < 2:
        return 0, {"source": "missing", "breakdown": []}

    shares = _build_player_share_index(rankings)

    def best_team_for(player):
        best = (None, 0)
        for t_name, pts in rankings.items():
            if " - " not in t_name and "/" not in t_name:
                continue
            parts = re.split(r"\s+[-/,]\s+", t_name)
            if any(p.strip() == player for p in parts):
                if pts > best[1]:
                    best = (t_name, pts)
        return best

    breakdown = []
    total = 0
    has_share = False
    has_individual = False
    for p in players:
        p = p.strip()
        if p in rankings:
            v = rankings[p]
            total += v
            has_individual = True
            breakdown.append({"label": p, "value": v, "type": "individual"})
        elif p in shares:
            has_share = True
            v = shares[p]
            total += v
            best_t, best_p = best_team_for(p)
            breakdown.append({
                "label": p, "value": v, "type": "share",
                "from": best_t, "fromPoints": best_p,
            })
        else:
            breakdown.append({"label": p, "value": 0, "type": "missing"})

    if has_share and has_individual: source = "mixed"
    elif has_share:                  source = "shares"
    elif has_individual:             source = "individuals"
    else:                            source = "missing"
    return total, {"source": source, "breakdown": breakdown}


_CLEAR = 0.10   # H2H win-rate must deviate > 10 % from 0.5 to be "clear winner"
_CLOSE = 0.10   # DVV ratio within 10 % of 0.5 is treated as "close"


def _h2h_for_team1(h2h: dict, team1_is_first: bool) -> tuple[int, int]:
    """Return (team1_wins, team2_wins) from a canonical h2h entry."""
    if team1_is_first:
        return h2h.get("team1_wins", 0), h2h.get("team2_wins", 0)
    return h2h.get("team2_wins", 0), h2h.get("team1_wins", 0)


def win_prob(team1: str, team2: str, rankings: dict, h2h_cache: dict) -> float:
    """
    Probability that team1 beats team2 (used by Monte Carlo).

    Decision order:
      1. Team H2H clear (≥ 3 games, win-rate outside [0.40, 0.60])
      2. Individual H2H clear (≥ 3 total games, avg rate outside [0.40, 0.60])
      3. DVV ratio, if |ratio - 0.5| > 10 %
      4. DVV close: use H2H or individual H2H as tiebreaker
      5. Coin flip
    """
    key = tuple(sorted([team1, team2]))
    h2h = h2h_cache.get(key) or {}
    team1_first = team1 <= team2
    tw, tl = _h2h_for_team1(h2h, team1_first)
    total = tw + tl

    a_names = team_last_names(key[0])
    b_names = team_last_names(key[1])
    ind_p_a = aggregate_individual_h2h(h2h, a_names, b_names)
    ind_p = ind_p_a if team1_first else (1 - ind_p_a if ind_p_a is not None else None)

    # Step 1: clear team H2H
    if total >= 3:
        p = tw / total
        if abs(p - 0.5) > _CLEAR:
            return p

    # Step 2: clear individual H2H
    if ind_p is not None and abs(ind_p - 0.5) > _CLEAR:
        return ind_p

    # Step 3: DVV ratio (not close)
    pts1 = lookup_team_points(team1, rankings)
    pts2 = lookup_team_points(team2, rankings)
    dvv_tot = pts1 + pts2
    if dvv_tot > 0:
        dvv_p = pts1 / dvv_tot
        if abs(dvv_p - 0.5) > _CLOSE:
            return dvv_p

    # Step 4: DVV close — use any H2H as tiebreaker
    if total >= 3:
        return tw / total
    if ind_p is not None:
        return ind_p
    return 0.5


def predict_prob(team1: str, team2: str, rankings: dict,
                 h2h_cache: dict) -> tuple[float, str]:
    """
    Best-guess probability that team1 beats team2, plus a reason string.
    Used for the deterministic bracket display (no coin-flip flattening).

    Same decision order as win_prob but:
      • Returns (prob, reason) instead of just prob
      • Never returns exactly 0.5 from the DVV step (shows raw ratio)

    Reason values: "h2h", "h2h_ind", "dvv", "seeding", "fifty_fifty", "no_data"
    """
    key = tuple(sorted([team1, team2]))
    h2h = h2h_cache.get(key) or {}
    team1_first = team1 <= team2
    tw, tl = _h2h_for_team1(h2h, team1_first)
    total = tw + tl
    team_p = tw / total if total > 0 else None

    a_names = team_last_names(key[0])
    b_names = team_last_names(key[1])
    ind_p_a = aggregate_individual_h2h(h2h, a_names, b_names)
    ind_p = ind_p_a if team1_first else (1 - ind_p_a if ind_p_a is not None else None)

    pts1, trace1 = lookup_team_points_traced(team1, rankings)
    pts2, trace2 = lookup_team_points_traced(team2, rankings)
    dvv_tot = pts1 + pts2
    if dvv_tot > 0:
        dvv_p = pts1 / dvv_tot
        either_synthetic = (trace1.get("source") == "seeding" or
                            trace2.get("source") == "seeding")
        dvv_reason = "seeding" if either_synthetic else "dvv"
    else:
        dvv_p = None
        dvv_reason = "no_data"

    # Step 1: clear team H2H (≥ 3 games required)
    if total >= 3 and team_p is not None and abs(team_p - 0.5) > _CLEAR:
        return team_p, "h2h"

    # Step 2: clear individual H2H
    if ind_p is not None and abs(ind_p - 0.5) > _CLEAR:
        return ind_p, "h2h_ind"

    # Step 3: DVV not close
    if dvv_p is not None and abs(dvv_p - 0.5) > _CLOSE:
        return dvv_p, dvv_reason

    # Step 4: DVV close — use any H2H as tiebreaker (≥ 3 games required for team H2H)
    if total >= 3 and team_p is not None:
        return team_p, "h2h"
    if ind_p is not None:
        return ind_p, "h2h_ind"

    if dvv_p is not None:
        return dvv_p, dvv_reason   # close with no H2H: show raw DVV
    return 0.5, "fifty_fifty"


# ── GBT Bracket fetching & simulation ────────────────────────────────────────

def _fetch_gbt_bracket_legacy(gender: str, force: bool = False) -> dict | None:
    """
    Legacy fallback: fetch the gbt.hanski.de bracket JSON. Kept as a
    secondary source — primary is now DVV (`scripts/dvv_tournament.py`).
    Cached on disk for 1 h.
    """
    cache_key = f"bracket_{gender}"
    if not force:
        cached = cache_get(cache_key)
        if cached is not None:
            print(f"GBT bracket ({gender}): using cached")
            return cached

    url = GBT_BRACKET_URL.format(gender=gender)
    print(f"Fetching GBT bracket from {url}…")
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
        cache_set(cache_key, data)
        return data
    except Exception as e:
        print(f"  WARNING: Could not fetch GBT bracket: {e}", file=sys.stderr)
        return None


def fetch_tournament_bracket(gender: str, force: bool = False) -> dict | None:
    """
    Primary bracket source = DVV (`beach.volleyball-verband.de`), with
    `gbt.hanski.de` als Fallback. Output schema is identical to the legacy
    GBT JSON — downstream consumers (`sync_players_available_from_brackets`,
    `simulate_gbt_bracket`, `_run`) work unchanged.
    """
    try:
        import dvv_tournament
        b = dvv_tournament.build_bracket(gender, force=force)
        if b and b.get("teams"):
            src = b.get("meta", {}).get("name", "?")
            print(f"  ✓ DVV-Bracket {gender}: {src} "
                  f"(teams={len(b['teams'])}, matches={len(b.get('matches', []))})")
            return b
    except Exception as e:
        print(f"  ⚠ DVV-Bracket {gender} fehlgeschlagen: {e}")

    print(f"  ℹ Fallback auf gbt.hanski.de für Bracket {gender}.")
    return _fetch_gbt_bracket_legacy(gender, force=force)


def _is_placeholder(players: list[str]) -> bool:
    """A placeholder team has only invented qualifier names like ['Q1'] or 'Quali...'."""
    if not players:
        return True
    joined = " ".join(players).lower()
    if all(re.fullmatch(r"q\d+", p.strip(), re.I) for p in players):
        return True
    if "quali" in joined:
        return True
    return False


def compute_bracket_prediction(bracket_data: dict, rankings: dict,
                                h2h_cache: dict) -> list[dict]:
    """
    Deterministic 'most-likely path' prediction. For each match, the team with
    the higher win_prob advances. Returns a list of match-result dicts.
    """
    rules     = bracket_data["rules"]
    teams_raw = bracket_data["teams"]

    seed_to_team: dict[str, str] = {}
    for info in teams_raw.values():
        seed = str(info["seeding"])
        if not seed.isdigit() or int(seed) > 50:
            continue
        seed_to_team[seed] = " - ".join(info["players"])

    # Known winners from DVV Spielplan (already-played matches) — when present,
    # we lock the predicted winner to the real result instead of computing
    # DVV/H2H probability.
    known_winners: dict[int, tuple[str, str]] = {}
    for m in (bracket_data.get("matches") or []):
        res = m.get("result") or {}
        w = res.get("winner")
        if w not in ("A", "B"):
            continue
        ta = " - ".join(m["team_a"]["players"])
        tb = " - ".join(m["team_b"]["players"])
        known_winners[int(m["match_num"])] = (ta, tb) if w == "A" else (tb, ta)

    match_results: dict[int, dict] = {}

    def resolve(ref: str):
        prefix, num = ref[0], ref[1:]
        if prefix == "S":
            return seed_to_team.get(num)
        if prefix in ("W", "L"):
            n = int(num)
            if n not in match_results:
                play(n)
            res = match_results.get(n)
            if not res:
                return None
            return res["winner"] if prefix == "W" else res["loser"]
        return None

    def play(num: int):
        if num in match_results:
            return
        rule = rules.get(str(num))
        if not rule:
            return
        a = resolve(rule["A"])
        b = resolve(rule["B"])
        if a is None or b is None:
            match_results[num] = {
                "match":  num,
                "teamA":  a or "TBD",
                "teamB":  b or "TBD",
                "refA":   rule["A"],
                "refB":   rule["B"],
                "probA":  0.5,
                "probB":  0.5,
                "winner": a or b or "TBD",
                "loser":  b or a or "TBD",
                "h2hUsed": False,
            }
            return
        # Lock to the played outcome if this match is already in the Spielplan.
        if num in known_winners and {a, b} == set(known_winners[num]):
            kw, kl = known_winners[num]
            pA = 1.0 if kw == a else 0.0
            reason = "played"
        else:
            pA, reason = predict_prob(a, b, rankings, h2h_cache)
        h2h_key = tuple(sorted([a, b]))
        h2h = h2h_cache.get(h2h_key) or {}
        winner, loser = (a, b) if pA >= 0.5 else (b, a)
        ptsA, traceA = lookup_team_points_traced(a, rankings)
        ptsB, traceB = lookup_team_points_traced(b, rankings)

        # Team H2H detail — canonical: smaller name = team1
        h2h_detail = None
        team_total = h2h.get("total", 0)
        if team_total > 0:
            if a <= b:
                h2h_detail = {"winsA": h2h["team1_wins"], "winsB": h2h["team2_wins"],
                               "total": team_total}
            else:
                h2h_detail = {"winsA": h2h["team2_wins"], "winsB": h2h["team1_wins"],
                               "total": team_total}

        # Individual H2H breakdown (for modal display)
        ind_breakdown = []
        ind_records = h2h.get("individual", {})
        a_is_first = (a <= b)   # whether teamA is the canonical "team1" in the cache
        for pa in team_last_names(a):
            for pb in team_last_names(b):
                # Records are stored with canonical-first player first
                main_key = f"{pa}|||{pb}" if a_is_first else f"{pb}|||{pa}"
                v = ind_records.get(main_key)
                if v:
                    wA = v["w"] if a_is_first else v["l"]
                    wB = v["l"] if a_is_first else v["w"]
                else:
                    alt_key = f"{pb}|||{pa}" if a_is_first else f"{pa}|||{pb}"
                    v = ind_records.get(alt_key)
                    if not v:
                        continue
                    wA = v["l"] if a_is_first else v["w"]
                    wB = v["w"] if a_is_first else v["l"]
                games = wA + wB
                if games > 0:
                    ind_breakdown.append({
                        "playerA": pa.capitalize(),
                        "playerB": pb.capitalize(),
                        "wA": wA, "wB": wB,
                    })

        match_results[num] = {
            "match":        num,
            "teamA":        a,
            "teamB":        b,
            "refA":         rule["A"],
            "refB":         rule["B"],
            "probA":        round(pA, 3),
            "probB":        round(1 - pA, 3),
            "winner":       winner,
            "loser":        loser,
            "h2hUsed":      reason in ("h2h", "h2h_ind"),
            "h2h":          h2h_detail,
            "indBreakdown": ind_breakdown if ind_breakdown else None,
            "reason":       reason,
            "ptsA":         ptsA,
            "ptsB":         ptsB,
            "traceA":       traceA,
            "traceB":       traceB,
        }

    for match_num in rules:
        play(int(match_num))

    return [match_results[k] for k in sorted(match_results.keys())]


def simulate_gbt_bracket(bracket_data: dict, rankings: dict,
                          h2h_cache: dict,
                          include_qualifiers: bool = False) -> dict[str, int]:
    """
    Simulate a GBT bracket using the actual `rules` graph.

    Match references:
      Sx = team with seeding x
      Wx = winner of match x
      Lx = loser of match x

    Qualifier handling:
      - Main-draw slots whose seed is filled with a placeholder team (players=['Q1'])
        are replaced each simulation with a real qualifier team (Qx seedings),
        weighted by DVV ranking.
      - Every real qualifier team gets +1 match (the qualifier round).

    Returns {team_name: matches_played}
    """
    rules     = bracket_data["rules"]
    teams_raw = bracket_data["teams"]

    # Categorise teams
    main_draw: dict[str, str] = {}     # seed → real team name (numeric seedings only)
    placeholders: dict[str, str] = {}  # seed → placeholder name (e.g. 'Q1')
    qualifiers: list[tuple[str, str]] = []  # [(team_name, dvv_pts)]

    # Only seedings 1..8 are real main-draw slots. 99 = withdrawn/wildcard, ignore.
    referenced_seeds = {r["A"][1:] for r in rules.values() if r["A"].startswith("S")} | \
                       {r["B"][1:] for r in rules.values() if r["B"].startswith("S")}

    for info in teams_raw.values():
        seed = str(info["seeding"])
        name = " - ".join(info["players"])
        if seed.isdigit():
            if seed not in referenced_seeds:
                continue  # withdrawn / wildcard, not in bracket
            if _is_placeholder(info["players"]):
                placeholders[seed] = name
            else:
                main_draw[seed] = name
        elif seed.startswith("Q"):
            if not _is_placeholder(info["players"]):
                qualifiers.append((name, lookup_team_points(name, rankings)))

    seed_to_team = dict(main_draw)
    if include_qualifiers and placeholders and qualifiers:
        # Weighted random sampling without replacement → fill S7/S8 from qualifier pool
        pool = list(qualifiers)
        for slot in placeholders:
            if not pool:
                seed_to_team[slot] = placeholders[slot]
                continue
            weights = [max(p, 1) for _, p in pool]
            chosen = random.choices(range(len(pool)), weights=weights, k=1)[0]
            seed_to_team[slot] = pool[chosen][0]
            pool.pop(chosen)
    else:
        # Leave placeholder teams in their slots — they'll play but won't map to any real player
        seed_to_team.update(placeholders)

    matches_played: dict[str, int] = defaultdict(int)
    match_outcome: dict[int, tuple[str, str]] = {}  # match_num → (winner, loser)

    # Known winners from DVV Spielplan (matches that have actually been played).
    # We use those FIXED instead of rolling DVV/H2H dice, so the simulation
    # reflects the real state of the tournament.
    known_winners: dict[int, tuple[str, str]] = {}
    for m in (bracket_data.get("matches") or []):
        res = m.get("result") or {}
        w = res.get("winner")
        if w not in ("A", "B"):
            continue
        team_a = " - ".join(m["team_a"]["players"])
        team_b = " - ".join(m["team_b"]["players"])
        if w == "A":
            known_winners[int(m["match_num"])] = (team_a, team_b)
        else:
            known_winners[int(m["match_num"])] = (team_b, team_a)

    # Every real qualifier team plays at least 1 qualifier match
    if include_qualifiers:
        for q_name, _ in qualifiers:
            matches_played[q_name] += 1

    def resolve(ref: str) -> str | None:
        """Resolve Sx / Wx / Lx → team name. None if unresolved (e.g. qualifier TBD)."""
        prefix, num = ref[0], ref[1:]
        if prefix == "S":
            return seed_to_team.get(num)
        if prefix in ("W", "L"):
            n = int(num)
            if n not in match_outcome:
                play_match(n)
            if n not in match_outcome:
                return None
            w, l = match_outcome[n]
            return w if prefix == "W" else l
        return None

    def play_match(num: int):
        if num in match_outcome:
            return
        rule = rules.get(str(num))
        if not rule:
            return
        a = resolve(rule["A"])
        b = resolve(rule["B"])
        if a is None or b is None:
            return  # can't simulate this match (missing team)
        matches_played[a] += 1
        matches_played[b] += 1
        # If the DVV Spielplan already shows a winner for this match, lock
        # in that outcome rather than rolling dice. Matches the known pair
        # by team-name set (Spielplan team-name format equals seed_to_team's).
        if num in known_winners:
            kw, kl = known_winners[num]
            pair_match  = {a, b} == {kw, kl}
            if pair_match:
                match_outcome[num] = (kw, kl)
                return
        p = win_prob(a, b, rankings, h2h_cache)
        if random.random() < p:
            match_outcome[num] = (a, b)
        else:
            match_outcome[num] = (b, a)

    for match_num in rules:
        play_match(int(match_num))

    return dict(matches_played)


# ── Generic bracket fallback (used when GBT bracket not available) ───────────

def simulate_bracket_single_elim(seeded_teams: list[str], rankings: dict,
                                  h2h_cache: dict) -> dict[str, int]:
    """Single-elimination bracket. Returns {team: matches_played}."""
    matches = defaultdict(int)
    field = list(seeded_teams)

    while len(field) > 1:
        next_round = []
        # pair highest vs lowest seed remaining
        for i in range(len(field) // 2):
            t1, t2 = field[i], field[-(i + 1)]
            matches[t1] += 1
            matches[t2] += 1
            p = win_prob(t1, t2, rankings, h2h_cache)
            winner = t1 if random.random() < p else t2
            next_round.append(winner)
        if len(field) % 2 == 1:
            next_round.append(field[len(field) // 2])  # bye
        field = next_round

    return dict(matches)


def simulate_bracket_double_elim(seeded_teams: list[str], rankings: dict,
                                  h2h_cache: dict) -> dict[str, int]:
    """
    Double-elimination bracket (as used at GBT).
    Teams get two losses before elimination.
    Returns {team: matches_played}.
    """
    matches = defaultdict(int)

    # Winners bracket and losers bracket tracking
    winners = list(seeded_teams)
    losers  = []
    losses  = defaultdict(int)

    def play(t1, t2):
        matches[t1] += 1
        matches[t2] += 1
        p = win_prob(t1, t2, rankings, h2h_cache)
        if random.random() < p:
            return t1, t2  # winner, loser
        return t2, t1

    # --- Winners bracket rounds until 1 winner ---
    while len(winners) > 1:
        next_winners = []
        next_losers  = []
        # Pair 1v2, 3v4, etc. (standard bracket seeding)
        half = len(winners) // 2
        for i in range(half):
            t1, t2 = winners[i], winners[-(i + 1)]
            w, l = play(t1, t2)
            next_winners.append(w)
            next_losers.append(l)
        if len(winners) % 2 == 1:
            next_winners.append(winners[half])  # bye
        winners = next_winners
        losers  = losers + next_losers

    # --- Losers bracket until 1 survivor ---
    while len(losers) > 1:
        next_losers = []
        half = len(losers) // 2
        for i in range(half):
            t1, t2 = losers[i], losers[-(i + 1)]
            w, l = play(t1, t2)
            next_losers.append(w)
            # l is eliminated — already counted in matches
        if len(losers) % 2 == 1:
            next_losers.append(losers[half])
        losers = next_losers

    # --- Grand Final ---
    if winners and losers:
        w, l = play(winners[0], losers[0])
        # If losers bracket winner wins, one more final needed
        if w == losers[0]:
            play(winners[0], losers[0])  # decisive match counts as another match

    return dict(matches)


# ── Player mapping ────────────────────────────────────────────────────────────

def load_players(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize(name: str) -> str:
    """Lowercase, strip accents roughly, for fuzzy matching."""
    replacements = {
        "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
        "é": "e", "è": "e", "ê": "e",
    }
    n = name.lower()
    for k, v in replacements.items():
        n = n.replace(k, v)
    return n


def map_teams_to_players(bracket_teams: list[str], players: list[dict],
                         fs_season: dict | None = None) -> dict[str, list[str]]:
    """
    Map bracket team names (e.g. 'Ehlers - Wickler') to player IDs.
    Matches by last name (case-insensitive, umlaut-normalized).

    Ambiguous last names (e.g. four Wüsts in players_all.json but only two are
    in the current bracket) are resolved in three layers:

      1. Firestore season snapshot (`fs_season`, keyed by player ID) — if a
         player ID isn't listed in the current Firestore season, they're not
         playing right now; we narrow the candidate pool to only the IDs
         present there. Eliminates almost all ambiguity in one stroke.

      2. players_available.json — names the user has previously confirmed via
         the ambiguous-name picker. Wins tie-breaks within the narrowed pool.

      3. Highest-`tp` candidate that hasn't been taken by another slot yet.

    Within one sim run each bracket slot also gets a different full name when
    the surname repeats, so Tamo and Lui Wüst don't both collapse to whichever
    Wüst the dict happened to overwrite last.

    `fs_season` is the parsed Firestore snapshot from firestore_sync.
    fetch_firestore_season(); pass None to disable the filter (manual mode).

    Returns {team_name: [player_id, player_id]}
    """
    # Augment the player pool with Firestore-only entries (players who are in
    # the current season but have no row in players_all.json yet — typical for
    # rookies like Milan Sievers who only appear from this season onward).
    # We synthesize a players_all-compatible record from the Firestore data.
    pool: list[dict] = list(players)
    if fs_season:
        existing_ids = {p["id"] for p in players}
        for pid, fs_p in fs_season.items():
            if pid in existing_ids:
                continue
            pool.append({
                "id":        pid,
                "firstName": fs_p.get("firstName", ""),
                "lastName":  fs_p.get("lastName", ""),
                "pos":       fs_p.get("pos", ""),
                "gender":    fs_p.get("gender", ""),
                "tp":        fs_p.get("tp", 0),
                "t":         fs_p.get("t", 0),
                "mp":        fs_p.get("mp", 0),
                "img":       fs_p.get("img", ""),
            })

    # Group all players by normalized last name. If a Firestore snapshot is
    # given, we keep TWO buckets: one narrowed to current-season players, one
    # with everyone. Surnames where the narrowed bucket is empty fall back to
    # the full bucket (defensive — handles stale snapshots).
    by_last_all: dict[str, list[dict]] = defaultdict(list)
    by_last_season: dict[str, list[dict]] = defaultdict(list)
    for p in pool:
        ln = normalize(p["lastName"])
        by_last_all[ln].append(p)
        if fs_season is None or p["id"] in fs_season:
            by_last_season[ln].append(p)

    # Names the user has confirmed via players_available.json — these win
    # tie-breaks for ambiguous surnames.
    confirmed_names: set[str] = set()
    if PLAYERS_AVL.exists():
        try:
            with open(PLAYERS_AVL, encoding="utf-8") as f:
                for entry in json.load(f):
                    if "name" in entry:
                        confirmed_names.add(entry["name"].strip())
        except Exception:
            pass

    # Per-surname assignment tracker, so two bracket slots sharing the same
    # last name pick two different full names.
    assigned: dict[str, set[str]] = defaultdict(set)

    result = {}
    for team in bracket_teams:
        # Split only on " - " / " / " / " , " — preserves hyphenated surnames like "Stadie-Seeber"
        parts = re.split(r"\s+[-/,]\s+", team)
        ids = []
        for part in parts:
            # Use last word as last name (handles "Philipp Konstantin Huster" → "Huster")
            last = normalize(part.strip().split()[-1])
            # Prefer the Firestore-narrowed pool; only fall back to the full
            # pool if narrowing leaves zero candidates (stale snapshot guard).
            candidates = by_last_season.get(last) or by_last_all.get(last, [])
            taken = assigned[last]

            pick = None
            # 1. Prefer a confirmed (in players_available.json) name not yet used
            for c in candidates:
                full = f"{c['firstName']} {c['lastName']}"
                if full in confirmed_names and full not in taken:
                    pick = c
                    break
            # 2. Highest-tp candidate not yet used
            if pick is None:
                avail = [c for c in candidates
                         if f"{c['firstName']} {c['lastName']}" not in taken]
                if avail:
                    pick = max(avail, key=lambda p: p.get("tp", 0))
            # 3. Fallback: all candidates already taken (more bracket slots than DB rows)
            if pick is None and candidates:
                pick = max(candidates, key=lambda p: p.get("tp", 0))

            if pick:
                full = f"{pick['firstName']} {pick['lastName']}"
                assigned[last].add(full)
                ids.append(pick["id"])
            else:
                print(f"  Note: no player record for '{part.strip()}' (in team '{team}')")
        result[team] = ids

    return result


# ── Sync players_available.json from current brackets ───────────────────────

def sync_players_available_from_brackets(force: bool = False) -> dict:
    """
    Reads both gender brackets, extracts every real (non-placeholder) player,
    matches their last name against players_all.json, and updates
    data/players_available.json:

      • New players → added with price from Firestore (if available) or -1
      • Existing players → price updated to Firestore value if different, else preserved
      • Players already in the file but not in either bracket → dropped

    Returns {'added':[], 'unmatched':[], 'pending':[], 'ambiguous':[],
             'prices_changed':[{'name', 'old', 'new'}]}
    """
    print("Syncing players_available.json from brackets…")

    # ── Try Firestore snapshot (source of truth) ──
    fs_season: dict | None = None
    try:
        import firestore_sync
        fs_season = firestore_sync.fetch_firestore_season(force=force)
        if fs_season:
            print(f"  ✓ Firestore-Snapshot: {len(fs_season)} Spieler aktiv "
                  f"(Alter: {firestore_sync.snapshot_age_seconds():.0f}s)")
        else:
            print("  ℹ Kein Firestore-Snapshot (data/firebase_auth.json fehlt) — "
                  "Fallback auf lokale Daten.")
    except RuntimeError as e:
        print(f"  ⚠ Firestore-Sync fehlgeschlagen: {e}")

    # Load existing prices keyed by name
    existing: list[dict] = []
    if PLAYERS_AVL.exists():
        try:
            with open(PLAYERS_AVL, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as e:
            print(f"  WARNING: could not read existing players_available.json: {e}",
                  file=sys.stderr)

    name_to_price: dict[str, float] = {}
    for entry in existing:
        if "name" in entry:
            name_to_price[entry["name"].strip()] = entry["price"]

    # Load all players for last-name matching
    with open(PLAYERS_ALL, encoding="utf-8") as f:
        all_players = json.load(f)

    # Augment with Firestore-only players (this-season rookies missing from
    # players_all.json) — same logic as in map_teams_to_players. Without this,
    # bracket surnames like "Sievers" (only present in the current season's
    # Firestore) silently appear as "unmatched".
    if fs_season:
        existing_ids = {p["id"] for p in all_players}
        for pid, fs_p in fs_season.items():
            if pid in existing_ids:
                continue
            all_players.append({
                "id":        pid,
                "firstName": fs_p.get("firstName", ""),
                "lastName":  fs_p.get("lastName", ""),
                "pos":       fs_p.get("pos", ""),
                "gender":    fs_p.get("gender", ""),
                "tp":        fs_p.get("tp", 0),
                "t":         fs_p.get("t", 0),
                "mp":        fs_p.get("mp", 0),
                "img":       fs_p.get("img", ""),
            })

    # Group by last-name. With Firestore: prefer the narrowed-to-active-season
    # pool; only fall back to the full pool if narrowing leaves zero candidates
    # for a given last name (defensive — stale snapshot guard).
    by_last_all: dict[str, list[dict]] = defaultdict(list)
    by_last_season: dict[str, list[dict]] = defaultdict(list)
    for p in all_players:
        ln = normalize(p["lastName"])
        by_last_all[ln].append(p)
        if fs_season is None or p["id"] in fs_season:
            by_last_season[ln].append(p)
    # Picker uses the narrowed pool when present, else full pool.
    def candidates_for(ln: str) -> list[dict]:
        return by_last_season.get(ln) or by_last_all.get(ln, [])

    bracket_names: set[str] = set()
    unmatched: list[str] = []
    ambiguous: list[dict] = []  # structured for the frontend picker

    # Track which full player names have already been assigned for a given normalized
    # last name, so that two bracket slots with the same surname get different players.
    assigned_by_last: dict[str, set[str]] = defaultdict(set)

    for g in ("m", "f"):
        bracket = fetch_tournament_bracket(g, force=force)
        if not bracket:
            continue
        # The upstream GBT bracket sometimes returns `teams` as an EMPTY LIST
        # between tournaments (no draw yet). Guard against that — we want a
        # mapping (seeding → info) when populated, anything else is "empty".
        teams_field = bracket.get("teams")
        if not isinstance(teams_field, dict):
            print(f"  ℹ Bracket {g}: keine teams dict (Stand: {type(teams_field).__name__}, len={len(teams_field) if hasattr(teams_field,'__len__') else '?'}) — übersprungen.")
            continue
        for info in teams_field.values():
            if _is_placeholder(info["players"]):
                continue
            seed = str(info["seeding"])
            # Only main-draw teams: numeric seedings 1..N (skip Q-seedings and 99 wildcards)
            if not seed.isdigit():
                continue
            if int(seed) > 50:
                continue

            team_context = " - ".join(info["players"])  # e.g. "Wüst - Schmidt" (for picker label)

            for ln in info["players"]:
                nln = normalize(ln)
                candidates = candidates_for(nln)
                already_used = assigned_by_last[nln]

                # Pick priority:
                #  1. A candidate the user explicitly chose before (in name_to_price)
                #     that isn't already taken by another slot in this bracket run.
                #  2. Highest-tp candidate not yet taken.
                #  3. Fallback: highest-tp overall (all slots saturated — shouldn't happen).
                player = None
                for c in candidates:
                    full = f"{c['firstName']} {c['lastName']}"
                    if full in name_to_price and full not in already_used:
                        player = c
                        break
                if player is None:
                    avail = [c for c in candidates
                             if f"{c['firstName']} {c['lastName']}" not in already_used]
                    if avail:
                        player = max(avail, key=lambda p: p.get("tp", 0))
                if player is None and candidates:
                    player = max(candidates, key=lambda p: p.get("tp", 0))

                if player:
                    chosen = f"{player['firstName']} {player['lastName']}"
                    bracket_names.add(chosen)
                    assigned_by_last[nln].add(chosen)

                    if len(candidates) > 1:
                        amb_entry = {
                            "lastName":    ln,
                            "teamContext": team_context,
                            "chosen":      chosen,
                            "candidates":  [
                                {
                                    "name":   f"{c['firstName']} {c['lastName']}",
                                    "id":     c["id"],
                                    "tp":     c.get("tp", 0),
                                    "t":      c.get("t", 0),
                                    "mp":     c.get("mp", 0),
                                    "pos":    c.get("pos", ""),
                                    "gender": c.get("gender", ""),
                                    "img":    c.get("img"),
                                }
                                for c in candidates
                            ],
                        }
                        # One picker entry per (lastName, chosen) — same player in
                        # multiple slots only needs one entry; different players need separate entries.
                        if not any(a["lastName"] == ln and a["chosen"] == chosen
                                   for a in ambiguous):
                            ambiguous.append(amb_entry)
                else:
                    if ln not in unmatched:
                        unmatched.append(ln)

    # Für die Preis-Lookup-Map (full-name → fs_player dict) — auch für den
    # "Brackets-leer"-Pfad unten gebraucht.
    fs_by_full_name: dict[str, dict] = {}
    if fs_season:
        for pid, p in fs_season.items():
            fs_by_full_name[f"{p['firstName']} {p['lastName']}".strip()] = p

    # SAFETY: if both brackets returned empty (pre-/post-tournament state),
    # `bracket_names` is empty. Writing `[]` to players_available.json would
    # silently wipe the user's manually-entered prices. Don't rewrite the
    # structure — but DO still update prices from Firestore for the existing
    # entries (the whole point of clicking "Aus Firestore"). Without this,
    # the user sees "17 prices missing" forever between tournaments.
    if not bracket_names:
        prices_changed: list[dict] = []
        if fs_by_full_name:
            for e in existing:
                nm = e.get("name", "").strip()
                fs_p = fs_by_full_name.get(nm)
                if not fs_p or fs_p.get("price") is None:
                    continue
                new_p = fs_p["price"]
                old_p = e.get("price")
                if old_p != new_p:
                    prices_changed.append({"name": nm, "old": old_p, "new": new_p})
                    print(f"  💰 Preis-Update: {nm}  {old_p}₡ → {new_p}₡")
                    e["price"] = new_p
            if prices_changed:
                with open(PLAYERS_AVL, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"  ⚠ Beide Brackets sind leer — players_available.json behält "
              f"Struktur, {len(prices_changed)} Preise aus Firestore aktualisiert.")
        return {"added": [], "removed": [], "unmatched": unmatched,
                "pending": [e["name"] for e in existing if e.get("price", -1) <= 0],
                "ambiguous": ambiguous, "prices_changed": prices_changed}

    # Build new list — ONLY contains players from the current brackets.
    # Players that were in the old file but aren't in any current bracket are dropped.
    # Preise: Firestore wins (source of truth). Falls Firestore-Snapshot fehlt,
    # bleibt der vom User gepflegte Preis erhalten.

    new_list: list[dict] = []
    added: list[str] = []
    removed: list[str] = []
    prices_changed = []  # type: list[dict]  (reuses name from empty-brackets path above)
    for name in sorted(bracket_names):
        fs_p = fs_by_full_name.get(name)
        old_price = name_to_price.get(name)
        if fs_p and fs_p.get("price") is not None:
            new_price = fs_p["price"]
            # Log any diff — including pending (-1) → real price, so the user
            # sees that "Preise fehlen" got resolved.
            if old_price is not None and old_price != new_price:
                prices_changed.append({"name": name, "old": old_price, "new": new_price})
                print(f"  💰 Preis-Update: {name}  {old_price}₡ → {new_price}₡")
        else:
            # No Firestore data — keep existing price, default -1 for new
            new_price = old_price if old_price is not None else -1
        if name not in name_to_price:
            added.append(name)
        new_list.append({"name": name, "price": new_price})

    # Track which old entries got dropped (so we can show it)
    for nm in name_to_price:
        if nm not in bracket_names:
            removed.append(nm)

    # Write
    with open(PLAYERS_AVL, "w", encoding="utf-8") as f:
        json.dump(new_list, f, ensure_ascii=False, indent=2)

    pending = [e["name"] for e in new_list if e["price"] is None or e["price"] <= 0]

    print(f"  {len(new_list)} players in list "
          f"({len(added)} new, {len(removed)} removed, {len(pending)} need price, "
          f"{len(unmatched)} unmatched in players_all.json)")
    if added:
        print(f"  + {', '.join(added)}")
    if removed:
        print(f"  - {', '.join(removed)}")
    if unmatched:
        print(f"  ⚠ Bracket last-names not in players_all.json: {', '.join(unmatched)}")
    if ambiguous:
        print(f"  ⚠ Mehrdeutige Nachnamen (höchste Saison-Pts gewählt):")
        for a in ambiguous:
            others = [c["name"] for c in a["candidates"] if c["name"] != a["chosen"]]
            print(f"     {a['lastName']} → {a['chosen']} (auch möglich: {', '.join(others)})")

    if prices_changed:
        print(f"  💰 {len(prices_changed)} Preise aus Firestore aktualisiert.")

    return {"added": added, "removed": removed, "unmatched": unmatched,
            "pending": pending, "ambiguous": ambiguous,
            "prices_changed": prices_changed}


# ── Hash helpers (for staleness detection) ───────────────────────────────────

def file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def players_available_hash() -> str:
    return file_hash(PLAYERS_AVL)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_simulation(gender: str = "m", simulations: int = 20000,
                    bracket_format: str = "double",
                    include_qualifiers: bool = False,
                    force_refresh: bool = False) -> dict:
    """
    Run the simulation programmatically. Returns the gender block that was added
    to tournament_sim.json.
    """
    args = argparse.Namespace(gender=gender, simulations=simulations,
                              bracket_format=bracket_format,
                              include_qualifiers=include_qualifiers,
                              force_refresh=force_refresh)
    return _run(args)


def main():
    parser = argparse.ArgumentParser(description="Simulate GBT tournament bracket.")
    parser.add_argument("--gender", choices=["m", "f"], default="m")
    parser.add_argument("--simulations", type=int, default=20000,
                        help="Number of Monte Carlo runs (default: 20000)")
    parser.add_argument("--bracket-format", choices=["single", "double"], default="double",
                        help="Bracket format (default: double elimination)")
    parser.add_argument("--include-qualifiers", action="store_true",
                        help="Simulate qualifier round and include qualifier teams in output. "
                             "Default: off (qualifier results usually not known yet at team-selection time).")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Ignore caches and re-fetch all remote data.")
    args = parser.parse_args()
    _run(args)


def _run(args) -> dict:
    gender = args.gender
    force = getattr(args, "force_refresh", False)

    # Auto-sync the available-players list from the current brackets first
    # (preserves manually-set prices, marks new players with price=-1)
    sync_info = sync_players_available_from_brackets(force=force)

    # ── Try to fetch official GBT bracket; fall back to local bracket file ──
    gbt_bracket = fetch_tournament_bracket(gender, force=force)
    bracket_file = ROOT / "data" / f"bracket_{gender}.json"

    # Treat brackets with non-dict `teams` (often an empty list between
    # tournaments) as "not loaded" and fall through to the local fallback.
    if gbt_bracket and not isinstance(gbt_bracket.get("teams"), dict):
        print(f"  ℹ GBT-Bracket {gender}: teams ist {type(gbt_bracket.get('teams')).__name__} "
              f"(noch nicht ausgelost?) — Fallback auf data/bracket_{gender}.json.")
        gbt_bracket = None

    if gbt_bracket:
        # Determine which seedings are actually referenced in the bracket rules
        rules = gbt_bracket.get("rules", {})
        referenced = {r["A"][1:] for r in rules.values() if r["A"].startswith("S")} | \
                     {r["B"][1:] for r in rules.values() if r["B"].startswith("S")}

        def keep(t):
            if _is_placeholder(t["players"]):
                return False
            seed = str(t["seeding"])
            if seed.isdigit():
                # With rules: filter to seeds actually referenced (drops withdrawn / seed-99).
                # Without rules (e.g. DVV bracket for a tournament size we don't have an
                # 8-team template for): accept all numeric seeds — they feed the generic
                # single/double-elim fallback simulation.
                return (seed in referenced) if referenced else True
            # Qualifier teams: only included when --include-qualifiers is set
            return args.include_qualifiers

        bracket_teams = [
            " - ".join(t["players"])
            for t in sorted(gbt_bracket["teams"].values(),
                            key=lambda t: str(t["seeding"]))
            if keep(t)
        ]
        print(f"  Tournament: {gbt_bracket['meta'].get('name', '?')} "
              f"({gbt_bracket['meta'].get('status', '?')})")
        print(f"  Bracket: {len(bracket_teams)} real teams "
              f"({gbt_bracket['meta'].get('matches', '?')} main-draw matches)")
    elif bracket_file.exists():
        with open(bracket_file, encoding="utf-8") as f:
            bracket_teams = json.load(f)
        print(f"  Falling back to {bracket_file} ({len(bracket_teams)} teams)")
    else:
        print(f"ERROR: Could not fetch GBT bracket and no fallback file at {bracket_file}",
              file=sys.stderr)
        sys.exit(1)

    # ── Fetch DVV rankings ──
    rankings = fetch_dvv_rankings(gender, force=force)

    # Augment with seeding-based synthetic points for international teams (no DVV data)
    if gbt_bracket:
        rankings = augment_rankings_with_seedings(gbt_bracket, rankings)

    # ── Resolve bracket team names to GBT team IDs ──
    print("Resolving team IDs from H2H teams_m.json…")
    team_id_map = fetch_team_id_map(gender, force=force)
    bracket_team_ids: dict[str, str] = {}
    for team in bracket_teams:
        tid = team_id_map.get(team)
        if tid:
            bracket_team_ids[team] = tid
        else:
            print(f"  WARNING: '{team}' not in H2H team list — H2H queries will skip it.")

    # ── Fetch H2H for every team pair in the bracket ──
    print("Fetching H2H bilanz for each pairing…")
    h2h_cache: dict[tuple, dict] = {}
    pairs = [(bracket_teams[i], bracket_teams[j])
             for i in range(len(bracket_teams))
             for j in range(i + 1, len(bracket_teams))]

    for t1, t2 in pairs:
        id1 = bracket_team_ids.get(t1)
        id2 = bracket_team_ids.get(t2)
        if not id1 or not id2:
            continue
        key = tuple(sorted([t1, t2]))
        if key in h2h_cache:
            continue
        h2h = fetch_h2h(id1, id2, gender, force=force)
        if h2h and (h2h["total"] > 0 or h2h.get("individual")):
            # Map the result back to canonical key order (alphabetical team names)
            if t1 > t2:
                h2h = {"team1_wins": h2h["team2_wins"], "team2_wins": h2h["team1_wins"],
                       "total": h2h["total"],
                       "individual": _flip_individual(h2h.get("individual", {}))}
            h2h_cache[key] = h2h
            ind_count = len(h2h.get("individual", {}))
            if h2h["total"] > 0:
                print(f"  {t1} vs {t2} → {h2h['team1_wins']}:{h2h['team2_wins']}"
                      + (f"  ({ind_count} Einzel-Bilanzen)" if ind_count else ""))
            elif ind_count:
                print(f"  {t1} vs {t2} → no team H2H, {ind_count} Einzel-Bilanzen")

    print(f"  {len(h2h_cache)} H2H records used.")

    # ── Monte Carlo simulation ──
    # Bracket-rules-driven sim only when we actually have rules — DVV-built
    # brackets for non-8-team tournaments have empty `rules`, in which case
    # we fall back to the generic single/double-elim sim.
    use_gbt_rules = bool(gbt_bracket and gbt_bracket.get("rules"))
    if use_gbt_rules:
        src = gbt_bracket.get("meta", {}).get("source", "gbt.hanski.de")
        sim_label = f"{src} bracket rules"
    else:
        sim_label = f"{args.bracket_format} elimination (fallback)"
    print(f"Simulating {args.simulations:,} tournaments ({sim_label})…")

    total_matches: dict[str, float] = defaultdict(float)

    if use_gbt_rules:
        for _ in range(args.simulations):
            result = simulate_gbt_bracket(gbt_bracket, rankings, h2h_cache,
                                           include_qualifiers=args.include_qualifiers)
            for team, m in result.items():
                total_matches[team] += m
    else:
        simulate_fn = (simulate_bracket_double_elim
                       if args.bracket_format == "double"
                       else simulate_bracket_single_elim)
        for _ in range(args.simulations):
            result = simulate_fn(bracket_teams, rankings, h2h_cache)
            for team, m in result.items():
                total_matches[team] += m

    expected: dict[str, float] = {
        team: total_matches[team] / args.simulations
        for team in bracket_teams
    }

    print("\nExpected matches per team:")
    for team, em in sorted(expected.items(), key=lambda x: -x[1]):
        pts = lookup_team_points(team, rankings)
        pts_str = str(pts) if pts > 0 else "?"
        print(f"  {em:.2f}  {team}  (DVV pts: {pts_str})")

    # ── Map teams → player IDs ──
    # Reuse the Firestore snapshot (already cached by sync_players_available_from_brackets)
    # so the player resolution sees the same active-season pool as the price sync did.
    players = load_players(PLAYERS_ALL)
    try:
        import firestore_sync
        fs_season_for_mapping = firestore_sync.fetch_firestore_season(force=False)
    except RuntimeError:
        fs_season_for_mapping = None
    team_to_players = map_teams_to_players(bracket_teams, players, fs_season=fs_season_for_mapping)

    # ── Build per-player expected matches ──
    player_expected: dict[str, float] = {}
    for team, ids in team_to_players.items():
        em = expected.get(team, 0.0)
        for pid in ids:
            player_expected[pid] = round(em, 3)

    # ── Build output for THIS gender ──
    meta = (gbt_bracket or {}).get("meta", {}) if gbt_bracket else {}
    gender_block = {
        "bracketFormat": args.bracket_format,
        "simulations": args.simulations,
        "tournamentId": meta.get("tournamentId"),
        "tournamentName": meta.get("name"),
        "bracketStatus": meta.get("status"),
        "bracketLastUpdate": meta.get("lastUpdate"),
        "ranAt": int(time.time()),
        "includeQualifiers": args.include_qualifiers,
        "teams": [
            {
                "name": team,
                "playerIds": team_to_players.get(team, []),
                "dvvPoints": lookup_team_points(team, rankings) or None,
                "expectedMatches": round(expected.get(team, 0.0), 3),
            }
            for team in bracket_teams
        ],
        "playerExpectedMatches": player_expected,
        "bracketPrediction":     compute_bracket_prediction(gbt_bracket, rankings, h2h_cache) if gbt_bracket else [],
    }

    # ── Merge with existing file (preserve other gender) ──
    merged = {"byGender": {}, "playerExpectedMatches": {}}
    if SIM_OUTPUT.exists():
        try:
            with open(SIM_OUTPUT, encoding="utf-8") as f:
                existing = json.load(f)
            # Support both old (flat) and new (nested) formats
            if "byGender" in existing:
                merged = existing
            elif existing.get("gender"):
                # Old flat format — promote into byGender
                old_g = existing["gender"]
                merged["byGender"][old_g] = {
                    k: v for k, v in existing.items() if k != "gender"
                }
        except Exception as e:
            print(f"  WARNING: existing sim file unreadable, overwriting: {e}", file=sys.stderr)

    merged["byGender"][gender] = gender_block

    # Combined player → expected matches across both genders
    combined: dict[str, float] = {}
    for g_block in merged["byGender"].values():
        combined.update(g_block.get("playerExpectedMatches", {}))
    merged["playerExpectedMatches"] = combined
    merged["playersAvailableHash"] = players_available_hash()
    merged["lastRunAt"] = int(time.time())
    # Surface sync info to the frontend
    merged["syncInfo"] = sync_info

    with open(SIM_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"\nOutput written to {SIM_OUTPUT}")
    print(f"  Genders in file: {list(merged['byGender'].keys())}")
    print(f"  Players with expected matches: {len(combined)}")
    return gender_block


if __name__ == "__main__":
    main()
