"""
DVV + FIVB scraper for the ELO module.

Two strict politeness invariants:

  1. **Raw HTML cache** under `data/raw/dvv/`. Every (endpoint, params)
     combination is fetched at most once across all runs. Re-runs of the same
     phase touch the network zero times.

  2. **Token-bucket throttle** in `_throttle()` — global, process-wide. Default
     0.75 s ± 0.25 s jitter between live requests, override via env var
     `ELO_SCRAPE_DELAY=<float seconds>`.

The phased CLI in `build_ratings.py` is the only intended caller — it gates
how many requests can happen in one run.

This module is intentionally NOT importing from `dvv_tournament.py` so it
keeps its own cache and can be developed / blown away independently.
"""
from __future__ import annotations

import csv
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir, load_dotenv_files  # noqa: E402
load_dotenv_files()


# ── Paths ─────────────────────────────────────────────────────────────────────

RAW_DIR = data_dir() / "raw" / "dvv"
RAW_DIR.mkdir(parents=True, exist_ok=True)

FIVB_CSV_PATH = data_dir() / "raw" / "fivb_archive.csv"
FIVB_CSV_URL  = ("https://raw.githubusercontent.com/BigTimeStats/beach-volleyball"
                 "/master/data/full_archive/full_archive.csv")

DVV_BASE = "https://beach.volleyball-verband.de/public"
DVV_TUR     = DVV_BASE + "/tur.php"
DVV_TUR_SP  = DVV_BASE + "/tur-sp.php"
DVV_TUR_SPL = DVV_BASE + "/tur-spiel.php"
DVV_TEAM    = DVV_BASE + "/team.php"

CATEGORY = "Deutsche Beach-Volleyball Tour\\German Beach Tour"

# ── Category tier mapping ────────────────────────────────────────────────────
#
# DVV exposes several tournament categories. We classify them into tiers so the
# ELO loop can apply different K-factor weights per tier (configurable via the
# tuning sliders). Categories not in the map are silently skipped (e.g. Junior,
# Senior, regional A-Cup which would pollute pro ratings).
#
#   tier_top     = Pro-level main tour (current + historic names). Always 1.0.
#                  Includes DM (Deutsche Meisterschaften) per user request.
#   tier_challenger = Secondary national tour formats (ROCK the BEACH, smart
#                  beach cup, Urlaubsguru, King of the Court, etc.). Tunable.
#   tier_qualifier = Qualifier-only events (lower skill ceiling). Tunable.
#
CATEGORY_TIERS: dict[str, str] = {
    # ── Tier-1: Pro-Top (always tracked, weight 1.0) ──
    "Deutsche Beach-Volleyball Tour\\German Beach Tour":  "top",
    "Techniker Beach Tour":                               "top",
    "smart super cup":                                    "top",
    "Deutsche Beach-Volleyball Meisterschaften":          "top",
    "German Beach Tour\\King of the Court":               "top",
    # ── Tier-2: Challenger / secondary national format ──
    "Deutsche Beach-Volleyball Tour\\ROCK the BEACH":     "challenger",
    "smart beach cup":                                    "challenger",
    "Deutsche Beach-Volleyball Tour\\2. Deutsche Beach Tour": "challenger",
    "Deutsche Beach-Volleyball Tour\\DBT2 - Ersatzturniere":  "challenger",
    "Deutsche Beach-Volleyball Tour\\Urlaubsguru Beach Cup":  "challenger",
    # ── Tier-3: Qualifier-only ──
    "Qualifier Timmendorfer Strand":                      "qualifier",
    "Road To Timmendorf":                                 "qualifier",
    "Top":                                                "qualifier",
}


def category_tier(category: str) -> str | None:
    """Returns 'top' / 'challenger' / 'qualifier' or None if untracked."""
    return CATEGORY_TIERS.get(category)


# ── Politeness ────────────────────────────────────────────────────────────────

