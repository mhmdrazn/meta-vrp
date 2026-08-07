"""Hybrid ALNS + ACO — aligned with Baseline Comparison notebook.

Combines ACO construction with ALNS intensification:

1. **Phase 1 (initialisation)**: Use greedy_construct (same starting point as ALNS)
   and deposit 100× pheromone onto the initial solution's edges.
2. **Phase 2 (ALNS loop)**: Same destroy operators as standard ALNS, but with
   3 repair operators:
   - ``repair_greedy`` (cheapest insertion)
   - ``repair_regret2`` (regret-based insertion)
   - ``repair_aco_guided`` (NEW — truck selection guided by pheromone trail)
3. **Pheromone feedback**: When a new global best is found, deposit 100× onto
   its edges.
4. **Slow evaporation**: ``pher *= (1 - rho × 0.01)`` (10× slower than ACO).
5. **Post-processing**: ``_rebalance_solution`` pass.

Key notebook references: ``run_hybrid()``, ``repair_aco_guided()``.
"""
from __future__ import annotations

import logging
import math
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
    deepcopy_routes,
    ensure_all_routes_capacity,
    ensure_capacity_with_refills,
    set_seed,
    weighted_choice,
)

# Re-use destroy operators and standard repair from alns.py
from .alns import (
    DestroyOp,
    RepairOp,
    _rebalance_solution,
    destroy_longest,
    destroy_random,
    destroy_shaw,
    destroy_worst,
    repair_greedy,
    repair_regret2,
)

log = logging.getLogger(__name__)


@dataclass
class HybridConfig:
    time_limit_sec: float = 8.0
    seed: int = 42

    # SA acceptance (same as notebook)
    init_temperature: float = 100.0
    cooling_rate: float = 0.995
    min_temperature: float = 1e-3

    # destroy/repair params
    k_remove_min: int = 2
    k_remove_max: int = 8

    # adaptive weights
    score_update_period: int = 20  # seg
    react: float = 0.1

    # pheromone params (from notebook)
    alpha: float = 3.0          # pheromone exponent for aco-guided repair
    beta: float = 1.0           # heuristic exponent for aco-guided repair
    rho: float = 0.1            # base evaporation rate (actual rate = rho * 0.01)
    deposit_multiplier: float = 100.0  # pheromone deposit = multiplier / cost

    # rebalance
    rebalance_period: int = 50

    # optional iteration cap
    max_iter: Optional[int] = None


# ---------------------------------------------------------------------------
# ACO-guided repair operator (ported from notebook's repair_aco_guided)
# ---------------------------------------------------------------------------

