from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..construct import greedy_construct
from ..data import Node, TimeMatrix
from ..evaluation import route_time_minutes
from ..objective import ObjectiveWeights, search_objective
from ..utils import (
    SimulatedAnnealing,
    TabuList,
    deepcopy_routes,
    ensure_all_routes_capacity,
    ensure_capacity_with_refills,
    set_seed,
    weighted_choice,
)

log = logging.getLogger(__name__)

DestroyOp = Callable[
    [List[List[str]], Dict[str, Node], TimeMatrix, int, Dict[str, List[str]]],
    Tuple[List[str], List[List[str]]],
]
RepairOp = Callable[
    [
        List[List[str]],
        List[str],
        Dict[str, Node],
        TimeMatrix,
        dict,
        Optional[Dict[str, List[str]]],
    ],
    List[List[str]],
]


@dataclass
class ALNSConfig:
    time_limit_sec: float = 8.0
    seed: int = 42

    # SA acceptance
    init_temperature: float = 1_000.0
    cooling_rate: float = 0.995  # geometric
    min_temperature: float = 1e-3

    # destroy/repair params
    k_remove_min: int = 2
    k_remove_max: int = 8

    # adaptive weights
    w_improve: float = 5.0
    w_accept: float = 2.0
    w_reject: float = 0.5
    score_update_period: int = 25

    # tabu
    tabu_tenure: int = 20
    use_tabu_on_removed_nodes: bool = True

    # feasibility/penalty (opsional)
    lambda_capacity: float = 0.0  # set >0 kalau mau penalti kapasitas

    # repair strategy
    use_construct_as_repair: bool = False
    rebalance_period: int = 50


