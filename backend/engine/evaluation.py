"""Route-level and solution-level evaluation functions.

Contains both:
  - ``route_time_minutes``: simple travel+service-time sum (used internally by
    rebalance, neighborhoods, repair operators).
  - ``evaluate_route``: notebook-aligned route evaluation with initial refill
    overhead, proportional service time, and time-window feasibility checking.
  - Metric helpers: ``total_time_minutes``, ``makespan_minutes``, ``route_time_std``,
    ``count_refill_visits``, ``count_active_vehicles``, ``load_profile_liters``.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .data import Node, TimeMatrix


# ---------------------------------------------------------------------------
# Operational constants (matching notebook cell 1)
# ---------------------------------------------------------------------------
TRUCK_CAP: float = 5000.0           # tank capacity (liters)
OP_WINDOW: float = 540.0            # operational window (minutes) 06:00–15:00
SERVICE_TIME_FULL: float = 20.0     # watering service time for 5000 L full (minutes)
SERVICE_TIME_REFILL: float = 5.0    # refill service time at station (minutes)


# ---------------------------------------------------------------------------
# Simple route time (used internally by rebalance, neighborhoods, etc.)
# ---------------------------------------------------------------------------

def route_time_minutes(
    route: List[str],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    vehicle_capacity: float = TRUCK_CAP,
    refill_ids: Optional[List[str]] = None,
    depot_id: Optional[str] = None,
) -> float:
    """Total travel + service + initial refill time (minutes) for a single route (cell 5)."""
    return evaluate_route(route, nodes, tm, vehicle_capacity, refill_ids, depot_id)[0]


# ---------------------------------------------------------------------------
# Notebook-aligned route evaluation (evaluate_route from cell 5)
# ---------------------------------------------------------------------------

def _find_depot_id(nodes: Dict[str, Node]) -> Optional[str]:
    """Find the depot node id."""
    for nid, node in nodes.items():
        if node.type == "depot":
            return nid
    return None


def _find_refill_ids(nodes: Dict[str, Node]) -> List[str]:
    """Find all refill station node ids."""
    return [nid for nid, node in nodes.items() if node.type == "refill"]


def evaluate_route(
    route: List[str],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    vehicle_capacity: float = TRUCK_CAP,
    refill_ids: Optional[List[str]] = None,
    depot_id: Optional[str] = None,
    delivery_amounts: Optional[Dict[str, float]] = None,
) -> Tuple[float, bool, Dict[str, float]]:
    """Evaluate a single route — notebook-aligned (cell 5).

    Includes:
      - Initial refill overhead: travel to nearest refill + SERVICE_TIME_REFILL.
      - Proportional service time at parks: SERVICE_TIME_FULL × (demand / TRUCK_CAP).
      - Fixed SERVICE_TIME_REFILL at refill stations.
      - Time-window feasibility check at every step (current_time + return-to-depot ≤ OP_WINDOW).

    Returns:
        (current_time, feasible, violations_dict)
    """
    if len(route) <= 2:
        return 0.0, True, {}

    if depot_id is None:
        depot_id = _find_depot_id(nodes)
    if refill_ids is None:
        refill_ids = _find_refill_ids(nodes)

    refills_available = refill_ids if refill_ids else []
    park_ids = set(nid for nid, n in nodes.items() if n.type == "park")
    refill_set = set(refills_available)

    current_time = 0.0
    violations: Dict[str, float] = {}

    # Initial fill: drive to nearest refill from depot + refill time
    if refills_available and depot_id:
        init_refill = min(refills_available, key=lambda r: tm.travel(depot_id, r))
        current_time += tm.travel(depot_id, init_refill) + SERVICE_TIME_REFILL
    current_load = float(vehicle_capacity)

    visit_order: Dict[str, List[int]] = {}
    if delivery_amounts:
        for idx, nd in enumerate(route[1:-1], start=1):
            if nd in park_ids:
                visit_order.setdefault(nd, []).append(idx)

    # Walk through consecutive edges in the route
    for i in range(len(route) - 1):
        frm, to = route[i], route[i + 1]
        current_time += tm.travel(frm, to)

        node_to = nodes.get(to)
        if node_to is None:
            continue

        if node_to.type == "depot":
            # Check time window on return to depot
            if current_time > OP_WINDOW:
                violations["tw_return_depot"] = current_time - OP_WINDOW
            continue

        if to in park_ids:
            if delivery_amounts and to in visit_order:
                positions = visit_order[to]
                dv = delivery_amounts.get(to, 0.0) / len(positions)
                if dv <= 0:
                    continue
                dv_actual = min(dv, current_load)
            else:
                demand = node_to.demand_liters
                dv_actual = min(demand, current_load)

            if dv_actual > 0:
                current_load -= dv_actual
                current_time += SERVICE_TIME_FULL * (dv_actual / vehicle_capacity)

        elif to in refill_set:
            current_load = float(vehicle_capacity)
            current_time += SERVICE_TIME_REFILL

        # Time window check: can we still return to depot within OP_WINDOW?
        if depot_id and to != depot_id:
            time_return = current_time + tm.travel(to, depot_id)
            if time_return > OP_WINDOW:
                violations[f"tw_{to}"] = time_return - OP_WINDOW

    feasible = len(violations) == 0
    return current_time, feasible, violations


def rebuild_route_with_refills(
    park_sequence: List[str],
    dmap_route: Dict[str, float],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: Optional[str] = None,
    refill_ids: Optional[List[str]] = None,
    vehicle_capacity: float = TRUCK_CAP,
) -> List[str]:
    if depot_id is None:
        depot_id = _find_depot_id(nodes) or "0"
    if not park_sequence:
        return [depot_id, depot_id]

    if refill_ids is None:
        refill_ids = _find_refill_ids(nodes)

    refills_available = refill_ids if refill_ids else []
    route = [depot_id]
    load = float(vehicle_capacity)
    pos = depot_id

    for nd in park_sequence:
        remaining = dmap_route.get(nd, 0.0)
        if remaining <= 0.1:
            continue
        while remaining > 0.1:
            if load < 0.1 and refills_available:
                nr = min(refills_available, key=lambda r: tm.travel(pos, r))
                route.append(nr)
                load = float(vehicle_capacity)
                pos = nr
            dv_actual = min(remaining, load)
            route.append(nd)
            load -= dv_actual
            remaining -= dv_actual
            pos = nd
            if remaining > 0.1 and load < 0.1 and refills_available:
                nr = min(refills_available, key=lambda r: tm.travel(pos, r))
                route.append(nr)
                load = float(vehicle_capacity)
                pos = nr
    route.append(depot_id)
    return route


def rebuild_routes_from_dmap(
    dmap: Dict[int, Dict[str, float]],
    num_trucks: int,
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    depot_id: Optional[str] = None,
    refill_ids: Optional[List[str]] = None,
    vehicle_capacity: float = TRUCK_CAP,
) -> List[List[str]]:
    park_ids = [nid for nid, n in nodes.items() if n.type == "park"]
    return [
        rebuild_route_with_refills(
            [nd for nd in park_ids if dmap[ri].get(nd, 0.0) > 0.1],
            dmap[ri],
            nodes,
            tm,
            depot_id,
            refill_ids,
            vehicle_capacity,
        )
        for ri in range(num_trucks)
    ]


# ---------------------------------------------------------------------------
# Aggregate metrics (used by solve.py and downstream)
# ---------------------------------------------------------------------------

def total_time_minutes(
    routes: List[List[str]], nodes: Dict[str, Node], tm: TimeMatrix
) -> float:
    return sum(route_time_minutes(r, nodes, tm) for r in routes if len(r) > 1)


def load_profile_liters(
    route: List[str], nodes: Dict[str, Node], vehicle_capacity: float
) -> List[float]:
    """Remaining capacity after visiting each node in route."""
    rem = 0.0
    profile: List[float] = []
    for nid in route:
        n = nodes[nid]
        if n.type == "refill":
            rem = vehicle_capacity
        elif n.type == "park":
            rem -= n.demand_liters
            if rem < 0:
                rem = 0.0
        profile.append(rem)
    return profile


def makespan_minutes(routes, nodes, tm):
    if not routes:
        return 0.0
    per_route = [route_time_minutes(r, nodes, tm) for r in routes if len(r) > 1]
    return max(per_route) if per_route else 0.0


def route_time_std(
    routes: List[List[str]], nodes: Dict[str, Node], tm: TimeMatrix
) -> float:
    """Population standard deviation of active-route durations (minutes)."""
    durations = [route_time_minutes(r, nodes, tm) for r in routes if len(r) > 2]
    if not durations:
        return 0.0
    mean = sum(durations) / len(durations)
    variance = sum((d - mean) ** 2 for d in durations) / len(durations)
    return variance ** 0.5


def count_refill_visits(
    routes: List[List[str]], nodes: Dict[str, Node]
) -> int:
    """Total number of refill-station visits across all active routes."""
    n = 0
    for r in routes:
        if len(r) <= 2:
            continue
        for nid in r[1:-1]:
            if nodes[nid].type == "refill":
                n += 1
    return n


def count_active_vehicles(routes: List[List[str]]) -> int:
    """Number of vehicles that actually visit at least one non-depot node."""
    return sum(1 for r in routes if len(r) > 2)


def count_empty_trucks(
    routes: List[List[str]], nodes: Dict[str, Node]
) -> int:
    """Count trucks with no park visits (only depot + possibly refill)."""
    n = 0
    for r in routes:
        has_park = any(
            nodes.get(nid) and nodes[nid].type == "park"
            for nid in r[1:-1]
        )
        if not has_park:
            n += 1
    return n


def capacity_trace_and_violations(route, nodes, vehicle_capacity):
    """Return (trace_strict, violations) where trace can go negative."""
    rem = 0.0
    trace = []
    violations = []
    for idx, nid in enumerate(route):
        n = nodes[nid]
        if n.type == "refill":
            rem = vehicle_capacity
        elif n.type == "park":
            rem -= n.demand_liters
            if rem < 0:
                violations.append((idx, nid, -rem))
        trace.append(rem)
    return trace, violations
