# Offline Experiments — Park-Watering VRP

Three experiments matching paper-revision spec §6/§7/§8. Each is available as **both a
standalone CLI script** (recommended for headless / batch reproduction — one command per
spec) and a Jupyter notebook (for interactive exploration).

| Experiment | Script (CLI) | Notebook | Output CSV |
|---|---|---|---|
| Baseline: 3 algorithms × 2 datasets × 20 seeds | `python -m experiments.baseline` | `01_baseline_comparison.ipynb` | `results/baseline_dataset_{a,b}.csv` |
| Scenario 1: vehicle availability (Hybrid ALNS, 3 tiers × ≥10 seeds) | `python -m experiments.scenario1` | `02_scenario_vehicle_availability.ipynb` | `results/scenario1_dataset_{a,b}.csv` |
| Scenario 2: refill availability (Hybrid ALNS, 100/50/25%, 5 subsets × ≥5 seeds) | `python -m experiments.scenario2` | `03_scenario_refill_availability.ipynb` | `results/scenario2_dataset_{a,b}.csv` + `results/refill_subsets.json` |

### Common flags (all three scripts)

```bash
python -m experiments.baseline --time-limit 30 --seeds 20
python -m experiments.scenario1 --time-limit 30 --seeds 10 --include-severe2
python -m experiments.scenario2 --time-limit 30 --seeds 5
```

All three accept `--datasets dataset_a dataset_b` to restrict to a subset, and `--out-dir`
to redirect output. Scripts skip any dataset whose static files are missing.

All three notebooks call `experiments.common.run_one()`, which is the **single**
function that invokes the solver. This mechanically enforces the paper-revision
requirement that every offline experiment uses "the same optimisation functions,
constraints, objective function, datasets, and time matrices" (spec §1) — there is
no separate experiment-side reimplementation.

## Running inside this repo

```bash
# From repo root
pip install -r backend/requirements.txt   # or experiments/requirements.txt for the minimal set
jupyter notebook experiments/
```

`FULL_FLEET`, `VEHICLE_CAPACITY_LITERS`, `DEPOT_ID`, `ALLOW_REFILL` live in
`experiments/common.py` — edit there, not inside each notebook.

## Running outside this repo (share with a collaborator)

The engine layer (`backend/engine/`) is deliberately dependency-free (numpy + pandas
+ stdlib only — see the portability check at the top of `backend/engine/*` files).
That's what makes it copyable.

Copy exactly this file tree into a fresh directory:

```
your_new_dir/
├── backend/
│   ├── __init__.py                   # empty file — keeps `backend` importable as a package
│   ├── data/                         # dataset artifacts (JSON + NPY)
│   │   ├── dataset_a.json
│   │   ├── dataset_a_time_matrix.npy
│   │   ├── dataset_b.json
│   │   └── dataset_b_time_matrix.npy
│   └── engine/                       # entire folder, unmodified
├── experiments/                      # this whole folder
└── requirements.txt                  # copy from experiments/requirements.txt
```

Then:

```bash
pip install -r requirements.txt
jupyter notebook experiments/
```

Because you're copying the same engine code the web app uses, any bug fix or
improvement lands identically on both sides — no forking, no drift.

## What each notebook writes

- Six CSV files under `results/` — one per (experiment × dataset) combination.
- For Scenario 2: `results/refill_subsets.json` records the exact refill station IDs
  chosen for each (dataset, availability level, subset index), keyed by a
  deterministic seed so the subsets are reproducible.

## Notes

- Time budgets: the notebooks default to `TIME_LIMIT_SEC = 30` (the manuscript
  configuration). The public Vercel demo uses a much shorter budget (5–15 s) —
  don't confuse the two.
- Every run is saved (per spec §6), including runs that turn out not to be the best.
- Seeds are `range(N)` — deterministic, so re-running should reproduce the numbers
  bit-for-bit (subject to the underlying solver's inherent determinism, which is
  seeded via `utils.set_seed`).
