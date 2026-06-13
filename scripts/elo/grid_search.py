"""
Model-aware grid search.

Loads the consolidated match list ONCE via build_ratings.get_consolidated_records
(~5 s), then walks the grid in-memory.  Each model-run over 115 k matches takes
~3-5 s for ELO, ~8-12 s for Glicko-2/TrueSkill, so a full grid finishes in
a few minutes.

Usage:
    python -m scripts.elo.grid_search --cutoff 2024-12-31
    python -m scripts.elo.grid_search --model trueskill --cutoff 2024-12-31 --quick
    python -m scripts.elo.grid_search --model glicko2

The reported metric is plain accuracy on the held-out set + calibration error
(weighted mean |predicted - actual| across buckets).
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _env import data_dir, load_dotenv_files  # noqa: E402
load_dotenv_files()

from elo import build_ratings  # noqa: E402
from elo import models as elo_models  # noqa: E402
from elo import priors as elo_priors  # noqa: E402
from elo import runner as elo_runner  # noqa: E402

DATA = data_dir()


# ── Per-model grids ───────────────────────────────────────────────────────────

GRIDS_FULL = {
    "elo": {
        "k_base":                     [15, 20, 25, 30, 35],
        "blend_individual_weight":    [0.6, 0.7, 0.8],
        "decay_pull":                 [0.05, 0.10, 0.15],
        "team_min_matches_for_blend": [3, 5, 10],
        "provisional_multiplier":     [1.0, 1.5, 2.0],
    },
    "glicko2": {
        "initial_phi":        [200, 300, 350],
        "tau":                [0.3, 0.5, 0.8, 1.0],
        "rating_period_days": [3, 7, 14, 30],
    },
    "trueskill": {
        "beta":                     [2.0, 3.0, 4.17, 6.0, 8.0],
        "tau":                      [0.02, 0.05, 0.083, 0.15],
        "sigma0":                   [5.0, 6.5, 8.33, 10.0],
        "sigma_inflation_per_year": [1.0, 1.1, 1.2, 1.3],
    },
}

GRIDS_QUICK = {
    "elo": {
        "k_base":                     [20, 30, 40],
        "blend_individual_weight":    [0.6, 0.8],
        "decay_pull":                 [0.10],
        "team_min_matches_for_blend": [5],
        "provisional_multiplier":     [1.5],
    },
    "glicko2": {
        "initial_phi":        [200, 350],
        "tau":                [0.3, 0.5, 0.8],
        "rating_period_days": [7, 14],
    },
    "trueskill": {
        "beta":                     [3.0, 4.17, 6.0],
        "tau":                      [0.02, 0.083],
        "sigma0":                   [6.5, 8.33],
        "sigma_inflation_per_year": [1.1, 1.2],
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def calibration_error(buckets: dict[int, list[int]]) -> float:
    total_n = sum(len(b) for b in buckets.values())
    if not total_n:
        return float("nan")
    err = 0.0
    for k, results in buckets.items():
        n = len(results)
        if not n:
            continue
        predicted = (k / 10) + 0.05
        actual = sum(results) / n
        err += n * abs(predicted - actual)
    return err / total_n


def _run_one(model_id: str, overrides: dict, records: list[dict],
             cutoff: str, priors: dict) -> dict:
    model = elo_models.make_model(model_id, overrides)
    if priors:
        model.set_priors(priors)
    t0 = time.time()
    run = elo_runner.run_model(records, model, train_end_date=cutoff)
    elapsed = time.time() - t0
    acc = (run.oos_correct / run.oos_total) if run.oos_total else 0.0
    cal = calibration_error(run.oos_calib)
    return {**overrides, "accuracy": acc, "calib_err": cal,
            "n": run.oos_total, "elapsed": elapsed}


def _print_top(results: list[dict], keys: list[str]) -> None:
    print("\n=== Best by raw accuracy ===")
    for r in sorted(results, key=lambda r: r["accuracy"], reverse=True)[:5]:
        print(f"  acc={r['accuracy']:.1%}  calib_err={r['calib_err']:.3f}  "
              f"{ {k: r[k] for k in keys} }")

    print("\n=== Best by calibration error (lower is better) ===")
    for r in sorted(results, key=lambda r: r["calib_err"])[:5]:
        print(f"  acc={r['accuracy']:.1%}  calib_err={r['calib_err']:.3f}  "
              f"{ {k: r[k] for k in keys} }")

    by_acc = sorted(results, key=lambda r: -r["accuracy"])
    by_cal = sorted(results, key=lambda r: r["calib_err"])
    rank = {id(r): 0 for r in results}
    for i, r in enumerate(by_acc):
        rank[id(r)] += i
    for i, r in enumerate(by_cal):
        rank[id(r)] += i
    print("\n=== Best by rank-sum (acc + calibration) ===")
    for r in sorted(results, key=lambda r: rank[id(r)])[:5]:
        print(f"  acc={r['accuracy']:.1%}  calib_err={r['calib_err']:.3f}  "
              f"{ {k: r[k] for k in keys} }")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["elo", "glicko2", "trueskill"],
                    default="elo")
    ap.add_argument("--cutoff", default="2024-12-31")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-priors", action="store_true",
                    help="skip DVV cold-start priors (for A/B comparisons)")
    args = ap.parse_args()

    grid_src = GRIDS_QUICK if args.quick else GRIDS_FULL
    grid = grid_src[args.model]
    combos = list(itertools.product(*grid.values()))
    keys = list(grid.keys())

    print(f"Loading consolidated match records ...")
    t0 = time.time()
    records = build_ratings.get_consolidated_records(force_reload=True)
    print(f"  loaded {len(records)} matches in {time.time() - t0:.1f}s")
    print(f"  model = {args.model}")
    print(f"  cutoff = {args.cutoff}")
    print(f"  evaluating {len(combos)} grid combinations\n")

    priors: dict = {}
    if not args.no_priors:
        try:
            priors = elo_priors.build_for_model(args.model)
            print(f"  loaded {len(priors)} cold-start priors\n")
        except Exception as e:
            print(f"  priors unavailable: {e}\n")

    results: list[dict] = []
    for i, combo in enumerate(combos, 1):
        overrides = dict(zip(keys, combo))
        r = _run_one(args.model, overrides, records, args.cutoff, priors)
        results.append(r)
        print(f"  [{i:>3}/{len(combos)}] {overrides}  "
              f"acc={r['accuracy']:.1%}  calib_err={r['calib_err']:.3f}  "
              f"({r['elapsed']:.1f}s)")

    _print_top(results, keys)

    # Persist best combination for this model
    best = sorted(results,
                  key=lambda r: (-r["accuracy"], r["calib_err"]))[0]
    out_path = DATA / f"elo_grid_results_{args.model}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Grid results for model={args.model} cutoff={args.cutoff} "
                f"quick={args.quick}\n")
        f.write(f"# Best combo:\n")
        f.write(json.dumps({k: best[k] for k in keys}, indent=2))
        f.write(f"\n# acc={best['accuracy']:.4f}  "
                f"calib_err={best['calib_err']:.4f}  n={best['n']}\n")
        f.write("\n# All combos (sorted by accuracy):\n")
        for r in sorted(results, key=lambda r: -r["accuracy"]):
            f.write(json.dumps(
                {**{k: r[k] for k in keys},
                 "accuracy": round(r["accuracy"], 4),
                 "calib_err": round(r["calib_err"], 4)}) + "\n")
    print(f"\n[grid] best -> {out_path.name}")


if __name__ == "__main__":
    main()
