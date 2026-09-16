import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    lat: float
    lon: float
    type: str  # 'depot' | 'park' | 'refill'
    demand_liters: float
    service_min: float


class TimeMatrix:
    def __init__(self, ids: List[str], matrix: np.ndarray):
        self.ids = ids
        self.index = {nid: i for i, nid in enumerate(ids)}
        self.M = matrix  # minutes

    def travel(self, a: str, b: str) -> float:
        return float(self.M[self.index[a], self.index[b]])


def load_nodes_csv(
    path: str,
) -> Tuple[Dict[str, Node], List[str]]:  # ⬅️ return ids_in_order juga
    df = pd.read_csv(path)
    required = {"id", "name", "lat", "lon", "type", "demand_liters", "service_min"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"dataset_a.csv missing columns: {missing}")

    nodes: Dict[str, Node] = {}
    ids_in_order: List[str] = []

    for _, r in df.iterrows():
        nid = str(r["id"]).strip()  # ⬅️ jadikan string + trim
        ntype = str(r["type"]).strip().lower()  # ⬅️ trim + lower
        node = Node(
            id=nid,
            name=str(r["name"]).strip(),
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            type=ntype,
            demand_liters=float(r["demand_liters"]),
            service_min=float(r["service_min"]),
        )
        nodes[nid] = node
        ids_in_order.append(nid)

    if not any(n.type == "depot" for n in nodes.values()):
        raise ValueError("dataset_a.csv must contain at least one node with type=depot")

    return nodes, ids_in_order


def load_time_matrix_csv(path: str, ids_in_order: List[str]) -> TimeMatrix:
    M = pd.read_csv(path, header=None).to_numpy(dtype=float)
    n = len(ids_in_order)
    if M.shape != (n, n):
        raise ValueError(f"time_matrix_a.csv must be {n}x{n}, got {M.shape}")
    return TimeMatrix(ids_in_order, M)


# ---------------------------------------------------------------------------
# JSON / NPY loaders — the "deployment-ready" static-file format (TASK 2)
# ---------------------------------------------------------------------------
def load_nodes_json(path: str) -> Tuple[Dict[str, Node], List[str]]:
    """Load nodes from a JSON file of the shape produced by scripts/convert_dataset.py.

    Expected schema:
        {
            "meta": {...},
            "nodes": [
                {"id": "0", "name": "...", "lat": -7.26, "lon": 112.75, "type": "depot",
                 "demand_liters": 0.0, "service_min": 0.0},
                ...
            ]
        }
    """
    with open(path, "r", encoding="utf-8") as f:
        payload: Dict[str, Any] = json.load(f)

    if "nodes" not in payload or not isinstance(payload["nodes"], list):
        raise ValueError(f"{path}: missing top-level 'nodes' list")

    nodes: Dict[str, Node] = {}
    ids_in_order: List[str] = []
    required_fields = {
        "id",
        "name",
        "lat",
        "lon",
        "type",
        "demand_liters",
        "service_min",
    }

    for i, row in enumerate(payload["nodes"]):
        missing = required_fields - set(row.keys())
        if missing:
            raise ValueError(f"{path} nodes[{i}] missing fields: {missing}")
        nid = str(row["id"]).strip()
        node = Node(
            id=nid,
            name=str(row["name"]).strip(),
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            type=str(row["type"]).strip().lower(),
            demand_liters=float(row["demand_liters"]),
            service_min=float(row["service_min"]),
        )
        nodes[nid] = node
        ids_in_order.append(nid)

    if not any(n.type == "depot" for n in nodes.values()):
        raise ValueError(f"{path} must contain at least one node with type=depot")

    return nodes, ids_in_order


def load_time_matrix_npy(path: str, ids_in_order: List[str]) -> TimeMatrix:
    """Load a time matrix from an .npy file (produced by scripts/convert_dataset.py)."""
    M = np.load(path)
    if M.dtype != np.float64:
        M = M.astype(np.float64)
    n = len(ids_in_order)
    if M.shape != (n, n):
        raise ValueError(
            f"{path}: matrix shape {M.shape} does not match nodes count {n}"
        )
    return TimeMatrix(ids_in_order, M)