@dataclass
class ScrapeStats:
    """Live counter, printed at the end of each run."""
    http_requests: int = 0
    cache_hits: int = 0
    errors: int = 0
    request_log: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"HTTP requests: {self.http_requests}   "
                f"cache hits: {self.cache_hits}   "
                f"errors: {self.errors}")


STATS = ScrapeStats()


def _scrape_delay() -> float:
    """Base delay between live requests. Read env each call so tweaks
    take effect without restart."""
    try:
        return float(os.environ.get("ELO_SCRAPE_DELAY", "0.75"))
    except ValueError:
        return 0.75


_last_request_at: float = 0.0


def _throttle() -> None:
    """Sleep until ≥ delay seconds have passed since the last live request.
    Adds ±0.25 s jitter to avoid lockstep request patterns."""
    global _last_request_at
    base = _scrape_delay()
    jitter = random.uniform(-0.25, 0.25) if base > 0.3 else 0.0
    wait_for = max(0.0, base + jitter)
    elapsed = time.time() - _last_request_at
    if elapsed < wait_for:
        time.sleep(wait_for - elapsed)
    _last_request_at = time.time()


# ── Cache key safety ──────────────────────────────────────────────────────────

def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def _raw_path(name: str) -> Path:
    return RAW_DIR / f"{_safe_name(name)}.html"


# ── HTTP layer ────────────────────────────────────────────────────────────────

USER_AGENT = "gbt-fantasy-elo/0.1 (research; mailto:malte.kindermann@gmx.de)"


def _fetch_html(url: str, params: Optional[dict], cache_name: str,
                force: bool = False) -> Optional[str]:
    """
    Fetch a DVV page with disk-cache + throttle.

    Returns the HTML text, or `None` on hard failure (after one retry on 5xx).
    """
    p = _raw_path(cache_name)
    if p.exists() and not force:
        STATS.cache_hits += 1
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass  # corrupt cache → re-fetch

    _throttle()
    STATS.request_log.append(f"{cache_name}  ←  {url} {params or ''}")

    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=20,
                             headers={"User-Agent": USER_AGENT})
            STATS.http_requests += 1
            if r.status_code >= 500 and attempt == 1:
                # transient, back off and retry once
                time.sleep(5.0)
                continue
            if not r.ok:
                # 4xx → don't retry, log and bail
                STATS.errors += 1
                print(f"  [scraper] {r.status_code} on {cache_name}",
                      file=sys.stderr)
                return None
            try:
                p.write_text(r.text, encoding="utf-8")
            except OSError as e:
                print(f"  [scraper] cache write failed for {cache_name}: {e}",
                      file=sys.stderr)
            return r.text
        except requests.RequestException as e:
            STATS.errors += 1
            if attempt == 1:
                print(f"  [scraper] retrying {cache_name} after error: {e}",
                      file=sys.stderr)
                time.sleep(5.0)
                continue
            print(f"  [scraper] giving up on {cache_name}: {e}", file=sys.stderr)
            return None
    return None


# ── Parsers: tournament list (tur.php?saison=NN) ──────────────────────────────

@dataclass
class TournamentRow:
    id: int
    name: str
    category: str
    gender: str             # 'm' | 'f' | '?'
    date_start: str         # ISO yyyy-mm-dd
    date_end: str           # ISO yyyy-mm-dd
    saison: int             # two-digit year (25, 26, …)


_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _parse_date_range(s: str) -> Optional[tuple[str, str]]:
    s = s.strip()
    m = re.match(
        r"\s*(\d{1,2})\.(\d{1,2})\.\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$", s)
    if m:
        d1, m1, d2, m2, y = m.groups()
        return (f"{y}-{int(m1):02d}-{int(d1):02d}",
                f"{y}-{int(m2):02d}-{int(d2):02d}")
    hits = list(_DATE_RE.finditer(s))
    if len(hits) >= 2:
        d1, m1, y1 = hits[0].groups()
        d2, m2, y2 = hits[1].groups()
        return (f"{y1}-{int(m1):02d}-{int(d1):02d}",
                f"{y2}-{int(m2):02d}-{int(d2):02d}")
    if len(hits) == 1:
        d, mo, y = hits[0].groups()
        iso = f"{y}-{int(mo):02d}-{int(d):02d}"
        return (iso, iso)
    return None


