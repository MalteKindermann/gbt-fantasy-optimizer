"""
Phased orchestrator for the ELO module.

Run order (each phase explicit, never automatically chained — that's the
politeness gate):

    python -m scripts.elo.build_ratings --phase discover  --saisons 25,26 --gender m
    python -m scripts.elo.build_ratings --phase tournaments
    python -m scripts.elo.build_ratings --phase matches --limit 20
    python -m scripts.elo.build_ratings --phase matches              # no limit
    python -m scripts.elo.build_ratings --phase teams
    python -m scripts.elo.build_ratings --phase fivb
    python -m scripts.elo.build_ratings --phase build

State persists between phases under `data_dir()`:
  - data/raw/dvv/*.html                   raw HTML cache (forever)
  - data/raw/dvv/_discovered.json         output of `discover`
  - data/raw/dvv/_match_stubs.json        output of `tournaments`
  - data/raw/dvv/_teams.json              output of `teams`
  - data/raw/fivb_archive.csv             FIVB download
  - data/matches.csv                       consolidated match list
  - data/elo_ratings.db                    final SQLite DB

The `build` phase is offline — no network — and rebuilds the DB from scratch.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir, load_dotenv_files  # noqa: E402
load_dotenv_files()

# scripts/ on sys.path -> import the elo package siblings
from elo import elo as elo_math  # noqa: E402
from elo import runner as elo_runner  # noqa: E402
from elo import scraper as sc    # noqa: E402
from elo import scraper_bvb as bvb  # noqa: E402


DATA = data_dir()
RAW  = DATA / "raw" / "dvv"
RAW.mkdir(parents=True, exist_ok=True)

DISCOVERED_JSON = RAW / "_discovered.json"
MATCHSTUB_JSON  = RAW / "_match_stubs.json"
TEAMS_JSON      = RAW / "_teams.json"
MATCHES_CSV     = DATA / "matches.csv"
DB_PATH         = DATA / "elo_ratings.db"

BVB_DIR             = DATA / "raw" / "bvb"
BVB_DISCOVERED_JSON = BVB_DIR / "_discovered.json"


# ── Normalisation helpers ─────────────────────────────────────────────────────

def normalise_name(s: str) -> str:
    """Lowercase, strip accents, collapse whitespace, drop common noise."""
    if not s:
        return ""
    s = s.strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    return " ".join(s.split())


def player_id_from_name(first: str, last: str) -> str:
    """Stable, human-readable id."""
    return f"{normalise_name(last)}_{normalise_name(first)}".strip("_")


# ── Phase: discover ───────────────────────────────────────────────────────────

def phase_discover(saisons: list[int], gender: str) -> int:
    """Merge-semantics: each call adds rows for (saison, gender). Previously
    discovered tournaments for OTHER genders/saisons are preserved."""
    existing: list[dict] = []
    if DISCOVERED_JSON.exists():
        existing = json.loads(DISCOVERED_JSON.read_text(encoding="utf-8"))
    new_rows: list[dict] = []
    for saison in saisons:
        rows = sc.fetch_tournament_list(saison)
        rows = sc.filter_german_beach_tour(rows, gender=gender)
        for r in rows:
            new_rows.append(asdict(r))
    # Dedup by tournament id
    by_id: dict[int, dict] = {r["id"]: r for r in existing}
    for r in new_rows:
        by_id[r["id"]] = r
    merged = sorted(by_id.values(), key=lambda r: r.get("date_start", ""))
    DISCOVERED_JSON.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[discover] +{len(new_rows)} new rows, "
          f"{len(merged)} total -> {DISCOVERED_JSON.name}")
    return len(merged)


def _load_discovered() -> list[dict]:
    if not DISCOVERED_JSON.exists():
        raise SystemExit("Run --phase discover first.")
    return json.loads(DISCOVERED_JSON.read_text(encoding="utf-8"))


# ── Phase: tournaments (spielplan main + qualifier) ───────────────────────────

def phase_tournaments() -> int:
    discovered = _load_discovered()
    all_stubs: list[dict] = []
    for trow in discovered:
        tid = trow["id"]
        cat  = trow.get("category", "")
        tier = sc.category_tier(cat) or "top"   # legacy rows pre-tier default to top
        for feld in (1, 2):
            stubs = sc.fetch_spielplan(tid, feld=feld)
            for s in stubs:
                d = asdict(s)
                d["saison"] = trow["saison"]
                d["gender"] = trow["gender"]
                d["tournament_name"] = trow["name"]
                d["tournament_date_start"] = trow["date_start"]
                d["tournament_date_end"]   = trow["date_end"]
                d["category"]              = cat
                d["category_tier"]         = tier
                all_stubs.append(d)
    MATCHSTUB_JSON.write_text(
        json.dumps(all_stubs, indent=2, ensure_ascii=False), encoding="utf-8")
    played = sum(1 for s in all_stubs if s.get("winner"))
    print(f"[tournaments] {len(all_stubs)} match stubs ({played} played) "
          f"-> {MATCHSTUB_JSON.name}")
    return len(all_stubs)


def _load_match_stubs() -> list[dict]:
    if not MATCHSTUB_JSON.exists():
        raise SystemExit("Run --phase tournaments first.")
    return json.loads(MATCHSTUB_JSON.read_text(encoding="utf-8"))


# ── Phase: matches (set-score details) ────────────────────────────────────────

def phase_matches(limit: Optional[int]) -> int:
    stubs = _load_match_stubs()
    played = [s for s in stubs if s.get("winner")]
    if limit is not None:
        played = played[:limit]
    fetched = 0
    skipped = 0
    for s in played:
        # Cache check: if already on disk, fetch_match_detail returns instantly.
        d = sc.fetch_match_detail(s["tournament_id"], s["match_num"],
                                  feld=s["feld"])
        if d is None:
            skipped += 1
            continue
        fetched += 1
    print(f"[matches] processed {fetched} match details, {skipped} unavailable "
          f"(limit={limit})")
    return fetched


# ── Phase: teams (name resolution) ────────────────────────────────────────────

def phase_teams() -> int:
    stubs = _load_match_stubs()
    team_ids: set[str] = set()
    for s in stubs:
        if s.get("team_a_id"):
            team_ids.add(s["team_a_id"])
        if s.get("team_b_id"):
            team_ids.add(s["team_b_id"])
    teams: dict[str, dict] = {}
    for tid in sorted(team_ids):
        info = sc.fetch_team(tid)
        if info is None:
            continue
        teams[tid] = {
            "team_id": info.team_id,
            "players": info.players,
            "club":    info.club,
        }
    TEAMS_JSON.write_text(
        json.dumps(teams, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[teams] resolved {len(teams)}/{len(team_ids)} teams "
          f"-> {TEAMS_JSON.name}")
    return len(teams)


def _load_teams() -> dict[str, dict]:
    if not TEAMS_JSON.exists():
        return {}
    return json.loads(TEAMS_JSON.read_text(encoding="utf-8"))


# ── Phase: fivb (CSV download) ────────────────────────────────────────────────

def phase_fivb() -> int:
    p = sc.ensure_fivb_csv()
    if not p:
        print("[fivb] download failed")
        return 0
    # Cheap header-row count
    n = 0
    with open(p, encoding="utf-8", errors="replace") as f:
        for _ in f:
            n += 1
    print(f"[fivb] archive size ~{n - 1:,} rows -> {p}")
    return n - 1


# ── Phase: bvb-discover (bvbinfo season indexes) ─────────────────────────────

def phase_bvb_discover(years: list[int], gender: str) -> int:
    """Merge-semantics like phase_discover — call once per (year-range, gender)."""
    BVB_DIR.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if BVB_DISCOVERED_JSON.exists():
        existing = json.loads(BVB_DISCOVERED_JSON.read_text(encoding="utf-8"))
    new_rows: list[dict] = []
    for year in years:
        refs = bvb.fetch_season(year, gender=gender)
        for r in refs:
            new_rows.append({
                "tournament_id": r.tournament_id,
                "name": r.name,
                "year": r.year,
                "gender": r.gender,
                "date_iso": r.date_iso,
                "date_range": r.date_range,
            })
    by_id: dict[int, dict] = {r["tournament_id"]: r for r in existing}
    for r in new_rows:
        by_id[r["tournament_id"]] = r
    merged = sorted(by_id.values(), key=lambda r: r.get("date_iso", "") or "9999")
    BVB_DISCOVERED_JSON.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[bvb-discover] +{len(new_rows)} new rows, "
          f"{len(merged)} total -> {BVB_DISCOVERED_JSON.relative_to(DATA)}")
    return len(merged)


def _load_bvb_discovered() -> list[dict]:
    if not BVB_DISCOVERED_JSON.exists():
        raise SystemExit("Run --phase bvb-discover first.")
    return json.loads(BVB_DISCOVERED_JSON.read_text(encoding="utf-8"))


# ── Phase: bvb-matches (per-tournament MatchResults) ──────────────────────────

def phase_bvb_matches(limit: Optional[int]) -> int:
    refs = _load_bvb_discovered()
    if limit is not None:
        refs = refs[:limit]
    fetched = 0
    for t in refs:
        ms = bvb.fetch_tournament_matches(t["tournament_id"], t["year"])
        if ms:
            fetched += 1
    print(f"[bvb-matches] fetched/cached {fetched}/{len(refs)} tournaments "
          f"(limit={limit})")
    return fetched


def _load_bvb_records(dvv_player_ids: set[str]) -> list[dict]:
    """Re-parse all cached bvb MatchResults pages into match records.

    Filtered to men's matches only. Player IDs use the same
    `player_id_from_name(first, last)` scheme as everywhere else, so
    bvb players merge with DVV by name automatically.
    """
    refs = _load_bvb_discovered() if BVB_DISCOVERED_JSON.exists() else []
    refs_by_id = {t["tournament_id"]: t for t in refs}
    out: list[dict] = []
    for ref in refs:
        ms = bvb.fetch_tournament_matches(ref["tournament_id"], ref["year"])
        for m in ms:
            if m.gender not in ("m", "f"):
                continue
            if len(m.team_w_players) < 2 or len(m.team_l_players) < 2:
                continue
            w1f, w1l = _split_fivb_name(m.team_w_players[0])
            w2f, w2l = _split_fivb_name(m.team_w_players[1])
            l1f, l1l = _split_fivb_name(m.team_l_players[0])
            l2f, l2l = _split_fivb_name(m.team_l_players[1])
            w1 = player_id_from_name(w1f, w1l)
            w2 = player_id_from_name(w2f, w2l)
            l1 = player_id_from_name(l1f, l1l)
            l2 = player_id_from_name(l2f, l2l)
            if not (w1 and w2 and l1 and l2):
                continue
            w_sets = sum(1 for a, b in m.set_scores if a > b)
            l_sets = sum(1 for a, b in m.set_scores if b > a)
            if w_sets == 0 and l_sets == 0:
                # No set scores parsed → assume 2:0 (rare path)
                w_sets, l_sets = 2, 0
            out.append({
                "date":            m.date_iso or "",
                "source":          "bvb",
                "category_tier":   "top",
                "tournament_id":   f"bvb:{ref['tournament_id']}",
                "tournament_name": ref["name"],
                "match_id":        (f"bvb_{ref['tournament_id']}_"
                                    f"{m.round_label[:6] or 'r'}_{len(out)}"),
                "round":           m.round_label,
                "round_kind":      elo_math.classify_round(m.round_label),
                "gender":          m.gender,
                "saison":          None,
                "player1a":        w1,
                "player1b":        w2,
                "player2a":        l1,
                "player2b":        l2,
                "team1_id":        elo_math.team_key(w1, w2),
                "team2_id":        elo_math.team_key(l1, l2),
                "team1_country":   m.team_w_country,
                "team2_country":   m.team_l_country,
                "winner":          1,
                "sets_won_1":      max(w_sets, 2),
                "sets_won_2":      l_sets,
                "set_scores":      m.set_scores,
            })
    print(f"  [build] bvb matches loaded: {len(out)} "
          f"(from {len(refs_by_id)} tournaments)")
    return out


def _dedup_bvb_vs_fivb(records: list[dict]) -> list[dict]:
    """
    BigTimeStats archive ends 2022-09. bvbinfo overlaps with it for 2022 and
    earlier — we keep only ONE record per (date, team1_id, team2_id, winner).
    Source-precedence: bvb > fivb (richer parsing, set scores intact).
    """
    seen: dict[tuple, dict] = {}
    for r in records:
        key = (r["date"], r["team1_id"], r["team2_id"])
        if key not in seen:
            seen[key] = r
            continue
        existing = seen[key]
        if existing["source"] == "fivb" and r["source"] == "bvb":
            seen[key] = r
    return list(seen.values())


# ── Phase: build (offline ELO + SQLite) ──────────────────────────────────────

def _split_team_display(display: str) -> list[str]:
    """'Henning - Pfretzschner (1)' -> ['Henning', 'Pfretzschner']."""
    import re
    txt = re.sub(r"\(\d+\)\s*$", "", display).strip()
    parts = [p.strip() for p in re.split(r"\s+-\s+", txt) if p.strip()]
    return parts


def _resolve_players_for_team(team_id: Optional[str], display: str,
                              teams: dict[str, dict]) -> tuple[str, str]:
    """
    Return (player1_id, player2_id) for a bracket-team entry.

    Preference order:
      1. team-page lookup (firstname + lastname) -> player_id_from_name
      2. last-name only from the spielplan display string

    Returns empty strings on failure.
    """
    if team_id and team_id in teams:
        info = teams[team_id]
        players = info.get("players") or []
        if len(players) >= 2:
            (f1, l1), (f2, l2) = players[0], players[1]
            return (player_id_from_name(f1, l1),
                    player_id_from_name(f2, l2))
    # Fallback: last-name only from the display string
    last_names = _split_team_display(display)
    if len(last_names) >= 2:
        return (player_id_from_name("", last_names[0]),
                player_id_from_name("", last_names[1]))
    if len(last_names) == 1:
        return (player_id_from_name("", last_names[0]), "")
    return ("", "")


def _parse_set_scores(point_summary: Optional[str]) -> list[tuple[int, int]]:
    """'21:18, 19:21, 15:12' -> [(21,18),(19,21),(15,12)]."""
    if not point_summary:
        return []
    import re
    out = []
    for m in re.finditer(r"(\d{1,2})\s*:\s*(\d{1,2})", point_summary):
        a, b = int(m.group(1)), int(m.group(2))
        out.append((a, b))
    return out


def _build_match_records() -> list[dict]:
    """Merge stubs + match-detail caches into a chronological match list."""
    stubs = _load_match_stubs()
    teams = _load_teams()
    out: list[dict] = []
    for s in stubs:
        if not s.get("winner"):
            continue
        # Set scores: the Spielplan's point_summary is the authoritative source
        # (e.g. "21:15, 22:20"). The tur-spiel.php detail page parser is too
        # ambiguous (picks up scores from unrelated parts of the page) so we
        # only fall back to it when the spielplan has no summary at all.
        set_scores: list[tuple[int, int]] = _parse_set_scores(
            s.get("point_summary"))

        p1a, p1b = _resolve_players_for_team(
            s.get("team_a_id"), s.get("team_a_display", ""), teams)
        p2a, p2b = _resolve_players_for_team(
            s.get("team_b_id"), s.get("team_b_display", ""), teams)
        if not (p1a and p1b and p2a and p2b):
            continue   # unresolved roster, skip

        sets_won_1, sets_won_2 = (0, 0)
        sm = s.get("set_summary") or ""
        try:
            a_s, b_s = sm.split(":")
            sets_won_1, sets_won_2 = int(a_s), int(b_s)
        except (ValueError, AttributeError):
            pass

        winner_int = 1 if s["winner"] == "A" else 2
        out.append({
            "date":          s.get("tournament_date_end") or s.get("date") or "",
            "source":        "dvv",
            "category_tier": s.get("category_tier") or "top",
            "tournament_id": str(s["tournament_id"]),
            "tournament_name": s.get("tournament_name", ""),
            "match_id":      f"f{s['feld']}_s{s['match_num']}",
            "round":         s.get("round_label", ""),
            "round_kind":    elo_math.classify_round(s.get("round_label", "")),
            "gender":        s.get("gender", "?"),
            "saison":        s.get("saison"),
            "player1a":      p1a,
            "player1b":      p1b,
            "player2a":      p2a,
            "player2b":      p2b,
            "team1_id":      elo_math.team_key(p1a, p1b),
            "team2_id":      elo_math.team_key(p2a, p2b),
            "team1_country": "Germany",
            "team2_country": "Germany",
            "winner":        winner_int,
            "sets_won_1":    sets_won_1,
            "sets_won_2":    sets_won_2,
            "set_scores":    set_scores,
        })
    return out


def _split_fivb_name(full: str) -> tuple[str, str]:
    """'Kevin Wong' -> ('Kevin', 'Wong').  Multi-word last names go to last."""
    full = (full or "").strip()
    if not full:
        return ("", "")
    parts = full.split()
    if len(parts) == 1:
        return ("", parts[0])
    return (parts[0], " ".join(parts[1:]))


def _parse_fivb_score(score: str) -> tuple[int, int, list[tuple[int, int]]]:
    """'21-18, 21-12' -> (sets_won_winner, sets_won_loser, [(21,18),(21,12)])"""
    sets: list[tuple[int, int]] = []
    if not score:
        return (0, 0, sets)
    import re
    for m in re.finditer(r"(\d{1,2})\s*[-:]\s*(\d{1,2})", score):
        a, b = int(m.group(1)), int(m.group(2))
        sets.append((a, b))
    w_sets = sum(1 for a, b in sets if a > b)
    l_sets = sum(1 for a, b in sets if b > a)
    return (w_sets, l_sets, sets)


def _build_fivb_records(dvv_player_ids: set[str]) -> list[dict]:
    p = sc.FIVB_CSV_PATH
    if not p.exists():
        return []
    out: list[dict] = []
    total_rows = 0
    for row in sc.iter_fivb_rows(p):
        total_rows += 1
        # BigTimeStats schema: lower-case prefixed columns
        date = (row.get("date") or "").strip()
        gender_raw = (row.get("gender") or "").strip().upper()
        # M = men; W = women in this dataset
        gender = "m" if gender_raw == "M" else "f" if gender_raw == "W" else "?"
        if gender not in ("m", "f"):
            continue
        round_lbl = row.get("round") or ""
        w1f, w1l = _split_fivb_name(row.get("w_player1") or "")
        w2f, w2l = _split_fivb_name(row.get("w_player2") or "")
        l1f, l1l = _split_fivb_name(row.get("l_player1") or "")
        l2f, l2l = _split_fivb_name(row.get("l_player2") or "")
        if not (w1l and w2l and l1l and l2l):
            continue
        w1 = player_id_from_name(w1f, w1l)
        w2 = player_id_from_name(w2f, w2l)
        l1 = player_id_from_name(l1f, l1l)
        l2 = player_id_from_name(l2f, l2l)
        w_sets, l_sets, set_scores = _parse_fivb_score(row.get("score") or "")
        out.append({
            "date":          date,
            "source":        "fivb",
            "category_tier": "top",
            "tournament_id": (row.get("circuit") or "fivb") + ":" + (
                row.get("tournament") or "?") + ":" + (row.get("year") or ""),
            "tournament_name": row.get("tournament") or "",
            "match_id":      (f"m{row.get('match_num') or 'x'}"
                              f"_{(row.get('bracket') or 'b')[:1]}"
                              f"_{(row.get('round') or 'r')[:6]}"
                              f"_{len(out)}"),
            "round":         round_lbl,
            "round_kind":    elo_math.classify_round(round_lbl),
            "gender":        gender,
            "saison":        None,
            "player1a":      w1,
            "player1b":      w2,
            "player2a":      l1,
            "player2b":      l2,
            "team1_id":      elo_math.team_key(w1, w2),
            "team2_id":      elo_math.team_key(l1, l2),
            "team1_country": (row.get("w_p1_country") or "").strip(),
            "team2_country": (row.get("l_p1_country") or "").strip(),
            "winner":        1,
            "sets_won_1":    max(w_sets, 2),
            "sets_won_2":    l_sets,
            "set_scores":    set_scores,
        })
    print(f"  [build] FIVB rows scanned: {total_rows}, matches kept: {len(out)}")
    return out


def _write_matches_csv(records: list[dict]) -> None:
    with open(MATCHES_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "source", "tournament_id", "match_id", "round",
                    "round_kind", "gender", "player1a", "player1b",
                    "player2a", "player2b", "team1_id", "team2_id",
                    "sets_won_1", "sets_won_2", "set_scores", "winner"])
        for r in records:
            w.writerow([
                r["date"], r["source"], r["tournament_id"], r["match_id"],
                r["round"], r["round_kind"], r["gender"],
                r["player1a"], r["player1b"], r["player2a"], r["player2b"],
                r["team1_id"], r["team2_id"],
                r["sets_won_1"], r["sets_won_2"],
                json.dumps(r["set_scores"]),
                r["winner"],
            ])


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    DROP TABLE IF EXISTS matches;
    DROP TABLE IF EXISTS elo_history;
    DROP TABLE IF EXISTS current_ratings;

    CREATE TABLE matches (
        source        TEXT NOT NULL,
        tournament_id TEXT NOT NULL,
        match_id      TEXT NOT NULL,
        date          TEXT,
        round_label   TEXT,
        round_kind    TEXT,
        gender        TEXT,
        player1a      TEXT, player1b TEXT,
        player2a      TEXT, player2b TEXT,
        team1_id      TEXT, team2_id TEXT,
        sets_won_1    INTEGER, sets_won_2 INTEGER,
        set_scores    TEXT,
        winner        INTEGER,
        predicted_p1  REAL,
        PRIMARY KEY (source, tournament_id, match_id)
    );

    CREATE TABLE elo_history (
        entity_id    TEXT NOT NULL,
        entity_kind  TEXT NOT NULL,   -- 'individual' | 'team'
        date         TEXT,
        tournament_id TEXT,
        match_id     TEXT,
        elo_before   REAL,
        elo_after    REAL,
        delta        REAL
    );
    CREATE INDEX idx_elo_history_entity_date ON elo_history(entity_id, date);

    CREATE TABLE current_ratings (
        entity_id      TEXT NOT NULL,
        entity_kind    TEXT NOT NULL,   -- 'individual' | 'team'
        elo            REAL,
        matches_played INTEGER,
        last_active    TEXT,
        PRIMARY KEY (entity_id, entity_kind)
    );
    """)


