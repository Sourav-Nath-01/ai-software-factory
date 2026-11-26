"""REST routes — start runs, list history, get results, download ZIP."""
from __future__ import annotations

import io
import threading
import zipfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.models import RunRequest, RunResult, RunSummary, RunStatus, RunMetrics, CodeFileResponse, DiffResponse, FileDiff
from backend.store.run_store import run_store
from backend.ws import active_queues
from src.core.config import settings

router = APIRouter(prefix="/api", tags=["runs"])

# Each run holds a thread, LLM calls and possibly a sandbox container. On a small
# server, extra runs are refused with HTTP 429 instead of exhausting memory.
_run_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)


def _safe_zip_name(path: str) -> str | None:
    """Normalise an LLM-chosen file path for a ZIP entry, or None if unsafe.

    Prevents "zip slip": entries like "../../.bashrc" or "/etc/x" would write
    outside the folder when the user extracts the archive.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return None
    return "/".join(parts)


# ── Start a run ──────────────────────────────────────────────

@router.post("/run", response_model=dict)
async def start_run(req: RunRequest, background_tasks: BackgroundTasks):
    """Start a pipeline run and return its run_id immediately."""
    import queue as q_module
    import threading
    from backend.models import Provider

    # Demo mode only when asked for — a missing key must not silently produce mock output
    is_demo = req.provider == Provider.DEMO
    if not is_demo and not (req.api_key or "").strip():
        raise HTTPException(status_code=400, detail=f"An API key is required for provider '{req.provider.value}'.")
    # Taken before the run record exists, so a refused request never shows up as "running"
    if not _run_slots.acquire(blocking=False):
        raise HTTPException(status_code=429,
                            detail="The server is busy with other runs. Please try again in a few minutes.")

    run_id = run_store.create_run(
        prompt=req.prompt,
        model="Demo Mode" if is_demo else req.model,
    )

    # Per-run event queue (WebSocket consumer reads from this)
    event_queue: q_module.Queue = q_module.Queue()
    active_queues[run_id] = event_queue

    def _run_pipeline():
        import threading as _threading
        try:
            # The key and loop limits are passed to this run's Pipeline only —
            # never written to os.environ or global settings, which every
            # concurrent run shares.
            if not is_demo:
                key   = (req.api_key or "").strip()
                model = req.model
            else:
                key   = ""
                model = "demo"

            from src.core.pipeline import Pipeline
            from backend.ws import hitl_events, hitl_decisions, hitl_waiting, cancel_events

            # Create HITL event for this run — pipeline will block on it after Planning
            hitl_event = _threading.Event()
            hitl_events[run_id] = hitl_event

            # Create cancel event — set by POST /api/runs/{run_id}/cancel
            cancel_event = _threading.Event()
            cancel_events[run_id] = cancel_event

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
                run_id=run_id,
                cancel_event=cancel_event,
                max_review_iterations=req.max_review_iterations,
                max_test_fix_iterations=req.max_test_fix_iterations,
                require_approval=req.require_approval,
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
            from src.core.pipeline import PipelineCancelledError
            if isinstance(e, PipelineCancelledError):
                run_store.update_failed(run_id, "Cancelled by user.")
            else:
                run_store.update_failed(run_id, str(e))
        finally:
            # Clean up HITL and cancel state for this run
            hitl_events.pop(run_id, None)
            hitl_decisions.pop(run_id, None)
            hitl_waiting.discard(run_id)
            cancel_events.pop(run_id, None)
            event_queue.put(None)
            _run_slots.release()


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
            name = _safe_zip_name(f["file_path"])
            if name:
                zf.writestr(name, f["content"])

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


# ── Code Diff endpoint ────────────────────────────────────────

@router.get("/runs/{run_id}/diff", response_model=DiffResponse)
async def get_diff(run_id: str):
    """
    Return a file-level before/after diff between the initial generated
    codebase (post-Coder) and the final improved codebase (post-Improver).

    This powers the Diff Viewer in the Result page.
    """
    data = run_store.get_run(run_id)
    if not data:
        raise HTTPException(status_code=404, detail="Run not found")
    if data["status"] not in ("complete",):
        raise HTTPException(status_code=400, detail="Run is not complete yet")

    final_files: list[dict]   = data.get("files", [])
    initial_files: list[dict] = data.get("initial_files", [])

    # Build lookup maps
    initial_map = {f["file_path"]: f["content"] for f in initial_files}
    final_map   = {f["file_path"]: f["content"] for f in final_files}
    all_paths   = sorted(set(list(initial_map.keys()) + list(final_map.keys())))

    diffs: list[FileDiff] = []
    files_changed = 0
    files_added   = 0
    files_unchanged = 0

    for path in all_paths:
        orig = initial_map.get(path, "")
        improved = final_map.get(path, "")
        is_new = path not in initial_map
        is_unchanged = (orig == improved) and not is_new

        if is_unchanged:
            files_unchanged += 1
        elif is_new:
            files_added += 1
        else:
            files_changed += 1

        diffs.append(FileDiff(
            file_path=path,
            original_content=orig,
            improved_content=improved,
            is_new=is_new,
            is_unchanged=is_unchanged,
        ))

    return DiffResponse(
        run_id=run_id,
        files_changed=files_changed,
        files_added=files_added,
        files_unchanged=files_unchanged,
        diffs=diffs,
    )