def fetch_tournament_list(saison: int, force: bool = False) -> list[TournamentRow]:
    """tur.php?saison=NN → list of all tournaments in that season."""
    name = f"tur_saison_{saison:02d}"
    html = _fetch_html(DVV_TUR, params={"saison": saison},
                       cache_name=name, force=force)
    if not html:
        return []
    return parse_tour_list(html, saison)


def parse_tour_list(html: str, saison: int) -> list[TournamentRow]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[TournamentRow] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        rng = _parse_date_range(date_text)
        if not rng:
            continue
        category = cells[1].get_text(" ", strip=True)
        loc_a = cells[2].find("a", href=True)
        if not loc_a:
            continue
        m = re.search(r"id=(\d+)", loc_a["href"])
        if not m:
            continue
        tid = int(m.group(1))
        name = loc_a.get_text(" ", strip=True)
        gender_text = cells[3].get_text(" ", strip=True)
        gender = ("m" if "Männer" in gender_text or "Herren" in gender_text
                  else "f" if "Frauen" in gender_text or "Damen" in gender_text
                  else "?")
        out.append(TournamentRow(
            id=tid, name=name, category=category, gender=gender,
            date_start=rng[0], date_end=rng[1], saison=saison,
        ))
    return out


def filter_german_beach_tour(rows: list[TournamentRow], gender: str = "m"
                             ) -> list[TournamentRow]:
    """Filter to tournaments whose category is in CATEGORY_TIERS (any tier).

    Each returned row keeps its original `category` string; the tier mapping
    happens downstream when records are built. The function name is retained
    for backward-compat; semantically this is now "filter tracked categories".
    """
    return [r for r in rows
            if r.category in CATEGORY_TIERS and r.gender == gender]


# ── Parsers: spielplan (tur-sp.php) ───────────────────────────────────────────

@dataclass
class MatchStub:
    """One row from the Spielplan, before details are fetched."""
    tournament_id: int
    feld: int                # 1 = Hauptfeld, 2 = Qualifikation
    match_num: int
    round_label: str
    date: str
    time: str
    team_a_id: Optional[str]
    team_b_id: Optional[str]
    team_a_display: str
    team_b_display: str
    set_summary: Optional[str]  # "2:0", "2:1" or None if not played
    point_summary: Optional[str]  # e.g. "21:18, 21:15"
    winner: Optional[str]        # 'A' | 'B' | None


