#!/usr/bin/env bash
# One-shot ELO setup for macOS / Linux.
# Builds the whole rating system (downloads data + trains the models).
# Run once:  chmod +x setup_elo.sh   then:  ./setup_elo.sh   (pass --quick etc.)
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    python3 scripts/elo/setup.py "$@"
else
    python scripts/elo/setup.py "$@"
fi
