"""
Publish locally-built ELO ratings to the cloud GCS bucket.

The cloud web app is read-only for ELO: it only *serves* the pre-built rating
JSONs from the bucket mounted at /data. Computation happens locally
(`python scripts/elo/build_ratings.py --phase build`), and this script is the
manual "upload to cloud" step — run it from your laptop where you're already
`gcloud`-logged-in.

Usage:
    python scripts/elo/publish.py                       # upload to the default bucket
    python scripts/elo/publish.py --bucket my-bucket    # override bucket
    python scripts/elo/publish.py --dry-run             # print the command, don't run

The bucket is mounted at /data on Cloud Run (DATA_DIR=/data), and all rating
JSONs live at the mount root, so we upload to the bucket root. Uses your
existing `gcloud` credentials — no secrets, no server call.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir  # noqa: E402

DEFAULT_BUCKET = "gbt-fantasy-optimizer-data"

# The per-model rankings + comparison stats the ranking tab needs to render.
REQUIRED_FILES = [
    "elo_current.json",
    "glicko2_current.json",
    "trueskill_current.json",
    "ensemble_current.json",
    "elo_models_meta.json",
]
# Name-merge rules — nice to have, regenerated on the next local build if absent.
OPTIONAL_FILES = [
    "elo_aliases.json",
]


def _fmt_size(n: int) -> str:
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb/1024:.1f} MB"


def _collect(data: Path) -> list[Path]:
    """Return the files to upload; exit with a helpful error if any required
    artifact is missing."""
    files: list[Path] = []
    missing: list[str] = []
    for name in REQUIRED_FILES:
        p = data / name
        if p.exists():
            files.append(p)
        else:
            missing.append(name)
    if missing:
        print("[error] Missing required rating file(s):", file=sys.stderr)
        for name in missing:
            print(f"          {data / name}", file=sys.stderr)
        print("        Build them first:  python scripts/elo/build_ratings.py --phase build",
              file=sys.stderr)
        sys.exit(1)
    for name in OPTIONAL_FILES:
        p = data / name
        if p.exists():
            files.append(p)
        else:
            print(f"[note] optional file not present, skipping: {name}")
    return files


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload local ELO ratings to the cloud GCS bucket.")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET,
                    help=f"GCS bucket name (default: {DEFAULT_BUCKET})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the gcloud command without executing it.")
    args = ap.parse_args()

    if shutil.which("gcloud") is None:
        print("[error] `gcloud` not found on PATH. Install the Google Cloud SDK "
              "and run `gcloud auth login` first.", file=sys.stderr)
        sys.exit(1)

    data = data_dir()
    files = _collect(data)
    dest = f"gs://{args.bucket}/"

    print(f"Publishing {len(files)} file(s) to {dest}")
    for p in files:
        print(f"  - {p.name:<24} {_fmt_size(p.stat().st_size)}")

    cmd = ["gcloud", "storage", "cp", *[str(p) for p in files], dest]

    if args.dry_run:
        print("\n[dry-run] would run:")
        print("  " + " ".join(cmd))
        return

    print()
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[error] gcloud exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)
    print("✓ Upload complete. The cloud ranking tab will reflect the new data.")


if __name__ == "__main__":
    main()
