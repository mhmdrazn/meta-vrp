"""ALNS (Adaptive Large Neighborhood Search) — aligned with Baseline Comparison notebook.

Iteratively destroys and repairs parts of the solution.  Operator weights updated
adaptively based on contribution.  Simulated annealing acceptance criterion.

Destroy operators: random, worst, shaw (related), longest.
Repair operators:  greedy (cheapest insertion), regret-2 (largest gap between top-2 trucks).

Key changes from the previous implementation to match the notebook:
  - Adaptive weight update: ``w = (1-react)*w + react*(score/count)`` every ``seg`` iters.
  - SA scoring: 3=new global best, 2=improving, 1=SA-accepted, 0=rejected.
  - _rebalance_solution: multi-pass heaviest→lightest park migration (from notebook).
  - Config defaults: init_temperature=100, score_update_period=20 (seg=20), react=0.1.
"""
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

    # SA acceptance (notebook defaults)
    init_temperature: float = 100.0
    cooling_rate: float = 0.995  # geometric
    min_temperature: float = 1e-3

    # destroy/repair params
    k_remove_min: int = 2
    k_remove_max: int = 8

    # adaptive weights (notebook style)
    score_update_period: int = 20  # seg: update every N iters
    react: float = 0.1             # reactivity factor

    # tabu
    tabu_tenure: int = 20
    use_tabu_on_removed_nodes: bool = True

    # feasibility/penalty
    lambda_capacity: float = 0.0

    # repair strategy
    use_construct_as_repair: bool = False
    rebalance_period: int = 50

    # optional iteration cap (None = time-limited only)
    max_iter: Optional[int] = None

    # Legacy fields kept for backward-compat with solve.py ALNSConfig construction
    w_improve: float = 5.0
    w_accept: float = 2.0
    w_reject: float = 0.5


# ---------------------------------------------------------------------------
# Rebalance (ported from notebook's _rebalance_solution)
# ---------------------------------------------------------------------------

def _rebalance_solution(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    groups: Dict[str, List[str]],
    vehicle_capacity: float,
    refill_ids: List[str],
    depot_id: str,
    objective_fn,
    max_passes: int = 10,
) -> Tuple[List[List[str]], float]:
    """Move park groups from heaviest truck to lightest until balanced.

    Returns (routes, cost).
    """
    current = deepcopy_routes(routes)

    for _ in range(max_passes):
        durations: Dict[int, float] = {}
        for ri, r in enumerate(current):
            if len(r) > 2:
                durations[ri] = route_time_minutes(r, nodes, tm)
            else:
                durations[ri] = 0.0

        active = {k: v for k, v in durations.items() if v > 0}
        if len(active) <= 1:
            break

        heaviest_idx = max(active, key=lambda k: active[k])
        lightest_idx = min(active, key=lambda k: active[k])
        gap = active[heaviest_idx] - active[lightest_idx]

        mean_t = sum(active.values()) / len(active)
        if mean_t < 1.0 or gap < mean_t * 0.15:
            break

        # Find park-group bases in the heaviest route
        heavy_route = current[heaviest_idx]
        bases_in_heavy: List[str] = []
        seen_bases = set()
        for nid in heavy_route[1:-1]:
            node = nodes.get(nid)
            if not node or node.type != "park":
                continue
            base = nid.split("#")[0]
            if base not in seen_bases:
                seen_bases.add(base)
                bases_in_heavy.append(base)

        if len(bases_in_heavy) <= 1:
            break

        best_move = None
        best_new_gap = gap

        for base in bases_in_heavy:
            parts = groups.get(base, [base])

            # Try moving all parts to lightest route
            cand = deepcopy_routes(current)
            cand[heaviest_idx] = [
                nid for nid in cand[heaviest_idx] if nid not in parts
            ]
            if not cand[heaviest_idx] or cand[heaviest_idx][0] != depot_id:
                cand[heaviest_idx].insert(0, depot_id)
            if not cand[heaviest_idx] or cand[heaviest_idx][-1] != depot_id:
                cand[heaviest_idx].append(depot_id)

            insert_pos = max(1, len(cand[lightest_idx]) - 1)
            cand[lightest_idx][insert_pos:insert_pos] = parts

            cand, _ = ensure_all_routes_capacity(
                cand, nodes, vehicle_capacity, refill_ids, tm, depot_id
            )

            # Check heaviest still has parks
            has_park_h = any(
                nodes.get(nid) and nodes[nid].type == "park"
                for nid in cand[heaviest_idx][1:-1]
            )
            if not has_park_h:
                continue

            t_h = route_time_minutes(cand[heaviest_idx], nodes, tm)
            t_l = route_time_minutes(cand[lightest_idx], nodes, tm)

            new_gap = abs(t_h - t_l)
            if new_gap < best_new_gap:
                best_new_gap = new_gap
                best_move = cand

        if best_move is None:
            break

        current = best_move

    cost = objective_fn(current)
    return current, cost


