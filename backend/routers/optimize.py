"""Stateless optimisation router (TASK 3 — public demo path, DB-free).

- POST /optimize     : load dataset via engine.io_utils, run engine.solve.solve(), return.
                      Does NOT persist anything.
- GET  /nodes        : list nodes for the selected dataset (backend-served, replaces the
                      hardcoded LOCAL_NODES in the frontend once TASK 8 lands).
- GET  /datasets     : discovery endpoint powering the frontend Dataset A/B selector.
- GET  /experiments  : serve pre-computed experiment CSVs as JSON (baseline, scenario1, scenario2).
"""
from __future__ import annotations

import csv
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..engine.io_utils import get_available_datasets, load_dataset
from ..engine.solve import solve as engine_solve
from ..schemas import OptimizeRequest, OptimizeResponse, RouteResult
from ..settings import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["optimize"])

# Hard-timeout runner for /optimize so a rogue solver run can't hold the worker forever.
_EXECUTOR = ThreadPoolExecutor(max_workers=1)


class NodeOut(BaseModel):
    id: str
    name: Optional[str] = None
    lat: float
    lon: float
    kind: Optional[Literal["depot", "refill", "park"]] = None
    demand_liters: float = 0.0
    service_min: float = 0.0


class DatasetOut(BaseModel):
    id: str
    label: str
    node_count: int
    park_count: int
    refill_count: int


