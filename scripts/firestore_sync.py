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

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent.parent
DATA_DIR   = ROOT / "data"
AUTH_FILE  = DATA_DIR / "firebase_auth.json"
CACHE_DIR  = DATA_DIR / ".cache"
CACHE_FILE = CACHE_DIR / "firestore_season.json"

# ── Endpoints ─────────────────────────────────────────────────────────────────
SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token?key={api_key}"
FIRESTORE_URL    = ("https://firestore.googleapis.com/v1/projects/gbt-fantasy/"
                    "databases/(default)/documents/season_stats/2026")

# ── TTLs ──────────────────────────────────────────────────────────────────────
SNAPSHOT_TTL_SECONDS = 600   # 10 min — Preise ändern sich relativ selten
ID_TOKEN_TTL_SECONDS = 50 * 60   # 50 min, kleiner als Firebase's 60-min Gültigkeit

# In-Memory Cache für ID-Token (überlebt nur den laufenden Prozess)
_id_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _load_auth() -> dict | None:
    """Liest data/firebase_auth.json. Returnt None wenn nicht da."""
    if not AUTH_FILE.exists():
        return None
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("apiKey") or not data.get("refreshToken"):
            return None
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


def _fetch_season_doc(id_token: str) -> dict:
    """GET das Firestore-Dokument. Returnt das Roh-JSON."""
    try:
        r = requests.get(
            FIRESTORE_URL,
            headers={"Authorization": f"Bearer {id_token}"},
            timeout=20,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Netzwerk-Fehler beim Firestore-Fetch: {e}") from e

    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message", r.text)
        except Exception:
            err = r.text
        raise RuntimeError(
            f"Firestore antwortete mit {r.status_code}: {err}. "
            f"Eventuell ID-Token expired oder Berechtigungen nicht ausreichend."
        )
    return r.json()


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

    id_token  = _refresh_id_token(auth["apiKey"], auth["refreshToken"])
    raw_doc   = _fetch_season_doc(id_token)
    players   = parse_season_players(raw_doc)
    _save_snapshot(players)
    # Also mirror the raw Firestore document to data/players_season.json, so
    # the frontend's loadSeasonOverlay can pick up new players (e.g. rookies
    # like Milan Sievers who only exist in Firestore, not in players_all.json).
    try:
        with open(DATA_DIR / "players_season.json", "w", encoding="utf-8") as f:
            json.dump(raw_doc, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  WARNING: konnte data/players_season.json nicht schreiben: {e}",
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
