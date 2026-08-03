"""Shared helpers for the offline experiment notebooks (TASK 5-7).

Every notebook (baseline, scenario1, scenario2) imports from this module — this is the
single place `engine.solve.solve()` gets called for the offline benchmark suite, which
mechanically enforces the paper-revision requirement "same optimisation functions...
across offline experiments" (spec §1).

This module MUST NOT import fastapi / sqlalchemy / backend.app / backend.database /
backend.routers — only backend.engine.*. That's what lets you copy `backend/engine/`
+ `experiments/` + `backend/data/*.json,.npy` outside this repo and run the notebooks
standalone with just `pip install -r experiments/requirements.txt`.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from backend.engine.data import Node
from backend.engine.io_utils import load_dataset
from backend.engine.solve import solve

# --- Shared config: which fleet size counts as "full" for each dataset ---------------
# Dataset A: doc's Scenario 1 defines 9 = full. Dataset B: filled in once we know its
# real fleet requirement (Phase 11 in the plan / TASK 6 pre-flight).
FULL_FLEET: Dict[str, int] = {
    "dataset_a": 9,
    "dataset_b": 5,   # provisional — dataset_b has ~51 parks, similar magnitude to A;
                       # can be tuned in scenario1.py once we have baseline evidence.
}

# Vehicle capacity + depot id are dataset-agnostic today (single depot, uniform fleet).
VEHICLE_CAPACITY_LITERS = 5000.0
DEPOT_ID = "0"
ALLOW_REFILL = True


@dataclass
class RunSpec:
    """One solver invocation's inputs. run_one() -> dict for CSV row."""
    dataset_id: str
    algorithm: str
    seed: int
    num_vehicles: int
    refill_ids: Optional[List[str]]  # None = "all refills in the dataset"
    time_limit_sec: float
    selected_park_ids: List[str]
    # Free-form extra columns per experiment (e.g. {"vehicle_level": "moderate",
    # "refill_pct": 50, "subset_index": 3}).
    extra_columns: Dict[str, Any] = field(default_factory=dict)


def all_park_ids(nodes: Dict[str, Node]) -> List[str]:
    return [nid for nid, n in nodes.items() if n.type == "park"]


def all_refill_ids(nodes: Dict[str, Node]) -> List[str]:
    return [nid for nid, n in nodes.items() if n.type == "refill"]


def distinct_refills_visited(routes: List[List[str]], nodes: Dict[str, Node]) -> int:
    """Scenario-2-specific metric: how many unique refill stations were actually used."""
    seen = set()
    for r in routes:
        for nid in r:
            if nid in nodes and nodes[nid].type == "refill":
                seen.add(nid)
    return len(seen)


def run_one(spec: RunSpec) -> dict:
    """Invoke solve() with the given spec, return a flat dict ready to become a CSV row.

    The dict has: every SolveResult scalar field (total_time, makespan, route_time_std,
    active_vehicles, refill_visits, computation_time, feasible), plus the identifying
    columns from RunSpec (dataset, algorithm, seed, vehicle_count), plus whatever the
    caller put in `extra_columns` (e.g. vehicle_level, refill_pct, subset_index).
    """
    nodes, tm, _spec = load_dataset(spec.dataset_id)
    refill_ids = spec.refill_ids if spec.refill_ids is not None else all_refill_ids(nodes)

    result = solve(
        nodes=nodes,
        tm=tm,
        selected_park_ids=spec.selected_park_ids,
        num_vehicles=spec.num_vehicles,
        depot_id=DEPOT_ID,
        vehicle_capacity=VEHICLE_CAPACITY_LITERS,
        refill_ids=refill_ids,
        allow_refill=ALLOW_REFILL,
        algorithm=spec.algorithm,
        time_limit_sec=spec.time_limit_sec,
        seed=spec.seed,
    )

    row: Dict[str, Any] = {
        "dataset": spec.dataset_id,
        "algorithm": spec.algorithm,
        "seed": spec.seed,
        "vehicle_count": spec.num_vehicles,
        "total_time": result["total_time"],
        "makespan": result["makespan"],
        "route_time_std": result["route_time_std"],
        "active_vehicles": result["active_vehicles"],
        "refill_visits": result["refill_visits"],
        "computation_time": result["computation_time"],
        "feasible": result["feasible"],
    }
    # Extras override nothing above (raise on collision so accidents are visible).
    for k, v in spec.extra_columns.items():
        if k in row:
            raise ValueError(f"extra_columns key {k!r} collides with a built-in column")
        row[k] = v
    # For Scenario 2's distinct_refill_stations_used metric — cheap to compute here so
    # callers don't have to load nodes themselves just for this.
    row["_routes"] = result["routes"]  # kept for callers that need it; strip before CSV
    return row


def run_many(specs: Iterable[RunSpec], progress: bool = True) -> List[dict]:
    """Sequential run over specs. No parallelism — solve() is CPU-heavy and each run
    already uses its full time budget, so parallelising here would just contend for
    the same cores."""
    specs = list(specs)
    out: List[dict] = []
    for i, spec in enumerate(specs, start=1):
        if progress:
            print(
                f"[{i}/{len(specs)}] {spec.dataset_id} {spec.algorithm} "
                f"seed={spec.seed} v={spec.num_vehicles} refills={('all' if spec.refill_ids is None else len(spec.refill_ids))}",
                flush=True,
            )
        row = run_one(spec)
        out.append(row)
    return out


def write_csv(rows: List[dict], path: str, columns: List[str]) -> None:
    """Write rows to CSV in the exact column order given.

    Any row keys not in `columns` are silently dropped (used to strip helper fields
    like `_routes`). Missing keys raise (unmapped extras usually mean a bug).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            for col in columns:
                if col not in row:
                    raise KeyError(f"row missing required column {col!r}: keys={sorted(row.keys())}")
            writer.writerow(row)
    print(f"Wrote {len(rows)} rows -> {path}")
