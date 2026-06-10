"""
DVV-Tournament-Scraper für den GBT Fantasy Optimizer
=====================================================

Holt das aktuelle / nächste Turnier der "Deutsche Beach-Volleyball Tour\\German
Beach Tour"-Kategorie direkt von der offiziellen DVV-Webseite
(`https://beach.volleyball-verband.de/public/`), inklusive Setzliste (welche
Teams sind in welchem Seed) und Spielplan (welche Matches sind schon gespielt).

Output-Schema ist kompatibel zur ehemaligen `fetch_gbt_bracket()`-Rückgabe aus
`simulate_tournament.py`, damit alle Downstream-Konsumenten
(`sync_players_available_from_brackets`, `simulate_gbt_bracket`, `_run`)
unverändert weiterlaufen.

Public API
----------
  discover_current_tournament(gender, force=False) -> dict | None
  fetch_setzliste(tournament_id, qualifier=False, force=False) -> list[dict]
  fetch_spielplan(tournament_id, qualifier=False, force=False) -> list[dict]
  build_bracket(gender, force=False) -> dict | None

Caching
-------
Disk-Cache unter `data/.cache/dvv_*.json` (TTL 1h für Tour-Liste & Setzliste,
30min für Spielplan, weil Ergebnisse häufiger reinkommen).
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_dotenv_files, data_dir  # noqa: E402
load_dotenv_files()

ROOT      = Path(__file__).resolve().parent.parent
CACHE_DIR = data_dir() / ".cache"

# ── Endpoints ─────────────────────────────────────────────────────────────────
BASE_URL    = "https://beach.volleyball-verband.de/public"
URL_LIST    = BASE_URL + "/tur.php"
URL_SHOW    = BASE_URL + "/tur-show.php"
URL_SETZ    = BASE_URL + "/tur-sl.php"
URL_SPIEL   = BASE_URL + "/tur-sp.php"

CATEGORY = "Deutsche Beach-Volleyball Tour\\German Beach Tour"

# Cache TTLs (seconds)
TTL_TOURLIST  = 3600         # 1h
TTL_SETZLISTE = 3600         # 1h
TTL_SPIELPLAN = 1800         # 30min — game results change more often


# ── Disk-Cache (eigene Helpers — Module ist standalone CLI-fähig) ─────────────

def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.json"


def _cache_get(name: str, ttl: int):
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry.get("fetched_at", 0) > ttl:
            return None
        return entry.get("data")
    except Exception:
        return None


def _cache_set(name: str, data) -> None:
    p = _cache_path(name)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "data": data},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  WARNING: konnte Cache {name} nicht schreiben: {e}", file=sys.stderr)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _fetch_html(url: str, params: dict | None = None) -> str:
    """GET text content. Wirft requests.HTTPError bei nicht-2xx."""
    r = requests.get(url, params=params, timeout=20,
                     headers={"User-Agent": "gbt-fantasy-optimizer/1.0"})
    r.raise_for_status()
    return r.text


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")

def _parse_date_range(s: str) -> tuple[date, date] | None:
    """
    Parst Datumsbereiche aus der DVV-Tour-Liste:
      '07.05. - 10.05.2026' → (2026-05-07, 2026-05-10)   (start ohne Jahr)
      '07.05.2026 - 10.05.2026' → (2026-05-07, 2026-05-10) (both with year)
      '07.05.2026' → (2026-05-07, 2026-05-07)            (single day)
    """
    s = s.strip()
    # Range with start-date missing the year (most common DVV format)
    m_short = re.match(
        r"\s*(\d{1,2})\.(\d{1,2})\.\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$", s)
    if m_short:
        d1, m1, d2, m2, year = m_short.groups()
        try:
            return (date(int(year), int(m1), int(d1)),
                    date(int(year), int(m2), int(d2)))
        except ValueError:
            return None

    # Both endpoints carry a year (rare but possible)
    dates = list(_DATE_RE.finditer(s))
    if len(dates) >= 2:
        d1, m1, y1 = dates[0].groups()
        d2, m2, y2 = dates[1].groups()
        try:
            return (date(int(y1), int(m1), int(d1)),
                    date(int(y2), int(m2), int(d2)))
        except ValueError:
            return None
    if len(dates) == 1:
        d, m, y = dates[0].groups()
        try:
            dt = date(int(y), int(m), int(d))
            return (dt, dt)
        except ValueError:
            return None
    return None


def _split_team_text(text: str) -> tuple[list[str], int | None]:
    """
    'Henning - Pfretzschner (1)' → (['Henning', 'Pfretzschner'], 1).
    '(N)' am Ende ist der Seed. Wenn keiner: None.
    """
    text = text.strip()
    seed = None
    m_seed = re.search(r"\((\d+)\)\s*$", text)
    if m_seed:
        seed = int(m_seed.group(1))
        text = text[:m_seed.start()].strip()
    # Trenner ist ' - ' (mit Leerzeichen drumherum), um z.B. 'Stadie-Seeber' nicht zu zerlegen
    parts = re.split(r"\s+-\s+", text)
    return [p.strip() for p in parts if p.strip()], seed


def _extract_team_id(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"team\.php\?id=(\d+)", href)
    return m.group(1) if m else None


def _parse_dvv_points(text: str) -> int:
    """'2.290' → 2290, '0' → 0, leere/unbekannte Werte → 0."""
    s = text.strip().replace(".", "").replace(",", "")
    try:
        return int(s)
    except ValueError:
        return 0


# ── Discovery: aktuelles/nächstes Turnier finden ──────────────────────────────

def _current_season_year() -> int:
    return datetime.now().year


def discover_current_tournament(gender: str, force: bool = False,
                                today: date | None = None) -> dict | None:
    """
    Findet das nächste / aktuell laufende DVV-German-Beach-Tour-Turnier für
    `gender` in {'m','f'}. Returnt:
        {'id': int, 'name': str, 'date_start': 'YYYY-MM-DD',
         'date_end': 'YYYY-MM-DD', 'location': str, 'gender': 'm'|'f',
         'state': 'upcoming'|'running'}
    oder None, falls keins existiert.
    """
    if gender not in ("m", "f"):
        raise ValueError("gender must be 'm' or 'f'")

    today = today or date.today()
    year = today.year
    cache_key = f"dvv_tour_list_{year}"

    rows = _cache_get(cache_key, ttl=TTL_TOURLIST) if not force else None
    if rows is None:
        rows = _parse_tour_list(_fetch_html(URL_LIST, params={"saison": year}))
        _cache_set(cache_key, rows)

    wanted_gender = "Männer" if gender == "m" else "Frauen"

    # Filter: category match + gender match + not finished yet
    candidates = []
    for row in rows:
        if row.get("category") != CATEGORY:
            continue
        if row.get("gender_text") != wanted_gender:
            continue
        de = row.get("date_end")
        if not de:
            continue
        end_d = date.fromisoformat(de)
        if end_d < today:
            continue   # already over
        candidates.append(row)

    if not candidates:
        return None

    # Earliest first
    candidates.sort(key=lambda r: r["date_start"])
    pick = candidates[0]
    start = date.fromisoformat(pick["date_start"])
    end   = date.fromisoformat(pick["date_end"])
    state = "running" if start <= today <= end else "upcoming"
    return {
        "id":         pick["id"],
        "name":       pick.get("location") or "?",
        "date_start": pick["date_start"],
        "date_end":   pick["date_end"],
        "location":   pick.get("location"),
        "gender":     gender,
        "state":      state,
    }


def _parse_tour_list(html: str) -> list[dict]:
    """
    Parst die Turnier-Listen-Tabelle. Returnt
      [{id, location, category, gender_text, teams, date_start, date_end}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        rng = _parse_date_range(date_text)
        if not rng:
            continue
        date_start, date_end = rng
        category = cells[1].get_text(" ", strip=True)
        # Cells[2] = location link
        loc_a = cells[2].find("a", href=True)
        if not loc_a:
            continue
        m = re.search(r"id=(\d+)", loc_a["href"])
        if not m:
            continue
        tid = int(m.group(1))
        location = loc_a.get_text(" ", strip=True)
        gender_text = cells[3].get_text(" ", strip=True)
        teams = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
        out.append({
            "id":          tid,
            "location":    location,
            "category":    category,
            "gender_text": gender_text,
            "teams":       teams,
            "date_start":  date_start.isoformat(),
            "date_end":    date_end.isoformat(),
        })
    return out


# ── Setzliste-Scraper ─────────────────────────────────────────────────────────

def fetch_setzliste(tournament_id: int, qualifier: bool = False,
                    force: bool = False) -> list[dict]:
    """
    Returnt [{seed: '1', players: ['Henning', 'Pfretzschner'],
              team_id: '65873', club: '...', dvv_points: 2290}, ...]
    Leere Liste, falls die Seite noch keine Tabelle hat oder 404.
    """
    feld = 2 if qualifier else 1
    cache_key = f"dvv_setz_{tournament_id}_{feld}"

    cached = _cache_get(cache_key, ttl=TTL_SETZLISTE) if not force else None
    if cached is not None:
        return cached

    try:
        html = _fetch_html(URL_SETZ, params=_setz_params(tournament_id, qualifier))
    except requests.HTTPError:
        return []

    rows = _parse_setzliste(html)
    _cache_set(cache_key, rows)
    return rows


def _setz_params(tournament_id: int, qualifier: bool) -> dict:
    p = {"id": tournament_id}
    if qualifier:
        p["feld"] = 2
    return p


def _parse_setzliste(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        header_row = t.find("tr")
        if not header_row:
            continue
        header_text = header_row.get_text(" ", strip=True)
        if "Platz" in header_text and "Team" in header_text:
            table = t
            break
    if not table:
        return []

    rows = []
    for tr in table.find_all("tr")[1:]:  # skip header
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        seed_text = cells[0].get_text(" ", strip=True)
        if not seed_text or not re.match(r"^\d+$|^Q?\d+$", seed_text):
            # Skip non-seed rows (sub-headers, empty separators)
            continue
        team_cell = cells[1]
        team_a = team_cell.find("a", href=True)
        if team_a:
            team_text = team_a.get_text(" ", strip=True)
            team_id   = _extract_team_id(team_a.get("href"))
        else:
            team_text = team_cell.get_text(" ", strip=True)
            team_id   = None
        players, _seed_suffix = _split_team_text(team_text)
        club = cells[2].get_text(" ", strip=True) if len(cells) > 2 else ""
        dvv  = _parse_dvv_points(cells[3].get_text(" ", strip=True)) if len(cells) > 3 else 0
        rows.append({
            "seed":       seed_text,
            "players":    players,
            "team_id":    team_id,
            "club":       club,
            "dvv_points": dvv,
        })
    return rows


# ── Spielplan-Scraper ─────────────────────────────────────────────────────────

def fetch_spielplan(tournament_id: int, qualifier: bool = False,
                    force: bool = False) -> list[dict]:
    """
    Returnt eine Liste von Matches:
      [{match_num, round, date, time, court,
        team_a: {team_id, players, seed, display},
        team_b: {team_id, players, seed, display},
        result: {sets:'2:0', detail:'21:16, 21:17',
                 winner:'A'|'B'|None,            # None = noch nicht gespielt
                 points_a:int, points_b:int}}]
    Leere Liste falls die Auslosung noch nicht steht (Tabellen ohne Match-Rows).
    """
    feld = 2 if qualifier else 1
    cache_key = f"dvv_spiel_{tournament_id}_{feld}"

    cached = _cache_get(cache_key, ttl=TTL_SPIELPLAN) if not force else None
    if cached is not None:
        return cached

    try:
        html = _fetch_html(URL_SPIEL, params=_setz_params(tournament_id, qualifier))
    except requests.HTTPError:
        return []

    matches = _parse_spielplan(html)
    _cache_set(cache_key, matches)
    return matches


def _parse_spielplan(html: str) -> list[dict]:
    """
    Pro <div class='sectionheader'>X</div> kommt eine <table> mit Match-Rows.
    Cells: [Spiel | Tag | Zeit | Court | Team1-Link | : | Team2-Link |
            Schiri | Score-Link | Punkte | Sätze].
    """
    soup = BeautifulSoup(html, "html.parser")
    matches: list[dict] = []

    for header in soup.find_all("div", class_="sectionheader"):
        round_name = header.get_text(" ", strip=True)
        table = header.find_next("table")
        if not table:
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "tD", "TD"])
            if len(cells) < 7:
                continue
            # The header row contains 'Spiel' as text, skip
            num_text = cells[0].get_text(" ", strip=True)
            if not num_text.isdigit():
                continue

            tag_text  = cells[1].get_text(" ", strip=True)
            time_text = cells[2].get_text(" ", strip=True)
            court     = cells[3].get_text(" ", strip=True)

            team_a = _parse_match_team(cells[4])
            team_b = _parse_match_team(cells[6]) if len(cells) > 6 else None
            if not team_a or not team_b:
                continue

            # Result cell: cells[8] (Score-Link with text like '2:0 (21:16, 21:17)')
            result = None
            if len(cells) > 8:
                result = _parse_match_result(cells[8].get_text(" ", strip=True),
                                             cells[9].get_text(" ", strip=True)
                                                 if len(cells) > 9 else "")

            matches.append({
                "match_num": int(num_text),
                "round":     round_name,
                "date":      tag_text,
                "time":      time_text,
                "court":     court,
                "team_a":    team_a,
                "team_b":    team_b,
                "result":    result,
            })
    return matches


