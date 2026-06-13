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

try:
    import jwt as _pyjwt  # pyjwt
except ImportError:
    _pyjwt = None

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


def _ensure_runtime_stubs() -> None:
    """
    Create empty stub data files at startup if missing so the frontend
    (which fetches them directly) never sees a 404 on first launch.
    Real content is populated by the first sim / Firestore-sync run.
    """
    if not PLAYERS_AVL.exists():
        PLAYERS_AVL.parent.mkdir(parents=True, exist_ok=True)
        with open(PLAYERS_AVL, "w", encoding="utf-8") as f:
            f.write("[]\n")
        print(f"  Created empty {PLAYERS_AVL.name} stub.")


_ensure_runtime_stubs()


# Single-flight: never run two simulations at once
_sim_lock = threading.Lock()

# Single-flight + shared status for the smart ELO refresh
_elo_refresh_lock = threading.Lock()
_elo_refresh_status: dict = {
    "phase": "idle",            # 'idle'|'discovering'|'fetching'|'checking'|'building'|'done'|'error'
    "message": "",
    "started_at": None,
    "finished_at": None,
    "summary": None,            # final smart_refresh() return value
}


# ── Auth ──────────────────────────────────────────────────────────────────────
# Supabase JWT verification is opt-in via the SUPABASE_URL env-var.
# Unset → auth disabled (local/self-host default). Set → every /api/* and
# /data/* request must carry a valid Bearer token, verified via the project's
# JWKS endpoint (asymmetric signing keys, the default since 2025).

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
_AUTH_ENABLED = bool(_SUPABASE_URL)

if _AUTH_ENABLED and _pyjwt is None:
    print("[fatal] SUPABASE_URL is set but PyJWT is not installed. "
          "Run: pip install -r scripts/requirements.txt", file=sys.stderr)
    sys.exit(1)

_jwks_client = None
if _AUTH_ENABLED:
    # PyJWKClient caches fetched keys (default 5 min lifespan). The JWKS
    # endpoint exposes the project's asymmetric signing keys (typically ES256).
    _jwks_url = f"{_SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    _jwks_client = _pyjwt.PyJWKClient(_jwks_url, lifespan=600)

# Paths that never require auth — needed so the login screen can load itself.
_PUBLIC_PREFIXES = ("/index.html", "/app.js", "/styles.css", "/config.js", "/favicon")

# Role hierarchy. Higher number = more permissions. Unknown roles fall back to
# `elo_viewer` (the safest default for an authenticated user with no claim).
_ROLE_ORDER = {"elo_viewer": 0, "elo_lab": 1, "admin": 2}
_DEFAULT_ROLE = "elo_viewer"


def _role_at_least(have: str | None, need: str) -> bool:
    if have is None:
        return False
    return _ROLE_ORDER.get(have, -1) >= _ROLE_ORDER[need]


def _path_is_public(path: str) -> bool:
    p = path.split("?", 1)[0]
    if p == "/" or p == "":
        return True
    return any(p == pfx or p.startswith(pfx) for pfx in _PUBLIC_PREFIXES)


def _extract_role(auth_header: str) -> str | None:
    """Return the caller's role, or None for invalid/missing token.

    Self-host mode (auth disabled) short-circuits to "admin" so every
    `_require_role` check transparently passes."""
    if not _AUTH_ENABLED:
        return "admin"
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(None, 1)[1].strip()
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = _pyjwt.decode(
            token, signing_key.key,
            algorithms=["ES256", "RS256", "HS256"],
            audience="authenticated",
        )
    except Exception:
        return None
    raw = (payload.get("app_metadata") or {}).get("role")
    return raw if raw in _ROLE_ORDER else _DEFAULT_ROLE


# Min-role required to read each file under /data/. Most are admin-only; the
# ELO snapshots are visible to anyone authenticated.
_ELO_DATA_NEEDLES = ("elo_", "_current.json", "elo_models_meta", "elo_aliases")


def _data_path_min_role(path: str) -> str:
    p = path.split("?", 1)[0].lower()
    name = p.rsplit("/", 1)[-1]
    if any(n in name for n in _ELO_DATA_NEEDLES):
        return "elo_viewer"
    return "admin"


