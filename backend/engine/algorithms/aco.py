from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..data import Node, TimeMatrix
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

    num_ants: int = 15
    alpha: float = 1.0         # pheromone exponent
    beta: float = 3.0          # heuristic (1/travel_time) exponent
    rho: float = 0.15          # evaporation rate
    q0: float = 0.1            # ACS exploitation threshold (0 = pure roulette)
    tau_min_factor: float = 0.05  # MMAS tau_min = tau_max * tau_min_factor
    elitist: bool = True       # only iteration-best ant deposits (MMAS-style)


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
    """Any part is fine for travel-time lookups — expand_split_delivery makes them all
    identical from any external node's perspective."""
    return groups[base][0]


def _construct_one_ant(
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: str,
    bases: List[str],
    groups: Dict[str, List[str]],
    num_vehicles: int,
    vehicle_capacity: float,
    tau: Dict[Tuple[str, str], float],
    cfg: ACOConfig,
) -> List[List[str]]:
    """Build one complete multi-vehicle solution from scratch using ACS rules."""
    remaining = set(bases)
    routes: List[List[str]] = []
    vehicle_idx = 0
    current: List[str] = [depot_id]
    running_load = 0.0

    while remaining and vehicle_idx < num_vehicles:
        last_id = current[-1]

        # Candidate bases whose full group demand fits the vehicle's remaining capacity.
        # (Any base is legal in the sense that ensure_all_routes_capacity will insert
        # refills where needed — but for ant construction we still prefer groups that
        # fit, mirroring the greedy_construct behaviour so search focus goes into
        # ordering rather than repair.)
        candidates = []
        for base in remaining:
            demand = _base_total_demand(base, groups, nodes)
            if demand <= vehicle_capacity - running_load + 1e-9:
                candidates.append(base)

        if not candidates:
            # Close this route, start a new vehicle.
            if current[-1] != depot_id:
                current.append(depot_id)
            routes.append(current)
            vehicle_idx += 1
            current = [depot_id]
            running_load = 0.0
            continue

        # ACS selection ---------------------------------------------------------
        # Score = (tau^alpha) * (eta^beta), eta = 1 / (travel_time + service + eps)
        rep_ids = [_base_representative(b, groups) for b in candidates]
        weights: List[float] = []
        for base, rep in zip(candidates, rep_ids):
            travel = tm.travel(last_id, rep)
            eta = 1.0 / (travel + nodes[rep].service_min + 1e-6)
            key = (last_id.split("#")[0], base)
            tau_ij = tau.get(key, 1.0)
            weights.append((tau_ij ** cfg.alpha) * (eta ** cfg.beta))

        if random.random() < cfg.q0:
            # Exploitation: argmax.
            best_i = max(range(len(candidates)), key=lambda i: weights[i])
        else:
            # Biased exploration via existing roulette helper.
            best_i = weighted_choice(weights)

        chosen_base = candidates[best_i]
        parts = groups[chosen_base]
        current.extend(parts)
        running_load += _base_total_demand(chosen_base, groups, nodes)
        remaining.remove(chosen_base)

    # Close the current route if it has any parks.
    if current[-1] != depot_id:
        current.append(depot_id)
    if len(current) > 2:  # more than [depot, depot]
        routes.append(current)

    # Any leftover bases (couldn't fit within num_vehicles) — append to the smallest
    # route (mirrors the fallback in greedy_construct). The subsequent
    # ensure_all_routes_capacity pass will insert refills to make it feasible.
    if remaining:
        if not routes:
            routes.append([depot_id, depot_id])
        smallest_idx = min(range(len(routes)), key=lambda i: len(routes[i]))
        tgt = routes[smallest_idx]
        insert_pos = len(tgt) - 1 if tgt[-1] == depot_id else len(tgt)
        leftover_parts: List[str] = []
        for base in remaining:
            leftover_parts.extend(groups[base])
        tgt[insert_pos:insert_pos] = leftover_parts

    # Pad out to `num_vehicles` routes if construction closed early — keeps the return
    # shape stable so downstream code (evaluation, etc.) doesn't need special cases.
    while len(routes) < num_vehicles:
        routes.append([depot_id, depot_id])

    return routes


