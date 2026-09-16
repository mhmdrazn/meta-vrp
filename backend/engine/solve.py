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
import random
import time
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np

from .algorithms.aco import ACOConfig, aco_optimize
from .algorithms.alns import ALNSConfig, alns_optimize
from .algorithms.hybrid import HybridConfig, hybrid_optimize
from .construct import greedy_construct
from .data import Node, TimeMatrix
from .evaluation import (
    count_active_vehicles,
    count_refill_visits,
    evaluate_route,
    load_profile_liters,
    makespan_minutes,
    route_time_minutes,
    route_time_std,
    total_time_minutes,
)
from .objective import evaluate_solution, search_objective
from .utils import (
    build_groups_from_expanded_ids,
    ensure_all_routes_capacity,
    ensure_groups_single_vehicle,
    set_seed,
    weighted_choice,
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


def _rebalance_solution_dmap(
    dmap: Dict[int, Dict[str, float]],
    num_trucks: int,
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: str,
    refill_ids: List[str],
    vehicle_capacity: float,
    max_passes: int = 10,
) -> Tuple[List[List[str]], Dict[int, Dict[str, float]]]:
    from .evaluation import (
        evaluate_route,
        rebuild_route_with_refills,
        rebuild_routes_from_dmap,
    )

    park_ids = [nid for nid, n in nodes.items() if n.type == "park"]
    current = {ri: dict(dmap[ri]) for ri in range(num_trucks)}
    for _ in range(max_passes):
        routes = rebuild_routes_from_dmap(
            current, num_trucks, nodes, tm, depot_id, refill_ids, vehicle_capacity
        )
        times = [
            (
                evaluate_route(
                    routes[ri],
                    nodes,
                    tm,
                    vehicle_capacity=vehicle_capacity,
                    delivery_amounts=current[ri],
                )[0]
                if len(routes[ri]) > 2
                else 0.0
            )
            for ri in range(num_trucks)
        ]
        heaviest_idx = max(range(num_trucks), key=lambda i: times[i])
        lightest_idx = min(range(num_trucks), key=lambda i: times[i])
        gap = times[heaviest_idx] - times[lightest_idx]
        mean_t = sum(times) / max(1, sum(1 for t in times if t > 0))
        if mean_t < 1.0 or gap < mean_t * 0.15:
            break
        heavy_parks = sorted(
            [nd for nd in park_ids if current[heaviest_idx].get(nd, 0.0) > 0.1],
            key=lambda nd: current[heaviest_idx][nd],
            reverse=True,
        )
        if not heavy_parks:
            break
        moved = False
        for nd in heavy_parks:
            amt = current[heaviest_idx][nd]
            for frac in [1.0, 0.5]:
                partial = amt * frac
                if partial < 1.0:
                    continue
                tmp_heavy = dict(current[heaviest_idx])
                tmp_heavy[nd] -= partial
                if tmp_heavy[nd] <= 0.1:
                    tmp_heavy.pop(nd, None)
                tmp_light = dict(current[lightest_idx])
                tmp_light[nd] = tmp_light.get(nd, 0.0) + partial
                r_h = rebuild_route_with_refills(
                    [x for x in park_ids if tmp_heavy.get(x, 0.0) > 0.1],
                    tmp_heavy,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                )
                r_l = rebuild_route_with_refills(
                    [x for x in park_ids if tmp_light.get(x, 0.0) > 0.1],
                    tmp_light,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                )
                t_h, f_h, _ = evaluate_route(
                    r_h,
                    nodes,
                    tm,
                    vehicle_capacity=vehicle_capacity,
                    delivery_amounts=tmp_heavy,
                )
                t_l, f_l, _ = evaluate_route(
                    r_l,
                    nodes,
                    tm,
                    vehicle_capacity=vehicle_capacity,
                    delivery_amounts=tmp_light,
                )
                if f_h and f_l and (max(t_h, t_l) < times[heaviest_idx]):
                    current[heaviest_idx] = tmp_heavy
                    current[lightest_idx] = tmp_light
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
    sol = rebuild_routes_from_dmap(
        current, num_trucks, nodes, tm, depot_id, refill_ids, vehicle_capacity
    )
    return sol, current


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
    hybrid_cfg: Optional[HybridConfig] = None,
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
    is_original_parks = all(
        nid in nodes and getattr(nodes[nid], "type", None) == "park"
        for nid in selected_raw
    )

    if is_original_parks:
        # Use notebook-aligned delivery-map optimization (cell 30, 31, 34-37)
        # Guarantees exact apple-to-apple fitness comparison with notebook baseline CSV
        from .construct import build_initial_solution, repair_empty_trucks
        from .evaluation import rebuild_route_with_refills, rebuild_routes_from_dmap

        sol, dmap = build_initial_solution(
            num_vehicles, nodes, tm, depot_id, refill_ids, vehicle_capacity, seed=seed
        )
        sol, dmap = repair_empty_trucks(
            sol, dmap, num_vehicles, nodes, tm, depot_id, refill_ids, vehicle_capacity
        )

        park_ids = [nid for nid, n in nodes.items() if n.type == "park"]
        refill_set = set(refill_ids)
        park_set = set(park_ids)

        if algorithm == "alns_standard":
            max_iter = alns_cfg.max_iter if (alns_cfg and alns_cfg.max_iter) else 200
            # Run ALNS on delivery_map
            rng = random.Random(seed)
            set_seed(seed)

            cur_dmap = {ri: dict(dmap[ri]) for ri in range(num_vehicles)}
            cur_fit = evaluate_solution(
                rebuild_routes_from_dmap(
                    cur_dmap,
                    num_vehicles,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                ),
                nodes,
                tm,
                delivery_map=cur_dmap,
            )[0]
            best_dmap = dict(cur_dmap)
            best_fit = cur_fit

            from .algorithms.alns import destroy_random, destroy_worst, destroy_shaw

            destroys = [destroy_random, destroy_worst, destroy_shaw]
            dw = [1.0] * len(destroys)
            rw = [1.0, 1.0]
            ds = [0.0] * len(destroys)
            rs = [0.0] * 2
            dc = [0] * len(destroys)
            rc = [0] * 2
            T = 100.0

            for it in range(max_iter):
                di = weighted_choice(dw, rng)
                ri_ = weighted_choice(rw, rng)
                k = rng.randint(1, max(2, len(park_ids) // 4))

                # destroy
                d_dmap = {ri: dict(cur_dmap[ri]) for ri in range(num_vehicles)}
                asg = [
                    (ri, nd)
                    for ri in range(num_vehicles)
                    for nd in park_ids
                    if d_dmap[ri].get(nd, 0) > 0.1
                ]
                removed = []
                if di == 0:
                    for ri, nd in rng.sample(asg, min(k, len(asg))):
                        amt = d_dmap[ri].pop(nd, 0)
                        if amt > 0.1:
                            removed.append((nd, amt))
                elif di == 1:
                    costs = []
                    for ri in range(num_vehicles):
                        base = evaluate_route(
                            rebuild_route_with_refills(
                                [x for x in park_ids if d_dmap[ri].get(x, 0) > 0.1],
                                d_dmap[ri],
                                nodes,
                                tm,
                                depot_id,
                                refill_ids,
                                vehicle_capacity,
                            ),
                            nodes,
                            tm,
                            vehicle_capacity=vehicle_capacity,
                            delivery_amounts=d_dmap[ri],
                        )[0]
                        for nd in list(d_dmap[ri].keys()):
                            tmp = dict(d_dmap[ri])
                            tmp.pop(nd, None)
                            t2 = evaluate_route(
                                rebuild_route_with_refills(
                                    [x for x in park_ids if tmp.get(x, 0) > 0.1],
                                    tmp,
                                    nodes,
                                    tm,
                                    depot_id,
                                    refill_ids,
                                    vehicle_capacity,
                                ),
                                nodes,
                                tm,
                                vehicle_capacity=vehicle_capacity,
                                delivery_amounts=tmp,
                            )[0]
                            costs.append((base - t2, ri, nd))
                    costs.sort(reverse=True)
                    for _, ri, nd in costs[: min(k, len(costs))]:
                        amt = d_dmap[ri].pop(nd, 0)
                        if amt > 0.1:
                            removed.append((nd, amt))
                else:
                    if asg:
                        seed_ri, seed_nd = rng.choice(asg)
                        ranked = sorted(asg, key=lambda x: tm.travel(seed_nd, x[1]))
                        for ri, nd in ranked[: min(k, len(ranked))]:
                            amt = d_dmap[ri].pop(nd, 0)
                            if amt > 0.1:
                                removed.append((nd, amt))

                if not removed:
                    continue

                # repair
                new_dmap = {ri: dict(d_dmap[ri]) for ri in range(num_vehicles)}
                for nd, amt in removed:
                    best_ri, best_t = None, float("inf")
                    for ri in range(num_vehicles):
                        tmp = dict(new_dmap[ri])
                        tmp[nd] = tmp.get(nd, 0) + amt
                        seq = [x for x in park_ids if tmp.get(x, 0) > 0.1]
                        t, feas, _ = evaluate_route(
                            rebuild_route_with_refills(
                                seq,
                                tmp,
                                nodes,
                                tm,
                                depot_id,
                                refill_ids,
                                vehicle_capacity,
                            ),
                            nodes,
                            tm,
                            vehicle_capacity=vehicle_capacity,
                            delivery_amounts=tmp,
                        )
                        if feas and t < best_t:
                            best_t, best_ri = t, ri
                    if best_ri is None:
                        best_ri = min(
                            range(num_vehicles), key=lambda r: sum(new_dmap[r].values())
                        )
                    new_dmap[best_ri][nd] = new_dmap[best_ri].get(nd, 0) + amt

                new_sol = rebuild_routes_from_dmap(
                    new_dmap,
                    num_vehicles,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                )
                new_sol, new_dmap = repair_empty_trucks(
                    new_sol,
                    new_dmap,
                    num_vehicles,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                )
                new_fit = evaluate_solution(new_sol, nodes, tm, delivery_map=new_dmap)[
                    0
                ]

                score = 0.0
                if new_fit < best_fit - 1e-6:
                    best_fit = new_fit
                    best_dmap = dict(new_dmap)
                    cur_dmap, cur_fit = new_dmap, new_fit
                    score = 3.0
                elif new_fit < cur_fit - 1e-6:
                    cur_dmap, cur_fit = new_dmap, new_fit
                    score = 2.0
                elif rng.random() < np.exp(-(new_fit - cur_fit) / max(T, 1e-6)):
                    cur_dmap, cur_fit = new_dmap, new_fit
                    score = 1.0

                T = max(T * 0.995, 1e-3)

            routes = rebuild_routes_from_dmap(
                best_dmap,
                num_vehicles,
                nodes,
                tm,
                depot_id,
                refill_ids,
                vehicle_capacity,
            )
            final_dmap = best_dmap

        elif algorithm == "aco":
            max_iter = aco_cfg.max_iter if (aco_cfg and aco_cfg.max_iter) else 200
            alpha = aco_cfg.alpha if aco_cfg else 1.0
            beta = aco_cfg.beta if aco_cfg else 2.0
            rho = aco_cfg.rho if aco_cfg else 0.1

            rng = random.Random(seed)
            set_seed(seed)
            all_nids = list(nodes.keys())
            node_to_idx = {nid: i for i, nid in enumerate(all_nids)}
            N = len(nodes)
            pher = np.ones((N, N))

            total_demand = sum(nodes[nd].demand_liters for nd in park_ids)
            demand_budget = total_demand / num_vehicles * 1.15
            best_sol, best_dmap, best_fit = None, None, float("inf")

            refills_available = (
                refill_ids
                if refill_ids
                else [nid for nid, n in nodes.items() if n.type == "refill"]
            )
            init_r = (
                min(refills_available, key=lambda r: tm.travel(depot_id, r))
                if refills_available
                else depot_id
            )
            init_overhead = tm.travel(depot_id, init_r) + 5.0

            for it in range(max_iter):
                iter_best_fit = float("inf")
                iter_best = None
                for _ in range(20):
                    park_list = list(park_ids)
                    rng.shuffle(park_list)
                    remaining = {nd: float(nodes[nd].demand_liters) for nd in park_list}
                    dmap_a = {ri: {} for ri in range(num_vehicles)}
                    for tid in range(num_vehicles):
                        cur_t = init_overhead
                        load = float(vehicle_capacity)
                        pos = depot_id
                        delivered_vol = 0.0
                        while True:
                            unmet = {nd: d for nd, d in remaining.items() if d > 0.1}
                            if not unmet:
                                break
                            cands = []
                            for nd, dem in unmet.items():
                                dv = min(load, dem)
                                if dv <= 0:
                                    continue
                                ft = (
                                    cur_t
                                    + tm.travel(pos, nd)
                                    + 20.0 * (dv / vehicle_capacity)
                                )
                                if ft + tm.travel(nd, depot_id) <= 540.0:
                                    cands.append(nd)
                            if not cands:
                                if load >= vehicle_capacity - 0.1:
                                    break
                                if not refills_available:
                                    break
                                nr = min(
                                    refills_available, key=lambda r: tm.travel(pos, r)
                                )
                                tr = tm.travel(pos, nr)
                                if cur_t + tr + 5.0 + tm.travel(nr, depot_id) > 540.0:
                                    break
                                cur_t += tr + 5.0
                                load = float(vehicle_capacity)
                                pos = nr
                                continue
                            weights_a = []
                            for nd in cands:
                                p_i, n_i = node_to_idx[pos], node_to_idx[nd]
                                tau = (pher[p_i][n_i]) ** alpha
                                eta = (1.0 / (tm.travel(pos, nd) + 1e-6)) ** beta
                                weights_a.append(tau * eta)
                            tot = sum(weights_a)
                            if tot <= 0:
                                nd = rng.choice(cands)
                            else:
                                r_val = rng.random() * tot
                                acc = 0.0
                                nd = cands[-1]
                                for c, w in zip(cands, weights_a):
                                    acc += w
                                    if acc >= r_val:
                                        nd = c
                                        break
                            dv = min(load, remaining[nd], demand_budget - delivered_vol)
                            if dv <= 0:
                                break
                            dmap_a[tid][nd] = dmap_a[tid].get(nd, 0.0) + dv
                            remaining[nd] -= dv
                            delivered_vol += dv
                            load -= dv
                            cur_t += tm.travel(pos, nd) + 20.0 * (dv / vehicle_capacity)
                            pos = nd
                            if load < 1.0 and refills_available:
                                nr = min(
                                    refills_available, key=lambda r: tm.travel(pos, r)
                                )
                                tr = tm.travel(pos, nr)
                                if cur_t + tr + 5.0 + tm.travel(nr, depot_id) <= 540.0:
                                    cur_t += tr + 5.0
                                    load = float(vehicle_capacity)
                                    pos = nr

                    leftover = {nd: d for nd, d in remaining.items() if d > 0.1}
                    if leftover:
                        for nd, d in list(leftover.items()):
                            if d <= 0.1:
                                continue
                            order = sorted(
                                range(num_vehicles),
                                key=lambda ri: sum(dmap_a[ri].values()),
                            )
                            for ri in order:
                                if d <= 0.1:
                                    break
                                tmp = dict(dmap_a[ri])
                                tmp[nd] = tmp.get(nd, 0.0) + d
                                seq = [x for x in park_ids if tmp.get(x, 0.0) > 0.1]
                                route = rebuild_route_with_refills(
                                    seq,
                                    tmp,
                                    nodes,
                                    tm,
                                    depot_id,
                                    refills_available,
                                    vehicle_capacity,
                                )
                                t, feas, _ = evaluate_route(
                                    route,
                                    nodes,
                                    tm,
                                    vehicle_capacity=vehicle_capacity,
                                    delivery_amounts=tmp,
                                )
                                if feas:
                                    dmap_a[ri][nd] = dmap_a[ri].get(nd, 0.0) + d
                                    d = 0.0
                                else:
                                    partial = d / 2
                                    while partial > 0.1:
                                        tmp2 = dict(dmap_a[ri])
                                        tmp2[nd] = tmp2.get(nd, 0.0) + partial
                                        seq2 = [
                                            x
                                            for x in park_ids
                                            if tmp2.get(x, 0.0) > 0.1
                                        ]
                                        route2 = rebuild_route_with_refills(
                                            seq2,
                                            tmp2,
                                            nodes,
                                            tm,
                                            depot_id,
                                            refills_available,
                                            vehicle_capacity,
                                        )
                                        t2, feas2, _ = evaluate_route(
                                            route2,
                                            nodes,
                                            tm,
                                            vehicle_capacity=vehicle_capacity,
                                            delivery_amounts=tmp2,
                                        )
                                        if feas2:
                                            dmap_a[ri][nd] = (
                                                dmap_a[ri].get(nd, 0.0) + partial
                                            )
                                            d -= partial
                                            break
                                        partial /= 2
                            if d > 0.1:
                                lightest = min(
                                    range(num_vehicles),
                                    key=lambda ri: sum(dmap_a[ri].values()),
                                )
                                dmap_a[lightest][nd] = dmap_a[lightest].get(nd, 0.0) + d

                    sol_a = rebuild_routes_from_dmap(
                        dmap_a,
                        num_vehicles,
                        nodes,
                        tm,
                        depot_id,
                        refill_ids,
                        vehicle_capacity,
                    )
                    sol_a, dmap_a = repair_empty_trucks(
                        sol_a,
                        dmap_a,
                        num_vehicles,
                        nodes,
                        tm,
                        depot_id,
                        refill_ids,
                        vehicle_capacity,
                    )
                    fit_val, _ = evaluate_solution(
                        sol_a, nodes, tm, delivery_map=dmap_a
                    )
                    if fit_val < iter_best_fit:
                        iter_best_fit = fit_val
                        iter_best = (sol_a, dmap_a)

                if iter_best_fit < best_fit:
                    best_fit = iter_best_fit
                    best_sol = [r[:] for r in iter_best[0]]
                    best_dmap = {
                        ri: dict(iter_best[1][ri]) for ri in range(num_vehicles)
                    }

                pher *= 1.0 - rho
                for fit_s, sol_s in [
                    (iter_best_fit, iter_best[0]),
                    (best_fit, best_sol),
                ]:
                    deposit = 1.0 / (fit_s + 1e-9)
                    for route in sol_s:
                        for a, b in zip(route[:-1], route[1:]):
                            p_a, p_b = node_to_idx[a], node_to_idx[b]
                            pher[p_a][p_b] += deposit
                            pher[p_b][p_a] += deposit

            routes = best_sol
            final_dmap = best_dmap

        else:  # alns_hybrid
            max_iter = (
                hybrid_cfg.max_iter if (hybrid_cfg and hybrid_cfg.max_iter) else 200
            )
            alpha = hybrid_cfg.alpha if hybrid_cfg else 1.0
            beta = hybrid_cfg.beta if hybrid_cfg else 2.0

            rng = random.Random(seed)
            set_seed(seed)
            all_nids = list(nodes.keys())
            node_to_idx = {nid: i for i, nid in enumerate(all_nids)}
            N = len(nodes)
            pher = np.ones((N, N))

            init_fit, _ = evaluate_solution(sol, nodes, tm, delivery_map=dmap)
            for route in sol:
                dep_val = 100.0 / (init_fit + 1e-9)
                for a, b in zip(route[:-1], route[1:]):
                    p_a, p_b = node_to_idx[a], node_to_idx[b]
                    pher[p_a][p_b] += dep_val
                    pher[p_b][p_a] += dep_val

            cur_dmap = {ri: dict(dmap[ri]) for ri in range(num_vehicles)}
            cur_fit = init_fit
            best_dmap = dict(cur_dmap)
            best_fit = cur_fit

            dw = [1.0, 1.0, 1.0]
            rw = [1.0, 1.0, 1.0]
            T = 100.0

            for it in range(max_iter):
                di = weighted_choice(dw, rng)
                ri_ = weighted_choice(rw, rng)
                k = rng.randint(1, max(2, len(park_ids) // 4))

                d_dmap = {ri: dict(cur_dmap[ri]) for ri in range(num_vehicles)}
                asg = [
                    (ri, nd)
                    for ri in range(num_vehicles)
                    for nd in park_ids
                    if d_dmap[ri].get(nd, 0) > 0.1
                ]
                removed = []
                if di == 0:
                    for ri, nd in rng.sample(asg, min(k, len(asg))):
                        amt = d_dmap[ri].pop(nd, 0)
                        if amt > 0.1:
                            removed.append((nd, amt))
                elif di == 1:
                    costs = []
                    for ri in range(num_vehicles):
                        base = evaluate_route(
                            rebuild_route_with_refills(
                                [x for x in park_ids if d_dmap[ri].get(x, 0) > 0.1],
                                d_dmap[ri],
                                nodes,
                                tm,
                                depot_id,
                                refill_ids,
                                vehicle_capacity,
                            ),
                            nodes,
                            tm,
                            vehicle_capacity=vehicle_capacity,
                            delivery_amounts=d_dmap[ri],
                        )[0]
                        for nd in list(d_dmap[ri].keys()):
                            tmp = dict(d_dmap[ri])
                            tmp.pop(nd, None)
                            t2 = evaluate_route(
                                rebuild_route_with_refills(
                                    [x for x in park_ids if tmp.get(x, 0) > 0.1],
                                    tmp,
                                    nodes,
                                    tm,
                                    depot_id,
                                    refill_ids,
                                    vehicle_capacity,
                                ),
                                nodes,
                                tm,
                                vehicle_capacity=vehicle_capacity,
                                delivery_amounts=tmp,
                            )[0]
                            costs.append((base - t2, ri, nd))
                    costs.sort(reverse=True)
                    for _, ri, nd in costs[: min(k, len(costs))]:
                        amt = d_dmap[ri].pop(nd, 0)
                        if amt > 0.1:
                            removed.append((nd, amt))
                else:
                    if asg:
                        seed_ri, seed_nd = rng.choice(asg)
                        ranked = sorted(asg, key=lambda x: tm.travel(seed_nd, x[1]))
                        for ri, nd in ranked[: min(k, len(ranked))]:
                            amt = d_dmap[ri].pop(nd, 0)
                            if amt > 0.1:
                                removed.append((nd, amt))

                if not removed:
                    continue

                new_dmap = {ri: dict(d_dmap[ri]) for ri in range(num_vehicles)}
                if ri_ < 2:
                    for nd, amt in removed:
                        best_ri, best_t = None, float("inf")
                        for ri in range(num_vehicles):
                            tmp = dict(new_dmap[ri])
                            tmp[nd] = tmp.get(nd, 0) + amt
                            seq = [x for x in park_ids if tmp.get(x, 0) > 0.1]
                            t, feas, _ = evaluate_route(
                                rebuild_route_with_refills(
                                    seq,
                                    tmp,
                                    nodes,
                                    tm,
                                    depot_id,
                                    refill_ids,
                                    vehicle_capacity,
                                ),
                                nodes,
                                tm,
                                vehicle_capacity=vehicle_capacity,
                                delivery_amounts=tmp,
                            )
                            if feas and t < best_t:
                                best_t, best_ri = t, ri
                        if best_ri is None:
                            best_ri = min(
                                range(num_vehicles),
                                key=lambda r: sum(new_dmap[r].values()),
                            )
                        new_dmap[best_ri][nd] = new_dmap[best_ri].get(nd, 0) + amt
                else:
                    for nd, amt in removed:
                        weights_h = []
                        for ri in range(num_vehicles):
                            tmp = dict(new_dmap[ri])
                            tmp[nd] = tmp.get(nd, 0) + amt
                            seq = [x for x in park_ids if tmp.get(x, 0) > 0.1]
                            r_b = rebuild_route_with_refills(
                                seq,
                                tmp,
                                nodes,
                                tm,
                                depot_id,
                                refill_ids,
                                vehicle_capacity,
                            )
                            t, feas, _ = evaluate_route(
                                r_b,
                                nodes,
                                tm,
                                vehicle_capacity=vehicle_capacity,
                                delivery_amounts=tmp,
                            )
                            if not feas:
                                weights_h.append(0.0)
                                continue
                            idx = r_b.index(nd) if nd in r_b else -1
                            last = r_b[idx - 1] if idx > 0 else depot_id
                            tau_h = (pher[node_to_idx[last]][node_to_idx[nd]]) ** alpha
                            eta_h = (1.0 / (t + 1e-6)) ** beta
                            weights_h.append(tau_h * eta_h)
                        tot_h = sum(weights_h)
                        if tot_h <= 0:
                            target_ri = min(
                                range(num_vehicles),
                                key=lambda r: sum(new_dmap[r].values()),
                            )
                        else:
                            r_h = rng.random() * tot_h
                            acc_h = 0.0
                            target_ri = num_vehicles - 1
                            for i, w in enumerate(weights_h):
                                acc_h += w
                                if acc_h >= r_h:
                                    target_ri = i
                                    break
                        new_dmap[target_ri][nd] = new_dmap[target_ri].get(nd, 0) + amt

                new_sol = rebuild_routes_from_dmap(
                    new_dmap,
                    num_vehicles,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                )
                new_sol, new_dmap = repair_empty_trucks(
                    new_sol,
                    new_dmap,
                    num_vehicles,
                    nodes,
                    tm,
                    depot_id,
                    refill_ids,
                    vehicle_capacity,
                )
                new_fit = evaluate_solution(new_sol, nodes, tm, delivery_map=new_dmap)[
                    0
                ]

                if new_fit < best_fit - 1e-6:
                    best_fit = new_fit
                    best_dmap = dict(new_dmap)
                    cur_dmap, cur_fit = new_dmap, new_fit
                    dep_val = 100.0 / (new_fit + 1e-9)
                    for route in new_sol:
                        for a, b in zip(route[:-1], route[1:]):
                            p_a, p_b = node_to_idx[a], node_to_idx[b]
                            pher[p_a][p_b] += dep_val
                            pher[p_b][p_a] += dep_val
                elif new_fit < cur_fit - 1e-6:
                    cur_dmap, cur_fit = new_dmap, new_fit
                elif rng.random() < np.exp(-(new_fit - cur_fit) / max(T, 1e-6)):
                    cur_dmap, cur_fit = new_dmap, new_fit

                T = max(T * 0.995, 1e-3)

            routes = rebuild_routes_from_dmap(
                best_dmap,
                num_vehicles,
                nodes,
                tm,
                depot_id,
                refill_ids,
                vehicle_capacity,
            )
            final_dmap = best_dmap

        # Rebalance
        routes, final_dmap = _rebalance_solution_dmap(
            final_dmap, num_vehicles, nodes, tm, depot_id, refill_ids, vehicle_capacity
        )

        obj_fitness, feasible = evaluate_solution(
            routes, nodes, tm, delivery_map=final_dmap
        )
        times = [
            evaluate_route(
                r,
                nodes,
                tm,
                vehicle_capacity=vehicle_capacity,
                delivery_amounts=final_dmap[ri],
            )[0]
            for ri, r in enumerate(routes)
            if len(r) > 2
        ]
        obj_total = sum(times)
        obj_makespan = max(times) if times else 0.0
        obj_std = float(np.std(times)) if len(times) > 1 else 0.0
        active = len(times)
        refill_set = set(refill_ids)
        refills = sum(1 for r in routes for nd in r[1:-1] if nd in refill_set)

        route_results: List[dict] = []
        for vid, r in enumerate(routes):
            if len(r) <= 2:
                continue
            route_results.append(
                {
                    "vehicle_id": vid,
                    "sequence": r,
                    "total_time_min": evaluate_route(
                        r,
                        nodes,
                        tm,
                        vehicle_capacity=vehicle_capacity,
                        delivery_amounts=final_dmap[vid],
                    )[0],
                    "load_profile_liters": load_profile_liters(
                        r, nodes, vehicle_capacity
                    ),
                }
            )

        t_end = time.perf_counter()
        return {
            "routes": routes,
            "route_results": route_results,
            "fitness": obj_fitness,
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
                "nodes_loaded": len(nodes),
                "refill_count": len(refill_ids),
                "timing_sec": {
                    "algorithm": round(t_end - t0, 4),
                },
            },
        }

    # Fallback to expansion if nodes are expanded
    nodes_exp, tm_exp, selected_ids_expanded = expand_split_delivery(
        nodes, tm, selected_raw, vehicle_capacity
    )
    groups, part_to_group = build_groups_from_expanded_ids(selected_ids_expanded)

    n = len(tm_exp.ids)
    if getattr(tm_exp.M, "shape", None) != (n, n):
        raise ValueError(
            f"time_matrix shape mismatch after expansion: expected ({n},{n})"
        )
    if not np.isfinite(tm_exp.M).all():
        raise ValueError("time_matrix contains NaN/Inf after expansion")

    # 3) Resolve depot / refill in the (possibly expanded) node set
    if (
        depot_id not in nodes_exp
        or getattr(nodes_exp[depot_id], "type", None) != "depot"
    ):
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
    # All algorithms now get the full time budget (hybrid no longer needs a split)
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
        hybrid_cfg_effective = (
            hybrid_cfg
            if isinstance(hybrid_cfg, HybridConfig)
            else HybridConfig(time_limit_sec=time_limit_sec, seed=seed)
        )
        # Force the caller's overall time budget onto Hybrid
        hybrid_cfg_effective = HybridConfig(
            time_limit_sec=time_limit_sec,
            seed=(
                seed
                if hybrid_cfg_effective.seed == HybridConfig.seed
                else hybrid_cfg_effective.seed
            ),
            init_temperature=hybrid_cfg_effective.init_temperature,
            cooling_rate=hybrid_cfg_effective.cooling_rate,
            min_temperature=hybrid_cfg_effective.min_temperature,
            k_remove_min=hybrid_cfg_effective.k_remove_min,
            k_remove_max=hybrid_cfg_effective.k_remove_max,
            score_update_period=hybrid_cfg_effective.score_update_period,
            react=hybrid_cfg_effective.react,
            alpha=hybrid_cfg_effective.alpha,
            beta=hybrid_cfg_effective.beta,
            rho=hybrid_cfg_effective.rho,
            deposit_multiplier=hybrid_cfg_effective.deposit_multiplier,
            rebalance_period=hybrid_cfg_effective.rebalance_period,
        )
        routes = hybrid_optimize(
            init_routes=routes,
            nodes=nodes_exp,
            tm=tm_exp,
            vehicle_capacity=vehicle_capacity,
            refill_ids=refill_ids,
            depot_id=depot_id,
            allow_refill=allow_refill,
            groups=groups,
            cfg=hybrid_cfg_effective,
        )
        algo_dur = time.perf_counter() - t_a0
    elif algorithm == "aco":
        t_a0 = time.perf_counter()
        aco_cfg_effective = (
            aco_cfg
            if isinstance(aco_cfg, ACOConfig)
            else ACOConfig(time_limit_sec=time_limit_sec, seed=seed)
        )
        # Force the caller's overall time budget onto ACO regardless of what the
        # caller passed in aco_cfg — same policy solve() applies to alns_cfg above,
        # keeps the three-algorithm comparison fair.
        aco_cfg_effective = ACOConfig(
            time_limit_sec=time_limit_sec,
            seed=(
                seed
                if aco_cfg_effective.seed == ACOConfig.seed
                else aco_cfg_effective.seed
            ),
            num_ants=aco_cfg_effective.num_ants,
            alpha=aco_cfg_effective.alpha,
            beta=aco_cfg_effective.beta,
            rho=aco_cfg_effective.rho,
            q0_deposit=aco_cfg_effective.q0_deposit,
            budget_factor=aco_cfg_effective.budget_factor,
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
    obj_fitness = search_objective(routes, nodes_exp, tm_exp)
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
                "load_profile_liters": load_profile_liters(
                    r, nodes_exp, vehicle_capacity
                ),
            }
        )

    t_end = time.perf_counter()

    return {
        "routes": routes,
        "route_results": route_results,
        "fitness": obj_fitness,
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
