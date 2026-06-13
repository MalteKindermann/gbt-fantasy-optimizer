"""
Smart incremental refresh of the rating system.

Driven by the UI "Aktualisieren" button (`POST /api/elo-refresh`). Goal:
keep ratings up-to-date with minimal network spend, since old tournaments
never change.

Strategy in three layers:

  1. **Re-discover** current saison/year on both gender × both sources
     (DVV + bvbinfo).  Cheap: 4-6 HTTPs total.

  2. **Identify deltas**:
     - **NEW**: tournament IDs that weren't in `_discovered.json` before
     - **RECENT**: tournaments whose end_date is within the last 21 days
       (might have late-arriving match results worth re-checking)
     - Everything else: skip (cached forever).

  3. **Targeted fetch** of just the delta tournaments. Then run the offline
     build only if matches.csv row count actually changed.

Typical "nothing new" cost: ~5 HTTPs + ~10s + no rebuild.
Typical "weekend tournament finished" cost: ~5-20 HTTPs + 6-8min build.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir  # noqa: E402

from elo import scraper as sc        # noqa: E402
from elo import scraper_bvb as bvb   # noqa: E402
from elo import build_ratings as br  # noqa: E402

DATA = data_dir()

# Tournaments that ended within this many days get a force-refresh
RECENT_DAYS = 21


def _today() -> _dt.date:
    return _dt.date.today()


def _current_saison() -> int:
    """DVV two-digit saison code (e.g. 26 for 2026)."""
    return _today().year % 100


def _current_year() -> int:
    return _today().year


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _matches_csv_rowcount() -> int:
    if not br.MATCHES_CSV.exists():
        return 0
    with open(br.MATCHES_CSV, encoding="utf-8") as f:
        return sum(1 for _ in f) - 1   # minus header


def _recent_cutoff_iso() -> str:
    cutoff = _today() - _dt.timedelta(days=RECENT_DAYS)
    return cutoff.isoformat()


def _is_recent_dvv(entry: dict) -> bool:
    end = entry.get("date_end") or entry.get("date_start") or ""
    return end >= _recent_cutoff_iso() and end <= _today().isoformat()


def _is_recent_bvb(entry: dict) -> bool:
    d = entry.get("date_iso") or ""
    return d >= _recent_cutoff_iso() and d <= _today().isoformat()


def smart_refresh(status_cb: Optional[Callable[[str, str, dict], None]] = None) -> dict:
    """Run a smart delta refresh. Returns a summary dict.

    `status_cb(phase, message, extras)` is invoked at each major step.  The
    HTTP layer in serve.py uses this to update the polling endpoint.
    """
    def cb(phase: str, msg: str, **extras):
        if status_cb:
            status_cb(phase, msg, extras)

    summary = {
        "started_at": _today().isoformat(),
        "new_tournaments_dvv": 0,
        "new_tournaments_bvb": 0,
        "recent_refreshed_dvv": 0,
        "recent_refreshed_bvb": 0,
        "matches_before": _matches_csv_rowcount(),
        "matches_after": None,
        "rebuilt": False,
        "error": None,
    }

    saison = _current_saison()
    year   = _current_year()

    # ── Step 1: snapshot ids before re-discover ──────────────────────────────
    dvv_existing = {e["id"] for e in _load_json(br.DISCOVERED_JSON, [])}
    bvb_existing = {e["tournament_id"]
                    for e in _load_json(br.BVB_DISCOVERED_JSON, [])}

    # ── Step 2: re-discover current saison/year on both sources × genders ──
    cb("discovering", f"DVV-Discover Saison {saison} (M+F)…")
    try:
        br.phase_discover([saison], "m")
        br.phase_discover([saison], "f")
    except Exception as e:
        summary["error"] = f"DVV discover failed: {e}"
        return summary

    cb("discovering", f"bvb-Discover {year} (M+F)…")
    try:
        br.phase_bvb_discover([year], "m")
        br.phase_bvb_discover([year], "f")
    except Exception as e:
        summary["error"] = f"bvb discover failed: {e}"
        return summary

    # ── Step 3: identify deltas ──────────────────────────────────────────────
    dvv_all = _load_json(br.DISCOVERED_JSON, [])
    bvb_all = _load_json(br.BVB_DISCOVERED_JSON, [])

    dvv_new = [e for e in dvv_all if e["id"] not in dvv_existing]
    bvb_new = [e for e in bvb_all if e["tournament_id"] not in bvb_existing]

    dvv_recent = [e for e in dvv_all
                  if e["id"] in dvv_existing and _is_recent_dvv(e)]
    bvb_recent = [e for e in bvb_all
                  if e["tournament_id"] in bvb_existing and _is_recent_bvb(e)]

    summary["new_tournaments_dvv"] = len(dvv_new)
    summary["new_tournaments_bvb"] = len(bvb_new)
    summary["recent_refreshed_dvv"] = len(dvv_recent)
    summary["recent_refreshed_bvb"] = len(bvb_recent)

    cb("fetching",
       f"Neue Turniere: DVV {len(dvv_new)}, bvb {len(bvb_new)} | "
       f"Recent: DVV {len(dvv_recent)}, bvb {len(bvb_recent)}")

    # ── Step 4: targeted fetch ───────────────────────────────────────────────
    # DVV: phase_tournaments rebuilds _match_stubs.json from ALL discovered.
    # We force-refresh per-tournament spielplan files for NEW + RECENT IDs.
    target_dvv_ids = {e["id"] for e in dvv_new + dvv_recent}
    if target_dvv_ids:
        cb("fetching", f"DVV-Spielpläne ({len(target_dvv_ids)} Turniere)…")
        for tid in target_dvv_ids:
            for feld in (1, 2):
                try:
                    sc.fetch_spielplan(tid, feld=feld, force=True)
                except Exception:
                    pass   # keep going on per-tournament errors

    # Then rebuild the consolidated stubs file from disk caches
    cb("fetching", "DVV-Stubs konsolidieren…")
    try:
        br.phase_tournaments()
    except Exception as e:
        summary["error"] = f"phase_tournaments failed: {e}"
        return summary

    # Teams phase (resolves any new team IDs that surfaced)
    cb("fetching", "DVV-Teams auflösen…")
    try:
        br.phase_teams()
    except Exception as e:
        summary["error"] = f"phase_teams failed: {e}"
        return summary

    # bvb: force-refetch per-tournament MatchResults for NEW + RECENT
    target_bvb = [(e["tournament_id"], e["year"]) for e in bvb_new + bvb_recent]
    if target_bvb:
        cb("fetching", f"bvb-Matches ({len(target_bvb)} Turniere)…")
        for tid, yr in target_bvb:
            try:
                bvb.fetch_tournament_matches(tid, yr, force=True)
            except Exception:
                pass

    # ── Step 5: rebuild only if matches.csv would change ─────────────────────
    # Cheap way: re-consolidate without writing CSV, count rows
    cb("checking", "Prüfe ob neue Matches entstanden sind…")
    try:
        records = br.get_consolidated_records(force_reload=True)
    except Exception as e:
        summary["error"] = f"consolidate failed: {e}"
        return summary
    new_rowcount = len(records)
    summary["matches_after"] = new_rowcount

    if new_rowcount == summary["matches_before"]:
        cb("done", f"Keine neuen Matches. Datenstand bleibt bei {new_rowcount}.")
        return summary

    # ── Step 6: full rebuild (writes matches.csv + 4 model JSONs + meta) ────
    delta = new_rowcount - summary["matches_before"]
    cb("building", f"{delta:+d} neue Matches → baue alle 4 Modelle neu…")
    try:
        br.phase_build()
    except Exception as e:
        summary["error"] = f"phase_build failed: {e}"
        return summary
    summary["rebuilt"] = True
    cb("done", f"Fertig. {delta:+d} neue Matches, Modelle aktualisiert.")
    return summary


if __name__ == "__main__":
    def _print(phase, msg, extras):
        print(f"[{phase}] {msg}")
    result = smart_refresh(_print)
    print()
    print(json.dumps(result, indent=2))
