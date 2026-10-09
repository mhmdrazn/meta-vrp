from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from typing import List, Tuple

import numpy as np

OSRM_HOST = "https://router.project-osrm.org"
CHUNK = 90  # OSRM demo host caps table requests around 100 coords per side; keep buffer
RETRY_DELAY_SEC = 3.0
MAX_RETRIES = 3


def read_nodes(path: str) -> Tuple[List[str], List[Tuple[float, float]]]:
    """Return (ids_in_order, [(lon, lat), ...]) — OSRM wants lon,lat order."""
    ids: List[str] = []
    coords: List[Tuple[float, float]] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ids.append(str(row["id"]))
            coords.append((float(row["lon"]), float(row["lat"])))
    return ids, coords


def _fetch_table(
    coords: List[Tuple[float, float]], sources: List[int], destinations: List[int]
) -> Tuple[np.ndarray, np.ndarray]:
    """Call OSRM /table for a rectangular sub-block.

    Returns (durations in seconds, distances in meters).
    """
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
    src_str = ";".join(str(i) for i in sources)
    dst_str = ";".join(str(i) for i in destinations)
    url = (
        f"{OSRM_HOST}/table/v1/driving/{coord_str}"
        f"?sources={src_str}&destinations={dst_str}&annotations=duration,distance"
    )

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("code") != "Ok":
                raise RuntimeError(f"OSRM error: {payload}")
            # OSRM returns nulls for unreachable pairs — treat as np.inf so we can spot them.
            dur = np.array(payload["durations"], dtype=float)  # seconds
            dist = np.array(payload["distances"], dtype=float)  # meters
            dur = np.where(np.isnan(dur), np.inf, dur)
            dist = np.where(np.isnan(dist), np.inf, dist)
            return dur, dist
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            last_exc = e
            print(f"  [retry {attempt}/{MAX_RETRIES}] {e}", file=sys.stderr)
            time.sleep(RETRY_DELAY_SEC * attempt)
    raise RuntimeError(f"OSRM /table failed after {MAX_RETRIES} attempts: {last_exc}")


def build_matrix(
    coords: List[Tuple[float, float]], chunk: int = CHUNK
) -> Tuple[np.ndarray, np.ndarray]:
    """Assemble the full NxN duration (s) and distance (m) matrices by tiling OSRM /table calls."""
    n = len(coords)
    matrix_sec = np.zeros((n, n), dtype=float)
    matrix_m = np.zeros((n, n), dtype=float)
    total_blocks = ((n + chunk - 1) // chunk) ** 2
    block_no = 0
    for i0 in range(0, n, chunk):
        i1 = min(i0 + chunk, n)
        sources = list(range(i0, i1))
        for j0 in range(0, n, chunk):
            j1 = min(j0 + chunk, n)
            destinations = list(range(j0, j1))
            block_no += 1
            print(
                f"[{block_no}/{total_blocks}] rows {i0}:{i1} x cols {j0}:{j1}",
                file=sys.stderr,
            )
            # OSRM /table wants ALL coords in the URL (sources+destinations point INTO
            # that shared list by index). Cheaper to just send the whole list every time
            # for our small N (<= a couple hundred).
            block_sec, block_m = _fetch_table(coords, sources, destinations)
            matrix_sec[i0:i1, j0:j1] = block_sec
            matrix_m[i0:i1, j0:j1] = block_m
            # Be a good citizen of the public demo host.
            time.sleep(0.4)
    return matrix_sec, matrix_m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nodes", required=True, help="Path to nodes CSV")
    ap.add_argument("--out", required=True, help="Output path for the time-matrix CSV (minutes)")
    ap.add_argument(
        "--distance-out",
        default=None,
        help="Optional output path (.npy) for the road-distance matrix in kilometers",
    )
    ap.add_argument("--chunk", type=int, default=CHUNK, help="OSRM per-request coordinate chunk size")
    args = ap.parse_args()

    ids, coords = read_nodes(args.nodes)
    print(f"Loaded {len(ids)} nodes from {args.nodes}", file=sys.stderr)

    matrix_sec, matrix_m = build_matrix(coords, chunk=args.chunk)

    if np.isinf(matrix_sec).any():
        n_bad = int(np.isinf(matrix_sec).sum())
        print(
            f"WARNING: {n_bad} unreachable pairs (OSRM returned null). "
            "Replacing with a large finite fallback so the solver doesn't crash on inf.",
            file=sys.stderr,
        )
        finite_max = matrix_sec[np.isfinite(matrix_sec)].max()
        fallback = finite_max * 10.0
        matrix_sec = np.where(np.isinf(matrix_sec), fallback, matrix_sec)

    if args.distance_out:
        if np.isinf(matrix_m).any():
            finite_max_m = matrix_m[np.isfinite(matrix_m)].max()
            matrix_m = np.where(np.isinf(matrix_m), finite_max_m * 10.0, matrix_m)
        np.save(args.distance_out, matrix_m / 1000.0)
        print(
            f"Wrote {matrix_m.shape[0]}x{matrix_m.shape[1]} distance matrix (km) to {args.distance_out}",
            file=sys.stderr,
        )

    # Convert seconds -> minutes to match the Dataset A convention.
    matrix_min = matrix_sec / 60.0

    # Write headerless CSV (same layout as existing data/time_matrix_a.csv).
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for row in matrix_min:
            writer.writerow([f"{v:.6g}" for v in row])
    print(
        f"Wrote {matrix_min.shape[0]}x{matrix_min.shape[1]} matrix (minutes) to {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