def _consolidate_records() -> list[dict]:
    """Build the chronologically-sorted, deduped master match list.

    Pure function: doesn't touch the network (relies on already-cached
    raw HTML) and doesn't write anywhere. Used by phase_build and by the
    in-memory tuning recompute path.
    """
    dvv = _build_match_records()
    dvv_pids: set[str] = set()
    for m in dvv:
        dvv_pids.update([m["player1a"], m["player1b"], m["player2a"], m["player2b"]])
    fivb = _build_fivb_records(dvv_pids)
    bvb_records = (_load_bvb_records(dvv_pids)
                   if BVB_DISCOVERED_JSON.exists() else [])
    all_records = _dedup_bvb_vs_fivb(dvv + fivb + bvb_records)
    # Apply user-editable name aliases (DVV ↔ FIVB ↔ bvb variant spellings).
    try:
        from elo import aliases as elo_aliases
        amap = elo_aliases.load_alias_map()
        if amap:
            n = elo_aliases.apply_aliases(all_records, amap)
            print(f"  [build] aliasing: remapped {n} player slots "
                  f"({len(amap)} aliases)")
    except Exception as e:
        print(f"  [build] aliasing skipped: {e}")
    all_records.sort(key=lambda r: (r["date"] or "0000", r["source"],
                                    r["tournament_id"], r["match_id"]))
    return all_records


