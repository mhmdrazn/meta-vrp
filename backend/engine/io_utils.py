"""Dataset registry + validation (TASK 2).

`load_dataset(dataset_id)` is the single entry point BOTH the FastAPI endpoint and the
experiment notebooks call to fetch nodes+time-matrix by id. `get_available_datasets()`
returns only the datasets whose static files actually exist on disk — data-driven, so
Dataset B automatically shows up once its JSON+NPY files land, without any code change.

`validate_dataset(nodes, tm)` performs the invariants the paper-revision spec (requirement
2) lists: unique ids, valid coordinates, complete demand/service-time values, exactly one
depot, square NxN matrix whose index order matches the nodes list exactly.

CLI:
    python -m backend.engine.io_utils validate       # validate every available dataset
    python -m backend.engine.io_utils list           # print discovered datasets
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Tuple

from .data import Node, TimeMatrix, load_nodes_json, load_time_matrix_npy

# Repo layout: <repo>/backend/engine/io_utils.py -> repo root = parents[2]
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_ENGINE_DIR)
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
_DEFAULT_PROCESSED_DIR = os.path.join(_BACKEND_DIR, "data")


@dataclass(frozen=True)
class DatasetSpec:
    id: str
    label: str
    nodes_path: str
    matrix_path: str


def _spec_for(dataset_id: str, label: str, processed_dir: str) -> DatasetSpec:
    return DatasetSpec(
        id=dataset_id,
        label=label,
        nodes_path=os.path.join(processed_dir, f"{dataset_id}.json"),
        matrix_path=os.path.join(processed_dir, f"{dataset_id}_time_matrix.npy"),
    )


# Registry: keep in one place so adding a Dataset C is a one-line change.
def _all_specs(processed_dir: str) -> List[DatasetSpec]:
    return [
        _spec_for("dataset_a", "Dataset A", processed_dir),
        _spec_for("dataset_b", "Dataset B", processed_dir),
    ]


def _processed_dir() -> str:
    # Env override lets deployments point at a different data directory (e.g. a mounted
    # volume) without editing code.
    return os.environ.get("DATASETS_DIR", _DEFAULT_PROCESSED_DIR)


def get_available_datasets() -> List[DatasetSpec]:
    """Return only the datasets whose JSON+NPY files actually exist."""
    return [
        s for s in _all_specs(_processed_dir())
        if os.path.exists(s.nodes_path) and os.path.exists(s.matrix_path)
    ]


def _spec(dataset_id: str) -> DatasetSpec:
    for s in _all_specs(_processed_dir()):
        if s.id == dataset_id:
            return s
    raise ValueError(f"Unknown dataset_id: {dataset_id!r}. Known: {[s.id for s in _all_specs(_processed_dir())]}")


@lru_cache(maxsize=8)
def load_dataset(dataset_id: str) -> Tuple[Dict[str, Node], TimeMatrix, DatasetSpec]:
    """Load a dataset by id (nodes JSON + time-matrix NPY). Cached per-process."""
    spec = _spec(dataset_id)
    if not os.path.exists(spec.nodes_path):
        raise FileNotFoundError(
            f"{spec.nodes_path} not found. Generate it with scripts/convert_dataset.py."
        )
    if not os.path.exists(spec.matrix_path):
        raise FileNotFoundError(
            f"{spec.matrix_path} not found. Generate it with scripts/convert_dataset.py."
        )
    nodes, ids_in_order = load_nodes_json(spec.nodes_path)
    tm = load_time_matrix_npy(spec.matrix_path, ids_in_order)
    return nodes, tm, spec


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_dataset(nodes: Dict[str, Node], tm: TimeMatrix) -> None:
    """Raise ValueError with a specific message on any invariant failure."""
    if not nodes:
        raise ValueError("nodes dict is empty")

    # 1. Unique ids (Dict already enforces this at load, but double-check the ordering list too).
    if len(set(nodes.keys())) != len(nodes):
        raise ValueError("duplicate node ids present in nodes dict")

    # 2. Valid coordinates + complete demand/service-time.
    for nid, n in nodes.items():
        if not (-90.0 <= n.lat <= 90.0):
            raise ValueError(f"node {nid}: latitude {n.lat} out of range [-90, 90]")
        if not (-180.0 <= n.lon <= 180.0):
            raise ValueError(f"node {nid}: longitude {n.lon} out of range [-180, 180]")
        if n.type == "park":
            if n.demand_liters <= 0:
                raise ValueError(f"park {nid}: demand_liters must be > 0, got {n.demand_liters}")
            if n.service_min < 0:
                raise ValueError(f"park {nid}: service_min must be >= 0, got {n.service_min}")

    # 3. Exactly one depot.
    depots = [nid for nid, n in nodes.items() if n.type == "depot"]
    if len(depots) != 1:
        raise ValueError(f"dataset must contain exactly one depot, found {len(depots)}: {depots}")

    # 4. Matrix shape & index-order correspondence.
    n = len(nodes)
    if tm.M.shape != (n, n):
        raise ValueError(f"time matrix shape {tm.M.shape} does not match nodes count {n}")
    if list(tm.ids) != list(nodes.keys()):
        raise ValueError(
            "time matrix ids order does not match nodes insertion order — "
            "matrix indices must correspond exactly to node ids"
        )

    # 5. No NaN/inf in the matrix.
    import numpy as np
    if not np.isfinite(tm.M).all():
        bad = int((~np.isfinite(tm.M)).sum())
        raise ValueError(f"time matrix contains {bad} non-finite entries")

    # 6. Diagonal ~= 0 (travel-time from a node to itself).
    if float(np.abs(np.diag(tm.M)).max()) > 1e-6:
        raise ValueError("time matrix diagonal is not zero (self-travel should be 0)")


def _cli_validate(dataset_ids: Iterable[str]) -> int:
    exit_code = 0
    for did in dataset_ids:
        try:
            nodes, tm, spec = load_dataset(did)
            validate_dataset(nodes, tm)
            n_park = sum(1 for n in nodes.values() if n.type == "park")
            n_refill = sum(1 for n in nodes.values() if n.type == "refill")
            print(
                f"[OK] {spec.id} ({spec.label}): "
                f"{len(nodes)} nodes (1 depot, {n_park} park, {n_refill} refill), "
                f"matrix {tm.M.shape}, min={tm.M.min():.3g} max={tm.M.max():.3g}"
            )
        except Exception as e:  # noqa: BLE001 — CLI wants a clean message, not a traceback
            print(f"[FAIL] {did}: {e}", file=sys.stderr)
            exit_code = 1
    return exit_code


def _cli_list() -> int:
    specs = get_available_datasets()
    if not specs:
        print("(no datasets found — run scripts/convert_dataset.py first)", file=sys.stderr)
        return 1
    for s in specs:
        print(f"{s.id}\t{s.label}\t{s.nodes_path}\t{s.matrix_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="print discovered datasets")
    vp = sub.add_parser("validate", help="validate all (or specified) datasets")
    vp.add_argument("dataset_ids", nargs="*", help="specific dataset ids to validate (default: all available)")
    args = ap.parse_args()

    if args.cmd == "list":
        return _cli_list()
    if args.cmd == "validate":
        ids = args.dataset_ids or [s.id for s in get_available_datasets()]
        if not ids:
            print("(no datasets available to validate)", file=sys.stderr)
            return 1
        return _cli_validate(ids)
    return 2


if __name__ == "__main__":
    sys.exit(main())
