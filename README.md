# Meta-VRP: Park Watering Route Optimization

Meta-VRP is a decision-support system that plans daily watering routes for a fleet of water-tanker trucks serving urban parks. Given a depot, a set of parks with water demand, and a network of refill stations, it assigns parks to trucks and sequences each route so that all demand is served within the operating window.

The routing engine combines **Ant Colony Optimization (ACO)** and **Adaptive Large Neighborhood Search (ALNS)** and works on real road-network travel times from OSRM. A React web application lets operators choose a planning budget, run the optimizer, and inspect the resulting routes on an interactive map.

---

## Table of Contents

- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Datasets and Data Pipeline](#datasets-and-data-pipeline)
- [Experiments and Reproducibility](#experiments-and-reproducibility)
- [Deployment](#deployment)
- [Development Workflow](#development-workflow)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgements](#acknowledgements)

---

## Key Features

**Optimization engine**

- Three metaheuristics behind one interface: ACO, standard ALNS, and a Hybrid ALNS + ACO (the default in the web application).
- Capacity-aware planning with automatic refill-station visits, split deliveries for parks whose demand exceeds one tank, and an operating-window constraint.
- Time-budgeted search: the solver keeps improving for exactly the requested wall-clock budget, so a longer budget buys more search effort.
- Real-world travel times from precomputed OSRM time matrices, not straight-line distances.
- Reproducible runs through an explicit random seed.

**Web application**

- Interactive Leaflet map with demand-coded parks, depot, refill stations, and per-vehicle route colors.
- **Planning modes**: Rapid (5 s), Standard (10 s), and Extended (20 s) computation budgets.
- Configurable fleet size (3 to 10 trucks) and a choice of service area (dataset).
- Decision-oriented result summary: makespan, total fleet operating time, vehicles assigned, feasibility, workload balance, refill visits, and planning time.
- Route details table with per-vehicle show/hide controls, so routes can be inspected one at a time or in any combination.
- Live progress for each stage (optimization, road-geometry generation, visualization) with measured timings.
- PDF report export with one page per vehicle route.
- Light and dark themes, responsive layout.

**Research support**

- Pre-computed experiment results (algorithm baseline, vehicle availability, refill availability) served through the API and visualized in the application.
- Notebooks for re-running every experiment, including runtime-budget comparisons across matched seeds.

---

## How It Works

### Problem definition

The problem is a capacitated vehicle routing problem with split deliveries, intermediate refills, and a shared time window:

| Element | Description |
| --- | --- |
| Depot | Single start and end point for every truck. |
| Parks | Customers with a water demand in liters. Demand larger than a tank is split across visits. |
| Refill stations | Locations where an empty truck refills (5 min service). Trucks start with an initial fill at the nearest station. |
| Trucks | Homogeneous fleet, 5,000 L tank capacity. |
| Operating window | 540 minutes (06:00 to 15:00), including the return to the depot. |
| Service time | 20 min for a full 5,000 L delivery, scaled proportionally for partial deliveries. |

### Objective

The search minimizes a penalized objective that favors short, balanced, feasible plans:

```text
fitness = total_time
        + 2.0 * refill_visits
        + 0.1 * std(route_times)
        + penalty * (unserved demand + time-window violations + empty trucks)
```

Hard-constraint violations carry a very large penalty (10^6), so any feasible plan outranks any infeasible one.

### Algorithms

| Algorithm | Summary |
| --- | --- |
| **ACO** | Ants construct complete delivery plans guided by pheromone trails and travel-time heuristics. Trails evaporate each iteration and are reinforced by the iteration-best and global-best plans. |
| **ALNS** | Starts from a greedy construction, then repeatedly destroys part of the plan (random, worst, related removal) and repairs it (greedy or regret insertion). Operator weights adapt to their success, and simulated annealing decides acceptance. |
| **Hybrid ALNS + ACO** | The ALNS loop with an additional pheromone-guided repair operator. Pheromone is seeded from the initial plan and reinforced whenever a new best solution is found, so the search learns which edges tend to appear in good plans. |

All three share the same evaluation function, which keeps comparisons fair.

### Planning modes

The planning mode is a **computation budget**, not a quality label. The solver loops until the budget is spent and returns the best plan found.

| Mode | Budget | Intended use |
| --- | --- | --- |
| Rapid | 5 s | Fast response, quick what-if checks. |
| Standard | 10 s | Balanced search. |
| Extended | 20 s | Longer search for higher-quality plans. |

Total wait time is the budget plus a small overhead for data loading, post-processing, and network transfer.

### Result metrics

| Metric | Meaning |
| --- | --- |
| Makespan | Completion time of the longest route, that is, when the last truck is back at the depot. |
| Total fleet operating time | Sum of all active route durations. |
| Vehicles assigned | Active trucks out of the fleet size requested. |
| Operationally feasible | Whether every park is fully served within the operating window. |
| Workload Std Dev | Standard deviation of route durations. Lower values mean a more even workload across trucks. |
| Refill visits | Total refill-station stops across all routes. |
| Planning time | Measured computation time of the solver. |

---

## Architecture

```text
┌──────────────────────────┐        ┌───────────────────────────────┐
│  React + Vite frontend   │  HTTP  │  FastAPI backend              │
│  - Leaflet map           │ ─────▶ │  - /optimize  (stateless)     │
│  - Planning controls     │ ◀───── │  - /nodes, /datasets          │
│  - Results and reports   │  JSON  │  - /experiments               │
└────────────┬─────────────┘        └───────────────┬───────────────┘
             │                                      │
             │ road geometry                        │ loads
             ▼                                      ▼
   ┌────────────────────┐              ┌───────────────────────────┐
   │  OSRM (public API) │              │  Dataset JSON + NPY       │
   └────────────────────┘              │  (nodes, time matrices)   │
                                       └───────────────────────────┘
```

**Request flow.** The browser sends the selected parks, fleet size, dataset, and time budget to `POST /optimize`. The backend loads the dataset and its time matrix, builds an initial solution, runs the chosen algorithm for the requested budget, and returns routes with their metrics. The browser then requests road geometry from OSRM to draw the routes on the map.

**Deployment modes.** The backend runs in one of two modes, controlled by `DEMO_MODE`:

| Mode | Behavior |
| --- | --- |
| `DEMO_MODE=1` (default) | Stateless public demo. Serves only `/optimize`, `/nodes`, `/datasets`, `/experiments`, and `/health`. No database is required or touched. |
| `DEMO_MODE=0` | Operational stack. Additionally mounts database-backed routers (catalog, groups, assignment, field status, history) and requires `DATABASE_URL`. |

The web application targets the demo mode. Operational pages exist in the router but are not linked in the current interface.

---

## Technology Stack

| Layer | Technologies |
| --- | --- |
| Frontend | React 19, TypeScript 5.9, Vite 7, Tailwind CSS 3, shadcn/ui (Radix UI), Framer Motion, React Router 7 |
| State and data | Zustand, TanStack Query, Axios |
| Mapping | Leaflet, React Leaflet, OSRM |
| Reporting | jsPDF, html2canvas |
| Backend | Python 3.10+, FastAPI, Pydantic, NumPy, pandas, Uvicorn |
| Persistence (operational mode only) | PostgreSQL (Supabase recommended), SQLAlchemy |
| Quality tooling | Black, Ruff, ESLint, Prettier, pre-commit, GitHub Actions |
| Hosting | Vercel (static frontend and Python serverless function) |

---

## Project Structure

```text
meta-vrp/
├── api/
│   └── index.py              # Vercel serverless entry; mounts the FastAPI app under /api
├── backend/
│   ├── app.py                # FastAPI application and mode switching
│   ├── settings.py           # Environment-driven configuration
│   ├── schemas.py            # Request and response models
│   ├── engine/               # Optimization core
│   │   ├── solve.py          # Unified solve() entry point for all algorithms
│   │   ├── algorithms/       # aco.py, alns.py, hybrid.py
│   │   ├── construct.py      # Greedy initial-solution construction
│   │   ├── evaluation.py     # Route timing and feasibility evaluation
│   │   ├── objective.py      # Shared search objective
│   │   ├── io_utils.py       # Dataset registry and loading
│   │   └── ...
│   ├── routers/              # optimize.py (demo) plus operational routers
│   ├── migrations/           # SQL for operational mode
│   └── data/                 # Dataset JSON/NPY and pre-computed experiment results
├── data/                     # Source CSV datasets and time matrices
├── frontend/
│   └── src/
│       ├── pages/            # OptimizePage, ResultsPage, and operational pages
│       ├── components/       # Maps, legend, and UI primitives (shadcn/ui)
│       ├── stores/           # Zustand stores
│       ├── lib/              # API client, formatting, color utilities
│       └── layouts/          # Application shell
├── scripts/                  # Dataset conversion and time-matrix generation
├── requirements.txt          # Production (demo) Python dependencies
├── vercel.json               # Build, routing, and function configuration
└── .pre-commit-config.yaml   # Local quality hooks
```

---

## Getting Started

### Prerequisites

- Python 3.10 or newer
- Node.js 18 or newer
- Git

### 1. Clone the repository

```bash
git clone https://github.com/mhmdrazn/meta-vrp.git
cd meta-vrp
```

### 2. Start the backend

Run all backend commands from the **repository root**, because the application is imported as `backend.app`.

```bash
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn backend.app:app --reload --port 8000
```

The API is now available at `http://localhost:8000`, with interactive documentation at `http://localhost:8000/docs`.

### 3. Start the frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. In development, Vite proxies `/api` to the backend at `http://127.0.0.1:8000`, so no frontend environment file is needed.

### 4. Run your first plan

1. Choose a **Study Area** and the **Number of Trucks**.
2. Select a **Planning Mode** (5, 10, or 20 seconds).
3. Click **Run Route Planning** and review the summary, map, and route details.

### Operational mode (optional)

Operational mode adds database-backed features and needs PostgreSQL.

```bash
pip install -r backend/requirements.txt

# Create the schema (run in order). Use a plain PostgreSQL URI such as
# postgresql://user:password@host:5432/dbname (without the "+psycopg2" driver suffix).
psql "<postgres-uri>" -f backend/migrations/001_init_schema.sql
psql "<postgres-uri>" -f backend/migrations/schema_additions.sql

# Set DATABASE_URL in backend/.env (see Configuration), then start with:
DEMO_MODE=0 python -m uvicorn backend.app:app --reload --port 8000
```

On PowerShell, set the variable first: `$env:DEMO_MODE = "0"`. Both migration files use `IF NOT EXISTS` and are safe to re-run. With Supabase, use the session pooler (port 5432) for local development and the transaction pooler (port 6543) for serverless deployments; the backend detects pooler hosts automatically.

---

## Configuration

### Backend environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `DEMO_MODE` | `1` | `1` for the stateless demo, `0` for the operational stack. |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed origins. |
| `DATABASE_URL` | none | PostgreSQL connection string. Required only when `DEMO_MODE=0`. See `backend/.env.example`. |

Solver defaults (time limit, vehicle capacity, depot, refill service time, penalties) are defined in [`backend/settings.py`](backend/settings.py). The default time limit applies only when a request does not specify `time_limit_sec`.

### Frontend environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `VITE_API_BASE_URL` | empty (`/api`) | Backend base URL. Set it only when frontend and backend are deployed on separate domains. |

---

## API Reference

All paths below are relative to the backend root. When deployed on Vercel, prefix them with `/api`.

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health` | Service status and active mode. |
| `GET` | `/datasets` | Available datasets with node, park, and refill counts. |
| `GET` | `/nodes?dataset_id=` | Depot, parks, and refill stations of a dataset. |
| `POST` | `/optimize` | Run the optimizer and return routes and metrics. Stateless; nothing is stored. |
| `GET` | `/experiments/{type}` | Pre-computed results. `type` is `baseline`, `scenario1`, or `scenario2`. Optional `dataset_id` filter. |
| `GET` | `/experiments/{type}/assets` | List of available figures and route maps for an experiment. |
| `GET` | `/experiment-assets/...` | Static figures and interactive route maps. |

### `POST /optimize`

Request:

```json
{
  "dataset_id": "dataset_a",
  "selected_node_ids": ["1", "2", "3"],
  "num_vehicles": 7,
  "algorithm": "alns_hybrid",
  "time_limit_sec": 10,
  "seed": 42
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `dataset_id` | string | `dataset_a` or `dataset_b`. Default `dataset_a`. |
| `selected_node_ids` | string[] | Park IDs to serve. Required, at least one. |
| `num_vehicles` | integer | Fleet size, at least 1. |
| `algorithm` | string | `aco`, `alns_standard`, or `alns_hybrid` (default). |
| `time_limit_sec` | number | Solver budget in seconds. Falls back to the server default. |
| `seed` | integer | Random seed for reproducibility. Default 42. |
| `refill_ids_override` | string[] | Restrict usable refill stations. Omit for all stations. |

Response (abridged):

```json
{
  "fitness": 2768.17,
  "makespan": 427.1,
  "total_time": 2617.04,
  "route_time_std": 13.4,
  "active_vehicles": 7,
  "refill_visits": 73,
  "computation_time": 10.02,
  "feasible": true,
  "algorithm": "alns_hybrid",
  "routes": [
    {
      "vehicle_id": 0,
      "sequence": ["0", "12", "45", "0"],
      "total_time_min": 389.5,
      "load_profile_liters": [5000, 3200, 0]
    }
  ]
}
```

---

## Datasets and Data Pipeline

Two service areas in Surabaya, Indonesia, are included. They appear in the interface as **Service Area A** and **Service Area B**.

| Service area | Dataset ID | Parks | Refill stations | Notes |
| --- | --- | --- | --- | --- |
| Service Area A | `dataset_a` | 46 | 41 | Baseline fleet of 10 trucks in experiments. |
| Service Area B | `dataset_b` | 51 | 25 | Baseline fleet of 5 trucks in experiments. |

Each dataset consists of a node file (`id`, `name`, `lat`, `lon`, `type`, `demand_liters`, `service_min`) and a pairwise travel-time matrix in minutes. Runtime files live in `backend/data/` as JSON plus NumPy arrays; the source CSVs live in `data/`.

To add or rebuild a dataset:

```bash
# Build a travel-time matrix from node coordinates using OSRM
python scripts/build_time_matrix.py --help

# Convert CSV nodes and matrices into the runtime JSON + NPY format (validates the result)
python scripts/convert_dataset.py --help
```

New datasets are registered in [`backend/engine/io_utils.py`](backend/engine/io_utils.py). Please respect the usage policy of the public OSRM demo server when generating matrices.

---

## Experiments and Reproducibility

The study behind this project evaluates the three algorithms under several conditions. Pre-computed results are stored in `backend/data/experiments/`, served by the `/experiments` endpoints, and displayed on the **Results** page, available at the `/results` route.

| Experiment | Question |
| --- | --- |
| Baseline comparison | How do ACO, ALNS, and Hybrid compare with full fleet and full refill availability? |
| Vehicle availability | How does performance change as the available fleet shrinks? |
| Refill availability | How does performance change when only 50% or 25% of refill stations are usable? |
| Runtime budget | How do results change with 5 s, 10 s, and 20 s budgets? |

Experiment protocol:

- 20 independent runs per configuration with matched seeds (`7, 14, 21, ..., 140`), so algorithms are compared on identical starting conditions.
- Metrics per run: fitness, total time, makespan, route-time standard deviation, active vehicles, refill visits, computation time, and feasibility.
- The experiment notebooks live in a local `experiments/` directory that is excluded from version control. Their results are exported to CSV in `backend/data/experiments/`.

> The public web application uses a shortened demonstration configuration. Results reported in the study were generated through controlled offline experiments.

---

## Deployment

The repository deploys to **Vercel as a single project**: the frontend is built to static files, and the FastAPI app runs as a Python serverless function.

How it is wired (see [`vercel.json`](vercel.json)):

- `buildCommand` installs and builds the frontend into `frontend/dist`.
- Requests to `/api/*` are rewritten to [`api/index.py`](api/index.py), which mounts the backend under `/api`.
- All other paths fall back to `index.html` for client-side routing.
- The function has `maxDuration` set to 60 seconds, which covers the longest planning mode plus overhead.

Setup steps:

1. Import the repository as a new Vercel project.
2. Set **Framework Preset** to `Other` and keep **Root Directory** at the repository root.
3. Leave build, output, and install commands on their defaults; `vercel.json` configures them.
4. Deploy. The frontend is served at the project root and the API at `/api`.

Production installs use the root [`requirements.txt`](requirements.txt), which intentionally excludes database packages. Because the frontend and backend share one origin, neither CORS settings nor `VITE_API_BASE_URL` are required. Confirm that your Vercel plan permits the configured function duration, and lower the time limit if it does not.

---

## Development Workflow

### Quality checks

```bash
# Frontend
cd frontend
npm run lint
npm run format
npx tsc --noEmit

# Backend (from the repository root)
black backend --check
ruff check backend

# Everything
pre-commit run --all-files
```

Install the hooks once to run these checks on every commit:

```bash
pip install pre-commit
pre-commit install
```

### Continuous integration

GitHub Actions (`ci_quality.yml`) runs on every push and pull request to `main` and `dev`:

- Frontend: Prettier check, ESLint, and TypeScript type check.
- Backend: Black formatting check and Ruff linting.

### Conventions

- Backend code follows Black (88 columns) and Ruff with isort rules; the frontend follows Prettier and ESLint.
- Commit messages use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`).

---

## Troubleshooting

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'backend'` | Uvicorn was started from inside `backend/`. | Run it from the repository root: `python -m uvicorn backend.app:app --reload`. |
| `ERR_CONNECTION_REFUSED` on `/api/optimize` | Backend or Vite dev server is not running. | Start both servers; the frontend needs the backend on port 8000. |
| Port 8000 or 5173 already in use | Another process holds the port. | Stop it, or start on another port (`--port 8001`; `npm run dev -- --port 5174`) and update the Vite proxy target. |
| Route geometry loads slowly or as straight lines | The public OSRM demo server is rate limited or unreachable. | Retry later or host your own OSRM instance. Unreachable segments fall back to straight lines. |
| Plan is marked not feasible with few trucks | Total workload does not fit the 540-minute window at that fleet size. | Increase the number of trucks. |
| `DATABASE_URL` error on startup | `DEMO_MODE=0` without a database. | Set `DATABASE_URL`, or use `DEMO_MODE=1`. |
| `npm install` fails | Unsupported Node version. | Use Node.js 18 or newer. |

---

## Contributing

Contributions are welcome.

1. Fork the repository and create a feature branch: `git checkout -b feat/short-description`.
2. Install the pre-commit hooks (`pre-commit install`).
3. Make your changes with clear, focused commits.
4. Make sure the quality checks above pass locally.
5. Open a pull request against `main` describing the change and how you verified it.

Please keep documentation up to date when you change behavior, configuration, or the API.

---

## License

No license file is currently included in this repository, so all rights are reserved by the authors. Please contact the maintainers before reusing the code or data.

---

## Acknowledgements

- [OSRM](https://project-osrm.org/) for road-network routing.
- [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors for map data and tiles.
- [Leaflet](https://leafletjs.com/), [shadcn/ui](https://ui.shadcn.com/), and the FastAPI and React communities.

Repository: [github.com/mhmdrazn/meta-vrp](https://github.com/mhmdrazn/meta-vrp) · Issues: [GitHub Issues](https://github.com/mhmdrazn/meta-vrp/issues)
