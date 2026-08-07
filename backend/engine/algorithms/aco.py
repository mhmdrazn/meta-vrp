"""ACO (Ant Colony Optimization) — aligned with Baseline Comparison notebook.

Each ant constructs a complete solution probabilistically (pheromone^alpha *
heuristic^beta).  Pheromone updated via evaporation + deposit from
iteration-best and global-best solutions.

Key differences from the previous MMAS-style implementation:
  - Demand-budget–driven construction per truck (not capacity-only).
  - Pure roulette-wheel selection (no ACS q0 exploitation threshold).
  - Evaporate ALL pheromone entries, not just seen edges.
  - Deposit from both iteration-best AND global-best.
  - Multi-pass rebalance (heaviest → lightest) after each ant construction.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..data import Node, TimeMatrix
from ..evaluation import route_time_minutes
from ..objective import ObjectiveWeights, search_objective
from ..utils import (
    build_groups_from_expanded_ids,
    deepcopy_routes,
    ensure_all_routes_capacity,
    ensure_groups_single_vehicle,
    set_seed,
    weighted_choice,
)

log = logging.getLogger(__name__)


@dataclass
class ACOConfig:
    time_limit_sec: float = 8.0
    seed: int = 42

    num_ants: int = 20
    alpha: float = 1.0          # pheromone exponent
    beta: float = 2.0           # heuristic (1/travel_time) exponent
    rho: float = 0.1            # evaporation rate
    q0_deposit: float = 1.0     # deposit constant Q in deposit = Q / cost
    budget_factor: float = 1.15  # demand budget per truck = total_demand / n_trucks * factor
    max_iter: Optional[int] = None  # optional iteration cap (None = time-limited only)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group_bases(nodes: Dict[str, Node], selected_expanded: List[str]) -> List[str]:
    """Distinct park bases (before split '#'), preserving discovery order."""
    seen = set()
    out: List[str] = []
    for sid in selected_expanded:
        base = sid.split("#")[0]
        if base not in seen:
            seen.add(base)
            out.append(base)
    return out


def _base_total_demand(
    base: str, groups: Dict[str, List[str]], nodes: Dict[str, Node]
) -> float:
    return sum(nodes[p].demand_liters for p in groups[base])


def _base_representative(base: str, groups: Dict[str, List[str]]) -> str:
    return groups[base][0]


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
) -> List[List[str]]:
    """Move park groups from heaviest truck to lightest until balanced.

    Mirrors the notebook's ``_rebalance_solution`` which iteratively transfers
    parks (full or half of the group's parts) between the longest and shortest
    routes until the gap falls below 15 % of the mean duration.
    """
    current = deepcopy_routes(routes)

    for _ in range(max_passes):
        # Compute route time per truck
        durations: Dict[int, float] = {}
        for ri, r in enumerate(current):
            if len(r) > 2:
                durations[ri] = route_time_minutes(r, nodes, tm)
            else:
                durations[ri] = 0.0

        if len(durations) <= 1:
            break

        heaviest_idx = max(durations, key=lambda k: durations[k])
        lightest_idx = min(durations, key=lambda k: durations[k])
        gap = durations[heaviest_idx] - durations[lightest_idx]

        mean_t = sum(durations.values()) / len(durations)
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

            # Try moving all parts to the lightest route
            cand = deepcopy_routes(current)
            cand[heaviest_idx] = [
                nid for nid in cand[heaviest_idx] if nid not in parts
            ]
            # Ensure depot bookends
            if cand[heaviest_idx][0] != depot_id:
                cand[heaviest_idx].insert(0, depot_id)
            if cand[heaviest_idx][-1] != depot_id:
                cand[heaviest_idx].append(depot_id)
            # Insert parts before the last depot in lightest
            insert_pos = max(1, len(cand[lightest_idx]) - 1)
            cand[lightest_idx][insert_pos:insert_pos] = parts

            # Ensure capacity
            cand, _ = ensure_all_routes_capacity(
                cand, nodes, vehicle_capacity, refill_ids, tm, depot_id
            )

            # Check if the heaviest still has parks
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
                best_move = ("full", base, cand)

        if best_move is None:
            break

        _, _, cand_routes = best_move
        current = cand_routes

    return current


# ---------------------------------------------------------------------------
# Ant construction (ported from notebook's _ant_construct)
# ---------------------------------------------------------------------------

def _construct_one_ant(
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: str,
    bases: List[str],
    groups: Dict[str, List[str]],
    num_vehicles: int,
    vehicle_capacity: float,
    refill_ids: List[str],
    tau: Dict[Tuple[str, str], float],
    cfg: ACOConfig,
) -> List[List[str]]:
    """Build a complete multi-vehicle solution using demand-budget construction.

    Each truck gets a demand budget of ``total_demand / num_vehicles * budget_factor``.
    Parks are selected probabilistically via ``tau^alpha * eta^beta`` (roulette wheel).
    When the tank runs out the nearest refill is visited.  Leftover demand after all
    trucks is redistributed.
    """
    total_demand = sum(_base_total_demand(b, groups, nodes) for b in bases)
    budget = total_demand / num_vehicles * cfg.budget_factor

    remaining: Dict[str, float] = {}
    for base in bases:
        remaining[base] = _base_total_demand(base, groups, nodes)

    routes: List[List[str]] = []

    for _tid in range(num_vehicles):
        route = [depot_id]
        load = vehicle_capacity
        cur = depot_id
        delivered_vol = 0.0

        while delivered_vol < budget:
            unmet = {b: d for b, d in remaining.items() if d > 0.1}
            if not unmet:
                break

            # Find feasible candidates (can serve within capacity)
            candidates = []
            for base, dem in unmet.items():
                rep = _base_representative(base, groups)
                dv = min(load, dem)
                if dv <= 0:
                    continue
                candidates.append(base)

            if not candidates:
                # Try refilling first
                if load >= vehicle_capacity - 0.1:
                    break  # already full, nothing fits
                if not refill_ids:
                    break
                nr = min(refill_ids, key=lambda r: tm.travel(cur, r))
                route.append(nr)
                load = vehicle_capacity
                cur = nr
                continue

            # Probabilistic selection: tau^alpha * eta^beta
            weights: List[float] = []
            for base in candidates:
                rep = _base_representative(base, groups)
                travel = tm.travel(cur, rep)
                eta = (1.0 / (travel + 1e-6)) ** cfg.beta
                key = (cur.split("#")[0], base)
                tau_ij = tau.get(key, 1.0)
                weights.append((tau_ij ** cfg.alpha) * eta)

            # Roulette wheel
            tot = sum(weights)
            if tot <= 0:
                chosen_idx = random.randrange(len(candidates))
            else:
                r = random.random() * tot
                acc = 0.0
                chosen_idx = len(candidates) - 1
                for ci, w in enumerate(weights):
                    acc += w
                    if acc >= r:
                        chosen_idx = ci
                        break

            chosen_base = candidates[chosen_idx]
            parts = groups.get(chosen_base, [chosen_base])
            dem = remaining[chosen_base]
            dv = min(load, dem, budget - delivered_vol)
            if dv <= 0:
                break

            # Add parts to route
            route.extend(parts)
            remaining[chosen_base] -= dv
            delivered_vol += dv
            load -= dv
            cur = parts[-1]

            # Check if need refill
            if load < 1.0 and refill_ids:
                nr = min(refill_ids, key=lambda r: tm.travel(cur, r))
                route.append(nr)
                load = vehicle_capacity
                cur = nr

        route.append(depot_id)
        routes.append(route)

    # Distribute leftover demand across trucks
    leftover = {b: d for b, d in remaining.items() if d > 0.1}
    if leftover:
        for base, d in list(leftover.items()):
            if d <= 0.1:
                continue
            # Sort trucks by current route time (lightest first)
            order = sorted(range(len(routes)), key=lambda ri: route_time_minutes(routes[ri], nodes, tm))
            placed = False
            for ri in order:
                if d <= 0.1:
                    break
                parts = groups.get(base, [base])
                # Try inserting
                cand = routes[ri][:]
                insert_pos = max(1, len(cand) - 1)
                cand[insert_pos:insert_pos] = parts
                placed = True
                routes[ri] = cand
                remaining[base] = 0
                d = 0
                break
            if not placed:
                # Force to lightest
                lightest = min(range(len(routes)), key=lambda ri: route_time_minutes(routes[ri], nodes, tm))
                parts = groups.get(base, [base])
                insert_pos = max(1, len(routes[lightest]) - 1)
                routes[lightest][insert_pos:insert_pos] = parts
                remaining[base] = 0

    # Pad out to num_vehicles
    while len(routes) < num_vehicles:
        routes.append([depot_id, depot_id])

    return routes


# ---------------------------------------------------------------------------
# Edge extraction (for pheromone deposit)
# ---------------------------------------------------------------------------

def _edges_of(
    routes: List[List[str]], part_to_base: Dict[str, str]
) -> List[Tuple[str, str]]:
    """Consecutive (from_base, to_base) edges over a full solution."""
    edges: List[Tuple[str, str]] = []
    for r in routes:
        prev_base: Optional[str] = None
        for nid in r:
            base = part_to_base.get(nid, nid.split("#")[0])
            if prev_base is not None and prev_base != base:
                edges.append((prev_base, base))
            prev_base = base
    return edges


# ---------------------------------------------------------------------------
# Main ACO loop (ported from notebook's run_aco)
# ---------------------------------------------------------------------------

def aco_optimize(
    init_routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    vehicle_capacity: float,
    refill_ids: List[str],
    depot_id: str,
    allow_refill: bool,
    groups: Dict[str, List[str]],
    cfg: Optional[ACOConfig] = None,
) -> List[List[str]]:
    """ACO optimiser — notebook-aligned implementation.

    Signature mirrors ``alns_optimize`` so ``solve()`` calls both uniformly.
    """
    cfg = cfg or ACOConfig()
    log.info("ACO starting with seed: %d", cfg.seed)
    set_seed(cfg.seed)

    # Pre-compute: bases, part->base map, num_vehicles
    part_to_base: Dict[str, str] = {}
    for base, parts in groups.items():
        for p in parts:
            part_to_base[p] = base
    bases = sorted(groups.keys())
    if not bases:
        return init_routes

    num_vehicles = max(1, len(init_routes))

    weights_obj = ObjectiveWeights()

    def objective(routes: List[List[str]]) -> float:
        return search_objective(routes, nodes, tm, weights_obj)

    def finalize(routes: List[List[str]]) -> List[List[str]]:
        """Apply safety passes (identical to ALNS) for feasibility."""
        routes = ensure_groups_single_vehicle(
            routes, groups, nodes, tm, depot_id,
            vehicle_capacity=vehicle_capacity, refill_ids=refill_ids,
        )
        routes, _ = ensure_all_routes_capacity(
            routes, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )
        return routes

    # --- Initialize pheromone ---
    tau: Dict[Tuple[str, str], float] = {}  # all default to 1.0 via .get(key, 1.0)

    # Seed cost from init routes for reference
    seed_routes = finalize(deepcopy_routes(init_routes))
    seed_cost = objective(seed_routes)
    best_routes = seed_routes
    best_cost = seed_cost

    start = time.time()
    iteration = 0

    def time_ok():
        return time.time() - start < cfg.time_limit_sec

    def iter_ok():
        if cfg.max_iter is not None:
            return iteration < cfg.max_iter
        return True

    while time_ok() and iter_ok():
        iteration += 1

        # Build num_ants solutions this iteration
        iter_best_routes: Optional[List[List[str]]] = None
        iter_best_cost = float("inf")

        for _ant in range(cfg.num_ants):
            if not time_ok():
                break
            raw = _construct_one_ant(
                nodes=nodes, tm=tm, depot_id=depot_id,
                bases=bases, groups=groups,
                num_vehicles=num_vehicles,
                vehicle_capacity=vehicle_capacity,
                refill_ids=refill_ids,
                tau=tau, cfg=cfg,
            )
            routes = finalize(raw)
            # Rebalance
            routes = _rebalance_solution(
                routes, nodes, tm, groups,
                vehicle_capacity, refill_ids, depot_id,
                objective,
            )
            cost = objective(routes)
            if cost < iter_best_cost:
                iter_best_cost = cost
                iter_best_routes = routes

        if iter_best_routes is None:
            break

        if iter_best_cost < best_cost - 1e-9:
            best_cost = iter_best_cost
            best_routes = deepcopy_routes(iter_best_routes)

        # --- Pheromone update (notebook style) ---
        # 1. Evaporate ALL pheromone
        for key in list(tau.keys()):
            tau[key] *= (1.0 - cfg.rho)

        # 2. Deposit from iteration-best AND global-best
        for cost_d, routes_d in [
            (iter_best_cost, iter_best_routes),
            (best_cost, best_routes),
        ]:
            deposit = cfg.q0_deposit / (cost_d + 1e-9)
            for edge in _edges_of(routes_d, part_to_base):
                current = tau.get(edge, 1.0)
                tau[edge] = current + deposit
                # Also deposit reverse edge
                rev = (edge[1], edge[0])
                current_rev = tau.get(rev, 1.0)
                tau[rev] = current_rev + deposit

    log.info(
        "ACO done: %d iterations, best_cost=%.3f, pheromone entries=%d",
        iteration, best_cost, len(tau),
    )
    return best_routes


__all__ = ["ACOConfig", "aco_optimize"]
