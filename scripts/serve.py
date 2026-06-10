#!/usr/bin/env python3
"""
Drop-in replacement for `python -m http.server` that also exposes simulation API.

Usage:
  python scripts/serve.py [port]

Endpoints:
  GET  /api/sim-status          → {exists, fresh, age_s, ...}
  POST /api/simulate?gender=m   → runs simulation for gender, returns when done
  POST /api/simulate?gender=all → runs for both genders sequentially

Static files are served from the repo root (one level up from this file).
"""

import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
from pathlib import Path

# Allow importing from same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import load_dotenv_files  # noqa: E402
load_dotenv_files()
from simulate_tournament import (
    run_simulation, SIM_OUTPUT, PLAYERS_AVL, players_available_hash
)

ROOT = Path(__file__).resolve().parent.parent
# Port resolution (CLI arg → env → default). Fly.io sets $PORT to 8080.
def _resolve_port() -> int:
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        return int(sys.argv[1])
    env_p = os.environ.get("PORT", "").strip()
    if env_p.isdigit():
        return int(env_p)
    return 8000
PORT = _resolve_port()


# Single-flight: never run two simulations at once
_sim_lock = threading.Lock()


def _read_sim() -> dict | None:
    if not SIM_OUTPUT.exists():
        return None
    try:
        with open(SIM_OUTPUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sim_status() -> dict:
    sim = _read_sim()
    if not sim:
        return {"exists": False, "fresh": False}
    cur_hash = players_available_hash()
    stored_hash = sim.get("playersAvailableHash", "")
    age_s = int(time.time() - sim.get("lastRunAt", 0))
    fresh = (cur_hash == stored_hash) and age_s < 6 * 3600  # 6 h freshness window
    return {
        "exists": True,
        "fresh": fresh,
        "age_s": age_s,
        "playersHashMatch": cur_hash == stored_hash,
        "tournaments": {
            g: {
                "name":   blk.get("tournamentName"),
                "id":     blk.get("tournamentId"),
                "status": blk.get("bracketStatus"),
            }
            for g, blk in sim.get("byGender", {}).items()
        },
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # quieter logs
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/sim-status"):
            self._send_json(200, sim_status())
            return
        # Files: prevent caching of the sim file so frontend always re-reads after API call
        if self.path.endswith("tournament_sim.json"):
            super().do_GET()
            return
        super().do_GET()

    def end_headers(self):
        # Disable caching for assets that change during dev (everything served by us)
        # — prevents stale app.js / index.html / json after edits
        if any(self.path.endswith(ext) for ext in (".js", ".html", ".json", ".css")) \
           or self.path.endswith("/") or "?" in self.path:
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_POST(self):
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)

        # ── Bulk-update prices in players_available.json ──
        if path == "/api/set-prices":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                updates = body.get("prices", [])
                if not isinstance(updates, list):
                    raise ValueError("prices must be a list")
            except Exception as e:
                self._send_json(400, {"error": f"invalid body: {e}"})
                return

            avl_file = PLAYERS_AVL
            try:
                with open(avl_file, encoding="utf-8") as f:
                    entries = json.load(f)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return

            update_map = {u["name"].strip(): int(u["price"]) for u in updates if "name" in u and "price" in u}
            applied = 0
            for e in entries:
                nm = e.get("name", "").strip()
                if nm in update_map:
                    e["price"] = update_map[nm]
                    applied += 1

            with open(avl_file, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)

            self._send_json(200, {"ok": True, "updated": applied})
            return

        # ── Swap one ambiguous player in players_available.json ──
        if path == "/api/swap-player":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                old_name = body["from"].strip()
                new_name = body["to"].strip()
            except Exception as e:
                self._send_json(400, {"error": f"invalid body: {e}"})
                return

            avl_file = PLAYERS_AVL
            try:
                with open(avl_file, encoding="utf-8") as f:
                    entries = json.load(f)
            except Exception as e:
                self._send_json(500, {"error": f"could not read players_available.json: {e}"})
                return

            # Find both indices up front. If `new_name` is already in the list
            # (e.g. the same last name occurs twice in the bracket and the other
            # slot is currently assigned to this player), we must SWAP names —
            # not blindly rename — otherwise the file ends up with two identical
            # entries and the other slot silently "follows" the change.
            from_idx = None
            to_idx   = None
            for i, e in enumerate(entries):
                nm = e.get("name", "").strip()
                if from_idx is None and nm == old_name:
                    from_idx = i
                if to_idx is None and nm == new_name:
                    to_idx = i

            if from_idx is None:
                self._send_json(404, {"error": f"player '{old_name}' not in available list"})
                return

            swapped = (to_idx is not None and to_idx != from_idx)
            if swapped:
                entries[from_idx]["name"] = new_name
                entries[to_idx]["name"]   = old_name
            else:
                entries[from_idx]["name"] = new_name

            with open(avl_file, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)

            # Patch tournament_sim.json so the chosen field reflects the user's pick.
            # On a swap, mirror it in `ambiguous` as well — change the chosen slot
            # from old→new AND the other slot (which had new) from new→old. Use a
            # two-pass approach so we don't ping-pong values within one loop.
            try:
                if SIM_OUTPUT.exists():
                    with open(SIM_OUTPUT, encoding="utf-8") as f:
                        sim = json.load(f)
                    amb = sim.get("syncInfo", {}).get("ambiguous", [])
                    updates = []
                    for i, a in enumerate(amb):
                        ch = a.get("chosen")
                        if ch == old_name:
                            updates.append((i, new_name))
                        elif swapped and ch == new_name:
                            updates.append((i, old_name))
                    for i, new_chosen in updates:
                        amb[i]["chosen"] = new_chosen
                    with open(SIM_OUTPUT, "w", encoding="utf-8") as f:
                        json.dump(sim, f, ensure_ascii=False, indent=2)
            except Exception:
                pass  # non-fatal

            self._send_json(200, {"ok": True, "from": old_name, "to": new_name, "swapped": swapped})
            return

        # ── Force-refresh the Firestore snapshot + re-sync players_available ──
        if path == "/api/firestore-sync":
            import firestore_sync
            from simulate_tournament import sync_players_available_from_brackets

            # If auth is missing, fail loudly so the frontend can guide the user.
            if firestore_sync._load_auth() is None:
                self._send_json(401, {
                    "error": "data/firebase_auth.json fehlt",
                    "hint": "Snippet aus fetch_auth_token.txt in der DevTools-Konsole "
                            "von https://gbt-fantasy.web.app/ ausführen und die "
                            "heruntergeladene Datei nach data/ verschieben.",
                })
                return

            force = params.get("force", ["1"])[0] in ("1", "true", "True")
            try:
                players = firestore_sync.fetch_firestore_season(force=force)
            except RuntimeError as e:
                self._send_json(401, {"error": str(e)})
                return

            # Re-sync players_available.json so prices flow into the file the
            # frontend reads. `force=False` here means "use the snapshot we
            # just refreshed" — we don't need ANOTHER round-trip.
            try:
                info = sync_players_available_from_brackets(force=False)
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send_json(500, {"error": f"re-sync fehlgeschlagen: {e}"})
                return

            self._send_json(200, {
                "ok": True,
                "players_in_snapshot": len(players or {}),
                "snapshot_age_s": int(firestore_sync.snapshot_age_seconds() or 0),
                "prices_changed": info.get("prices_changed", []),
                "added":          info.get("added", []),
                "removed":        info.get("removed", []),
                "pending":        info.get("pending", []),
            })
            return

        if path != "/api/simulate":
            self._send_json(404, {"error": "not found"})
            return

        gender = params.get("gender", ["m"])[0]
        if gender not in ("m", "f", "all"):
            self._send_json(400, {"error": "gender must be m | f | all"})
            return

        force = params.get("force", ["0"])[0] in ("1", "true", "True")
        include_quali = params.get("qualifiers", ["0"])[0] in ("1", "true", "True")
        sims = int(params.get("simulations", ["20000"])[0])

        if not _sim_lock.acquire(blocking=False):
            self._send_json(409, {"error": "simulation already running"})
            return

        try:
            t0 = time.time()
            genders = ["m", "f"] if gender == "all" else [gender]
            for g in genders:
                run_simulation(gender=g, simulations=sims,
                               include_qualifiers=include_quali,
                               force_refresh=force)
            self._send_json(200, {
                "ok": True,
                "genders": genders,
                "duration_s": round(time.time() - t0, 2),
                "status": sim_status(),
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            self._send_json(500, {"error": str(e)})
        finally:
            _sim_lock.release()


class ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print(f"Serving GBT Fantasy Optimizer on http://localhost:{PORT}")
    print(f"  Static root: {ROOT}")
    print(f"  API: GET /api/sim-status , POST /api/simulate?gender=m|f|all")
    print(f"  (Ctrl-C to stop)")
    with ReusableServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping…")