def alns_optimize(
    init_routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    vehicle_capacity: float,
    refill_ids: List[str],
    depot_id: str,
    allow_refill: bool,
    groups: Dict[str, List[str]],
    cfg: Optional[ALNSConfig] = None,
) -> List[List[str]]:
    cfg = cfg or ALNSConfig()

    log.info(f"ALNS starting with seed: {cfg.seed}")
    set_seed(cfg.seed)

    # reset failed-rebalance cache for this run
    if hasattr(_rebalance_longest_shortest, "failed_moves"):
        _rebalance_longest_shortest.failed_moves.clear()
    else:
        _rebalance_longest_shortest.failed_moves = set()

    # --- operator pools ---
    destroy_ops: List[Tuple[str, DestroyOp]] = [
        ("random_removal", destroy_random),
        ("shaw_removal", destroy_shaw),
        ("worst_removal", destroy_worst),
        ("longest_removal", destroy_longest),
    ]
    repair_ops: List[Tuple[str, RepairOp]] = [
        ("greedy_insert", repair_greedy),
        ("regret2_insert", repair_regret2),
    ]

    d_weights = [1.0] * len(destroy_ops)
    r_weights = [1.0] * len(repair_ops)
    d_scores = [0.0] * len(destroy_ops)
    r_scores = [0.0] * len(repair_ops)
    d_uses = [1e-9] * len(destroy_ops)
    r_uses = [1e-9] * len(repair_ops)

    sa = SimulatedAnnealing(
        T=cfg.init_temperature, alpha=cfg.cooling_rate, Tmin=cfg.min_temperature
    )

    tabu = TabuList(maxlen=cfg.tabu_tenure)

    # Shared objective (identical for ALNS and ACO — TASK 4 requirement).
    weights = ObjectiveWeights()

    def objective(routes: List[List[str]]) -> float:
        return search_objective(routes, nodes, tm, weights)

    # init
    best = deepcopy_routes(init_routes)
    best_cost = objective(best)
    current = deepcopy_routes(best)
    current_cost = best_cost

    start = time.time()
    it = 0
    reb_accepted = False

    no_improve_iters = 0
    MAX_NO_IMPROVE = 10000

    while time.time() - start < cfg.time_limit_sec:
        it += 1
        improved_best = False

        di = weighted_choice(d_weights)
        ri = weighted_choice(r_weights)
        d_name, d_op = destroy_ops[di]
        r_name, r_op = repair_ops[ri]

        k_remove = random.randint(cfg.k_remove_min, cfg.k_remove_max)

        # --- DESTROY ---
        removed, partial = d_op(current, nodes, tm, k_remove, groups)
        if cfg.use_tabu_on_removed_nodes and tabu.contains_any(removed):
            continue

        # --- REPAIR ---
        if cfg.use_construct_as_repair:
            repaired = greedy_construct(
                nodes=nodes,
                tm=tm,
                selected_parks=[p for p in removed if nodes[p].type == "park"],
                depot_id=depot_id,
                num_vehicles=len(partial),
                vehicle_capacity=vehicle_capacity,
                allow_refill=allow_refill,
                refill_ids=refill_ids,
            )
        else:
            repaired = r_op(
                partial,
                removed,
                nodes,
                tm,
                {
                    "vehicle_capacity": vehicle_capacity,
                    "refill_ids": refill_ids,
                    "allow_refill": allow_refill,
                    "depot_id": depot_id,
                },
                groups,
            )
        repaired, _ins = ensure_all_routes_capacity(
            repaired, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )
        new_cost = objective(repaired)
        delta = new_cost - current_cost

        # --- acceptance ---
        accepted = False
        if delta <= 0:
            accepted = True
        else:
            accepted = sa.accept(delta)

        if accepted:
            current = repaired
            current_cost = new_cost
            if new_cost < best_cost - 1e-9:
                best = deepcopy_routes(repaired)
                best_cost = new_cost
                improved_best = True
                d_scores[di] += cfg.w_improve
                r_scores[ri] += cfg.w_improve
            else:
                d_scores[di] += cfg.w_accept
                r_scores[ri] += cfg.w_accept

            if cfg.use_tabu_on_removed_nodes:
                tabu.add_many(removed)
        else:
            d_scores[di] += cfg.w_reject
            r_scores[ri] += cfg.w_reject

        d_uses[di] += 1
        r_uses[ri] += 1

        if it % cfg.score_update_period == 0:
            for i in range(len(d_weights)):
                d_weights[i] = max(1e-3, d_weights[i] * (1.0 + d_scores[i] / d_uses[i]))
                d_scores[i] = 0.0
                d_uses[i] = 1e-9
            for i in range(len(r_weights)):
                r_weights[i] = max(1e-3, r_weights[i] * (1.0 + r_scores[i] / r_uses[i]))
                r_scores[i] = 0.0
                r_uses[i] = 1e-9

        if cfg.rebalance_period > 0 and it % cfg.rebalance_period == 0:
            rebalanced_routes, rebalanced_cost, reb_accepted = (
                _rebalance_longest_shortest(
                    current,
                    nodes,
                    tm,
                    vehicle_capacity,
                    refill_ids,
                    depot_id,
                    groups,
                    objective,
                    current_cost,
                    sa,
                )
            )
            if reb_accepted:
                current = rebalanced_routes
                current_cost = rebalanced_cost
                if rebalanced_cost < best_cost - 1e-9:
                    best = deepcopy_routes(rebalanced_routes)
                    best_cost = rebalanced_cost
                    improved_best = True

        sa.cool()

        if improved_best:
            no_improve_iters = 0
        else:
            no_improve_iters += 1
            if no_improve_iters >= MAX_NO_IMPROVE:
                log.info(
                    "ALNS early stop: no improvement in %d iterations (best_cost=%.2f)",
                    no_improve_iters,
                    best_cost,
                )
                break

    return best


