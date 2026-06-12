"""FastAPI application — serves API + static frontend from a single process."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.routes.runs import router as runs_router
from backend.ws import router as ws_router

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure output dirs exist
    Path("output/runs").mkdir(parents=True, exist_ok=True)
    Path("output/memory").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="AI Software Factory",
    description="Multi-agent code generation pipeline with live streaming",
    version="2.0.0",
    lifespan=lifespan,
)

# ── CORS (allow Vite dev server in development) ──────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────
app.include_router(runs_router)
app.include_router(ws_router)


# ── Health check ─────────────────────────────────────────────
@app.get("/api/health")
async def health():
    from src.core.sandbox import DOCKER_AVAILABLE
    from src.core.memory import _CHROMADB_AVAILABLE
    return {
        "status": "ok",
        "docker_sandbox": DOCKER_AVAILABLE,
        "vector_memory": _CHROMADB_AVAILABLE,
    }


# ── Serve React frontend (production build) ──────────────────
_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
