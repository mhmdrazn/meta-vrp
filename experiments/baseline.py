"""Baseline comparison script (spec §6).

3 algorithms (ACO, Standard ALNS, Hybrid ALNS) x 2 datasets x 20 seeds.
Full vehicle availability, all refill stations, same computation budget (30 s),
same objective function (shared search_objective), same seeds (0..19).

Every run saved (not only best). One CSV per dataset:
    experiments/results/baseline_dataset_a.csv
    experiments/results/baseline_dataset_b.csv

Columns (spec §6):
    dataset, algorithm, seed, vehicle_count, total_time, makespan, route_time_std,
    active_vehicles, refill_visits, computation_time, feasible

Usage:
    python -m experiments.baseline
    python -m experiments.baseline --time-limit 5      # quick smoke test
    python -m experiments.baseline --datasets dataset_a
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Make repo root importable when running as `python experiments/baseline.py` too.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from backend.engine.io_utils import get_available_datasets  # noqa: E402
from experiments.common import (  # noqa: E402
    FULL_FLEET, RunSpec, all_park_ids, run_many, write_csv,
)
from backend.engine.io_utils import load_dataset  # noqa: E402

ALGORITHMS = ("aco", "alns_standard", "alns_hybrid")
DEFAULT_SEEDS = tuple(range(20))
DEFAULT_TIME_LIMIT_SEC = 30.0
OUT_COLUMNS = [
    "dataset", "algorithm", "seed", "vehicle_count",
    "total_time", "makespan", "route_time_std",
    "active_vehicles", "refill_visits", "computation_time", "feasible",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--datasets", nargs="*", default=None,
                    help="Which dataset ids to run (default: all available)")
    ap.add_argument("--time-limit", type=float, default=DEFAULT_TIME_LIMIT_SEC,
                    help=f"Seconds per solver run (default: {DEFAULT_TIME_LIMIT_SEC})")
    ap.add_argument("--seeds", type=int, default=len(DEFAULT_SEEDS),
                    help=f"Number of seeds per algorithm (default: {len(DEFAULT_SEEDS)})")
    ap.add_argument("--out-dir", default=os.path.join(_REPO, "experiments", "results"))
    args = ap.parse_args()

    available = {s.id for s in get_available_datasets()}
    requested = args.datasets or sorted(available)
    missing = [d for d in requested if d not in available]
    if missing:
        print(f"[error] Requested datasets not available: {missing}", file=sys.stderr)
        print(f"        Available: {sorted(available)}", file=sys.stderr)
        return 1

    seeds = tuple(range(args.seeds))
    os.makedirs(args.out_dir, exist_ok=True)

    t0 = time.perf_counter()
    for ds in requested:
        nodes, _tm, spec = load_dataset(ds)
        parks = all_park_ids(nodes)
        fleet = FULL_FLEET.get(ds)
        if fleet is None:
            print(f"[warn] No FULL_FLEET entry for {ds}; skipping.", file=sys.stderr)
            continue

        specs = [
            RunSpec(
                dataset_id=ds, algorithm=algo, seed=seed,
                num_vehicles=fleet, refill_ids=None,
                time_limit_sec=args.time_limit,
                selected_park_ids=parks,
            )
            for algo in ALGORITHMS for seed in seeds
        ]
        print(f"\n=== {spec.label} ({len(specs)} runs, ~{len(specs) * args.time_limit / 60:.1f} min wall) ===")
        rows = run_many(specs, progress=True)
        out_path = os.path.join(args.out_dir, f"baseline_{ds}.csv")
        write_csv(rows, out_path, OUT_COLUMNS)

    print(f"\nBaseline done in {time.perf_counter() - t0:.1f}s wall time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