def _edges_of(
    routes: List[List[str]], part_to_base: Dict[str, str]
) -> List[Tuple[str, str]]:
    """Consecutive (from_base, to_base) edges over a full solution — depot uses id-as-is."""
    edges: List[Tuple[str, str]] = []
    for r in routes:
        prev_base: Optional[str] = None
        for nid in r:
            base = part_to_base.get(nid, nid.split("#")[0])
            if prev_base is not None and prev_base != base:
                edges.append((prev_base, base))
            prev_base = base
    return edges


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
    """MMAS-style ACO. Signature mirrors alns_optimize so solve() calls both uniformly."""
    cfg = cfg or ACOConfig()
    log.info(f"ACO starting with seed: {cfg.seed}")
    set_seed(cfg.seed)

    # Pre-compute: bases, part->base map (for edge extraction), and num_vehicles
    # inferred from init_routes so ant construction matches the ALNS-side vehicle count.
    part_to_base: Dict[str, str] = {}
    for base, parts in groups.items():
        for p in parts:
            part_to_base[p] = base
    bases = sorted(groups.keys())
    if not bases:
        return init_routes  # nothing to optimise

    num_vehicles = max(1, len(init_routes))

    weights_obj = ObjectiveWeights()

    def objective(routes: List[List[str]]) -> float:
        return search_objective(routes, nodes, tm, weights_obj)

    def finalize(routes: List[List[str]]) -> List[List[str]]:
        """Apply the SAME safety passes ALNS runs after each iteration — guarantees
        identical feasibility semantics across algorithms."""
        routes = ensure_groups_single_vehicle(
            routes,
            groups,
            nodes,
            tm,
            depot_id,
            vehicle_capacity=vehicle_capacity,
            refill_ids=refill_ids,
        )
        routes, _ = ensure_all_routes_capacity(
            routes, nodes, vehicle_capacity, refill_ids, tm, depot_id
        )
        return routes

    # Seed pheromone from the greedy-construct solution's cost.
    seed_routes = finalize(deepcopy_routes(init_routes))
    seed_cost = objective(seed_routes)
    if seed_cost <= 1e-9:
        seed_cost = 1.0
    tau0 = 1.0 / (cfg.rho * seed_cost)
    tau: Dict[Tuple[str, str], float] = {}
    tau_max = tau0
    tau_min = tau_max * cfg.tau_min_factor

    best_routes = seed_routes
    best_cost = seed_cost

    start = time.time()
    iteration = 0
    while time.time() - start < cfg.time_limit_sec:
        iteration += 1

        # Build num_ants ant solutions this iteration.
        iter_best_routes: Optional[List[List[str]]] = None
        iter_best_cost = float("inf")

        for _ant in range(cfg.num_ants):
            if time.time() - start >= cfg.time_limit_sec:
                break
            raw = _construct_one_ant(
                nodes=nodes,
                tm=tm,
                depot_id=depot_id,
                bases=bases,
                groups=groups,
                num_vehicles=num_vehicles,
                vehicle_capacity=vehicle_capacity,
                tau=tau,
                cfg=cfg,
            )
            routes = finalize(raw)
            cost = objective(routes)
            if cost < iter_best_cost:
                iter_best_cost = cost
                iter_best_routes = routes

        if iter_best_routes is None:
            break  # ran out of time

        if iter_best_cost < best_cost - 1e-9:
            best_cost = iter_best_cost
            best_routes = iter_best_routes
            # MMAS: recompute tau bounds from the new best cost.
            tau_max = 1.0 / (cfg.rho * max(best_cost, 1e-9))
            tau_min = tau_max * cfg.tau_min_factor

        # --- Pheromone update -----------------------------------------------
        # 1. Evaporate every edge we've seen so far.
        for key in list(tau.keys()):
            tau[key] = max(tau_min, tau[key] * (1.0 - cfg.rho))

        # 2. Deposit: MMAS elitist = iteration-best only. Non-elitist = every ant.
        depositors: List[Tuple[List[List[str]], float]] = []
        if cfg.elitist:
            depositors.append((iter_best_routes, iter_best_cost))
        else:
            depositors.append((iter_best_routes, iter_best_cost))
            # (For simplicity we still deposit only iter-best here — full non-elitist
            # would require tracking all ants; MMAS-elitist is the standard mode for
            # this problem size anyway.)

        for routes_d, cost_d in depositors:
            deposit_amount = 1.0 / max(cost_d, 1e-9)
            for edge in _edges_of(routes_d, part_to_base):
                current = tau.get(edge, tau0)
                tau[edge] = min(tau_max, current + deposit_amount)

    log.info(
        "ACO done: %d iterations, best_cost=%.3f, pheromone entries=%d",
        iteration,
        best_cost,
        len(tau),
    )
    return best_routes


# Public re-exports for parity with the alns module surface area.
__all__ = ["ACOConfig", "aco_optimize"]
