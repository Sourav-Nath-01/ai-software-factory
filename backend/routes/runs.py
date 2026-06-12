"""REST routes — start runs, list history, get results, download ZIP."""
from __future__ import annotations

import io
import zipfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.models import RunRequest, RunResult, RunSummary, RunStatus, RunMetrics, CodeFileResponse
from backend.store.run_store import run_store
from backend.ws import active_queues

router = APIRouter(prefix="/api", tags=["runs"])


# ── Start a run ──────────────────────────────────────────────

@router.post("/run", response_model=dict)
async def start_run(req: RunRequest, background_tasks: BackgroundTasks):
    """Start a pipeline run and return its run_id immediately."""
    import queue as q_module
    import threading
    from src.core.config import settings

    is_demo = not req.api_key or len(req.api_key) < 10

    run_id = run_store.create_run(
        prompt=req.prompt,
        model="Demo Mode" if is_demo else req.model,
    )

    # Per-run event queue (WebSocket consumer reads from this)
    event_queue: q_module.Queue = q_module.Queue()
    active_queues[run_id] = event_queue

    def _run_pipeline():
        import os
        try:
            settings.max_review_iterations  = req.max_review_iterations
            settings.max_test_fix_iterations = req.max_test_fix_iterations

            if not is_demo:
                key      = (req.api_key or "").strip()
                model    = req.model
                provider = req.provider.value if req.provider else "openai"

                # Set the correct env var so litellm picks it up automatically
                if provider == "gemini":
                    os.environ["GEMINI_API_KEY"] = key
                    os.environ["GOOGLE_API_KEY"] = key
                elif provider == "groq":
                    os.environ["GROQ_API_KEY"] = key
                else:
                    os.environ["OPENAI_API_KEY"] = key
                    settings.openai_api_key = key
            else:
                key   = ""
                model = "demo"

            from src.core.pipeline import Pipeline

            # Capture real metrics from the pipeline's 'complete' event
            captured_metrics: dict = {}

            def on_event(event: dict):
                nonlocal captured_metrics
                if event.get("type") == "complete" and "metrics" in event:
                    captured_metrics = event["metrics"]
                event_queue.put(event)

            pipeline = Pipeline(
                model=model,
                api_key=key,
                demo=is_demo,
                event_callback=on_event,
            )
            state = pipeline.run(req.prompt)

            files = [
                {"file_path": f.file_path, "content": f.content, "language": f.language}
                for f in state.codebase.files
            ] if state.codebase else []

            # Fallback: derive metrics from final state if event was missed
            if not captured_metrics and state.codebase:
                loc = sum(f.content.count("\n") + 1 for f in state.codebase.files)
                captured_metrics = {
                    "files_generated": len(state.codebase.files),
                    "lines_of_code": loc,
                    "review_iterations": state.review_iterations,
                    "test_fix_iterations": state.test_fix_iterations,
                    "issues_found": 0,
                    "issues_fixed": 0,
                    "tests_passed": bool(state.test_result and state.test_result.passed),
                    "duration_seconds": 0.0,
                }

            run_store.update_complete(run_id, metrics=captured_metrics, files=files)

        except Exception as e:
            run_store.update_failed(run_id, str(e))
        finally:
            event_queue.put(None)


    thread = threading.Thread(target=_run_pipeline, daemon=True)
    thread.start()

    return {"run_id": run_id, "status": "running"}


# ── List runs ────────────────────────────────────────────────

@router.get("/runs", response_model=list[RunSummary])
async def list_runs(limit: int = Query(20, ge=1, le=100)):
    runs = run_store.list_runs(limit=limit)
    result = []
    for r in runs:
        metrics = RunMetrics(**r["metrics"]) if r.get("metrics") else None
        result.append(RunSummary(
            run_id=r["run_id"],
            prompt=r["prompt"],
            status=RunStatus(r["status"]),
            model=r["model"],
            created_at=r["created_at"],
            metrics=metrics,
        ))
    return result


# ── Get single run ───────────────────────────────────────────

@router.get("/runs/{run_id}", response_model=RunResult)
async def get_run(run_id: str):
    data = run_store.get_run(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="Run not found")

    metrics = RunMetrics(**data["metrics"]) if data.get("metrics") else None
    files = [CodeFileResponse(**f) for f in data.get("files", [])]
    return RunResult(
        run_id=data["run_id"],
        prompt=data["prompt"],
        status=RunStatus(data["status"]),
        model=data["model"],
        created_at=data["created_at"],
        metrics=metrics,
        files=files,
        error=data.get("error"),
    )


# ── Download ZIP ─────────────────────────────────────────────

@router.get("/runs/{run_id}/download")
async def download_run(run_id: str):
    data = run_store.get_run(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="Run not found")
    if data["status"] != "complete":
        raise HTTPException(status_code=400, detail="Run is not complete yet")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in data.get("files", []):
            zf.writestr(f["file_path"], f["content"])

    buf.seek(0)
    project_name = data["prompt"][:30].replace(" ", "_").lower()
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_name}_{run_id}.zip"'},
    )


# ── Stats (for landing page) ─────────────────────────────────

@router.get("/stats")
async def get_stats():
    runs = run_store.list_runs(limit=100)
    total = len(runs)
    complete = sum(1 for r in runs if r["status"] == "complete")
    success_rate = round((complete / total * 100) if total > 0 else 0)
    avg_files = 0
    if complete > 0:
        total_files = sum(
            r["metrics"]["files_generated"]
            for r in runs
            if r["status"] == "complete" and r.get("metrics")
        )
        avg_files = round(total_files / complete)
    return {
        "total_runs": total,
        "successful_runs": complete,
        "success_rate": success_rate,
        "avg_files_generated": avg_files,
    }
