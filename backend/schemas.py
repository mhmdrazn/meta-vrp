from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class OptimizeRequest(BaseModel):
    selected_node_ids: Annotated[
        List[str],
        Field(min_length=1, description="Park node IDs selected for the optimisation"),
    ]
    num_vehicles: Annotated[
        int, Field(ge=1, description="Number of vehicles to dispatch")
    ]
    # Additive fields — all have defaults so existing clients keep working unchanged.
    dataset_id: str = Field(
        default="dataset_a", description="Dataset identifier: 'dataset_a' | 'dataset_b'"
    )
    algorithm: Literal["aco", "alns_standard", "alns_hybrid"] = Field(
        default="alns_hybrid",
        description="Which algorithm to run (default keeps the existing hybrid ALNS behaviour)",
    )
    refill_ids_override: Optional[List[str]] = Field(
        default=None,
        description="If provided, restricts the available refill stations to this subset. "
        "None = all refills available (default). Empty list = no refills available.",
    )
    seed: Optional[int] = Field(
        default=None, description="RNG seed for reproducibility"
    )
    time_limit_sec: Optional[float] = Field(
        default=None,
        description="Override for the solver time budget in seconds. None = use settings.TIME_LIMIT_SEC.",
    )


class RouteResult(BaseModel):
    vehicle_id: int
    sequence: List[str]
    total_time_min: float
    load_profile_liters: List[float]


class OptimizeResponse(BaseModel):
    # --- kept for backward compatibility (existing frontend reads these) ---
    objective_time_min: float
    vehicle_used: int
    routes: List[RouteResult]
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    job_id: Optional[str] = None
    # --- new metric fields (additive — matches SolveResult) ---
    fitness: float = 0.0
    total_time: float = 0.0
    makespan: float = 0.0
    route_time_std: float = 0.0
    active_vehicles: int = 0
    refill_visits: int = 0
    computation_time: float = 0.0
    feasible: bool = True
    algorithm: str = "alns_hybrid"
