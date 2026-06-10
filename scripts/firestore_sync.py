"""
Firestore-Sync für den GBT Fantasy Optimizer
============================================

Holt das aktuelle Saison-Dokument (Preise, Vornamen, IDs, Stats) direkt aus
der Firestore-Datenbank von gbt-fantasy.web.app — eine einzige Quelle der
Wahrheit für (a) Preise und (b) Auflösung mehrdeutiger Nachnamen.

Setup
-----
Der User legt einmalig eine Datei `data/firebase_auth.json` an, indem er das
Snippet in `fetch_auth_token.txt` in der DevTools-Konsole von
https://gbt-fantasy.web.app/ ausführt. Inhalt der Datei:

    {
      "apiKey":       "AIzaSy…",
      "refreshToken": "AMf-vBy…",
      "uid":          "...",
      "savedAt":      "..."
    }

Mit dem Refresh-Token tauscht der Server bei Bedarf einen frischen ID-Token
ein (gültig 1 h) und liest damit das Firestore-Dokument
`season_stats/2026`.

Public API
----------
  fetch_firestore_season(force=False) -> dict[str, dict] | None
      Top-Level Entry. Returnt ein Dict
          { "<playerId>": { "firstName", "lastName", "price", "pos",
                            "gender", "tp", "t", "mp", "img" }, ... }
      oder wirft RuntimeError mit klarer Meldung, falls Setup fehlt /
      Token expired / Netzwerk hängt.

  parse_season_players(raw_doc) -> dict[str, dict]
      Reine Funktion: typed-value-Baum → flaches Dict. Wird auch für den
      manuellen Fallback genutzt (player kann ein `players_season.json` als
      Pfad reinwerfen).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Shared env/dotenv loader + data-dir resolver
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_dotenv_files, data_dir
load_dotenv_files()

# ── Paths (env-aware) ─────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent.parent
DATA_DIR   = data_dir()
AUTH_FILE  = DATA_DIR / "firebase_auth.json"
CACHE_DIR  = DATA_DIR / ".cache"
CACHE_FILE = CACHE_DIR / "firestore_season.json"

# ── Endpoints ─────────────────────────────────────────────────────────────────
SECURE_TOKEN_URL  = "https://securetoken.googleapis.com/v1/token?key={api_key}"
FIRESTORE_URL_TPL = ("https://firestore.googleapis.com/v1/projects/gbt-fantasy/"
                     "databases/(default)/documents/season_stats/{year}")
TOURNAMENTS_LIST_URL = ("https://firestore.googleapis.com/v1/projects/gbt-fantasy/"
                       "databases/(default)/documents/tournaments?pageSize=100")

# Erstes Jahr, das Firestore zurückblickend hat. Per Probe gefunden — 2024 und
# älter geben 404. Falls Firestore irgendwann ältere Jahre nachträgt, einfach
# hier dekrementieren.
EARLIEST_SEASON_YEAR = 2025


def current_season_year() -> int:
    """
    Aktuelles Saison-Jahr aus der System-Zeit. Per env-var `CURRENT_SEASON_YEAR`
    überschreibbar (handy zum Backtesten oder bei Saison-Wechseln mitten im Jahr).
    """
    override = os.environ.get("CURRENT_SEASON_YEAR", "").strip()
    if override.isdigit():
        return int(override)
    return datetime.date.today().year


def season_years() -> list[int]:
    """
    Liste aller Jahre, die wir aus Firestore zu holen versuchen — vom frühesten
    verfügbaren Jahr bis zum aktuellen Saison-Jahr. Automatisch zukunftssicher:
    sobald das System-Datum 2027 zeigt, wird auch 2027 mitgesynced.
    """
    return list(range(EARLIEST_SEASON_YEAR, current_season_year() + 1))

# ── TTLs ──────────────────────────────────────────────────────────────────────
SNAPSHOT_TTL_SECONDS = 600   # 10 min — Preise ändern sich relativ selten
ID_TOKEN_TTL_SECONDS = 50 * 60   # 50 min, kleiner als Firebase's 60-min Gültigkeit

# In-Memory Cache für ID-Token (überlebt nur den laufenden Prozess)
_id_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _load_auth() -> dict | None:
    """
    Auth-Reihenfolge (höchste Priorität zuerst):
      1. ENV-Variablen FIREBASE_API_KEY + FIREBASE_REFRESH_TOKEN
         (Production-Setup: Fly.io secrets, Docker --env, .env.local)
      2. Datei data/firebase_auth.json
         (Legacy lokales Setup — bleibt unterstützt, damit alte Setups
         weiter funktionieren)
    Returnt None wenn nichts gefunden.
    """
    env_key   = os.environ.get("FIREBASE_API_KEY", "").strip()
    env_token = os.environ.get("FIREBASE_REFRESH_TOKEN", "").strip()
    if env_key and env_token:
        return {"apiKey": env_key, "refreshToken": env_token, "_source": "env"}

    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("apiKey") and data.get("refreshToken"):
                data["_source"] = "file"
                return data
        except Exception as e:
            print(f"  WARNING: konnte {AUTH_FILE} nicht lesen: {e}", file=sys.stderr)
    return None


def _refresh_id_token(api_key: str, refresh_token: str) -> str:
    """
    Tauscht das Refresh-Token gegen einen frischen ID-Token (gültig 1 h).
    Cached in-memory bis SHORTLY vor Ablauf. Wirft RuntimeError bei Auth-Fehler.
    """
    now = time.time()
    if _id_token_cache["token"] and _id_token_cache["expires_at"] > now + 60:
        return _id_token_cache["token"]

    url = SECURE_TOKEN_URL.format(api_key=api_key)
    try:
        r = requests.post(
            url,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Netzwerk-Fehler beim Token-Refresh: {e}") from e

    if r.status_code != 200:
        # Häufigster Fall: TOKEN_EXPIRED (User hat sich ausgeloggt o.ä.)
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        raise RuntimeError(
            f"Firebase-Auth abgelehnt ({r.status_code}: {err}). "
            f"Bitte data/firebase_auth.json neu anlegen — siehe fetch_auth_token.txt."
        )

    payload = r.json()
    id_token = payload.get("id_token") or payload.get("access_token")
    expires_in = int(payload.get("expires_in", 3600))
    if not id_token:
        raise RuntimeError(f"Token-Antwort enthielt keinen id_token: {payload}")

    _id_token_cache["token"] = id_token
    _id_token_cache["expires_at"] = now + min(expires_in, ID_TOKEN_TTL_SECONDS)
    return id_token


def _fetch_season_doc(id_token: str, year: int | None = None) -> dict | None:
    """
    GET das `season_stats/<year>`-Dokument. Returnt das Roh-JSON.
    Spezielle 404-Behandlung: returnt None statt zu werfen, damit aufrufende
    Stellen sauber zwischen "Jahr existiert nicht" und echten Fehlern trennen
    können (Auth-Fehler / Netzwerk → RuntimeError).
    """
    if year is None:
        year = current_season_year()
    url = FIRESTORE_URL_TPL.format(year=year)
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {id_token}"},
            timeout=20,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Netzwerk-Fehler beim Firestore-Fetch ({year}): {e}") from e

    if r.status_code == 404:
        return None
    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        raise RuntimeError(
            f"Firestore antwortete mit {r.status_code} (year={year}): {err}. "
            f"Eventuell ID-Token expired oder Berechtigungen nicht ausreichend."
        )
    return r.json()


def fetch_archive_season(year: int, force: bool = False) -> dict | None:
    """
    Holt ein historisches Saison-Dokument (`season_stats/<year>`) einmalig
    und speichert es als `data/players_season_<year>.json` (Roh-Firestore-JSON,
    selbes Format wie `players_season.json`).

    Strategie:
      • Wenn die Datei bereits existiert und `force=False`: skip — historische
        Daten ändern sich nicht mehr, deshalb reicht einmal pro Jahr.
      • Wenn keine Auth-Daten vorhanden: silent skip (returnt None).
      • Wenn Firestore 404 antwortet (Jahr existiert nicht): logge das, returnt None.

    Returnt den geparsten dict (`{id: {firstName, lastName, ...}}`) oder None.
    """
    target = DATA_DIR / f"players_season_{year}.json"
    if target.exists() and not force:
        # Bereits gecacht — direkt aus der Datei lesen (kein Netzwerk-Roundtrip).
        try:
            with open(target, encoding="utf-8") as f:
                return parse_season_players(json.load(f))
        except Exception as e:
            print(f"  WARNING: konnte {target.name} nicht lesen, hole neu: {e}",
                  file=sys.stderr)

    auth = _load_auth()
    if auth is None:
        return None

    try:
        id_token = _refresh_id_token(auth["apiKey"], auth["refreshToken"])
        raw_doc  = _fetch_season_doc(id_token, year=year)
    except RuntimeError as e:
        print(f"  ⚠ Archive-Fetch fehlgeschlagen ({year}): {e}", file=sys.stderr)
        return None

    if raw_doc is None:
        print(f"  ℹ season_stats/{year} existiert nicht in Firestore — überspringe.")
        return None

    # Roh-Doc auf Disk schreiben (Frontend kann es später überlagern)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(raw_doc, f, ensure_ascii=False, indent=2)
        print(f"  ✓ Archive {year}: {len(raw_doc.get('fields',{}).get('pl',{}).get('mapValue',{}).get('fields',{}))} "
              f"Spieler nach data/{target.name} gespeichert.")
    except Exception as e:
        print(f"  WARNING: konnte {target.name} nicht schreiben: {e}", file=sys.stderr)

    return parse_season_players(raw_doc)


# ── Parser für typed-value-Bäume ──────────────────────────────────────────────

def _fs_val(field: dict) -> Any:
    """
    Konvertiert einen einzelnen Firestore-typed-value-Knoten in einen
    Python-Wert. Spiegelbild von app.js `fsVal()`.
    """
    if field is None:
        return None
    if "stringValue"  in field: return field["stringValue"]
    if "integerValue" in field: return int(field["integerValue"])
    if "doubleValue"  in field: return float(field["doubleValue"])
    if "booleanValue" in field: return bool(field["booleanValue"])
    if "timestampValue" in field: return field["timestampValue"]
    if "nullValue" in field: return None
    if "arrayValue" in field:
        return [_fs_val(v) for v in field["arrayValue"].get("values", [])]
    if "mapValue" in field:
        return {k: _fs_val(v) for k, v in field["mapValue"].get("fields", {}).items()}
    return None


def parse_season_players(raw_doc: dict) -> dict[str, dict]:
    """
    Wandelt das Firestore-Doc in
        { "<playerId>": { "firstName", "lastName", "price",
                          "pos", "gender", "tp", "t", "mp", "img" } }
    um. Tolerant gegenüber fehlenden Feldern (alte Snapshots).
    """
    fields = (raw_doc.get("fields") or {})
    pl = _fs_val(fields.get("pl")) or {}    # { id: { pr, fn, ln, ... } }
    out: dict[str, dict] = {}
    for pid, p in pl.items():
        if not isinstance(p, dict):
            continue
        out[str(pid)] = {
            "firstName": p.get("fn") or "",
            "lastName":  p.get("ln") or "",
            "price":     int(p["pr"]) if p.get("pr") is not None else None,
            "pos":       p.get("pos") or "",
            "gender":    p.get("g") or "",
            "tp":        float(p["tp"]) if p.get("tp") is not None else 0.0,
            "t":         int(p["t"])  if p.get("t")  is not None else 0,
            "mp":        int(p["mp"]) if p.get("mp") is not None else 0,
            "img":       p.get("ip") or "",
        }
    return out


# ── Aktuelles Turnier (für tagesaktuelle Preise) ──────────────────────────────
#
# `season_stats/<year>.pl[id].pr` enthält Preise — aber die werden auf
# gbt-fantasy.web.app nur unregelmäßig fortgeschrieben. Die echte Wahrheit liegt
# in `tournaments/<doc_id>.players[]` — ein Array `[{id, price, firstName,
# lastName, position, gender, imagePath, ...}]`, das pro Turnier neu gepflegt
# wird. Beim Sync nehmen wir den Tournament-Preis als Override; falls ein
# Spieler nur dort steht (Rookie, noch nicht in season_stats), wird er
# synthetisiert ins Snapshot übernommen.

def _pick_current_tournament(docs: list[dict]) -> dict | None:
    """
    Aus einer Liste von Tournament-Docs das jetzt relevante wählen:
      • Running: today ∈ [start, end] → das jüngste davon
      • Sonst: das nächste upcoming (start > today, kleinster start)
      • Sonst: das letzte vergangene (für die "zwischen Turnieren"-Phase)
    """
    today = datetime.date.today().isoformat()
    running, upcoming, past = [], [], []
    for d in docs:
        f = d.get("fields", {})
        start = (f.get("start") or {}).get("timestampValue", "")[:10]
        end   = (f.get("end")   or {}).get("timestampValue", "")[:10]
        if not start:
            continue
        if start <= today and (not end or today <= end):
            running.append((start, d))
        elif start > today:
            upcoming.append((start, d))
        else:
            past.append((start, d))
    if running:
        return max(running)[1]
    if upcoming:
        return min(upcoming)[1]
    if past:
        return max(past)[1]
    return None


def fetch_current_tournament_players(id_token: str) -> dict[str, dict] | None:
    """
    Holt die `players[]` aus dem aktuellen/nächsten `tournaments/<doc>`-Eintrag.
    Returns:
        {playerId: {price, firstName, lastName, pos, gender, img}}
        oder None bei Fehler (silent — nicht-fatal, Caller fällt auf season_stats zurück)
    """
    try:
        r = requests.get(
            TOURNAMENTS_LIST_URL,
            headers={"Authorization": f"Bearer {id_token}"},
            timeout=10,
        )
        r.raise_for_status()
        docs = r.json().get("documents", [])
    except Exception as e:
        print(f"  WARN tournaments-Liste-Fetch fehlgeschlagen: {e}", file=sys.stderr)
        return None

    chosen = _pick_current_tournament(docs)
    if chosen is None:
        return None

    name = (chosen["fields"].get("name") or {}).get("stringValue", "?")
    arr  = (chosen["fields"].get("players") or {}).get("arrayValue", {}).get("values", [])
    POS_DE_TO_EN = {"Abwehr": "Abwehr", "Block": "Block", "Hybrid": "Hybrid"}
    out: dict[str, dict] = {}
    for entry in arr:
        m = entry.get("mapValue", {}).get("fields", {})
        pid   = _fs_val(m.get("id"))
        price = _fs_val(m.get("price"))
        if pid is None:
            continue
        out[str(pid)] = {
            "firstName": _fs_val(m.get("firstName")) or "",
            "lastName":  _fs_val(m.get("lastName"))  or "",
            "pos":       POS_DE_TO_EN.get(_fs_val(m.get("position")) or "", _fs_val(m.get("position")) or ""),
            "gender":    _fs_val(m.get("gender"))    or "",
            "img":       _fs_val(m.get("imagePath")) or "",
            "price":     int(price) if price is not None else None,
        }
    print(f"  ✓ Aktuelles Turnier {name!r}: {len(out)} Spielerpreise als Override.")
    return out


def _overlay_tournament_prices(players: dict[str, dict],
                                tour_players: dict[str, dict]) -> int:
    """
    Mergt Tournament-Daten ins season_stats-Players-Dict:
      • Spieler in season_stats UND Turnier: price wird auf den Turnier-Wert gesetzt
      • Spieler nur im Turnier (Rookie): wird neu angelegt mit price + Identität,
        Stats (tp/t/mp) bleiben 0 — kommen mit dem nächsten season_stats-Update
    Returns Anzahl Preis-Änderungen (für Logging).
    """
    changed = 0
    for pid, tp in tour_players.items():
        new_price = tp.get("price")
        if new_price is None:
            continue
        if pid in players:
            if players[pid].get("price") != new_price:
                changed += 1
                players[pid]["price"] = new_price
        else:
            # Rookie: nur im Turnier, noch nicht in season_stats
            players[pid] = {
                "firstName": tp.get("firstName", ""),
                "lastName":  tp.get("lastName", ""),
                "price":     new_price,
                "pos":       tp.get("pos", ""),
                "gender":    tp.get("gender", ""),
                "tp":        0.0,
                "t":         0,
                "mp":        0,
                "img":       tp.get("img", ""),
            }
    return changed


# ── Disk-Cache ────────────────────────────────────────────────────────────────

def _load_cached_snapshot(ttl: int = SNAPSHOT_TTL_SECONDS):
    """Returnt das gecachte (geparste) Dict oder None."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry.get("fetched_at", 0) > ttl:
            return None
        return entry.get("players")
    except Exception:
        return None