def _parse_match_team(cell) -> dict | None:
    """{'team_id', 'players', 'seed', 'display'}"""
    a = cell.find("a", href=True)
    text = (a.get_text(" ", strip=True) if a else cell.get_text(" ", strip=True))
    if not text:
        return None
    players, seed = _split_team_text(text)
    if not players:
        return None
    return {
        "team_id": _extract_team_id(a.get("href")) if a else None,
        "players": players,
        "seed":    seed,
        "display": text,
    }


def _parse_match_result(score_text: str, points_text: str) -> dict | None:
    """
    '2:0 (21:16, 21:17)' → {sets:'2:0', detail:'21:16, 21:17', winner:'A'}
    leerer Text          → None
    """
    score_text = score_text.strip()
    if not score_text:
        return None
    m = re.match(r"(\d+)\s*:\s*(\d+)\s*(?:\(([^)]*)\))?", score_text)
    if not m:
        return None
    a_sets, b_sets = int(m.group(1)), int(m.group(2))
    detail = (m.group(3) or "").strip()
    if a_sets == 0 and b_sets == 0:
        return None  # noch nicht gespielt
    winner = "A" if a_sets > b_sets else ("B" if b_sets > a_sets else None)

    # points_text may be '38, 20'
    points_a = points_b = 0
    pm = re.match(r"(\d+)\s*,\s*(\d+)", points_text.strip())
    if pm:
        points_a, points_b = int(pm.group(1)), int(pm.group(2))

    return {
        "sets":   f"{a_sets}:{b_sets}",
        "detail": detail,
        "winner": winner,
        "points_a": points_a,
        "points_b": points_b,
    }