def _rebalance_longest_shortest(
    routes,
    nodes,
    tm,
    vehicle_capacity,
    refill_ids,
    depot_id,
    groups,
    objective,
    current_cost,
    sa,
):
    failed_moves = getattr(_rebalance_longest_shortest, "failed_moves", None)
    if failed_moves is None:
        failed_moves = set()
        _rebalance_longest_shortest.failed_moves = failed_moves

    route_durations = []
    for idx, r in enumerate(routes):
        if len(r) > 2:
            dur = route_time_minutes(r, nodes, tm)
            route_durations.append((dur, idx))

    if len(route_durations) <= 1:
        return routes, current_cost, False

    longest_dur, longest_idx = max(route_durations, key=lambda x: x[0])
    shortest_dur, shortest_idx = min(route_durations, key=lambda x: x[0])

    if longest_dur - shortest_dur < 1e-3:
        return routes, current_cost, False

    longest_route = routes[longest_idx]

    base_to_nodes_in_longest = {}
    for nid in longest_route:
        node = nodes.get(nid)
        if not node or node.type != "park":
            continue
        base = nid.split("#")[0]
        base_to_nodes_in_longest.setdefault(base, [])
        base_to_nodes_in_longest[base].append(nid)

    if not base_to_nodes_in_longest:
        return routes, current_cost, False

    candidate_bases = list(base_to_nodes_in_longest.keys())
    candidate_bases = [
        b for b in candidate_bases if (b, longest_idx, shortest_idx) not in failed_moves
    ]
    if not candidate_bases:
        return routes, current_cost, False

    best_new_routes = None
    best_new_cost = float("inf")
    best_base = None

    for base in candidate_bases:
        parts = base_to_nodes_in_longest[base]

        move_key = (base, longest_idx, shortest_idx)
        if move_key in failed_moves:
            continue

        cand_routes = deepcopy_routes(routes)

        cand_longest = cand_routes[longest_idx]
        cand_longest = [nid for nid in cand_longest if nid not in parts]
        if cand_longest and cand_longest[0] != routes[longest_idx][0]:
            cand_longest.insert(0, routes[longest_idx][0])
        if cand_longest and cand_longest[-1] != routes[longest_idx][-1]:
            cand_longest.append(routes[longest_idx][-1])
        cand_routes[longest_idx] = cand_longest

        cand_shortest = cand_routes[shortest_idx]
        if len(cand_shortest) >= 2:
            insert_pos = len(cand_shortest) - 1
        else:
            insert_pos = 1
        cand_shortest[insert_pos:insert_pos] = parts
        cand_routes[shortest_idx] = cand_shortest

        cand_routes, _ = ensure_all_routes_capacity(
            cand_routes, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )

        cand_cost = objective(cand_routes)

        delta_tmp = cand_cost - current_cost
        if delta_tmp > 10 * longest_dur:
            failed_moves.add(move_key)
            continue

        if cand_cost < best_new_cost:
            best_new_cost = cand_cost
            best_new_routes = cand_routes
            best_base = base

    if best_new_routes is None:
        return routes, current_cost, False

    delta = best_new_cost - current_cost

    if delta <= 0 or sa.accept(delta):
        log.info(
            "[REBALANCE] ACCEPT base=%s | longest_idx=%d -> shortest_idx=%d | Delta=%.2f",
            best_base,
            longest_idx,
            shortest_idx,
            delta,
        )
        return best_new_routes, best_new_cost, True
    else:
        log.info(
            "[REBALANCE] REJECT base=%s | longest_idx=%d -> shortest_idx=%d | Delta=%.2f",
            best_base,
            longest_idx,
            shortest_idx,
            delta,
        )
        failed_moves.add((best_base, longest_idx, shortest_idx))
        return routes, current_cost, False


# =========================
# Destroy operators
# =========================


def destroy_random(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    k: int,
    groups: Dict[str, List[str]],
) -> Tuple[List[str], List[List[str]]]:
    parks = []
    for r in routes:
        parks.extend([nid for nid in r[1:-1] if nodes[nid].type == "park"])
    if not parks or k <= 0:
        return [], routes

    random.shuffle(parks)
    removed_set = set()
    for nid in parks:
        base = nid.split("#")[0]
        for p in groups.get(base, [nid]):
            removed_set.add(p)
        if sum(1 for p in removed_set if nodes[p].type == "park") >= k:
            break

    new_routes = []
    for r in routes:
        new_r = [
            nid for nid in r if not (nid in removed_set and nodes[nid].type == "park")
        ]
        if new_r and new_r[0] != r[0]:
            new_r.insert(0, r[0])
        if new_r and new_r[-1] != r[-1]:
            new_r.append(r[-1])
        new_routes.append(new_r)

    return list(removed_set), new_routes


