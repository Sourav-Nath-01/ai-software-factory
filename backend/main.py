"""FastAPI application — serves API + static frontend from a single process."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.routes.runs import router as runs_router
from backend.routes.hitl import router as hitl_router
from backend.ws import router as ws_router

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure output dirs exist (OUTPUT_DIR, default ./output)
    from src.core.config import settings
    (settings.output_path / "runs").mkdir(parents=True, exist_ok=True)
    # Mark any 'running' runs from a previous crashed/killed server as failed
    from backend.store.run_store import run_store
    cleaned = run_store.cleanup_stale_runs()
    if cleaned:
        import logging
        logging.getLogger("uvicorn").info(
            f"Startup cleanup: marked {cleaned} stale 'running' run(s) as failed."
        )
    yield


app = FastAPI(
    title="AI Software Factory",
    description="Multi-agent code generation pipeline with live streaming",
    version="2.0.0",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────
# The frontend is same-origin in production and proxied by Vite in dev, so
# cross-origin access is only for explicitly listed origins. (A "*" wildcard
# would let any website drive this API from a visitor's browser.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# ── Routers ──────────────────────────────────────────────────
app.include_router(runs_router)
app.include_router(hitl_router)
app.include_router(ws_router)


# ── Health check ─────────────────────────────────────────────
@app.get("/api/health")
async def health():
    from src.core.sandbox import DOCKER_AVAILABLE
    from src.core.memory import _CHROMADB_AVAILABLE
    from src.core.static_analysis import BANDIT_AVAILABLE, FLAKE8_AVAILABLE, PIP_AUDIT_AVAILABLE
    return {
        "status": "ok",
        "docker_sandbox": DOCKER_AVAILABLE,
        "vector_memory": _CHROMADB_AVAILABLE,
        "static_analysis": {
            "bandit":    BANDIT_AVAILABLE,
            "flake8":    FLAKE8_AVAILABLE,
            "pip_audit": PIP_AUDIT_AVAILABLE,
        },
    }


# ── Serve React frontend (production build) ──────────────────
_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