# In-process cache so the tuning API doesn't re-parse 115k rows per click.
_cached_records: Optional[list[dict]] = None


def get_consolidated_records(force_reload: bool = False) -> list[dict]:
    global _cached_records
    if _cached_records is None or force_reload:
        _cached_records = _consolidate_records()
    return _cached_records


def _calib_error(buckets: dict[int, list[int]]) -> float:
    """Weighted mean abs deviation between predicted-mid and actual win-rate."""
    total = sum(len(b) for b in buckets.values())
    if not total:
        return float("nan")
    err = 0.0
    for k, results in buckets.items():
        if not results:
            continue
        predicted = (k / 10) + 0.05
        actual = sum(results) / len(results)
        err += len(results) * abs(predicted - actual)
    return err / total


def _build_one_model(model_id: str, all_records: list[dict],
                     train_end_date: Optional[str],
                     persist_db: bool = False) -> dict:
    """Build a single model end-to-end. Returns a meta dict for elo_models_meta.json.

    When `persist_db` is True (only used for the ELO baseline so we don't
    quadruple the DB size), writes match predictions + elo_history into the
    SQLite DB.  All three models always write their own *_current.json.
    """
    from elo import models as elo_models
    from elo import priors as elo_priors
    model = elo_models.make_model(model_id)
    try:
        priors = elo_priors.build_for_model(model_id)
        if priors:
            model.set_priors(priors)
            print(f"[build:{model_id}] applied {len(priors)} DVV cold-start priors")
    except Exception as e:
        print(f"[build:{model_id}] priors unavailable: {e}")

    print(f"\n[build:{model_id}] running ELO loop ...")
    t0 = time.time()
    run = elo_runner.run_model(all_records, model,
                               train_end_date=train_end_date,
                               collect_history=persist_db)
    elapsed = __import__('time').time() - t0
    print(f"[build:{model_id}] loop done in {elapsed:.1f}s")

    # Top-5 sanity print
    players_export = elo_runner.build_player_export(run)
    print(f"[build:{model_id}] top 5 (>=5 matches):")
    top = [p for p in players_export if p["matches"] >= 5][:5]
    for i, p in enumerate(top, 1):
        print(f"   {i}. {p['name']:<28}  {p['elo_combined']:>6.0f}  "
              f"({p['matches']}M, {p['country']})")

    in_acc  = (run.in_sample_correct / run.in_sample_total
               if run.in_sample_total else None)
    oos_acc = (run.oos_correct / run.oos_total
               if run.oos_total else None)
    in_calib = _calib_error(run.in_sample_calib)
    oos_calib = _calib_error(run.oos_calib)
    print(f"[build:{model_id}] in-sample (DVV 25+): "
          f"{in_acc:.1%}  calib_err={in_calib:.3f}  (n={run.in_sample_total})"
          if in_acc is not None else f"[build:{model_id}] no in-sample data")
    if run.train_end_date:
        print(f"[build:{model_id}] OOS (after {run.train_end_date}): "
              f"{oos_acc:.1%}  calib_err={oos_calib:.3f}  (n={run.oos_total})"
              if oos_acc is not None else f"[build:{model_id}] no OOS data")

    # Write the model-specific JSON (skip on OOS to avoid stale rankings)
    if not train_end_date:
        json_path = DATA / f"{model_id}_current.json"
        json_path.write_text(json.dumps({
            "generated_at": "",
            "model": model_id,
            "players": players_export,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[build:{model_id}] JSON -> {json_path.name} "
              f"({len(players_export)} players)")

    # Optionally persist to the central SQLite DB (only for one model — the
    # match-level predictions are model-specific so we pick ELO as canonical).
    if persist_db:
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        _ensure_schema(conn)
        cur = conn.cursor()
        for r, pred in zip(all_records, run.match_predictions):
            cur.execute("""
            INSERT INTO matches (source, tournament_id, match_id, date, round_label,
                round_kind, gender, player1a, player1b, player2a, player2b,
                team1_id, team2_id, sets_won_1, sets_won_2, set_scores, winner,
                predicted_p1)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (r["source"], r["tournament_id"], r["match_id"], r["date"],
                  r["round"], r["round_kind"], r["gender"],
                  r["player1a"], r["player1b"], r["player2a"], r["player2b"],
                  r["team1_id"], r["team2_id"],
                  r["sets_won_1"], r["sets_won_2"],
                  json.dumps(r["set_scores"]), r["winner"], pred))
        cur.executemany("""
            INSERT INTO elo_history (entity_id, entity_kind, date, tournament_id,
                match_id, elo_before, elo_after, delta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, run.history_rows)
        for pid in model.state_indiv:
            cur.execute("""
            INSERT OR REPLACE INTO current_ratings
                (entity_id, entity_kind, elo, matches_played, last_active)
            VALUES (?, 'individual', ?, ?, ?);
            """, (pid, model.display_indiv(pid),
                  model.n_played_ind.get(pid, 0),
                  run.last_active_ind.get(pid, "")))
        for tid in model.state_team:
            cur.execute("""
            INSERT OR REPLACE INTO current_ratings
                (entity_id, entity_kind, elo, matches_played, last_active)
            VALUES (?, 'team', ?, ?, ?);
            """, (tid, model.display_team(tid),
                  model.n_played_team.get(tid, 0),
                  run.last_active_team.get(tid, "")))
        conn.commit(); conn.close()

    return {
        "id": model_id,
        "in_sample_acc":    in_acc,
        "in_sample_calib":  in_calib,
        "in_sample_n":      run.in_sample_total,
        "oos_acc":          oos_acc,
        "oos_calib":        oos_calib,
        "oos_n":            run.oos_total,
        "n_players":        len(players_export),
        "elapsed_s":        round(elapsed, 1),
    }


def phase_build(train_end_date: Optional[str] = None,
                models: Optional[list[str]] = None,
                oos_cutoff: str = "2024-12-31") -> None:
    """Build all three models end-to-end.

    When `train_end_date` is None (the normal case) we ALWAYS also run a
    second pass at `oos_cutoff` to populate the OOS metrics — those numbers
    drive the model-comparison line in the UI and `elo_models_meta.json`.
    The OOS pass does NOT overwrite the per-model *_current.json so the
    rankings stay computed from ALL data.
    """
    print("[build] consolidating match records ...")
    all_records = get_consolidated_records(force_reload=True)
    _write_matches_csv(all_records)
    src_counts: dict[str, int] = {}
    for r in all_records:
        src_counts[r["source"]] = src_counts.get(r["source"], 0) + 1
    print(f"[build] sources={src_counts} -> {len(all_records)} total matches "
          f"-> {MATCHES_CSV.name}")

    model_ids = models or ["elo", "glicko2", "trueskill", "ensemble"]

    # ── Pass 1: full training (writes the *_current.json files) ──
    if train_end_date:
        print(f"[build] OUT-OF-SAMPLE only: train cutoff = {train_end_date}")
        metas = [_build_one_model(mid, all_records,
                                  train_end_date=train_end_date,
                                  persist_db=(i == 0 and mid == "elo"))
                 for i, mid in enumerate(model_ids)]
    else:
        print(f"[build] PASS 1/2: full training (writes *_current.json)")
        metas_full = [_build_one_model(mid, all_records,
                                       train_end_date=None,
                                       persist_db=(i == 0 and mid == "elo"))
                      for i, mid in enumerate(model_ids)]
        # ── Pass 2: OOS evaluation (no JSON writes) for meta numbers ──
        print(f"\n[build] PASS 2/2: OOS evaluation at cutoff {oos_cutoff}")
        metas_oos = [_build_one_model(mid, all_records,
                                      train_end_date=oos_cutoff,
                                      persist_db=False)
                     for mid in model_ids]
        # Merge: keep full-training in-sample stats + player counts,
        # overlay OOS stats from the held-out pass
        metas = []
        for full, oos in zip(metas_full, metas_oos):
            merged = dict(full)
            merged["oos_acc"]   = oos["oos_acc"]
            merged["oos_calib"] = oos["oos_calib"]
            merged["oos_n"]     = oos["oos_n"]
            metas.append(merged)

    # Comparison table
    print(f"\n=== Model comparison ===")
    print(f"  {'Model':<12}  {'in-sample':>10}  {'calib':>7}  "
          f"{'OOS':>8}  {'calib':>7}  {'players':>8}")
    for m in metas:
        in_acc = f"{m['in_sample_acc']:.1%}" if m['in_sample_acc'] is not None else "  -  "
        oos_acc = f"{m['oos_acc']:.1%}" if m.get('oos_acc') is not None else "  -  "
        oos_cal = (f"{m['oos_calib']:.3f}" if m.get('oos_calib')
                   and m['oos_calib'] == m['oos_calib'] else "  -  ")
        print(f"  {m['id']:<12}  {in_acc:>10}  {m['in_sample_calib']:>7.3f}  "
              f"{oos_acc:>8}  {oos_cal:>7}  {m['n_players']:>8}")

    # Write comparison meta — always, on both training modes
    from datetime import date as _dt
    meta_path = DATA / "elo_models_meta.json"
    meta_path.write_text(json.dumps({
        "generated_at": _dt.today().isoformat(),
        "train_end_date": train_end_date,
        "oos_cutoff": oos_cutoff if not train_end_date else train_end_date,
        "models": metas,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[build] meta -> {meta_path.name}")

    print(f"[build] {sc.STATS.summary()}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase",
                    required=True,
                    choices=["discover", "tournaments", "matches", "teams",
                             "fivb", "bvb-discover", "bvb-matches", "build"])
    ap.add_argument("--saisons", default="25,26",
                    help="comma-separated two-digit years for DVV (default 25,26)")
    ap.add_argument("--years",   default="2022,2023,2024,2025,2026",
                    help="comma-separated four-digit years for bvbinfo "
                         "(default 2022..2026)")
    ap.add_argument("--gender", default="m", choices=["m", "f"])
    ap.add_argument("--limit", type=int, default=None,
                    help="cap for --phase matches / bvb-matches (default: unlimited)")
    ap.add_argument("--train-end-date", default=None,
                    help="ISO date (YYYY-MM-DD) — for --phase build, freezes ratings "
                         "at this date and reports out-of-sample accuracy on later matches")
    args = ap.parse_args()

    saisons = [int(x.strip()) for x in args.saisons.split(",") if x.strip()]
    years   = [int(x.strip()) for x in args.years.split(",") if x.strip()]

    if args.phase == "discover":
        phase_discover(saisons, args.gender)
    elif args.phase == "tournaments":
        phase_tournaments()
    elif args.phase == "matches":
        # Safety: explicit warning when running unlimited the first time
        if args.limit is None:
            print("[matches] --limit not set: will fetch ALL played matches "
                  "(cached on disk; only new ones go over the network).")
        phase_matches(args.limit)
    elif args.phase == "teams":
        phase_teams()
    elif args.phase == "fivb":
        phase_fivb()
    elif args.phase == "bvb-discover":
        phase_bvb_discover(years, args.gender)
    elif args.phase == "bvb-matches":
        if args.limit is None:
            print("[bvb-matches] --limit not set: will fetch ALL discovered "
                  "tournaments (cached on disk; only new ones go over the network).")
        phase_bvb_matches(args.limit)
    elif args.phase == "build":
        phase_build(train_end_date=args.train_end_date)
    print(f"[{args.phase}] {sc.STATS.summary()}")


if __name__ == "__main__":
    main()