# ── Cloud Run Jobs integration ───────────────────────────────────────────────
# When ELO_REFRESH_JOB_NAME is set (Cloud Run deploy), /api/elo-refresh fires
# the named Cloud Run Job instead of running smart_refresh() in-process. The
# job uses the same Docker image but runs with 2 GiB RAM / 1 h timeout so it
# can complete a full rebuild without hitting the Service request-timeout.
# Self-host leaves the env-var unset and keeps the in-process path.

_ELO_JOB_NAME = os.environ.get("ELO_REFRESH_JOB_NAME", "").strip()
# e.g. "projects/gbt-fantasy-optimizer/locations/us-central1/jobs/elo-rebuild"

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


def _metadata_token() -> str:
    import urllib.request
    req = urllib.request.Request(_METADATA_TOKEN_URL,
                                 headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["access_token"]


def _run_job_api(method: str, url: str, body: dict | None = None) -> dict:
    import urllib.request
    headers = {
        "Authorization": f"Bearer {_metadata_token()}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _job_execution_to_phase(exec_doc: dict) -> tuple[str, str, dict | None]:
    """Map a Cloud Run Jobs Execution document to (phase, message, summary).

    Phase vocabulary matches the in-process refresh status so the UI doesn't
    need to know the difference. Summary is set only on completion."""
    conds = exec_doc.get("conditions", []) or []
    completed = next((c for c in conds if c.get("type") == "Completed"), None)
    if completed:
        state = completed.get("state", "")
        if state == "CONDITION_SUCCEEDED":
            return "done", "Job abgeschlossen.", {"rebuilt": True}
        if state == "CONDITION_FAILED":
            return "error", completed.get("message") or "Job fehlgeschlagen.", None
    # Not yet completed.
    started = next((c for c in conds if c.get("type") == "Started"), None)
    if started and started.get("state") == "CONDITION_SUCCEEDED":
        return "building", "Job läuft …", None
    return "discovering", "Job wird gestartet …", None


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
        if not self._require_auth_or_401():
            return
        if self.path.startswith("/api/sim-status"):
            if not self._require_role("admin"):
                return
            self._send_json(200, sim_status())
            return

        # ── List available rating models for the UI dropdown ──
        if self.path.startswith("/api/elo-models"):
            if not self._require_role("elo_viewer"):
                return
            try:
                from elo import models as elo_models
                self._send_json(200, {"models": elo_models.available_models()})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # ── ELO smart-refresh status (polled by the UI button) ──
        if self.path.startswith("/api/elo-refresh-status"):
            if not self._require_role("elo_viewer"):
                return
            # Cloud mode: if an execution is in-flight, poll the Jobs API and
            # mirror its state onto our existing status dict so the UI doesn't
            # care which path produced it.
            exec_name = _elo_refresh_status.get("execution_name")
            if (_ELO_JOB_NAME and exec_name
                    and _elo_refresh_status.get("phase") not in ("done", "error")):
                try:
                    exec_doc = _run_job_api(
                        "GET", f"https://run.googleapis.com/v2/{exec_name}")
                    phase, message, summary = _job_execution_to_phase(exec_doc)
                    _elo_refresh_status["phase"] = phase
                    _elo_refresh_status["message"] = message
                    if phase in ("done", "error"):
                        import datetime as _dt
                        _elo_refresh_status["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
                        if summary is not None and not _elo_refresh_status.get("summary"):
                            _elo_refresh_status["summary"] = summary
                except Exception as e:
                    # Polling failure is non-fatal — keep the last known phase.
                    _elo_refresh_status["message"] = f"poll error: {e}"
            self._send_json(200, dict(_elo_refresh_status))
            return

        # ── Slider schema per model for the tuning tab ──
        if self.path.startswith("/api/elo-model-schema"):
            if not self._require_role("elo_viewer"):
                return
            _, _, query = self.path.partition("?")
            params = urllib.parse.parse_qs(query)
            model_id = params.get("model", ["elo"])[0]
            try:
                from elo import models as elo_models
                model = elo_models.make_model(model_id)
                self._send_json(200, {
                    "model": model_id,
                    "name": model.name,
                    "sliders": type(model).slider_spec(),
                })
            except Exception as e:
                self._send_json(400, {"error": str(e)})
            return

        # Gate static file reads under /data/ by role.
        p = self.path.split("?", 1)[0]
        if p.startswith("/data/"):
            if not self._require_role(_data_path_min_role(self.path)):
                return

        # Files: prevent caching of the sim file so frontend always re-reads after API call
        if self.path.endswith("tournament_sim.json"):
            super().do_GET()
            return
        super().do_GET()

    def end_headers(self):
        # CORS — required when frontend (Vercel) and backend (Fly) are on
        # different origins. Defaults to "*" for local/self-host.
        origin = os.environ.get("CORS_ALLOW_ORIGIN", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if origin != "*":
            self.send_header("Vary", "Origin")
        # Disable caching for assets that change during dev (everything served by us)
        # — prevents stale app.js / index.html / json after edits
        if any(self.path.endswith(ext) for ext in (".js", ".html", ".json", ".css")) \
           or self.path.endswith("/") or "?" in self.path:
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_OPTIONS(self):
        # CORS preflight — no body, no auth.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_auth_or_401(self) -> bool:
        """Return True if request is allowed to proceed. Sends 401 itself otherwise.

        Stores the caller's role in `self.request_role` for downstream
        `_require_role` checks. Self-host mode → "admin"."""
        if _path_is_public(self.path):
            self.request_role = "admin"
            return True
        role = _extract_role(self.headers.get("Authorization", ""))
        if role is None:
            self._send_json(401, {"error": "auth required"})
            return False
        self.request_role = role
        return True

    def _require_role(self, min_role: str) -> bool:
        """Return True if `self.request_role` meets `min_role`, else send 403."""
        have = getattr(self, "request_role", None)
        if _role_at_least(have, min_role):
            return True
        self._send_json(403, {
            "error": "forbidden",
            "role": have,
            "required": min_role,
        })
        return False

    def do_POST(self):
        if not self._require_auth_or_401():
            return
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)

        # ── Bulk-update prices in players_available.json ──
        if path == "/api/set-prices":
            if not self._require_role("admin"):
                return
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
            if not self._require_role("admin"):
                return
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
            if not self._require_role("admin"):
                return
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

        # ── Recompute a rating model with custom hyperparameters (sandbox) ──
        if path == "/api/elo-recompute":
            if not self._require_role("elo_lab"):
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception as e:
                self._send_json(400, {"error": f"invalid body: {e}"})
                return

            try:
                from elo import models as elo_models
                from elo import runner as elo_runner
                from elo import build_ratings as elo_build
            except Exception as e:
                self._send_json(500, {"error": f"elo modules not importable: {e}"})
                return

            model_id = body.pop("model", "elo")
            train_end_date = body.pop("train_end_date", None) or None

            try:
                model = elo_models.make_model(model_id, body)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send_json(400, {"error": f"bad config override: {e}"})
                return

            t0 = time.time()
            try:
                records = elo_build.get_consolidated_records()
                run = elo_runner.run_model(records, model,
                                           train_end_date=train_end_date)
                players = elo_runner.build_player_export(run)
            except Exception as e:
                import traceback; traceback.print_exc()
                self._send_json(500, {"error": str(e)})
                return

            in_acc = (run.in_sample_correct / run.in_sample_total
                      if run.in_sample_total else None)
            oos_acc = (run.oos_correct / run.oos_total
                       if run.oos_total else None)
            def _calib(b):
                return [{"bucket_lo": k / 10, "n": len(v),
                         "predicted": (k / 10) + 0.05,
                         "actual": sum(v) / len(v) if v else None}
                        for k, v in sorted(b.items())]

            # Effective config: only export fields that exist on the model's cfg
            cfg = getattr(model, "cfg", None)
            cfg_dict = ({k: getattr(cfg, k)
                         for k in cfg.__dataclass_fields__}
                        if cfg is not None and hasattr(cfg, "__dataclass_fields__")
                        else {})

            self._send_json(200, {
                "ok": True,
                "model": model_id,
                "config": cfg_dict,
                "train_end_date": train_end_date,
                "n_matches": len(records),
                "n_players": len(players),
                "in_sample": {
                    "n": run.in_sample_total,
                    "correct": run.in_sample_correct,
                    "accuracy": in_acc,
                    "calibration": _calib(run.in_sample_calib),
                },
                "oos": {
                    "n": run.oos_total,
                    "correct": run.oos_correct,
                    "accuracy": oos_acc,
                    "calibration": _calib(run.oos_calib),
                } if train_end_date else None,
                "duration_s": round(time.time() - t0, 2),
                "players": players,
            })
            return

        # ── Smart ELO refresh: cloud → fire Cloud Run Job; local → in-proc thread ──
        if path == "/api/elo-refresh":
            if not self._require_role("elo_lab"):
                return
            if not _elo_refresh_lock.acquire(blocking=False):
                self._send_json(409, {
                    "error": "refresh already running",
                    "status": dict(_elo_refresh_status),
                })
                return

            import datetime as _dt
            _elo_refresh_status["started_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            _elo_refresh_status["finished_at"] = None
            _elo_refresh_status["summary"] = None

            if _ELO_JOB_NAME:
                # Cloud mode: trigger a Cloud Run Job execution.
                try:
                    url = f"https://run.googleapis.com/v2/{_ELO_JOB_NAME}:run"
                    resp = _run_job_api("POST", url, body={})
                except Exception as e:
                    _elo_refresh_lock.release()
                    import traceback; traceback.print_exc()
                    self._send_json(500, {"error": f"job trigger failed: {e}"})
                    return
                # The :run response is a long-running Operation. The execution
                # we'll poll lives at metadata.name (operation name has format
                # "projects/…/jobs/…/executions/…").
                exec_name = (resp.get("metadata") or {}).get("name") or resp.get("name", "")
                if exec_name.endswith("/operations/…"):
                    exec_name = ""
                _elo_refresh_status["execution_name"] = exec_name
                _elo_refresh_status["phase"] = "discovering"
                _elo_refresh_status["message"] = "Job wird gestartet …"
                _elo_refresh_lock.release()  # the long work happens out-of-process
                self._send_json(202, {
                    "ok": True,
                    "status": dict(_elo_refresh_status),
                    "hint": "poll GET /api/elo-refresh-status until phase in {done,error}",
                })
                return

            # Local mode: spawn in-process worker as before.
            try:
                from elo import refresh as elo_refresh
            except Exception as e:
                _elo_refresh_lock.release()
                self._send_json(500, {"error": f"refresh module: {e}"})
                return

            def _status_cb(phase: str, message: str, extras: dict) -> None:
                _elo_refresh_status["phase"] = phase
                _elo_refresh_status["message"] = message

            def _worker() -> None:
                _elo_refresh_status["phase"] = "discovering"
                _elo_refresh_status["message"] = "starte…"
                try:
                    result = elo_refresh.smart_refresh(_status_cb)
                    _elo_refresh_status["summary"] = result
                    if result.get("error"):
                        _elo_refresh_status["phase"] = "error"
                        _elo_refresh_status["message"] = result["error"]
                    else:
                        _elo_refresh_status["phase"] = "done"
                except Exception as e:
                    import traceback; traceback.print_exc()
                    _elo_refresh_status["phase"] = "error"
                    _elo_refresh_status["message"] = str(e)
                finally:
                    _elo_refresh_status["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")
                    _elo_refresh_lock.release()

            threading.Thread(target=_worker, daemon=True).start()
            self._send_json(202, {
                "ok": True,
                "status": dict(_elo_refresh_status),
                "hint": "poll GET /api/elo-refresh-status until phase in {done,error}",
            })
            return

        if path != "/api/simulate":
            self._send_json(404, {"error": "not found"})
            return

        if not self._require_role("admin"):
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
    print(f"  Auth: {'enabled (Supabase JWKS)' if _AUTH_ENABLED else 'disabled (no SUPABASE_URL)'}")
    print(f"  (Ctrl-C to stop)")
    with ReusableServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping…")