def _run_solve(req: OptimizeRequest) -> OptimizeResponse:
    """Actual work — loads the requested dataset, delegates to engine.solve.solve()."""
    try:
        nodes, tm, spec = load_dataset(req.dataset_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Dataset load failed: {e}")

    # Refill subset: explicit override wins, else every refill in the dataset.
    if req.refill_ids_override is not None:
        refill_ids = list(req.refill_ids_override)
    else:
        refill_ids = [nid for nid, n in nodes.items() if n.type == "refill"]

    # Time budget: request override wins, else settings default.
    time_limit = float(req.time_limit_sec) if req.time_limit_sec else float(settings.TIME_LIMIT_SEC)

    try:
        result = engine_solve(
            nodes=nodes,
            tm=tm,
            selected_park_ids=[str(x) for x in req.selected_node_ids],
            num_vehicles=req.num_vehicles,
            depot_id=settings.DEPOT_ID,
            vehicle_capacity=float(settings.VEHICLE_CAPACITY_LITERS),
            refill_ids=refill_ids,
            allow_refill=bool(settings.ALLOW_REFILL),
            algorithm=req.algorithm,
            time_limit_sec=time_limit,
            seed=req.seed if req.seed is not None else 42,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    diagnostics = dict(result["diagnostics"])
    route_refills = []
    for r in result["routes"]:
        refill_pos = [
            i for i, nid in enumerate(r) if nid in nodes and nodes[nid].type == "refill"
        ]
        route_refills.append({"sequence": r, "refill_indices": refill_pos})
    diagnostics["refill_positions"] = route_refills
    diagnostics["dataset_id"] = spec.id

    return OptimizeResponse(
        # Back-compat aliases (existing frontend still reads these).
        objective_time_min=result["makespan"],
        vehicle_used=result["active_vehicles"],
        routes=[RouteResult(**rr) for rr in result["route_results"]],
        diagnostics=diagnostics,
        job_id=None,  # demo mode never persists; job_id is always None.
        # New unified metrics.
        total_time=result["total_time"],
        makespan=result["makespan"],
        route_time_std=result["route_time_std"],
        active_vehicles=result["active_vehicles"],
        refill_visits=result["refill_visits"],
        computation_time=result["computation_time"],
        feasible=result["feasible"],
        algorithm=result["algorithm"],
    )


@router.post("/optimize", response_model=OptimizeResponse)
def optimize(req: OptimizeRequest) -> OptimizeResponse:
    """Run a single optimisation. Stateless — no DB writes, no job history."""
    hard_timeout = max(
        3.0,
        float(req.time_limit_sec if req.time_limit_sec else settings.TIME_LIMIT_SEC) + 5.0,
    )
    fut = _EXECUTOR.submit(_run_solve, req)
    try:
        return fut.result(timeout=hard_timeout)
    except FTimeout:
        raise HTTPException(
            status_code=504,
            detail=f"Optimization timed out after {hard_timeout:.1f}s",
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Unhandled error in /optimize")
        raise HTTPException(
            status_code=500, detail=f"Internal error: {type(e).__name__}: {e}"
        )


@router.get("/nodes", response_model=List[NodeOut])
def list_nodes(dataset_id: str = Query("dataset_a")) -> List[NodeOut]:
    """List all nodes of a dataset (dataset-aware — powers the frontend map)."""
    try:
        nodes, _tm, _spec = load_dataset(dataset_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Dataset load failed: {e}")

    out: List[NodeOut] = []
    for nid, n in nodes.items():
        out.append(
            NodeOut(
                id=n.id,
                name=n.name,
                lat=float(n.lat),
                lon=float(n.lon),
                kind=n.type,  # type: ignore[arg-type]
                demand_liters=float(n.demand_liters),
                service_min=float(n.service_min),
            )
        )
    return out


@router.get("/datasets", response_model=List[DatasetOut])
def list_datasets() -> List[DatasetOut]:
    """Discovery endpoint — returns only datasets whose static files exist on disk."""
    out: List[DatasetOut] = []
    for spec in get_available_datasets():
        try:
            nodes, _tm, _spec = load_dataset(spec.id)
        except Exception as e:  # noqa: BLE001
            log.warning("Skipping dataset %s: %s", spec.id, e)
            continue
        park_count = sum(1 for n in nodes.values() if n.type == "park")
        refill_count = sum(1 for n in nodes.values() if n.type == "refill")
        out.append(
            DatasetOut(
                id=spec.id,
                label=spec.label,
                node_count=len(nodes),
                park_count=park_count,
                refill_count=refill_count,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Experiment results endpoint — serves pre-computed CSV data as JSON.
# ---------------------------------------------------------------------------

_EXPERIMENTS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "experiments")

VALID_EXPERIMENTS = ("baseline", "scenario1", "scenario2")


def _normalize_dataset_id(v: str) -> str:
    """The notebooks print human labels ("Dataset A"); the rest of the app (routers,
    frontend Select values) uses the slug form ("dataset_a"). Normalize here so the
    /experiments dataset_id filter matches the same convention as /datasets and
    /optimize, instead of leaking the notebook's display string as an API identifier."""
    return v.strip().lower().replace(" ", "_")


def _read_csv(path: str) -> List[Dict[str, Any]]:
    """Read a CSV file and return as list of dicts with numeric coercion."""
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row: Dict[str, Any] = {}
            for k, v in raw.items():
                if v is None or v == "":
                    row[k] = None
                    continue
                if k == "dataset":
                    row[k] = _normalize_dataset_id(v)
                    continue
                if v.lower() in ("true", "false"):
                    row[k] = v.lower() == "true"
                    continue
                try:
                    row[k] = int(v)
                except ValueError:
                    try:
                        row[k] = float(v)
                    except ValueError:
                        row[k] = v
            rows.append(row)
    return rows


@router.get("/experiments/{experiment_type}")
def get_experiments(
    experiment_type: str,
    dataset_id: Optional[str] = Query(None),
) -> List[Dict[str, Any]]:
    """Return pre-computed experiment results as JSON.

    experiment_type: baseline | scenario1 | scenario2
    dataset_id (optional): filter to a single dataset (e.g. dataset_a).
    """
    if experiment_type not in VALID_EXPERIMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid experiment type. Valid: {VALID_EXPERIMENTS}",
        )

    exp_dir = os.path.normpath(_EXPERIMENTS_DIR)
    if not os.path.isdir(exp_dir):
        raise HTTPException(status_code=404, detail="No experiment data available.")

    all_rows: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(exp_dir)):
        if not fname.startswith(f"{experiment_type}_") or not fname.endswith(".csv"):
            continue
        fpath = os.path.join(exp_dir, fname)
        try:
            all_rows.extend(_read_csv(fpath))
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to read experiment file %s: %s", fname, e)

    if dataset_id:
        all_rows = [r for r in all_rows if r.get("dataset") == dataset_id]

    return all_rows


# ---------------------------------------------------------------------------
# Experiment assets — convergence/gantt PNGs + interactive route-map HTML that the
# notebooks render per (dataset, algorithm/level). Static bytes are served by the
# StaticFiles mount in app.py at /experiment-assets/<experiment_type>/<filename>;
# this endpoint just tells the frontend which filenames actually exist so it isn't
# guessing (scenario2 has no per-level convergence/gantt, only a combined chart).
# ---------------------------------------------------------------------------

_ASSETS_DIR = os.path.join(_EXPERIMENTS_DIR, "assets")

_DATASET_LABELS = {"dataset_a": "DatasetA", "dataset_b": "DatasetB"}


@router.get("/experiments/{experiment_type}/assets")
def get_experiment_assets(
    experiment_type: str,
    dataset_id: Optional[str] = Query(None),
) -> List[str]:
    """List available asset filenames for an experiment type, optionally filtered
    to one dataset's files (matched by the notebook's "DatasetA"/"DatasetB" prefix)."""
    if experiment_type not in VALID_EXPERIMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid experiment type. Valid: {VALID_EXPERIMENTS}",
        )

    asset_dir = os.path.normpath(os.path.join(_ASSETS_DIR, experiment_type))
    if not os.path.isdir(asset_dir):
        return []

    fnames = sorted(os.listdir(asset_dir))
    if dataset_id:
        prefix = _DATASET_LABELS.get(dataset_id)
        other_prefixes = [p for p in _DATASET_LABELS.values() if p != prefix]
        # Keep files for this dataset, plus dataset-agnostic shared charts (e.g.
        # scenario2's combined convergence grid covers both datasets in one image).
        fnames = [
            f for f in fnames
            if (prefix and f.startswith(prefix)) or not f.startswith(tuple(other_prefixes))
        ]
    return fnames