def _save_snapshot(players: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"fetched_at": time.time(), "players": players},
                f, ensure_ascii=False, indent=2
            )
    except Exception as e:
        print(f"  WARNING: konnte {CACHE_FILE} nicht schreiben: {e}", file=sys.stderr)


def snapshot_age_seconds() -> float | None:
    """Sekunden seit letztem erfolgreichem Snapshot. None wenn keiner da."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            entry = json.load(f)
        return max(0.0, time.time() - float(entry.get("fetched_at", 0)))
    except Exception:
        return None


# ── Public entry ──────────────────────────────────────────────────────────────

def fetch_firestore_season(force: bool = False) -> dict[str, dict] | None:
    """
    Top-Level Entry. Liefert
        { "<playerId>": { firstName, lastName, price, pos, gender, tp, t, mp, img } }

    Cache-Strategie: liest erst Disk-Cache (TTL 10 min). Bei Cache-Miss
    (oder force=True) wird Auth + Fetch + Parse durchgeführt.

    Returns:
        Dict oder None (wenn `firebase_auth.json` fehlt — kein Crash, nur soft fail).

    Raises:
        RuntimeError bei Auth-Problemen oder Netzwerk-Fehlern, wenn ein
        Fetch eigentlich nötig wäre. Mit klarer User-Anleitung im Text.
    """
    if not force:
        cached = _load_cached_snapshot()
        if cached is not None:
            return cached

    auth = _load_auth()
    if auth is None:
        # Kein Setup — IMMER soft fail. Caller bestimmt selbst (z.B. über
        # ein dediziertes "must_succeed=True"-Flag, falls jemals nötig),
        # ob das Fehlen von Auth ein Fehler ist. Das macht das Skript
        # robust für User, die nur den lokalen Workflow nutzen wollen.
        return None

    current_year = current_season_year()
    id_token  = _refresh_id_token(auth["apiKey"], auth["refreshToken"])
    raw_doc   = _fetch_season_doc(id_token, year=current_year)
    if raw_doc is None:
        raise RuntimeError(
            f"season_stats/{current_year} existiert nicht in Firestore — "
            f"falls die Saison schon angelegt sein sollte, "
            f"setze CURRENT_SEASON_YEAR per env-var.")
    players   = parse_season_players(raw_doc)

    # Tournament-Price-Overlay: `season_stats.pr` ist nicht immer tagesaktuell,
    # die echte Wahrheit für das laufende/anstehende Turnier ist in
    # `tournaments/<doc>.players[].price`. Wir mergen das jetzt drüber.
    tour_players = fetch_current_tournament_players(id_token)
    if tour_players:
        n_changed = _overlay_tournament_prices(players, tour_players)
        if n_changed:
            print(f"  ✓ {n_changed} Preise via tournaments[]-Override aktualisiert.")

    _save_snapshot(players)
    # Spiegele das Roh-Doc nach data/players_season_<year>.json UND nach dem
    # legacy-Pfad data/players_season.json (für Backward-Compat mit dem
    # bestehenden Frontend, das nur den einen Pfad kennt). Beide Dateien sind
    # gitignored.
    for target_name in (f"players_season_{current_year}.json",
                         "players_season.json"):
        try:
            with open(DATA_DIR / target_name, "w", encoding="utf-8") as f:
                json.dump(raw_doc, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  WARNING: konnte data/{target_name} nicht schreiben: {e}",
                  file=sys.stderr)

    # Archive-Jahre einmalig nachholen — alle Jahre von EARLIEST_SEASON_YEAR
    # bis (current_year - 1). File-exists Check macht das idempotent: einmal
    # pro Jahr gefetched, danach ewig gecacht.
    for year in season_years():
        if year == current_year:
            continue   # wir haben das aktuelle Jahr gerade frisch geholt
        try:
            fetch_archive_season(year)
        except Exception as e:
            print(f"  WARNING: Archive-Fetch {year} fehlgeschlagen: {e}",
                  file=sys.stderr)

    return players


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Firestore-Saison-Snapshot holen")
    parser.add_argument("--force", action="store_true", help="Cache ignorieren")
    parser.add_argument("--print", action="store_true", help="Snapshot ausgeben")
    args = parser.parse_args()

    try:
        players = fetch_firestore_season(force=args.force)
    except RuntimeError as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        sys.exit(1)

    if players is None:
        print("Kein firebase_auth.json gefunden — nichts geholt.")
        sys.exit(0)

    age = snapshot_age_seconds()
    print(f"✓ {len(players)} Spieler im Snapshot (Alter: {age:.0f}s).")
    if args.print:
        for pid, p in sorted(players.items()):
            print(f"  {pid}  {p['firstName']:<14} {p['lastName']:<20} "
                  f"{p['pos']:<6} {p['gender']}  ₡{p['price']}  tp={p['tp']}")
