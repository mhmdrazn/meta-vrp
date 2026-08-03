"""Stateless optimisation router (TASK 3 — public demo path, DB-free).

- POST /optimize     : load dataset via engine.io_utils, run engine.solve.solve(), return.
                      Does NOT persist anything.
- GET  /nodes        : list nodes for the selected dataset (backend-served, replaces the
                      hardcoded LOCAL_NODES in the frontend once TASK 8 lands).
- GET  /datasets     : discovery endpoint powering the frontend Dataset A/B selector.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from typing import List, Literal, Optional

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