# ── Bracket composition ───────────────────────────────────────────────────────

# Static 8-team double-elimination match graph — identical zum Format,
# das gbt.hanski.de heute schon liefert und das `simulate_gbt_bracket`
# konsumiert.
RULES_8_DOUBLE_ELIM = {
    "1":  {"A": "S1", "B": "S8"},
    "2":  {"A": "S4", "B": "S5"},
    "3":  {"A": "S3", "B": "S6"},
    "4":  {"A": "S2", "B": "S7"},
    "5":  {"A": "L1", "B": "L2"},
    "6":  {"A": "L3", "B": "L4"},
    "7":  {"A": "W1", "B": "W2"},
    "8":  {"A": "W3", "B": "W4"},
    "9":  {"A": "W5", "B": "L8"},
    "10": {"A": "W6", "B": "L7"},
    "11": {"A": "W7", "B": "W9"},
    "12": {"A": "W8", "B": "W10"},
    "13": {"A": "W11", "B": "W12"},
}


def build_bracket(gender: str, force: bool = False,
                  today: date | None = None) -> dict | None:
    """
    Setzt Discovery + Setzliste + Spielplan zu einem Bracket-Dict zusammen.
    Schema bleibt identisch zur `fetch_gbt_bracket()`-Ausgabe — plus zwei neue
    Felder: `meta.source='dvv'` und `meta.matches` (Roh-Spielplan-Ergebnisse).

    `today` ist optional; default = echtes Heute. Praktisch für Tests.

    Returnt None, falls keine Discovery möglich (kein passendes Turnier).
    """
    info = discover_current_tournament(gender, force=force, today=today)
    if info is None:
        return None

    tid = info["id"]
    setz = fetch_setzliste(tid, qualifier=False, force=force)
    matches = fetch_spielplan(tid, qualifier=False, force=force)

    teams: dict[str, dict] = {}
    for entry in setz:
        seed = entry["seed"]
        teams[seed] = {
            "seeding": seed,
            "players": entry["players"],
            # Extra-Metadaten, die Downstream ignorieren darf:
            "teamId":     entry.get("team_id"),
            "club":       entry.get("club"),
            "dvvPoints":  entry.get("dvv_points"),
        }

    # Many GBT main draws are listed with only 6 seeded teams in the Setzliste
    # — slots 7 and 8 are reserved for the two qualifier winners. Until quali
    # is decided we fill these with placeholders (best-effort: top-seeded
    # qualifier teams, since those most often win). This keeps the 8-team
    # rules template applicable, so the bracket can already be rendered.
    numeric_seeds = sorted(int(s) for s in teams.keys() if s.isdigit())
    if numeric_seeds == [1, 2, 3, 4, 5, 6]:
        quali = fetch_setzliste(tid, qualifier=True, force=force)
        # Top-seeded qualifiers fill seed 7 + 8 as a best-guess placeholder.
        # We tag the players list with a trailing 'Q' marker so downstream
        # code can recognize them and the UI can show "(Quali)" if it wants.
        q_teams = quali[:2]
        for idx, q in enumerate(q_teams):
            seed = str(7 + idx)
            teams[seed] = {
                "seeding":   seed,
                "players":   q["players"] or [f"Q{idx+1}"],
                "teamId":    q.get("team_id"),
                "club":      q.get("club"),
                "dvvPoints": q.get("dvv_points"),
                "qualiPending": True,   # signal placeholder
            }
        # Pad up to 8 with bare 'Q'-placeholders if the qualifier Setzliste
        # came back empty or short.
        for idx in range(len(q_teams), 2):
            seed = str(7 + idx)
            teams[seed] = {
                "seeding": seed,
                "players": [f"Q{idx+1}"],
                "qualiPending": True,
            }
        numeric_seeds = sorted(int(s) for s in teams.keys() if s.isdigit())

    # Rules: 8-Team-Double-Elim Template, falls genau 8 numerische Seeds.
    rules = dict(RULES_8_DOUBLE_ELIM) if numeric_seeds[:8] == list(range(1, 9)) else {}

    # Status ableiten
    status = "drawn"
    if not teams or any(s.startswith("Q") for s in teams):
        status = "pending"
    if matches and any((m.get("result") or {}).get("winner") for m in matches):
        status = "running"
    if matches and all((m.get("result") or {}).get("winner") for m in matches):
        status = "complete"

    return {
        "meta": {
            "source":        "dvv",
            "name":          info.get("location"),
            "tournamentId":  tid,
            "gender":        "M" if gender == "m" else "F",
            "status":        status,
            "lastUpdate":    datetime.now().isoformat(timespec="seconds"),
            "dateStart":     info.get("date_start"),
            "dateEnd":       info.get("date_end"),
        },
        "teams":               teams,
        "rules":               rules,
        "initialBracketState": "",
        "times":               [],
        "matches":             matches,  # NEU: Roh-Spielplan für Sim & UI
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DVV Bracket-Scraper")
    parser.add_argument("--gender", choices=["m", "f"], default="m")
    parser.add_argument("--force", action="store_true", help="Cache ignorieren")
    parser.add_argument("--print", action="store_true",
                        help="Setzliste + Bracket-Shape ausgeben")
    args = parser.parse_args()

    info = discover_current_tournament(args.gender, force=args.force)
    if not info:
        print(f"Kein laufendes/anstehendes Turnier für gender={args.gender} gefunden.")
        sys.exit(1)
    print(f"Turnier: {info['name']} (id={info['id']}, "
          f"{info['date_start']}–{info['date_end']}, state={info['state']})")

    b = build_bracket(args.gender, force=args.force)
    if not b:
        sys.exit(1)
    print(f"Status: {b['meta']['status']}, source={b['meta']['source']}")
    print(f"Teams: {len(b['teams'])}, Rules: {len(b['rules'])}, Matches: {len(b['matches'])}")
    if args.print:
        for seed, t in sorted(b["teams"].items(),
                              key=lambda kv: (not kv[0].isdigit(), kv[0])):
            disp = " - ".join(t["players"])
            print(f"  Seed {seed:<3} {disp:<35} (DVV: {t.get('dvvPoints', '?')})")
        if b["matches"]:
            print()
            played = sum(1 for m in b["matches"] if (m.get("result") or {}).get("winner"))
            print(f"Spielplan: {played}/{len(b['matches'])} Matches gespielt.")
            for m in b["matches"][:5]:
                r = m.get("result") or {}
                w = r.get("winner") or "—"
                print(f"  M{m['match_num']:>2} ({m['round']}): "
                      f"{m['team_a']['display']} vs {m['team_b']['display']} "
                      f"→ {r.get('sets', '?')} (W: {w})")
