"""
Minimal env-var loader (kein python-dotenv-Dep nötig).

Lädt `.env.local` und `.env` aus dem Repo-Root in `os.environ`, falls
vorhanden — `.env.local` hat Priorität (überschreibt `.env`). Existierende
echte Environment-Variablen werden NICHT überschrieben (so kann Fly.io seine
Secrets sauber injizieren, ohne dass eine versehentlich mitdeployte
`.env.local` Vorrang hätte).

Außerdem stellt `data_dir()` den DATA_DIR-Pfad bereit (env-var `DATA_DIR`
override → sonst `<repo>/data`).

Usage in Entry-Point-Scripts:
    from _env import load_dotenv_files, data_dir
    load_dotenv_files()
    DATA = data_dir()
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file. Comments (#), blank lines, optional quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip matching surrounding quotes
            if (len(val) >= 2 and val[0] == val[-1]
                    and val[0] in ("'", '"')):
                val = val[1:-1]
            if key:
                out[key] = val
    except Exception:
        pass
    return out


_loaded = False


def load_dotenv_files() -> None:
    """
    Idempotent: liest `.env` und `.env.local` ein, mergt in os.environ.
    Existierende echte ENV-Variablen werden NIE überschrieben (Fly.io / Docker
    Secrets gewinnen immer gegen Repo-Dateien).
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    merged: dict[str, str] = {}
    merged.update(_parse_dotenv(_ROOT / ".env"))
    merged.update(_parse_dotenv(_ROOT / ".env.local"))   # .local hat Priorität
    for k, v in merged.items():
        os.environ.setdefault(k, v)


def data_dir() -> Path:
    """
    Returnt den Daten-Pfad. Default = <repo>/data. Per env-var `DATA_DIR`
    überschreibbar (z.B. auf Fly.io: `/data` als Volume-Mount).
    Erstellt den Ordner, falls er nicht existiert.
    """
    load_dotenv_files()
    p = Path(os.environ.get("DATA_DIR") or (_ROOT / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p