def _extract_team_id(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    m = re.search(r"team\.php\?id=(\d+)", href)
    return m.group(1) if m else None


def fetch_spielplan(tournament_id: int, feld: int = 1,
                    force: bool = False) -> list[MatchStub]:
    name = f"tur-sp_{tournament_id}_f{feld}"
    params = {"id": tournament_id}
    if feld == 2:
        params["feld"] = 2
    html = _fetch_html(DVV_TUR_SP, params=params, cache_name=name, force=force)
    if not html:
        return []
    return parse_spielplan(html, tournament_id, feld)


def parse_spielplan(html: str, tournament_id: int, feld: int) -> list[MatchStub]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[MatchStub] = []
    for header in soup.find_all("div", class_="sectionheader"):
        round_name = header.get_text(" ", strip=True)
        table = header.find_next("table")
        if not table:
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 7:
                continue
            num_text = cells[0].get_text(" ", strip=True)
            if not num_text.isdigit():
                continue
            tag_text  = cells[1].get_text(" ", strip=True)
            time_text = cells[2].get_text(" ", strip=True)

            team_a_a = cells[4].find("a", href=True)
            team_b_a = cells[6].find("a", href=True) if len(cells) > 6 else None
            display_a = (team_a_a.get_text(" ", strip=True) if team_a_a
                         else cells[4].get_text(" ", strip=True))
            display_b = (team_b_a.get_text(" ", strip=True) if team_b_a
                         else cells[6].get_text(" ", strip=True)
                              if len(cells) > 6 else "")
            tid_a = _extract_team_id(team_a_a.get("href") if team_a_a else None)
            tid_b = _extract_team_id(team_b_a.get("href") if team_b_a else None)

            set_sum = None
            pt_sum = None
            winner = None
            if len(cells) > 8:
                score_text = cells[8].get_text(" ", strip=True)
                m = re.match(r"(\d+)\s*:\s*(\d+)\s*(?:\(([^)]*)\))?",
                             score_text)
                if m:
                    a_s, b_s = int(m.group(1)), int(m.group(2))
                    if not (a_s == 0 and b_s == 0):
                        set_sum = f"{a_s}:{b_s}"
                        pt_sum = (m.group(3) or "").strip() or None
                        winner = ("A" if a_s > b_s
                                  else "B" if b_s > a_s else None)

            out.append(MatchStub(
                tournament_id=tournament_id, feld=feld,
                match_num=int(num_text), round_label=round_name,
                date=tag_text, time=time_text,
                team_a_id=tid_a, team_b_id=tid_b,
                team_a_display=display_a, team_b_display=display_b,
                set_summary=set_sum, point_summary=pt_sum, winner=winner,
            ))
    return out


# ── Parsers: match detail (tur-spiel.php?id=X&feld=Y&spiel=N) ────────────────

@dataclass
class MatchDetail:
    tournament_id: int
    feld: int
    match_num: int
    set_scores: list[tuple[int, int]]    # [(21,18), (19,21), (15,12)]
    date: Optional[str]
    team_a_display: Optional[str]
    team_b_display: Optional[str]


def fetch_match_detail(tournament_id: int, match_num: int, feld: int = 1,
                       force: bool = False) -> Optional[MatchDetail]:
    name = f"tur-spiel_{tournament_id}_f{feld}_s{match_num}"
    params = {"id": tournament_id, "feld": feld, "spiel": match_num}
    html = _fetch_html(DVV_TUR_SPL, params=params, cache_name=name, force=force)
    if not html:
        return None
    return parse_match_detail(html, tournament_id, match_num, feld)


_SET_PAIR_RE = re.compile(r"(\d{1,2})\s*[:\-]\s*(\d{1,2})")


def parse_match_detail(html: str, tournament_id: int, match_num: int,
                       feld: int) -> Optional[MatchDetail]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Date: look for first dd.mm.yyyy occurrence
    iso_date: Optional[str] = None
    m = _DATE_RE.search(text)
    if m:
        d, mo, y = m.groups()
        iso_date = f"{y}-{int(mo):02d}-{int(d):02d}"

    # Set scores: scan for 21:NN / 19:NN / 15:NN patterns in plausible windows.
    # Heuristic — keep pairs where at least one side is a "set-winning" score
    # (≥ 15) and the loser is between 0 and the winner.
    sets: list[tuple[int, int]] = []
    for sm in _SET_PAIR_RE.finditer(text):
        a, b = int(sm.group(1)), int(sm.group(2))
        hi, lo = max(a, b), min(a, b)
        # Beach set ends 21 (with ≥2 lead) or 15 in deciding set; cap at 35.
        if 15 <= hi <= 35 and 0 <= lo < hi:
            sets.append((a, b))
        if len(sets) >= 3:
            break

    if not sets:
        return None

    # Team displays — try to read the two team links if present
    team_a_d = team_b_d = None
    team_links = soup.find_all("a", href=re.compile(r"team\.php\?id=\d+"))
    if len(team_links) >= 2:
        team_a_d = team_links[0].get_text(" ", strip=True)
        team_b_d = team_links[1].get_text(" ", strip=True)

    return MatchDetail(
        tournament_id=tournament_id, feld=feld, match_num=match_num,
        set_scores=sets, date=iso_date,
        team_a_display=team_a_d, team_b_display=team_b_d,
    )


# ── Parsers: team page (team.php?id=N) — name resolution ──────────────────────

@dataclass
class TeamInfo:
    team_id: str
    players: list[tuple[str, str]]   # [(firstname, lastname), …]
    club: Optional[str]


def fetch_team(team_id: str, force: bool = False) -> Optional[TeamInfo]:
    name = f"team_{team_id}"
    html = _fetch_html(DVV_TEAM, params={"id": team_id},
                       cache_name=name, force=force)
    if not html:
        return None
    return parse_team(html, team_id)


def parse_team(html: str, team_id: str) -> TeamInfo:
    soup = BeautifulSoup(html, "html.parser")
    players: list[tuple[str, str]] = []
    # The team page lists players as "Vorname Nachname (DVV-Punkte)" with
    # links to /public/spieler.php?id=N. Pull each link's anchor text.
    for a in soup.find_all("a", href=re.compile(r"spieler\.php\?id=\d+")):
        txt = a.get_text(" ", strip=True)
        if not txt:
            continue
        # Strip trailing parentheses (points)
        txt = re.sub(r"\s*\(.*?\)\s*$", "", txt).strip()
        # DVV team pages use "Lastname, Firstname" (comma-separated).
        # Fallback to space-split for pages that drop the comma.
        if "," in txt:
            last, _, first = txt.partition(",")
            players.append((first.strip(), last.strip()))
        else:
            parts = txt.split()
            if len(parts) >= 2:
                # No comma → assume "Firstname Lastname" (rare on team.php).
                first = parts[0]
                last = " ".join(parts[1:])
                players.append((first, last))
            else:
                players.append(("", txt))
    club: Optional[str] = None
    # Heuristic: look for "Verein:" / "Club:" label
    m = re.search(r"(?:Verein|Club)\s*:?\s*([A-Za-zÄÖÜäöüß0-9 .\-]+)",
                  soup.get_text(" "))
    if m:
        club = m.group(1).strip()
    return TeamInfo(team_id=team_id, players=players, club=club)


# ── FIVB CSV loader ───────────────────────────────────────────────────────────

def ensure_fivb_csv(force: bool = False) -> Optional[Path]:
    """Download the BigTimeStats archive once, cache locally."""
    if FIVB_CSV_PATH.exists() and not force:
        return FIVB_CSV_PATH
    _throttle()
    print(f"  [scraper] downloading FIVB archive → {FIVB_CSV_PATH}",
          file=sys.stderr)
    try:
        r = requests.get(FIVB_CSV_URL, timeout=60,
                         headers={"User-Agent": USER_AGENT})
        STATS.http_requests += 1
        r.raise_for_status()
        FIVB_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIVB_CSV_PATH.write_bytes(r.content)
        return FIVB_CSV_PATH
    except requests.RequestException as e:
        STATS.errors += 1
        print(f"  [scraper] FIVB download failed: {e}", file=sys.stderr)
        return None


def iter_fivb_rows(csv_path: Path):
    """Yield raw dict rows from the FIVB archive — column names preserved."""
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


# ── Smoke test entrypoint (run this module directly) ──────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--saison", type=int, default=26,
                    help="DVV season (two-digit year)")
    ap.add_argument("--gender", default="m")
    args = ap.parse_args()
    rows = fetch_tournament_list(args.saison)
    rows = filter_german_beach_tour(rows, gender=args.gender)
    print(f"{args.saison}/{args.gender}: {len(rows)} GBT tournaments")
    for r in rows[:10]:
        print(f"  id={r.id:>5}  {r.date_start} - {r.date_end}   {r.name}")
    print(STATS.summary())