def destroy_shaw(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    k: int,
    groups: Dict[str, List[str]],
) -> Tuple[List[str], List[List[str]]]:
    parks = []
    for r in routes:
        parks.extend([nid for nid in r[1:-1] if nodes[nid].type == "park"])
    if not parks or k <= 0:
        return [], routes

    seed = random.choice(parks)

    def proximity(p):
        return tm.travel(seed, p) + tm.travel(p, seed)

    ordered = [p for p in parks if p != seed]
    ordered.sort(key=proximity)

    removed_set = set()
    for nid in [seed] + ordered:
        base = nid.split("#")[0]
        for part in groups.get(base, [nid]):
            removed_set.add(part)
        if sum(1 for p in removed_set if nodes[p].type == "park") >= k:
            break

    new_routes = []
    for r in routes:
        new_r = [
            nid for nid in r if not (nid in removed_set and nodes[nid].type == "park")
        ]
        if new_r and new_r[0] != r[0]:
            new_r.insert(0, r[0])
        if new_r and new_r[-1] != r[-1]:
            new_r.append(r[-1])
        new_routes.append(new_r)
    return list(removed_set), new_routes


def destroy_worst(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    k: int,
    groups: Dict[str, List[str]],
) -> Tuple[List[str], List[List[str]]]:
    candidates: List[Tuple[str, float, int, int]] = []
    for ri, r in enumerate(routes):
        for i in range(1, len(r) - 1):
            nid = r[i]
            if nodes[nid].type != "park":
                continue
            a, b = r[i - 1], r[i + 1]
            score = (
                tm.travel(a, nid)
                + tm.travel(nid, b)
                - tm.travel(a, b)
                + nodes[nid].service_min
            )
            candidates.append((nid, score, ri, i))

    if not candidates or k <= 0:
        return [], routes

    candidates.sort(key=lambda x: x[1], reverse=True)

    removed_set = set()
    for nid, _, _, _ in candidates:
        base = nid.split("#")[0]
        for part in groups.get(base, [nid]):
            removed_set.add(part)
        if sum(1 for p in removed_set if nodes[p].type == "park") >= k:
            break

    new_routes = []
    for r in routes:
        new_r = [
            nid for nid in r if not (nid in removed_set and nodes[nid].type == "park")
        ]
        if new_r and new_r[0] != r[0]:
            new_r.insert(0, r[0])
        if new_r and new_r[-1] != r[-1]:
            new_r.append(r[-1])
        new_routes.append(new_r)

    return list(removed_set), new_routes


def destroy_longest(routes, nodes, tm, k, groups):
    durations = [route_time_minutes(r, nodes, tm) for r in routes]
    if not durations:
        return [], routes
    longest_idx = max(range(len(routes)), key=lambda i: durations[i])
    longest_route = routes[longest_idx]

    parks = [nid for nid in longest_route[1:-1] if nodes[nid].type == "park"]
    if not parks:
        return [], routes

    random.shuffle(parks)

    removed_set = set()
    for nid in parks:
        base = nid.split("#")[0]
        for p in groups.get(base, [nid]):
            removed_set.add(p)
        if sum(1 for p in removed_set if nodes[p].type == "park") >= k:
            break

    new_routes = []
    for r in routes:
        new_r = [
            nid for nid in r if not (nid in removed_set and nodes[nid].type == "park")
        ]
        if new_r and new_r[0] != r[0]:
            new_r.insert(0, r[0])
        if new_r and new_r[-1] != r[-1]:
            new_r.append(r[-1])
        new_routes.append(new_r)

    return list(removed_set), new_routes


# =========================
# Repair operators
# =========================


