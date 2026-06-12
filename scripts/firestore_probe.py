"""
One-shot Firestore explorer: dumps a raw `tournaments/<id>` doc and probes
a handful of guess-paths (subcollections, sibling collections) to see what
per-tournament data exists beyond what `firestore_sync` currently parses.

Run:
    python scripts/firestore_probe.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_dotenv_files  # noqa: E402
load_dotenv_files()

from firestore_sync import (  # noqa: E402
    _load_auth, _refresh_id_token, _pick_current_tournament,
)

BASE = "https://firestore.googleapis.com/v1/projects/gbt-fantasy/databases/(default)/documents"


def auth_headers() -> dict:
    a = _load_auth()
    if not a:
        sys.exit("Keine Auth (FIREBASE_API_KEY/REFRESH_TOKEN oder firebase_auth.json fehlt).")
    tok = _refresh_id_token(a["apiKey"], a["refreshToken"])
    return {"Authorization": f"Bearer {tok}"}


def get(url: str, headers: dict) -> tuple[int, dict | str]:
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except Exception as e:
        return (-1, f"ERR {e}")
    try:
        body = r.json()
    except Exception:
        body = r.text
    return (r.status_code, body)


def field_keys(doc: dict) -> list[str]:
    return sorted((doc.get("fields") or {}).keys())


def summarize_player_entry(entry: dict) -> dict:
    """Extract field-keys of a single players[] map entry."""
    m = (entry.get("mapValue") or {}).get("fields") or {}
    return {k: list(v.keys())[0] if isinstance(v, dict) else type(v).__name__
            for k, v in m.items()}


def main():
    h = auth_headers()

    # 1. List tournaments docs
    print("=" * 70)
    print("1. tournaments/ — list")
    print("=" * 70)
    sc, body = get(f"{BASE}/tournaments?pageSize=100", h)
    if sc != 200:
        sys.exit(f"  tournaments list failed: {sc} {body}")
    docs = body.get("documents", [])
    print(f"  {len(docs)} tournaments docs found.")
    for d in docs:
        name = (d["fields"].get("name") or {}).get("stringValue", "?")
        doc_id = d["name"].rsplit("/", 1)[-1]
        keys = field_keys(d)
        print(f"    {doc_id:30s}  name={name!r:45s}  fields={keys}")

    chosen = _pick_current_tournament(docs)
    if chosen is None:
        sys.exit("Kein aktuelles Turnier gefunden — Abbruch.")
    doc_id = chosen["name"].rsplit("/", 1)[-1]
    name = (chosen["fields"].get("name") or {}).get("stringValue", "?")
    print(f"\n  -> Detail-Probe gegen: {doc_id} ({name!r})\n")

    # 2. Full raw doc dump
    print("=" * 70)
    print(f"2. tournaments/{doc_id} — raw doc (alle Felder)")
    print("=" * 70)
    print(f"  Top-level field keys: {field_keys(chosen)}")
    # Print full doc trimmed
    raw_path = Path(__file__).resolve().parent.parent / "data" / f"_probe_tournament_{doc_id}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(chosen, f, ensure_ascii=False, indent=2)
    print(f"  Vollständiges Doc gedumpt nach: {raw_path}")

    # 3. Probe players[] entries — what fields per player?
    print()
    print("=" * 70)
    print("3. tournaments/<id>.players[] — Felder pro Spieler-Eintrag")
    print("=" * 70)
    arr = (chosen["fields"].get("players") or {}).get("arrayValue", {}).get("values", [])
    print(f"  {len(arr)} Spieler-Einträge.")
    if arr:
        # Collect union of all field keys across all entries
        all_keys = set()
        for entry in arr:
            m = (entry.get("mapValue") or {}).get("fields") or {}
            all_keys.update(m.keys())
        print(f"  Union aller Feld-Keys: {sorted(all_keys)}")
        print(f"\n  Beispiel-Eintrag (erster):")
        print(json.dumps(arr[0], ensure_ascii=False, indent=2))

    # 4. Probe guessed subcollections / sibling paths
    print()
    print("=" * 70)
    print(f"4. Probe weitere Pfade für tournament {doc_id}")
    print("=" * 70)
    candidates = [
        # Subcollections under tournaments/<id>
        f"tournaments/{doc_id}/results",
        f"tournaments/{doc_id}/players",
        f"tournaments/{doc_id}/scores",
        f"tournaments/{doc_id}/matches",
        f"tournaments/{doc_id}/stats",
        f"tournaments/{doc_id}/standings",
        # Sibling top-level collections
        f"tournament_results/{doc_id}",
        f"tournament_stats/{doc_id}",
        f"tournament_scores/{doc_id}",
        f"results/{doc_id}",
        f"scores/{doc_id}",
        f"standings/{doc_id}",
        f"player_scores/{doc_id}",
        # Maybe results live under season_stats
        f"season_stats/2026/tournaments/{doc_id}",
        f"season_stats/2026/results/{doc_id}",
    ]
    for path in candidates:
        sc, body = get(f"{BASE}/{path}?pageSize=5", h)
        marker = "OK" if sc == 200 else ("!" if sc == 404 else "?")
        snippet = ""
        if sc == 200 and isinstance(body, dict):
            if "documents" in body:
                snippet = f"  -> {len(body['documents'])} docs"
                if body["documents"]:
                    first = body["documents"][0]
                    snippet += f", first fields: {field_keys(first)}"
            elif "fields" in body:
                snippet = f"  -> fields: {field_keys(body)}"
        print(f"  {marker} {sc}  {path}{snippet}")

    # 4b. Dump full stats subcollection
    print()
    print("=" * 70)
    print(f"4b. tournaments/{doc_id}/stats — Doc-IDs + Beispiel-Eintrag")
    print("=" * 70)
    sc, body = get(f"{BASE}/tournaments/{doc_id}/stats?pageSize=20", h)
    if sc == 200 and isinstance(body, dict):
        stats_docs = body.get("documents", [])
        for sd in stats_docs:
            sid = sd["name"].rsplit("/", 1)[-1]
            print(f"    doc_id={sid}  fields={field_keys(sd)}")
        if stats_docs:
            dump_path = Path(__file__).resolve().parent.parent / "data" / f"_probe_stats_{doc_id}.json"
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(stats_docs, f, ensure_ascii=False, indent=2)
            print(f"  Gedumpt nach: {dump_path}")
            print(f"\n  Beispiel (erster Eintrag):")
            print(json.dumps(stats_docs[0], ensure_ascii=False, indent=2)[:3000])

    # 5. List root collections (Firestore lets you list via the special listCollectionIds RPC,
    #    but plain REST also exposes them through the documents listing)
    print()
    print("=" * 70)
    print("5. Root-Collections")
    print("=" * 70)
    sc, body = get(f"{BASE}:listCollectionIds", h)
    if sc == 200 and isinstance(body, dict):
        ids = body.get("collectionIds", [])
        print(f"  Root collections: {ids}")
    else:
        # Fallback: try POST as documented
        try:
            r = requests.post(f"{BASE}:listCollectionIds", headers=h, json={}, timeout=15)
            print(f"  POST status: {r.status_code}")
            try:
                body = r.json()
                print(f"  collectionIds: {body.get('collectionIds', [])}")
            except Exception:
                print(f"  body: {r.text[:300]}")
        except Exception as e:
            print(f"  Fehler: {e}")

    # 6. List subcollections of tournaments/<id>
    print()
    print("=" * 70)
    print(f"6. Sub-Collections von tournaments/{doc_id}")
    print("=" * 70)
    try:
        r = requests.post(
            f"{BASE}/tournaments/{doc_id}:listCollectionIds",
            headers=h, json={}, timeout=15,
        )
        print(f"  status: {r.status_code}")
        try:
            body = r.json()
            print(f"  collectionIds: {body.get('collectionIds', [])}")
        except Exception:
            print(f"  body: {r.text[:300]}")
    except Exception as e:
        print(f"  Fehler: {e}")


if __name__ == "__main__":
    main()
