from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from backend.engine.io_utils import get_available_datasets, load_dataset  # noqa: E402
from experiments.common import (  # noqa: E402
    RunSpec, all_park_ids, run_many, run_one, write_csv,
)

ALGORITHM = "alns_hybrid"
DEFAULT_SEEDS = tuple(range(10))
DEFAULT_TIME_LIMIT_SEC = 30.0

# Vehicle tiers per dataset. Ordered (level_label, vehicle_count).
# Dataset A per spec: 9 full / 7 moderate / 6 severe (5 optional-if-feasible).
# Dataset B per spec: 100 / ~75-80 / ~55-65% of its normal fleet (FULL_FLEET["dataset_b"] = 5).
TIERS: dict = {
    "dataset_a": [
        ("full", 9),
        ("moderate", 7),
        ("severe", 6),
    ],
    "dataset_b": [
        ("full", 5),
        ("moderate", 4),   # 80% of 5
        ("severe", 3),     # 60% of 5
    ],
}

OUT_COLUMNS = [
    "dataset", "algorithm", "vehicle_level", "vehicle_count", "seed",
    "total_time", "makespan", "route_time_std",
    "active_vehicles", "refill_visits", "computation_time", "feasible",
]


def _probe_severe2_feasible(dataset_id: str, time_limit_sec: float) -> bool:
    """Quick 3-seed feasibility probe for the 5-vehicle 'severe2' tier on Dataset A."""
    nodes, _tm, _spec = load_dataset(dataset_id)
    parks = all_park_ids(nodes)
    feas_count = 0
    for seed in (100, 101, 102):
        r = run_one(RunSpec(
            dataset_id=dataset_id, algorithm=ALGORITHM, seed=seed,
            num_vehicles=5, refill_ids=None,
            time_limit_sec=min(5.0, time_limit_sec),
            selected_park_ids=parks,
        ))
        feas_count += 1 if r["feasible"] else 0
    print(f"[probe] {dataset_id} @ 5 vehicles: feasible in {feas_count}/3 probes.")
    return feas_count >= 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--datasets", nargs="*", default=None)
    ap.add_argument("--time-limit", type=float, default=DEFAULT_TIME_LIMIT_SEC)
    ap.add_argument("--seeds", type=int, default=len(DEFAULT_SEEDS))
    ap.add_argument("--include-severe2", action="store_true",
                    help="Include the 5-vehicle tier for Dataset A if a probe shows it's feasible.")
    ap.add_argument("--out-dir", default=os.path.join(_REPO, "experiments", "results"))
    args = ap.parse_args()

    available = {s.id for s in get_available_datasets()}
    requested = args.datasets or sorted(available)
    missing = [d for d in requested if d not in available]
    if missing:
        print(f"[error] Requested datasets not available: {missing}", file=sys.stderr)
        return 1

    seeds = tuple(range(args.seeds))
    os.makedirs(args.out_dir, exist_ok=True)

    tiers = {k: list(v) for k, v in TIERS.items()}
    if args.include_severe2 and "dataset_a" in requested:
        if _probe_severe2_feasible("dataset_a", args.time_limit):
            tiers["dataset_a"].append(("severe2", 5))

    t0 = time.perf_counter()
    for ds in requested:
        nodes, _tm, spec = load_dataset(ds)
        parks = all_park_ids(nodes)
        dataset_tiers = tiers.get(ds, [])
        if not dataset_tiers:
            print(f"[warn] No tiers defined for {ds}; skipping.", file=sys.stderr)
            continue

        specs = []
        for level_label, vcount in dataset_tiers:
            for seed in seeds:
                specs.append(RunSpec(
                    dataset_id=ds, algorithm=ALGORITHM, seed=seed,
                    num_vehicles=vcount, refill_ids=None,
                    time_limit_sec=args.time_limit,
                    selected_park_ids=parks,
                    extra_columns={"vehicle_level": level_label},
                ))

        print(f"\n=== {spec.label}: {len(dataset_tiers)} tiers x {len(seeds)} seeds = {len(specs)} runs "
              f"(~{len(specs) * args.time_limit / 60:.1f} min) ===")
        rows = run_many(specs, progress=True)
        out_path = os.path.join(args.out_dir, f"scenario1_{ds}.csv")
        write_csv(rows, out_path, OUT_COLUMNS)

    print(f"\nScenario 1 done in {time.perf_counter() - t0:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