def repair_greedy(
    routes: List[List[str]],
    removed: List[str],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    ctx: dict,
    groups: Optional[Dict[str, List[str]]] = None,
    balance_probability: float = 0.7,
    balance_tolerance: float = 1.05,
) -> List[List[str]]:
    vehicle_capacity = ctx["vehicle_capacity"]
    refill_ids = ctx["refill_ids"]
    depot_id = ctx["depot_id"]

    current = [r[:] for r in routes]

    by_base: Dict[str, List[str]] = {}
    for nid in removed:
        node = nodes.get(nid)
        if not node or node.type != "park":
            continue
        base = nid.split("#")[0]

        is_split_group = False
        if groups is not None and base in groups:
            is_split_group = True

        if is_split_group:
            by_base.setdefault(base, [])
            by_base[base].append(nid)
        else:
            by_base.setdefault(nid, [])
            by_base[nid].append(nid)

    for k in by_base:
        by_base[k].sort()

    group_order = sorted(by_base.keys())

    for base in group_order:
        parts = by_base[base]
        if not parts:
            continue

        p0 = parts[0]

        best_overall_delta = float("inf")
        best_overall_route_idx = -1
        best_overall_pos = 1
        insert_options = []

        for ri, r in enumerate(current):
            best_pos_in_route = 1
            best_delta_in_route = float("inf")
            for j in range(1, len(r)):
                a, b = r[j - 1], r[j]
                node_p0 = nodes.get(p0)
                if node_p0 and a in tm.index and b in tm.index and p0 in tm.index:
                    delta = (
                        tm.travel(a, p0)
                        + tm.travel(p0, b)
                        - tm.travel(a, b)
                        + node_p0.service_min
                    )
                    if delta < best_delta_in_route:
                        best_delta_in_route = delta
                        best_pos_in_route = j
                else:
                    continue

            if best_delta_in_route != float("inf"):
                insert_options.append((best_delta_in_route, ri, best_pos_in_route))

            if best_delta_in_route < best_overall_delta:
                best_overall_delta = best_delta_in_route
                best_overall_route_idx = ri
                best_overall_pos = best_pos_in_route

        if best_overall_route_idx == -1:
            log.warning(f"Cannot find valid insertion spot for group {base}. Skipping.")
            continue

        target_route_idx = best_overall_route_idx
        target_pos = best_overall_pos

        if random.random() < balance_probability and len(current) > 1:
            route_durations = [
                (route_time_minutes(r, nodes, tm), i)
                for i, r in enumerate(current)
                if len(r) > 2
            ]
            if route_durations:
                shortest_route_duration, shortest_route_idx = min(route_durations)
                shortest_route_option = None
                for delta, ri, pos in insert_options:
                    if ri == shortest_route_idx:
                        shortest_route_option = (delta, ri, pos)
                        break

                if shortest_route_option:
                    shortest_delta, _, shortest_pos = shortest_route_option
                    if (
                        shortest_delta <= best_overall_delta * balance_tolerance
                        or best_overall_delta <= 1e-9
                    ):
                        target_route_idx = shortest_route_idx
                        target_pos = shortest_pos

        tgt = current[target_route_idx]
        target_pos = min(target_pos, len(tgt)) if len(tgt) > 0 else 1
        tgt[target_pos:target_pos] = parts

        route_to_fix = current[target_route_idx]
        fixed_route, _ = ensure_capacity_with_refills(
            route_to_fix, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )
        current[target_route_idx] = fixed_route

    return current


def repair_regret2(
    routes: List[List[str]],
    removed: List[str],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    ctx: dict,
    groups: Optional[Dict[str, List[str]]] = None,
) -> List[List[str]]:
    vehicle_capacity = ctx["vehicle_capacity"]
    refill_ids = ctx["refill_ids"]
    depot_id = ctx["depot_id"]

    current = deepcopy_routes(routes)

    by_base: Dict[str, List[str]] = {}
    for nid in removed:
        if nodes[nid].type != "park":
            continue
        base = nid.split("#")[0]
        by_base.setdefault(base, [])
        by_base[base].append(nid)

    for k in by_base:
        by_base[k].sort()

    for base in sorted(by_base.keys()):
        parts = by_base[base]
        p0 = parts[0]

        cand_per_route: List[Tuple[float, float, int, int]] = []
        for ri, r in enumerate(current):
            deltas = []
            spots = []
            for j in range(1, len(r)):
                a, b = r[j - 1], r[j]
                delta = (
                    tm.travel(a, p0)
                    + tm.travel(p0, b)
                    - tm.travel(a, b)
                    + nodes[p0].service_min
                )
                deltas.append(delta)
                spots.append(j)
            if not deltas:
                continue
            order = sorted(range(len(deltas)), key=lambda idx: deltas[idx])
            d1 = deltas[order[0]]
            j1 = spots[order[0]]
            d2 = deltas[order[1]] if len(order) >= 2 else (d1 + 1e6)
            cand_per_route.append((d1, d2, ri, j1))

        if not cand_per_route:
            target_ri = min(range(len(current)), key=lambda i: len(current[i]))
            j_best = 1
        else:
            cand_per_route.sort(key=lambda x: (x[1] - x[0]), reverse=True)
            d1, d2, target_ri, j_best = cand_per_route[0]

        tgt = current[target_ri]
        tgt[j_best:j_best] = parts

        current, _ = ensure_all_routes_capacity(
            current, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )

    return current
