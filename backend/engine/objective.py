"""Shared internal search objective used by both ALNS and ACO.

Requirement 5 of the paper-revision spec mandates that all comparison algorithms use the
same objective function — this module is the single source of truth. Formula preserved
verbatim from the original inline closure inside alns.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .data import Node, TimeMatrix
from .evaluation import route_time_minutes


@dataclass
class ObjectiveWeights:
    alpha_variance: float = 1.0
    lambda_total_time: float = 1e-3
    gamma_overload_threshold: float = 1.0
    overload_weight: float = 0.01


def search_objective(
    routes: List[List[str]],
    nodes: Dict[str, Node],
    tm: TimeMatrix,
    weights: ObjectiveWeights = ObjectiveWeights(),
) -> float:
    """Combined objective: makespan + variance + tiny total-time term + overload penalty.

    Kept identical to the original ALNS internal objective so behaviour is unchanged and
    all algorithms are judged on the same fitness during search.
    """
    route_durations = [route_time_minutes(r, nodes, tm) for r in routes if len(r) > 2]
    if not route_durations:
        return 0.0

    makespan = max(route_durations)
    total_time = sum(route_durations)
    mean_duration = total_time / len(route_durations)
    variance = sum((d - mean_duration) ** 2 for d in route_durations) / len(route_durations)

    over = [max(0.0, d - weights.gamma_overload_threshold * mean_duration) for d in route_durations]
    overload_penalty = sum(o**2 for o in over)

    return (
        makespan
        + weights.alpha_variance * variance
        + weights.lambda_total_time * total_time
        + weights.overload_weight * overload_penalty
    )
