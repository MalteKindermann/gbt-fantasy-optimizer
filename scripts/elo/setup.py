"""
One-shot ELO setup — builds the whole rating system with a single command.

For people who just want the 🏅 ELO ranking and the ELO tournament tree to work
without knowing anything about the internals. It runs all the build phases of
`build_ratings.py` in order, shows a progress bar, installs missing Python
packages, and tells you exactly what to do next.

    python scripts/elo/setup.py            # full history (best ratings)
    python scripts/elo/setup.py --quick    # last ~4 years only (faster)
    python scripts/elo/setup.py --dry-run  # just show what would run
    python scripts/elo/setup.py --skip-deps

Or double-click `setup_elo.bat` (Windows) / run `./setup_elo.sh` (Mac/Linux).

Everything is cached on disk, so if a step fails (usually a network hiccup) you
can simply run it again — already-downloaded data is skipped.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

# German text + progress bars render cleanly even on the Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8")           # type: ignore[attr-defined]
except Exception:
    pass

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build_ratings.py"
REQUIREMENTS = HERE.parents[1] / "scripts" / "requirements.txt"   # <repo>/scripts/requirements.txt
if not REQUIREMENTS.exists():
    REQUIREMENTS = HERE.parent / "requirements.txt"               # fallback: <repo>/scripts/requirements.txt

FULL_SAISONS = ",".join(str(y) for y in range(15, 27))            # 15..26
FULL_YEARS   = ",".join(str(y) for y in range(2015, 2027))        # 2015..2026
QUICK_SAISONS = ",".join(str(y) for y in range(22, 27))           # 22..26
QUICK_YEARS   = ",".join(str(y) for y in range(2022, 2027))       # 2022..2026


def _phases(saisons: str, years: str):
    """Return the ordered list of (title, extra-args, hint) build phases."""
    return [
        ("DVV-Turniere finden (Männer)", ["--phase", "discover", "--saisons", saisons, "--gender", "m"], "kurz"),
        ("DVV-Turniere finden (Frauen)", ["--phase", "discover", "--saisons", saisons, "--gender", "f"], "kurz"),
        ("DVV-Spielpläne einlesen",       ["--phase", "tournaments"], "mittel"),
        ("DVV-Teams & Namen auflösen",    ["--phase", "teams"], "mittel"),
        ("FIVB-Archiv laden",             ["--phase", "fivb"], "einmaliger großer Download"),
        ("bvbinfo-Turniere finden (Männer)", ["--phase", "bvb-discover", "--years", years, "--gender", "m"], "kurz"),
        ("bvbinfo-Turniere finden (Frauen)", ["--phase", "bvb-discover", "--years", years, "--gender", "f"], "kurz"),
        ("bvbinfo-Spiele einlesen",       ["--phase", "bvb-matches"], "mittel"),
        ("Rating-Modelle berechnen",      ["--phase", "build"], "~6-8 Min"),
    ]


# ── Dependencies ───────────────────────────────────────────────────────────────

REQUIRED_IMPORTS = {
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "trueskill": "trueskill",
    "jwt": "pyjwt",
}


def _missing_deps() -> list[str]:
    import importlib
    missing = []
    for mod, pkg in REQUIRED_IMPORTS.items():
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(pkg)
    return missing


def ensure_deps(skip: bool) -> None:
    missing = _missing_deps()
    if not missing:
        print("✓ Alle Python-Pakete vorhanden.")
        return
    if skip:
        print(f"! Fehlende Pakete ({', '.join(missing)}), aber --skip-deps gesetzt — überspringe.")
        return
    print(f"→ Installiere fehlende Pakete: {', '.join(missing)} …")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print("\n✗ Konnte die Pakete nicht automatisch installieren.")
        print(f"  Bitte manuell ausführen:  {sys.executable} -m pip install -r {REQUIREMENTS}")
        sys.exit(rc)
    still = _missing_deps()
    if still:
        print(f"\n✗ Nach der Installation fehlen weiterhin: {', '.join(still)}. Bitte manuell prüfen.")
        sys.exit(1)
    print("✓ Pakete installiert.")


# ── Progress rendering ───────────────────────────────────────────────────────

_SPINNER = "|/-\\"


def _bar(done: int, total: int, width: int = 20) -> str:
    filled = int(width * done / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _clip(s: str, n: int) -> str:
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def run_phase(idx: int, total: int, title: str, extra_args: list[str], hint: str) -> bool:
    """Run one build phase as a subprocess with a live progress line.
    Returns True on success. Prints a persistent ✓/✗ summary line."""
    # -u = unbuffered, so the child's progress lines stream live (Windows buffers otherwise).
    cmd = [sys.executable, "-u", str(BUILD), *extra_args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    tail: list[str] = []
    last_line = ""
    lock = threading.Lock()

    def _reader():
        nonlocal last_line
        for line in proc.stdout:                       # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue
            with lock:
                last_line = line
                tail.append(line)
                if len(tail) > 20:
                    tail.pop(0)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    start = time.time()
    spin = 0
    prefix = f"{_bar(idx - 1, total)} Schritt {idx}/{total}"
    while proc.poll() is None:
        with lock:
            info = _clip(last_line, 48)
        elapsed = int(time.time() - start)
        sys.stdout.write(f"\r{prefix} {_SPINNER[spin % 4]} {title} ({elapsed}s) — {info:<49}")
        sys.stdout.flush()
        spin += 1
        time.sleep(0.4)

    proc.wait()
    t.join(timeout=1)
    elapsed = int(time.time() - start)
    # Clear the live line.
    sys.stdout.write("\r" + " " * 100 + "\r")
    if proc.returncode == 0:
        print(f"{_bar(idx, total)} ✓ Schritt {idx}/{total}: {title} — fertig ({elapsed}s)")
        return True
    print(f"{_bar(idx - 1, total)} ✗ Schritt {idx}/{total}: {title} — FEHLGESCHLAGEN ({elapsed}s)")
    print("  Letzte Ausgabe:")
    for line in tail[-15:]:
        print("    " + line)
    return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Baut das komplette ELO-Ratingsystem mit einem Befehl.")
    ap.add_argument("--quick", action="store_true",
                    help="Nur die letzten ~4 Jahre laden (schneller, schwächere Ratings).")
    ap.add_argument("--full", action="store_true",
                    help="Vollen Verlauf laden (Standard).")
    ap.add_argument("--skip-deps", action="store_true",
                    help="Keine Python-Pakete automatisch installieren.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nur anzeigen, was ausgeführt würde — nichts starten.")
    args = ap.parse_args()

    quick = args.quick and not args.full
    saisons = QUICK_SAISONS if quick else FULL_SAISONS
    years   = QUICK_YEARS if quick else FULL_YEARS
    phases  = _phases(saisons, years)
    total   = len(phases)

    print("=" * 64)
    print(" GBT Fantasy — ELO-Ratings einrichten")
    print("=" * 64)
    print(f" Modus: {'Quick (letzte ~4 Jahre)' if quick else 'Voller Verlauf (2015–2026)'}")
    print(f" Schritte: {total}")
    if not args.dry_run:
        print(" Der erste Lauf lädt viele Turnierdaten (höfliches Rate-Limit),")
        print(" das kann " + ("~5–10 Min" if quick else "~15–40 Min") + " dauern. Alles wird gecached —")
        print(" ein erneuter Start überspringt bereits geladene Daten.")
    print("=" * 64)

    if args.dry_run:
        print("\n[dry-run] Folgende Schritte würden laufen:\n")
        for i, (title, extra, hint) in enumerate(phases, 1):
            cmd = f"{Path(sys.executable).name} {BUILD.name} " + " ".join(extra)
            print(f"  {i}. {title}  ({hint})")
            print(f"       {cmd}")
        print("\n[dry-run] Keine Änderungen vorgenommen.")
        return

    if not BUILD.exists():
        print(f"✗ {BUILD} nicht gefunden — bitte im geklonten Repository ausführen.")
        sys.exit(1)

    ensure_deps(args.skip_deps)
    print()

    try:
        for i, (title, extra, hint) in enumerate(phases, 1):
            ok = run_phase(i, total, title, extra, hint)
            if not ok:
                print()
                print("Ein Schritt ist fehlgeschlagen (meist ein Netzwerkproblem).")
                print("Starte das Setup einfach nochmal — bereits geladene Daten")
                print("werden übersprungen, es geht dort weiter, wo es abgebrochen ist.")
                sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nAbgebrochen. Fortschritt ist gecached — beim nächsten Start geht's weiter.")
        sys.exit(130)

    print()
    print("=" * 64)
    print(" ✓ Fertig! Die ELO-Ratings wurden gebaut.")
    print("=" * 64)
    print(" Starte jetzt die App:")
    print("     python scripts/serve.py")
    print(" und öffne http://localhost:8000 → Tab „🏅 ELO Rangliste“.")
    print(" Der ELO-Turnierbaum steht dann auch im Turnier-Baum-Tab bereit.")
    print()
    print(" (Nur für Cloud-Hosting: danach `python scripts/elo/publish.py`,")
    print("  um die Ratings in die Cloud zu laden.)")


if __name__ == "__main__":
    main()
