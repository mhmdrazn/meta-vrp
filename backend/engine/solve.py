"""Universal solver entry point.

Single function that the FastAPI endpoint AND the offline experiment notebooks call.
Zero dependency on FastAPI, SQLAlchemy, or DATABASE_URL — this module (and its
transitive imports) must remain importable in a bare Python environment that only has
numpy and pandas installed (see experiments/requirements.txt).

The dispatch is one pipeline with a conditional branch, not three duplicated paths:
    - alns_standard = alns_optimize only
    - alns_hybrid   = alns_optimize + improve_routes (the existing behaviour)
    - aco           = aco_optimize only

Every algorithm produces the same output structure so downstream code (API response
mapping, CSV writers) is algorithm-agnostic.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np

from .algorithms.aco import ACOConfig, aco_optimize
from .algorithms.alns import ALNSConfig, alns_optimize
from .construct import greedy_construct
from .data import Node, TimeMatrix
from .evaluation import (
    count_active_vehicles,
    count_refill_visits,
    load_profile_liters,
    makespan_minutes,
    route_time_minutes,
    route_time_std,
    total_time_minutes,
)
from .improve import improve_routes
from .utils import (
    build_groups_from_expanded_ids,
    ensure_all_routes_capacity,
    ensure_groups_single_vehicle,
)
from .validators import is_feasible

log = logging.getLogger(__name__)

Algorithm = Literal["aco", "alns_standard", "alns_hybrid"]


# ---------------------------------------------------------------------------
# Split-delivery expansion (moved verbatim from backend/app.py)
# ---------------------------------------------------------------------------
def expand_split_delivery(
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    selected_ids: List[str],
    vehicle_capacity: float,
) -> Tuple[Dict[str, Node], TimeMatrix, List[str]]:
    """Split parks whose demand > capacity into multiple 'id#k' sub-nodes.

    Depot/refill are never split. TimeMatrix is extended by duplicating rows/columns
    keyed on the base id. selected_ids is expanded so '1' becomes ['1#1', '1#2', ...].
    Service time is prorated by the volume served per sub-node.
    """
    orig_ids: List[str] = tm.ids
    index = tm.index

    new_nodes: Dict[str, Node] = {}
    new_ids: List[str] = []

    split_plan: Dict[str, int] = {}
    for nid in orig_ids:
        n = nodes[nid]
        if n.type == "park" and n.demand_liters > vehicle_capacity:
            k = math.ceil(n.demand_liters / vehicle_capacity)
            split_plan[nid] = k

    for nid in orig_ids:
        n = nodes[nid]
        if nid in split_plan:
            k = split_plan[nid]
            total = n.demand_liters
            remaining = total
            for i in range(1, k + 1):
                served = min(vehicle_capacity, remaining)
                remaining -= served
                sub_id = f"{nid}#{i}"
                sub_service = n.service_min * (served / total) if total > 0 else 0.0
                new_nodes[sub_id] = Node(
                    id=sub_id,
                    name=f"{n.name} (part {i}/{k})",
                    lat=n.lat,
                    lon=n.lon,
                    type=n.type,
                    demand_liters=served,
                    service_min=sub_service,
                )
                new_ids.append(sub_id)
        else:
            new_nodes[nid] = n
            new_ids.append(nid)

    m2 = np.zeros((len(new_ids), len(new_ids)), dtype=float)
    for i, nid_i in enumerate(new_ids):
        base_i = nid_i.split("#")[0]
        ii = index[base_i]
        for j, nid_j in enumerate(new_ids):
            base_j = nid_j.split("#")[0]
            jj = index[base_j]
            m2[i, j] = tm.M[ii, jj]

    tm2 = TimeMatrix(new_ids, m2)

    expanded_selected: List[str] = []
    for sid in selected_ids:
        if sid in split_plan:
            k = split_plan[sid]
            expanded_selected.extend([f"{sid}#{i}" for i in range(1, k + 1)])
        else:
            expanded_selected.append(sid)

    return new_nodes, tm2, expanded_selected


def split_route_into_k_by_load(
    route: List[str],
    nodes: Dict[str, Node],
    vehicle_capacity: float,
    k: int,
    depot_id: str,
    groups: Dict[str, List[str]],
    part_to_group: Dict[str, str],
) -> List[List[str]]:
    """Split a single route into k roughly-load-balanced routes, group-safe."""
    if k <= 1 or len(route) <= 2:
        return [route[:]]

    parks_idx = []
    for i in range(1, len(route) - 1):
        node = nodes.get(route[i])
        if node and node.type == "park":
            parks_idx.append(i)

    if len(parks_idx) < k - 1:
        return [route[:]]

    total_demand = sum(
        nodes[route[i]].demand_liters for i in parks_idx if route[i] in nodes
    )
    if total_demand <= 0:
        return [route[:]]

    target = total_demand / k
    cuts = []
    acc = 0.0
    next_target = target
    last_valid_idx_before_target = -1

    for i in parks_idx:
        current_node_id = route[i]
        node_obj = nodes.get(current_node_id)
        if not node_obj:
            continue

        is_safe_cut_point = True
        if i + 1 < len(route) - 1:
            next_node_id = route[i + 1]
            next_node_obj = nodes.get(next_node_id)
            if next_node_obj and next_node_obj.type == "park":
                current_group = part_to_group.get(current_node_id)
                next_group = part_to_group.get(next_node_id)
                if current_group and next_group and current_group == next_group:
                    is_safe_cut_point = False

        acc += node_obj.demand_liters

        if acc < next_target - 1e-9 and is_safe_cut_point:
            last_valid_idx_before_target = i

        if acc >= next_target - 1e-9:
            cut_point_found = False
            if is_safe_cut_point:
                cuts.append(i)
                cut_point_found = True
            elif last_valid_idx_before_target != -1:
                if not cuts or cuts[-1] != last_valid_idx_before_target:
                    cuts.append(last_valid_idx_before_target)
                    cut_point_found = True

            if cut_point_found:
                last_valid_idx_before_target = -1
                next_target += target

            if len(cuts) >= k - 1:
                break

    def sanitize(seg: List[str]) -> List[str]:
        cleaned = [seg[0]]
        for nid in seg[1:]:
            if nid != cleaned[-1]:
                cleaned.append(nid)

        if len(cleaned) >= 3 and cleaned[-1] == depot_id:
            node_before_depot = nodes.get(cleaned[-2])
            if node_before_depot and node_before_depot.type == "refill":
                cleaned.pop(-2)

        has_park = False
        for n_id in cleaned[1:-1]:
            node_obj = nodes.get(n_id)
            if node_obj and node_obj.type == "park":
                has_park = True
                break
        return cleaned if has_park and len(cleaned) > 2 else []

    segments: List[List[str]] = []
    prev = 0
    cuts.sort()
    for cut in cuts:
        if cut < len(route) - 1 and cut > prev:
            seg = [depot_id] + route[prev + 1 : cut + 1] + [depot_id]
            seg = sanitize(seg)
            if seg:
                segments.append(seg)
            prev = cut

    seg = [depot_id] + route[prev + 1 : -1] + [depot_id]
    seg = sanitize(seg)
    if seg:
        segments.append(seg)

    while len(segments) < k:
        segments.append([depot_id, depot_id])

    while len(segments) > k and len(segments) > 1:
        last = segments.pop()
        if len(segments[-1]) > 1 and len(last) > 2:
            segments[-1] = segments[-1][:-1] + last[1:-1] + [depot_id]
        elif len(last) > 2:
            segments[-1] = last

    return segments


# ---------------------------------------------------------------------------
# Universal solve() entry point
# ---------------------------------------------------------------------------
def solve(
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    selected_park_ids: List[str],
    num_vehicles: int,
    depot_id: str,
    vehicle_capacity: float,
    refill_ids: List[str],
    allow_refill: bool,
    algorithm: Algorithm = "alns_hybrid",
    time_limit_sec: float = 30.0,
    seed: int = 42,
    alns_cfg: Optional[ALNSConfig] = None,
    aco_cfg: Optional[object] = None,
    alns_time_frac: float = 0.9,
) -> dict:
    """Universal solver — one interface, three algorithms, same output shape.

    Returns:
        {
            routes: List[List[str]],
            route_results: List[dict],   # per-vehicle dicts w/ vehicle_id, sequence, total_time_min, load_profile_liters
            total_time: float,
            makespan: float,
            route_time_std: float,
            active_vehicles: int,
            refill_visits: int,
            computation_time: float,
            feasible: bool,
            algorithm: str,
            seed: int,
            diagnostics: dict,           # free-form per-phase timing + expansion info
        }
    """
    t0 = time.perf_counter()

    # 1) Validate selected parks against original dataset
    if not selected_park_ids:
        raise ValueError("selected_park_ids must not be empty")
    selected_raw = [str(x) for x in selected_park_ids]
    for nid in selected_raw:
        if nid not in nodes:
            raise ValueError(f"Unknown node id (original dataset): {nid}")
        if getattr(nodes[nid], "type", None) != "park":
            raise ValueError(f"{nid} is not type=park")

    # 2) Split-delivery expansion (if any park's demand > vehicle capacity)
    nodes_exp, tm_exp, selected_ids_expanded = expand_split_delivery(
        nodes, tm, selected_raw, vehicle_capacity
    )
    groups, part_to_group = build_groups_from_expanded_ids(selected_ids_expanded)

    n = len(tm_exp.ids)
    if getattr(tm_exp.M, "shape", None) != (n, n):
        raise ValueError(f"time_matrix shape mismatch after expansion: expected ({n},{n})")
    if not np.isfinite(tm_exp.M).all():
        raise ValueError("time_matrix contains NaN/Inf after expansion")

    # 3) Resolve depot / refill in the (possibly expanded) node set
    if depot_id not in nodes_exp or getattr(nodes_exp[depot_id], "type", None) != "depot":
        depots = [nid for nid, node in nodes_exp.items() if node.type == "depot"]
        if len(depots) == 1:
            depot_id = depots[0]
        else:
            raise ValueError(f"Invalid depot_id. Available depots: {depots}")

    # Refill ids: caller-provided (may be a subset, e.g. Scenario 2's 50%/25% experiments).
    # We do NOT auto-fall back to "all refills" here — that decision belongs to the caller
    # (API endpoint or experiment script). Empty list = no refills available.
    refill_ids = list(refill_ids)

    # 4) Configure algorithm-specific time budgets
    if algorithm == "alns_hybrid":
        alns_time = max(0.0, time_limit_sec * alns_time_frac)
        improve_time = max(0.1, time_limit_sec - alns_time)
    else:
        # standard_alns / aco each get the full budget
        alns_time = time_limit_sec
        improve_time = 0.0

    if alns_cfg is None:
        alns_cfg = ALNSConfig(time_limit_sec=alns_time, seed=seed)
    else:
        # Override the time budget from the caller-supplied one (per-algorithm) but keep
        # every other tuning knob the caller specified.
        alns_cfg = ALNSConfig(
            time_limit_sec=alns_time,
            seed=alns_cfg.seed if alns_cfg.seed != ALNSConfig.seed else seed,
            init_temperature=alns_cfg.init_temperature,
            cooling_rate=alns_cfg.cooling_rate,
            min_temperature=alns_cfg.min_temperature,
            k_remove_min=alns_cfg.k_remove_min,
            k_remove_max=alns_cfg.k_remove_max,
            w_improve=alns_cfg.w_improve,
            w_accept=alns_cfg.w_accept,
            w_reject=alns_cfg.w_reject,
            score_update_period=alns_cfg.score_update_period,
            tabu_tenure=alns_cfg.tabu_tenure,
            use_tabu_on_removed_nodes=alns_cfg.use_tabu_on_removed_nodes,
            lambda_capacity=alns_cfg.lambda_capacity,
            use_construct_as_repair=alns_cfg.use_construct_as_repair,
            rebalance_period=alns_cfg.rebalance_period,
        )

    t_prep = time.perf_counter()

    # 5) Greedy construction (seed solution for every algorithm)
    routes = greedy_construct(
        nodes=nodes_exp,
        tm=tm_exp,
        selected_parks=selected_ids_expanded,
        depot_id=depot_id,
        num_vehicles=num_vehicles,
        vehicle_capacity=vehicle_capacity,
        allow_refill=allow_refill,
        refill_ids=refill_ids,
    )
    t_cons = time.perf_counter()

    if len(routes) == 1 and num_vehicles > 1:
        routes = split_route_into_k_by_load(
            route=routes[0],
            nodes=nodes_exp,
            vehicle_capacity=vehicle_capacity,
            k=num_vehicles,
            depot_id=depot_id,
            groups=groups,
            part_to_group=part_to_group,
        )

    # 6) Algorithm dispatch — the "hybrid" bit is literally just the extra improve phase
    algo_dur = 0.0
    improve_dur = 0.0
    if algorithm == "alns_standard":
        t_a0 = time.perf_counter()
        routes = alns_optimize(
            init_routes=routes,
            nodes=nodes_exp,
            tm=tm_exp,
            vehicle_capacity=vehicle_capacity,
            refill_ids=refill_ids,
            depot_id=depot_id,
            allow_refill=allow_refill,
            cfg=alns_cfg,
            groups=groups,
        )
        algo_dur = time.perf_counter() - t_a0
    elif algorithm == "alns_hybrid":
        t_a0 = time.perf_counter()
        routes = alns_optimize(
            init_routes=routes,
            nodes=nodes_exp,
            tm=tm_exp,
            vehicle_capacity=vehicle_capacity,
            refill_ids=refill_ids,
            depot_id=depot_id,
            allow_refill=allow_refill,
            cfg=alns_cfg,
            groups=groups,
        )
        algo_dur = time.perf_counter() - t_a0
        t_i0 = time.perf_counter()
        routes = improve_routes(
            routes,
            nodes_exp,
            tm_exp,
            vehicle_capacity=vehicle_capacity,
            refill_ids=refill_ids,
            depot_id=depot_id,
            time_limit_sec=improve_time,
            max_no_improve=10000,
            groups=groups,
        )
        improve_dur = time.perf_counter() - t_i0
    elif algorithm == "aco":
        t_a0 = time.perf_counter()
        aco_cfg_effective = aco_cfg if isinstance(aco_cfg, ACOConfig) else ACOConfig(
            time_limit_sec=time_limit_sec, seed=seed
        )
        # Force the caller's overall time budget onto ACO regardless of what the
        # caller passed in aco_cfg — same policy solve() applies to alns_cfg above,
        # keeps the three-algorithm comparison fair.
        aco_cfg_effective = ACOConfig(
            time_limit_sec=time_limit_sec,
            seed=seed if aco_cfg_effective.seed == ACOConfig.seed else aco_cfg_effective.seed,
            num_ants=aco_cfg_effective.num_ants,
            alpha=aco_cfg_effective.alpha,
            beta=aco_cfg_effective.beta,
            rho=aco_cfg_effective.rho,
            q0=aco_cfg_effective.q0,
            tau_min_factor=aco_cfg_effective.tau_min_factor,
            elitist=aco_cfg_effective.elitist,
        )
        routes = aco_optimize(
            init_routes=routes,
            nodes=nodes_exp,
            tm=tm_exp,
            vehicle_capacity=vehicle_capacity,
            refill_ids=refill_ids,
            depot_id=depot_id,
            allow_refill=allow_refill,
            groups=groups,
            cfg=aco_cfg_effective,
        )
        algo_dur = time.perf_counter() - t_a0
    else:
        raise ValueError(f"Unknown algorithm: {algorithm!r}")

    # 7) Safety passes (identical for every algorithm — this is what makes ACO/ALNS use
    #    the same feasibility semantics)
    routes = ensure_groups_single_vehicle(
        routes,
        groups,
        nodes_exp,
        tm_exp,
        depot_id,
        vehicle_capacity=vehicle_capacity,
        refill_ids=refill_ids,
    )
    routes, _ = ensure_all_routes_capacity(
        routes, nodes_exp, vehicle_capacity, refill_ids, tm_exp, depot_id
    )

    # 8) Evaluate — every metric downstream code needs
    obj_makespan = makespan_minutes(routes, nodes_exp, tm_exp)
    obj_total = total_time_minutes(routes, nodes_exp, tm_exp)
    obj_std = route_time_std(routes, nodes_exp, tm_exp)
    active = count_active_vehicles(routes)
    refills = count_refill_visits(routes, nodes_exp)
    feasible = is_feasible(routes, nodes_exp, vehicle_capacity, selected_ids_expanded)

    route_results: List[dict] = []
    for vid, r in enumerate(routes):
        if len(r) <= 2:
            continue
        route_results.append(
            {
                "vehicle_id": vid,
                "sequence": r,
                "total_time_min": route_time_minutes(r, nodes_exp, tm_exp),
                "load_profile_liters": load_profile_liters(r, nodes_exp, vehicle_capacity),
            }
        )

    t_end = time.perf_counter()

    return {
        "routes": routes,
        "route_results": route_results,
        "total_time": obj_total,
        "makespan": obj_makespan,
        "route_time_std": obj_std,
        "active_vehicles": active,
        "refill_visits": refills,
        "computation_time": t_end - t0,
        "feasible": feasible,
        "algorithm": algorithm,
        "seed": seed,
        "diagnostics": {
            "depot_id": depot_id,
            "nodes_loaded": len(nodes_exp),
            "refill_count": len(refill_ids),
            "timing_sec": {
                "prep": round(t_prep - t0, 4),
                "construct": round(t_cons - t_prep, 4),
                "algorithm": round(algo_dur, 4),
                "improve": round(improve_dur, 4),
                "total": round(t_end - t0, 4),
            },
            "expanded": {
                "selected_in": selected_raw,
                "selected_expanded": selected_ids_expanded,
                "groups_count": len(groups),
            },
        },
    }
