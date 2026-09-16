"""Shared internal search objective used by ALNS, ACO, and Hybrid algorithms.

Formula ported directly from notebook cell 6 (evaluate_solution):
  fitness = total_time
          + REFILL_PENALTY * n_refill_visits
          + BALANCE_PENALTY * std(route_times)
          + PENALTY * (shortfall / TRUCK_CAP)
          + PENALTY * (tw_violation / OP_WINDOW)
          + PENALTY * n_empty_trucks
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from .data import Node, TimeMatrix
from .evaluation import evaluate_route

# Default operational constants (matching notebook cell 1 & cell 6)
TRUCK_CAP: float = 5000.0
OP_WINDOW: float = 540.0
SERVICE_TIME_FULL: float = 20.0
SERVICE_TIME_REFILL: float = 5.0
REFILL_PENALTY: float = 2.0
BALANCE_PENALTY: float = 0.1
PENALTY: float = 1e6


@dataclass
class ObjectiveWeights:
    penalty: float = PENALTY
    refill_penalty: float = REFILL_PENALTY
    balance_penalty: float = BALANCE_PENALTY
    truck_cap: float = TRUCK_CAP
    op_window: float = OP_WINDOW
    service_time_full: float = SERVICE_TIME_FULL
    service_time_refill: float = SERVICE_TIME_REFILL


def evaluate_solution(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    delivery_map: Optional[Dict[int, Dict[str, float]]] = None,
    weights: ObjectiveWeights = ObjectiveWeights(),
) -> Tuple[float, bool]:
    """Evaluate a solution — notebook-aligned (cell 6).

    Returns (total_fitness, all_feasible).
    """
    total_fitness = 0.0
    all_feasible = True

    park_ids = [nid for nid, n in nodes.items() if n.type == "park"]
    refill_set = set(nid for nid, n in nodes.items() if n.type == "refill")
    park_set = set(park_ids)

    # 1. Demand fulfillment constraint
    if delivery_map is not None:
        delivered: Dict[str, float] = {}
        for ri in range(len(routes)):
            if ri in delivery_map:
                for nd, amt in delivery_map[ri].items():
                    delivered[nd] = delivered.get(nd, 0.0) + amt
        for nd in park_ids:
            shortfall = nodes[nd].demand_liters - delivered.get(nd, 0.0)
            if shortfall > 0.1:
                total_fitness += weights.penalty * (shortfall / weights.truck_cap)
                all_feasible = False
    else:
        total_park_demand = sum(nodes[nd].demand_liters for nd in park_ids)
        served_park_ids = set()
        for r in routes:
            for nid in r[1:-1]:
                if nodes.get(nid) and nodes[nid].type == "park":
                    served_park_ids.add(nid)
        delivered_demand = sum(nodes[nid].demand_liters for nid in served_park_ids)
        shortfall = max(0.0, total_park_demand - delivered_demand)
        if shortfall > 0.1:
            total_fitness += weights.penalty * (shortfall / weights.truck_cap)
            all_feasible = False

    # 2. Route time + refill penalty + time window violation penalty
    for ri, r in enumerate(routes):
        dmap_r = delivery_map[ri] if (delivery_map and ri in delivery_map) else None
        t, feas, viol = evaluate_route(
            r, nodes, tm, vehicle_capacity=weights.truck_cap, delivery_amounts=dmap_r
        )
        n_refill = sum(1 for nid in r[1:-1] if nid in refill_set)
        total_fitness += t + n_refill * weights.refill_penalty
        if not feas:
            all_feasible = False
            for v in viol.values():
                total_fitness += weights.penalty * (v / weights.op_window)

    # 3. Balance penalty: penalize route time standard deviation across active trucks
    times = [
        evaluate_route(
            r,
            nodes,
            tm,
            vehicle_capacity=weights.truck_cap,
            delivery_amounts=(
                delivery_map[ri] if (delivery_map and ri in delivery_map) else None
            ),
        )[0]
        for ri, r in enumerate(routes)
        if len(r) > 2
    ]
    if len(times) > 1:
        total_fitness += weights.balance_penalty * float(np.std(times))

    # 4. Penalty for empty trucks (no parks served)
    n_empty = sum(
        1
        for r in routes
        if not any(nodes.get(nid) and nodes[nid].type == "park" for nid in r[1:-1])
    )
    if n_empty > 0:
        total_fitness += weights.penalty * n_empty
        all_feasible = False

    return total_fitness, all_feasible


def search_objective(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    delivery_map: Optional[Dict[int, Dict[str, float]]] = None,
    weights: ObjectiveWeights = ObjectiveWeights(),
) -> float:
    """Single-source-of-truth objective function (notebook cell 6 evaluate_solution)."""
    fitness, _ = evaluate_solution(
        routes, nodes, tm, delivery_map=delivery_map, weights=weights
    )
    return fitness
