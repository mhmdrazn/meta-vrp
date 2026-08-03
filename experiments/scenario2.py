from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from typing import Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from backend.engine.io_utils import get_available_datasets, load_dataset  # noqa: E402
from experiments.common import (  # noqa: E402
    FULL_FLEET, RunSpec, all_park_ids, all_refill_ids,
    distinct_refills_visited, run_many, write_csv,
)

ALGORITHM = "alns_hybrid"
DEFAULT_SEEDS_PER_SUBSET = 5   # >=5 per spec
NUM_SUBSETS_50_25 = 5          # 5 different random subsets per level (spec §8)
DEFAULT_TIME_LIMIT_SEC = 30.0
REFILL_LEVELS = (100, 50, 25)
SUBSET_RNG_SEED_BASE = 1000    # namespace separate from solve() seeds

OUT_COLUMNS = [
    "dataset", "algorithm", "refill_pct", "subset_id", "seed",
    "total_time", "makespan", "route_time_std",
    "refill_visits", "distinct_refill_stations_used",
    "computation_time", "feasible",
]


def _subset_seed(dataset_id: str, pct: int, subset_id: int) -> int:
    """Deterministic RNG seed for a given (dataset, pct, subset) — reproducible."""
    return SUBSET_RNG_SEED_BASE + abs(hash((dataset_id, pct, subset_id))) % 10_000_000


def _generate_subset(all_refills: List[str], pct: int, dataset_id: str, subset_id: int) -> dict:
    if pct == 100:
        return {
            "dataset_id": dataset_id, "pct": pct, "subset_id": subset_id,
            "rng_seed": None, "refill_ids": sorted(all_refills),
        }
    rng_seed = _subset_seed(dataset_id, pct, subset_id)
    rng = random.Random(rng_seed)
    k = max(1, round(len(all_refills) * pct / 100.0))
    chosen = sorted(rng.sample(all_refills, k))
    return {
        "dataset_id": dataset_id, "pct": pct, "subset_id": subset_id,
        "rng_seed": rng_seed, "refill_ids": chosen,
    }


def _build_all_subsets(dataset_ids: List[str]) -> Dict[str, dict]:
    """Return {dataset_id: {"100": [...], "50": [...], "25": [...]}}. All chosen refill
    IDs are baked in here BEFORE any solver runs, so a mid-run crash never invalidates
    the reproducibility record."""
    out: Dict[str, dict] = {}
    for ds in dataset_ids:
        nodes, _tm, _spec = load_dataset(ds)
        refills = all_refill_ids(nodes)
        entry: Dict[str, list] = {}
        for pct in REFILL_LEVELS:
            if pct == 100:
                entry[str(pct)] = [_generate_subset(refills, pct, ds, subset_id=0)]
            else:
                entry[str(pct)] = [
                    _generate_subset(refills, pct, ds, subset_id=i)
                    for i in range(NUM_SUBSETS_50_25)
                ]
        out[ds] = entry
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--datasets", nargs="*", default=None)
    ap.add_argument("--time-limit", type=float, default=DEFAULT_TIME_LIMIT_SEC)
    ap.add_argument("--seeds", type=int, default=DEFAULT_SEEDS_PER_SUBSET)
    ap.add_argument("--out-dir", default=os.path.join(_REPO, "experiments", "results"))
    args = ap.parse_args()

    available = {s.id for s in get_available_datasets()}
    requested = args.datasets or sorted(available)
    missing = [d for d in requested if d not in available]
    if missing:
        print(f"[error] Requested datasets not available: {missing}", file=sys.stderr)
        return 1

    os.makedirs(args.out_dir, exist_ok=True)

    # 1) Build & persist subsets FIRST so reproducibility record survives crashes.
    subsets = _build_all_subsets(requested)
    subsets_path = os.path.join(args.out_dir, "refill_subsets.json")
    # Merge with any pre-existing subsets file to preserve other datasets' records.
    existing: dict = {}
    if os.path.exists(subsets_path):
        try:
            with open(subsets_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:  # noqa: BLE001 — corrupt file: start fresh
            existing = {}
    existing.update(subsets)
    with open(subsets_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    print(f"[subsets] Wrote refill subset record -> {subsets_path}")

    seeds = tuple(range(args.seeds))
    t0 = time.perf_counter()

    for ds in requested:
        nodes, _tm, spec = load_dataset(ds)
        parks = all_park_ids(nodes)
        fleet = FULL_FLEET.get(ds)
        if fleet is None:
            print(f"[warn] No FULL_FLEET entry for {ds}; skipping.", file=sys.stderr)
            continue

        specs = []
        for pct in REFILL_LEVELS:
            for subset in subsets[ds][str(pct)]:
                for seed in seeds:
                    specs.append(RunSpec(
                        dataset_id=ds, algorithm=ALGORITHM, seed=seed,
                        num_vehicles=fleet, refill_ids=subset["refill_ids"],
                        time_limit_sec=args.time_limit,
                        selected_park_ids=parks,
                        extra_columns={
                            "refill_pct": pct,
                            "subset_id": subset["subset_id"],
                        },
                    ))

        print(f"\n=== {spec.label}: {len(specs)} runs (~{len(specs) * args.time_limit / 60:.1f} min) ===")
        rows = run_many(specs, progress=True)

        # Attach the Scenario-2-specific column that isn't part of the shared SolveResult.
        for row in rows:
            row["distinct_refill_stations_used"] = distinct_refills_visited(row["_routes"], nodes)

        out_path = os.path.join(args.out_dir, f"scenario2_{ds}.csv")
        write_csv(rows, out_path, OUT_COLUMNS)

    print(f"\nScenario 2 done in {time.perf_counter() - t0:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
