"""Feasibility validation for solver outputs.

`is_feasible` is the single source of truth for whether a solution meets all constraints.
Both the API endpoint and the offline experiment scripts use it — no separate
per-algorithm feasibility check exists.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

from .data import Node, TimeMatrix
from .evaluation import capacity_trace_and_violations


def is_feasible(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    vehicle_capacity: float,
    required_park_ids: Iterable[str],
) -> bool:
    """A solution is feasible iff every required park (base id or split part) is visited
    exactly the number of times its splits demand AND no route has any capacity
    violation.

    `required_park_ids` should be the post-expand-split-delivery list — i.e. every
    base id + every "base#k" split part that solve() built up front.
    """
    required = set(required_park_ids)
    visited: dict = {}
    for r in routes:
        for nid in r[1:-1]:
            if nid in required:
                visited[nid] = visited.get(nid, 0) + 1

    for nid in required:
        if visited.get(nid, 0) < 1:
            return False

    for r in routes:
        if len(r) <= 2:
            continue
        _, violations = capacity_trace_and_violations(r, nodes, vehicle_capacity)
        if violations:
            return False

    return True
