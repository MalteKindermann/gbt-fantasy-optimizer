"""
bvbinfo.com scraper — fills the 2022-09..2025 gap left by BigTimeStats.

Two endpoints we care about:

  http://bvbinfo.com/Season.asp?AssocID=3&Gender=M&Year=YYYY&Process=
      → season index page listing every FIVB Men tournament in YYYY,
        with anchor links to Tournament.asp?ID=N.

  http://bvbinfo.info/MatchResults?TournID1=N
      → all matches for tournament N, including date, round, players, score.

Same politeness model as scraper.py (raw HTML cache + token-bucket throttle).
Cache lives under data/raw/bvb/ so it doesn't collide with the DVV cache.

We deliberately reuse the global STATS and throttle from scraper.py so the
HTTP counters keep totalling across both sources within one process.
"""
from __future__ import annotations

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

# Share the throttle + STATS with the main scraper so the per-run summary is
# accurate across both modules.
from elo import scraper as _sc  # noqa: E402


# ── Paths ─────────────────────────────────────────────────────────────────────

RAW_DIR = data_dir() / "raw" / "bvb"
RAW_DIR.mkdir(parents=True, exist_ok=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

BVB_BASE      = "http://bvbinfo.com"
BVB_INFO_BASE = "http://bvbinfo.info"

# AssocID values on bvbinfo:
#   1 = AVP, 3 = FIVB, 11 = CEV, 17 = NORCECA
ASSOC_FIVB = 3


# ── HTTP ──────────────────────────────────────────────────────────────────────

USER_AGENT = ("gbt-fantasy-elo/0.1 (research / bvbinfo gap-fill; "
              "mailto:malte.kindermann@gmx.de)")


def _bvb_path(name: str) -> Path:
    return RAW_DIR / (_sc._safe_name(name) + ".html")


def _fetch(url: str, cache_name: str, force: bool = False) -> Optional[str]:
    """Polite GET with raw HTML cache. Mirrors scraper._fetch_html semantics."""
    p = _bvb_path(cache_name)
    if p.exists() and not force:
        _sc.STATS.cache_hits += 1
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    _sc._throttle()
    _sc.STATS.request_log.append(f"bvb:{cache_name}  <-  {url}")

    for attempt in (1, 2):
        try:
            r = requests.get(url, timeout=20,
                             headers={"User-Agent": USER_AGENT})
            _sc.STATS.http_requests += 1
            if r.status_code >= 500 and attempt == 1:
                time.sleep(5.0)
                continue
            if not r.ok:
                _sc.STATS.errors += 1
                print(f"  [bvb] {r.status_code} on {cache_name}",
                      file=sys.stderr)
                return None
            try:
                p.write_text(r.text, encoding="utf-8")
            except OSError as e:
                print(f"  [bvb] cache write failed for {cache_name}: {e}",
                      file=sys.stderr)
            return r.text
        except requests.RequestException as e:
            _sc.STATS.errors += 1
            if attempt == 1:
                print(f"  [bvb] retrying {cache_name} after error: {e}",
                      file=sys.stderr)
                time.sleep(5.0)
                continue
            print(f"  [bvb] giving up on {cache_name}: {e}", file=sys.stderr)
            return None
    return None


# ── Season index (Season.asp) ─────────────────────────────────────────────────

@dataclass
class BvbTournamentRef:
    tournament_id: int
    name: str           # location, e.g. "Doha"
    year: int
    gender: str         # 'm' | 'f'
    date_range: str     # raw range string, e.g. "Feb 1-5"
    date_iso: str       # best-effort ISO start date (yyyy-mm-dd), empty if unparseable


def fetch_season(year: int, gender: str = "m", assoc_id: int = ASSOC_FIVB,
                 force: bool = False) -> list[BvbTournamentRef]:
    g = "M" if gender == "m" else "W"
    url = f"{BVB_BASE}/Season.asp?AssocID={assoc_id}&Gender={g}&Year={year}&Process="
    html = _fetch(url, f"season_{assoc_id}_{g}_{year}", force=force)
    if not html:
        return []
    return _parse_season(html, year, gender)


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_short_date(s: str, year: int) -> str:
    """'Feb 1-5' / 'September 10-13' -> '2023-02-01' (start day, ISO)."""
    s = s.strip()
    m = re.match(r"(\w+)\s+(\d{1,2})", s)
    if not m:
        return ""
    month = _MONTHS.get(m.group(1)[:4].lower()) or _MONTHS.get(m.group(1)[:3].lower())
    if not month:
        return ""
    day = int(m.group(2))
    return f"{year}-{month:02d}-{day:02d}"


def _parse_season(html: str, year: int, gender: str) -> list[BvbTournamentRef]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[BvbTournamentRef] = []
    for a in soup.find_all("a", href=re.compile(r"Tournament\.asp\?ID=\d+")):
        m = re.search(r"ID=(\d+)", a["href"])
        if not m:
            continue
        tid = int(m.group(1))
        tr = a.find_parent("tr")
        if not tr:
            continue
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        # tournament location is in the same cell as the link
        name_text = a.get_text(" ", strip=True)
        out.append(BvbTournamentRef(
            tournament_id=tid, name=name_text, year=year, gender=gender,
            date_range=date_text, date_iso=_parse_short_date(date_text, year),
        ))
    # Dedup (the page contains the same tournament rows nested in summary tables)
    seen: dict[int, BvbTournamentRef] = {}
    for t in out:
        if t.tournament_id not in seen:
            seen[t.tournament_id] = t
    return sorted(seen.values(), key=lambda r: r.date_iso or "9999")


# ── Match results (MatchResults?TournID1=N) ───────────────────────────────────

@dataclass
class BvbMatch:
    tournament_id: int
    year: int
    gender: str               # match-row gender ('m'/'f') — bvb pages may mix
    date_iso: str             # parsed from the section header
    round_label: str
    team_w_players: list[str] # full names, e.g. ["Anders Mol", "Christian Sorum"]
    team_w_country: str
    team_w_seed: Optional[str]
    team_l_players: list[str]
    team_l_country: str
    team_l_seed: Optional[str]
    score: str                # raw, e.g. "21-18, 19-21, 15-12"
    set_scores: list[tuple[int, int]]   # parsed
    duration: str


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")


def _parse_section_date(text: str) -> str:
    """'Thursday, February 2, 2023' -> '2023-02-02'."""
    text = text.strip()
    m = re.match(
        r"(?:\w+,\s*)?(\w+)\s+(\d{1,2}),?\s+(\d{4})", text)
    if not m:
        return ""
    month_token, day, year = m.groups()
    key = month_token[:4].lower() if month_token[:4].lower() in _MONTHS else month_token[:3].lower()
    month = _MONTHS.get(key)
    if not month:
        return ""
    return f"{year}-{month:02d}-{int(day):02d}"


_PARTY_RE = re.compile(
    r"^\s*(?:\((?P<seed>[^)]+)\)\s*)?"
    r"(?P<players>.+?),\s*(?P<country>[A-Za-zÀ-ÿ. \-]+?)\s*$"
)


def _parse_party(text: str) -> tuple[list[str], str, Optional[str]]:
    """
    '(9) Raisa Schoon/Katja Stam, Netherlands' →
    (['Raisa Schoon', 'Katja Stam'], 'Netherlands', '9')
    """
    text = text.strip()
    m = _PARTY_RE.match(text)
    if not m:
        return ([text], "", None)
    seed = m.group("seed")
    players_text = m.group("players").strip()
    country = (m.group("country") or "").strip()
    players = [p.strip() for p in re.split(r"\s*/\s*", players_text) if p.strip()]
    return (players, country, seed)


_SCORE_PAIR_RE = re.compile(r"(\d{1,2})\s*[-:]\s*(\d{1,2})")


def _parse_score(s: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in _SCORE_PAIR_RE.findall(s)]


def fetch_tournament_matches(tournament_id: int, year: int,
                             force: bool = False) -> list[BvbMatch]:
    url = f"{BVB_INFO_BASE}/MatchResults?TournID1={tournament_id}"
    html = _fetch(url, f"matches_{tournament_id}", force=force)
    if not html:
        return []
    return _parse_match_results(html, tournament_id, year)


def _parse_match_results(html: str, tournament_id: int, year: int) -> list[BvbMatch]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[BvbMatch] = []
    current_date = ""
    table = soup.find("table", id="dgResults")
    if not table:
        return out
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) == 1 and "clsHeadLine" in (cells[0].get("class") or []):
            # Date header row spanning columns
            current_date = _parse_section_date(cells[0].get_text(" ", strip=True))
            continue
        # Skip column-header row (Time/Gender/Round/…)
        if len(cells) >= 7 and all("clsHeadLine" in (c.get("class") or [])
                                   for c in cells[:7]):
            continue
        if len(cells) < 7:
            continue
        # Cell layout: [Time, Gender, Round, Preview, Results, Score, Duration]
        gender_text = cells[1].get_text(" ", strip=True).upper()
        gender = ("m" if gender_text.startswith("M")
                  else "f" if gender_text.startswith("W") else "?")
        round_label = cells[2].get_text(" ", strip=True)
        result_text = cells[4].get_text(" ", strip=True)
        score_text = cells[5].get_text(" ", strip=True)
        duration = cells[6].get_text(" ", strip=True)

        if " def. " not in result_text:
            continue
        w_text, _, l_text = result_text.partition(" def. ")
        w_players, w_country, w_seed = _parse_party(w_text)
        l_players, l_country, l_seed = _parse_party(l_text)
        if len(w_players) < 2 or len(l_players) < 2:
            continue
        sets = _parse_score(score_text)

        out.append(BvbMatch(
            tournament_id=tournament_id, year=year, gender=gender,
            date_iso=current_date, round_label=round_label,
            team_w_players=w_players, team_w_country=w_country, team_w_seed=w_seed,
            team_l_players=l_players, team_l_country=l_country, team_l_seed=l_seed,
            score=score_text, set_scores=sets, duration=duration,
        ))
    return out


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2023)
    ap.add_argument("--gender", default="m")
    args = ap.parse_args()
    seasons = fetch_season(args.year, gender=args.gender)
    print(f"{args.year}/{args.gender}: {len(seasons)} FIVB tournaments")
    for t in seasons[:5]:
        print(f"  id={t.tournament_id:>5}  {t.date_iso}  {t.name}")
    print(_sc.STATS.summary())