# ---------------------------------------------------------------------------
# Main ALNS loop (aligned with notebook's run_alns)
# ---------------------------------------------------------------------------

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

    log.info("ALNS starting with seed: %d", cfg.seed)
    set_seed(cfg.seed)

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

    rng = random.Random(cfg.seed)

    n_destroy = len(destroy_ops)
    n_repair = len(repair_ops)

    # Adaptive weights (notebook style)
    d_weights = [1.0] * n_destroy
    r_weights = [1.0] * n_repair
    d_scores = [0.0] * n_destroy
    r_scores = [0.0] * n_repair
    d_counts = [0] * n_destroy
    r_counts = [0] * n_repair

    sa = SimulatedAnnealing(
        T=cfg.init_temperature, alpha=cfg.cooling_rate, Tmin=cfg.min_temperature
    )

    tabu = TabuList(maxlen=cfg.tabu_tenure)

    weights_obj = ObjectiveWeights()

    def objective(routes: List[List[str]]) -> float:
        return search_objective(routes, nodes, tm, weights_obj)

    # init
    best = deepcopy_routes(init_routes)
    best_cost = objective(best)
    current = deepcopy_routes(best)
    current_cost = best_cost

    start = time.time()
    it = 0

    def should_continue() -> bool:
        if cfg.max_iter is not None:
            return it < cfg.max_iter
        return time.time() - start < cfg.time_limit_sec

    while should_continue():
        it += 1

        di = weighted_choice(d_weights, rng)
        ri = weighted_choice(r_weights, rng)
        d_name, d_op = destroy_ops[di]
        r_name, r_op = repair_ops[ri]

        k_remove = rng.randint(cfg.k_remove_min, cfg.k_remove_max)

        # --- DESTROY ---
        try:
            removed, partial = d_op(current, nodes, tm, k_remove, groups, rng=rng)
        except TypeError:
            removed, partial = d_op(current, nodes, tm, k_remove, groups)
        if cfg.use_tabu_on_removed_nodes and tabu.contains_any(removed):
            continue

        # --- REPAIR ---
        if cfg.use_construct_as_repair:
            repaired = greedy_construct(
                nodes=nodes, tm=tm,
                selected_parks=[p for p in removed if nodes[p].type == "park"],
                depot_id=depot_id,
                num_vehicles=len(partial),
                vehicle_capacity=vehicle_capacity,
                allow_refill=allow_refill,
                refill_ids=refill_ids,
            )
        else:
            try:
                repaired = r_op(
                    partial, removed, nodes, tm,
                    {
                        "vehicle_capacity": vehicle_capacity,
                        "refill_ids": refill_ids,
                        "allow_refill": allow_refill,
                        "depot_id": depot_id,
                    },
                    groups,
                    rng=rng,
                )
            except TypeError:
                repaired = r_op(
                    partial, removed, nodes, tm,
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

        # --- Acceptance (notebook scoring: 3/2/1/0) ---
        d_counts[di] += 1
        r_counts[ri] += 1
        score = 0.0

        if new_cost < best_cost - 1e-6:
            # New global best
            best_cost = new_cost
            best = deepcopy_routes(repaired)
            current = repaired
            current_cost = new_cost
            score = 3.0
        elif new_cost < current_cost - 1e-6:
            # Improving current
            current = repaired
            current_cost = new_cost
            score = 2.0
        elif sa.accept(delta, rng):
            # SA-accepted (worse)
            current = repaired
            current_cost = new_cost
            score = 1.0
            # SA-accepted (worse)
            current = repaired
            current_cost = new_cost
            score = 1.0
        # else: rejected, score = 0.0

        d_scores[di] += score
        r_scores[ri] += score

        if cfg.use_tabu_on_removed_nodes and score == 0.0:
            tabu.add_many(removed)

        # --- Adaptive weight update (notebook formula) ---
        if it % cfg.score_update_period == 0:
            for i in range(n_destroy):
                if d_counts[i] > 0:
                    d_weights[i] = (1 - cfg.react) * d_weights[i] + cfg.react * (d_scores[i] / d_counts[i])
                d_scores[i] = 0.0
                d_counts[i] = 0
            for i in range(n_repair):
                if r_counts[i] > 0:
                    r_weights[i] = (1 - cfg.react) * r_weights[i] + cfg.react * (r_scores[i] / r_counts[i])
                r_scores[i] = 0.0
                r_counts[i] = 0

        # --- Rebalance ---
        if cfg.rebalance_period > 0 and it % cfg.rebalance_period == 0:
            rebalanced, reb_cost = _rebalance_solution(
                current, nodes, tm, groups,
                vehicle_capacity, refill_ids, depot_id, objective,
            )
            reb_delta = reb_cost - current_cost
            if reb_delta <= 0 or sa.accept(reb_delta):
                current = rebalanced
                current_cost = reb_cost
                if reb_cost < best_cost - 1e-9:
                    best = deepcopy_routes(rebalanced)
                    best_cost = reb_cost

        sa.cool()

    # Final rebalance pass
    best, best_cost = _rebalance_solution(
        best, nodes, tm, groups,
        vehicle_capacity, refill_ids, depot_id, objective,
    )

    log.info("ALNS done: %d iterations, best_cost=%.3f", it, best_cost)
    return best


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
    """Shaw removal (related): pick a seed, remove k geographically closest parks."""
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
            log.warning("Cannot find valid insertion spot for group %s. Skipping.", base)
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