def repair_aco_guided(
    routes: List[List[str]],
    removed: List[str],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    ctx: dict,
    groups: Optional[Dict[str, List[str]]],
    pher: Dict[Tuple[str, str], float],
    alpha: float = 3.0,
    beta: float = 1.0,
) -> List[List[str]]:
    """Re-insert removed parks using pheromone-guided truck selection.

    For each removed park group, evaluate every route as a potential target.
    The selection probability for route ``ri`` is proportional to
    ``tau(predecessor, park)^alpha * (1 / route_cost)^beta``.
    """
    vehicle_capacity = ctx["vehicle_capacity"]
    refill_ids = ctx["refill_ids"]
    depot_id = ctx["depot_id"]

    current = deepcopy_routes(routes)

    by_base: Dict[str, List[str]] = {}
    for nid in removed:
        node = nodes.get(nid)
        if not node or node.type != "park":
            continue
        base = nid.split("#")[0]
        if groups is not None and base in groups:
            by_base.setdefault(base, [])
            by_base[base].append(nid)
        else:
            by_base.setdefault(nid, [])
            by_base[nid].append(nid)

    for k in by_base:
        by_base[k].sort()

    for base in sorted(by_base.keys()):
        parts = by_base[base]
        if not parts:
            continue
        p0 = parts[0]

        weights: List[float] = []
        insert_positions: List[int] = []

        for ri, r in enumerate(current):
            # Find best insertion position in this route
            best_pos = 1
            best_delta = float("inf")
            for j in range(1, len(r)):
                a, b = r[j - 1], r[j]
                if a in tm.index and b in tm.index and p0 in tm.index:
                    delta = (
                        tm.travel(a, p0)
                        + tm.travel(p0, b)
                        - tm.travel(a, b)
                        + nodes[p0].service_min
                    )
                    if delta < best_delta:
                        best_delta = delta
                        best_pos = j

            insert_positions.append(best_pos)

            if best_delta == float("inf"):
                weights.append(0.0)
                continue

            # Check feasibility after insertion
            test_route = r[:]
            test_pos = min(best_pos, len(test_route))
            test_route[test_pos:test_pos] = parts
            test_route, _ = ensure_capacity_with_refills(
                test_route, nodes, vehicle_capacity, refill_ids, tm, depot_id
            )
            t = route_time_minutes(test_route, nodes, tm)

            # Find predecessor of p0 in the built route
            if p0 in test_route:
                idx = test_route.index(p0)
                last = test_route[idx - 1] if idx > 0 else depot_id
            else:
                last = depot_id

            # Pheromone-guided weight
            last_base = last.split("#")[0]
            p0_base = p0.split("#")[0]
            tau_key = (last_base, p0_base)
            tau_val = pher.get(tau_key, 1.0)

            tau_component = tau_val ** alpha
            eta_component = (1.0 / (t + 1e-6)) ** beta
            weights.append(tau_component * eta_component)

        # Select route via roulette wheel
        tot = sum(weights)
        if tot <= 0:
            target_ri = min(range(len(current)), key=lambda r: sum(
                nodes[nid].demand_liters for nid in current[r] if nodes.get(nid) and nodes[nid].type == "park"
            ))
        else:
            r_val = random.random() * tot
            acc = 0.0
            target_ri = len(current) - 1
            for i, w in enumerate(weights):
                acc += w
                if acc >= r_val:
                    target_ri = i
                    break

        # Insert parts
        tgt = current[target_ri]
        pos = insert_positions[target_ri] if target_ri < len(insert_positions) else 1
        pos = min(pos, len(tgt))
        tgt[pos:pos] = parts

        # Ensure capacity
        fixed, _ = ensure_capacity_with_refills(
            current[target_ri], nodes, vehicle_capacity, refill_ids, tm, depot_id
        )
        current[target_ri] = fixed

    return current


# ---------------------------------------------------------------------------
# Edge extraction for pheromone operations
# ---------------------------------------------------------------------------

def _edges_of_routes(
    routes: List[List[str]],
) -> List[Tuple[str, str]]:
    """Extract consecutive (from_base, to_base) edges for pheromone deposit."""
    edges: List[Tuple[str, str]] = []
    for r in routes:
        for i in range(len(r) - 1):
            a_base = r[i].split("#")[0]
            b_base = r[i + 1].split("#")[0]
            if a_base != b_base:
                edges.append((a_base, b_base))
    return edges


# ---------------------------------------------------------------------------
# Main Hybrid ALNS+ACO loop (ported from notebook's run_hybrid)
# ---------------------------------------------------------------------------

