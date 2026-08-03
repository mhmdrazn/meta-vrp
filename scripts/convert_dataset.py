from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys

import numpy as np

# Repo-root import so `python scripts/convert_dataset.py` works from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.engine.data import (  # noqa: E402
    load_nodes_csv,
    load_nodes_json,
    load_time_matrix_csv,
    load_time_matrix_npy,
)
from backend.engine.io_utils import validate_dataset  # noqa: E402


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def convert(
    dataset_id: str,
    nodes_csv: str,
    matrix_csv: str,
    out_dir: str,
    refill_service_min: float = 12.0,
) -> None:
    print(f"[{dataset_id}] loading source CSVs...", file=sys.stderr)
    nodes, ids_in_order = load_nodes_csv(nodes_csv)
    tm = load_time_matrix_csv(matrix_csv, ids_in_order)

    # Validate the source data BEFORE writing anything so we don't produce a garbage JSON.
    print(f"[{dataset_id}] validating source data...", file=sys.stderr)
    validate_dataset(nodes, tm)

    os.makedirs(out_dir, exist_ok=True)
    out_nodes = os.path.join(out_dir, f"{dataset_id}.json")
    out_matrix = os.path.join(out_dir, f"{dataset_id}_time_matrix.npy")

    payload = {
        "meta": {
            "dataset_id": dataset_id,
            "generated_from_nodes": os.path.relpath(nodes_csv, _REPO_ROOT).replace("\\", "/"),
            "generated_from_matrix": os.path.relpath(matrix_csv, _REPO_ROOT).replace("\\", "/"),
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "node_count": len(nodes),
            "nodes_csv_sha256_prefix": _sha256_of(nodes_csv),
            "matrix_csv_sha256_prefix": _sha256_of(matrix_csv),
            # Global refill service time (see io_utils/validate_dataset notes) — matches
            # the current settings.REFILL_SERVICE_MIN constant.
            "refill_service_min": refill_service_min,
            "time_unit": "minutes",
            "volume_unit": "liters",
        },
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "lat": n.lat,
                "lon": n.lon,
                "type": n.type,
                "demand_liters": n.demand_liters,
                "service_min": n.service_min,
            }
            for n in nodes.values()
        ],
    }

    with open(out_nodes, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    np.save(out_matrix, tm.M.astype(np.float64))

    # Round-trip verification: load back via the JSON/NPY loaders and confirm identity.
    print(f"[{dataset_id}] verifying round-trip...", file=sys.stderr)
    nodes_rt, ids_rt = load_nodes_json(out_nodes)
    tm_rt = load_time_matrix_npy(out_matrix, ids_rt)
    validate_dataset(nodes_rt, tm_rt)

    if ids_rt != ids_in_order:
        raise RuntimeError("id order mismatch after round-trip")
    for nid in ids_in_order:
        a, b = nodes[nid], nodes_rt[nid]
        if a.name != b.name or a.type != b.type:
            raise RuntimeError(f"node {nid} name/type mismatch after round-trip")
        for fld in ("lat", "lon", "demand_liters", "service_min"):
            if abs(getattr(a, fld) - getattr(b, fld)) > 1e-9:
                raise RuntimeError(f"node {nid}.{fld} numeric mismatch after round-trip")
    if not np.allclose(tm.M, tm_rt.M, atol=1e-9, rtol=0):
        raise RuntimeError("time matrix numeric mismatch after round-trip")

    print(
        f"[{dataset_id}] wrote {out_nodes} and {out_matrix} — round-trip OK",
        file=sys.stderr,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", required=True, help="Dataset id (e.g. dataset_a, dataset_b)")
    ap.add_argument("--nodes", required=True, help="Path to source nodes CSV")
    ap.add_argument("--matrix", required=True, help="Path to source time-matrix CSV (headerless)")
    ap.add_argument(
        "--out",
        default=os.path.join(_REPO_ROOT, "backend", "data"),
        help="Output directory (default: <repo>/backend/data)",
    )
    ap.add_argument(
        "--refill-service-min",
        type=float,
        default=12.0,
        help="Global refill service time in minutes (stored in meta.refill_service_min)",
    )
    args = ap.parse_args()

    try:
        convert(
            dataset_id=args.id,
            nodes_csv=args.nodes,
            matrix_csv=args.matrix,
            out_dir=args.out,
            refill_service_min=args.refill_service_min,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
