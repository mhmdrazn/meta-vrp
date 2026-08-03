# app.py — FastAPI application entry.
#
# Two modes, controlled by settings.DEMO_MODE (env var DEMO_MODE, default "1"):
#   DEMO_MODE=1 (public reviewer demo, TASK 3): only the stateless /optimize, /nodes,
#     /datasets, /health endpoints; NO database calls anywhere in the reachable import
#     graph, so the app boots cleanly without DATABASE_URL set.
#   DEMO_MODE=0 (operational stack): additionally mounts the DB-backed operational
#     routers (catalog / groups / assign / status / history) and the /optimize
#     endpoint persists jobs to Supabase.
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers.optimize import router as optimize_router
from .settings import settings

app = FastAPI(
    title="Meta-VRP API",
    version="0.2",
    docs_url="/docs",
    swagger_ui_parameters={"displayRequestDuration": True, "tryItOutEnabled": True},
)

_cors_origins_env = os.getenv("CORS_ORIGINS", "*")
_cors_origins = (
    ["*"]
    if _cors_origins_env.strip() == "*"
    else [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("meta-vrp")


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "message": "FastAPI backend running",
        "demo_mode": settings.DEMO_MODE,
    }


# Stateless demo router is ALWAYS mounted — it works in both modes.
app.include_router(optimize_router)


# --- Operational-mode-only routers (require Supabase / DATABASE_URL) -----------------
# Import + include are gated behind DEMO_MODE so that backend.database (which raises
# at import time if DATABASE_URL is unset) is never even touched in demo deployments.
if not settings.DEMO_MODE:
    from .routers import (  # noqa: E402  (deferred import is intentional)
        routes_assign,
        routes_catalog,
        routes_groups,
        routes_history,
        routes_status,
    )

    app.include_router(routes_groups.router)
    app.include_router(routes_catalog.router)
    app.include_router(routes_assign.router)
    app.include_router(routes_status.router)
    app.include_router(routes_history.router)
    log.info("Operational mode: DB-backed routers mounted.")
else:
    log.info("Demo mode: DB-backed routers NOT mounted; only stateless endpoints active.")