def hybrid_optimize(
    init_routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    vehicle_capacity: float,
    refill_ids: List[str],
    depot_id: str,
    allow_refill: bool,
    groups: Dict[str, List[str]],
    cfg: Optional[HybridConfig] = None,
) -> List[List[str]]:
    """Hybrid ALNS+ACO optimiser — notebook-aligned implementation.

    Signature mirrors ``alns_optimize`` so ``solve()`` calls both uniformly.
    """
    cfg = cfg or HybridConfig()
    log.info("Hybrid ALNS+ACO starting with seed: %d", cfg.seed)
    set_seed(cfg.seed)

    weights_obj = ObjectiveWeights()

    def objective(routes: List[List[str]]) -> float:
        return search_objective(routes, nodes, tm, weights_obj)

    # --- Phase 1: Initialise from greedy_construct + pheromone seeding ---
    current = deepcopy_routes(init_routes)
    current, _ = ensure_all_routes_capacity(
        current, nodes, vehicle_capacity, refill_ids, tm, depot_id
    )
    init_cost = objective(current)

    # Initialise pheromone matrix (dict-based, default 1.0)
    pher: Dict[Tuple[str, str], float] = {}

    # Deposit 100× onto initial solution's edges (notebook: dep = 100 / cost)
    init_deposit = cfg.deposit_multiplier / (init_cost + 1e-9)
    for edge in _edges_of_routes(current):
        pher[edge] = pher.get(edge, 1.0) + init_deposit
        rev = (edge[1], edge[0])
        pher[rev] = pher.get(rev, 1.0) + init_deposit

    cur_dmap = deepcopy_routes(current)
    cur_cost = init_cost
    best = deepcopy_routes(current)
    best_cost = init_cost

    # --- Phase 2: ALNS with 3 repair operators (greedy, regret, aco-guided) ---
    destroy_ops = [
        ("random_removal", destroy_random),
        ("shaw_removal", destroy_shaw),
        ("worst_removal", destroy_worst),
        ("longest_removal", destroy_longest),
    ]
    repair_names = ["greedy", "regret", "aco"]

    n_destroy = len(destroy_ops)
    n_repair = len(repair_names)

    d_weights = [1.0] * n_destroy
    r_weights = [1.0] * n_repair
    d_scores = [0.0] * n_destroy
    r_scores = [0.0] * n_repair
    d_counts = [0] * n_destroy
    r_counts = [0] * n_repair

    sa = SimulatedAnnealing(
        T=cfg.init_temperature, alpha=cfg.cooling_rate, Tmin=cfg.min_temperature
    )

    start = time.time()
    it = 0

    def time_ok():
        return time.time() - start < cfg.time_limit_sec

    def iter_ok():
        if cfg.max_iter is not None:
            return it < cfg.max_iter
        return True

    ctx = {
        "vehicle_capacity": vehicle_capacity,
        "refill_ids": refill_ids,
        "allow_refill": allow_refill,
        "depot_id": depot_id,
    }

    while time_ok() and iter_ok():
        it += 1

        di = weighted_choice(d_weights)
        ri_ = weighted_choice(r_weights)

        k_remove = random.randint(cfg.k_remove_min, cfg.k_remove_max)

        # --- DESTROY ---
        d_name, d_op = destroy_ops[di]
        removed, partial = d_op(cur_dmap, nodes, tm, k_remove, groups)
        if not removed:
            continue

        # --- REPAIR (3 operators) ---
        repair_name = repair_names[ri_]
        if repair_name == "greedy":
            new_routes = repair_greedy(partial, removed, nodes, tm, ctx, groups)
        elif repair_name == "regret":
            new_routes = repair_regret2(partial, removed, nodes, tm, ctx, groups)
        else:  # "aco"
            new_routes = repair_aco_guided(
                partial, removed, nodes, tm, ctx, groups,
                pher, cfg.alpha, cfg.beta,
            )

        new_routes, _ = ensure_all_routes_capacity(
            new_routes, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )
        new_cost = objective(new_routes)

        # --- Acceptance (notebook scoring) ---
        d_counts[di] += 1
        r_counts[ri_] += 1
        score = 0.0

        if new_cost < best_cost - 1e-6:
            # New global best
            best_cost = new_cost
            best = deepcopy_routes(new_routes)
            cur_dmap = new_routes
            cur_cost = new_cost
            score = 3.0

            # Pheromone feedback: deposit 100× on new best (notebook FIX 1)
            dep = cfg.deposit_multiplier / (new_cost + 1e-9)
            for edge in _edges_of_routes(new_routes):
                pher[edge] = pher.get(edge, 1.0) + dep
                rev = (edge[1], edge[0])
                pher[rev] = pher.get(rev, 1.0) + dep

        elif new_cost < cur_cost - 1e-6:
            cur_dmap = new_routes
            cur_cost = new_cost
            score = 2.0
        elif sa.accept(new_cost - cur_cost):
            cur_dmap = new_routes
            cur_cost = new_cost
            score = 1.0

        d_scores[di] += score
        r_scores[ri_] += score

        # Slow evaporation: rho * 0.01 (notebook FIX 1: 10× slower)
        evap_rate = cfg.rho * 0.01
        for key in list(pher.keys()):
            pher[key] *= (1.0 - evap_rate)

        sa.cool()

        # --- Adaptive weight update ---
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
                cur_dmap, nodes, tm, groups,
                vehicle_capacity, refill_ids, depot_id, objective,
            )
            reb_delta = reb_cost - cur_cost
            if reb_delta <= 0 or sa.accept(reb_delta):
                cur_dmap = rebalanced
                cur_cost = reb_cost
                if reb_cost < best_cost - 1e-9:
                    best = deepcopy_routes(rebalanced)
                    best_cost = reb_cost

    # Final rebalance
    best, best_cost = _rebalance_solution(
        best, nodes, tm, groups,
        vehicle_capacity, refill_ids, depot_id, objective,
    )

    log.info("Hybrid ALNS+ACO done: %d iterations, best_cost=%.3f", it, best_cost)
    return best


__all__ = ["HybridConfig", "hybrid_optimize"]
