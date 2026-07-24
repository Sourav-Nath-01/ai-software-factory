"""Human-in-the-Loop (HITL) REST endpoints.

After the Planner stage completes, the pipeline pauses and emits a
`hitl_checkpoint` event over WebSocket. The frontend renders an approval
modal showing the proposed architecture (tech stack, files, modules).

The user can:
  - Approve → POST /api/runs/{run_id}/approve   → pipeline continues
  - Reject  → POST /api/runs/{run_id}/reject    → pipeline aborts with reason

Implementation uses threading.Event to block the pipeline thread without
busy-waiting, keeping CPU usage at zero during the wait.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# hitl_events: run_id -> threading.Event (set when user approves/rejects)
# hitl_decisions: run_id -> {"approved": bool, "reason": str}
# Both are populated by the pipeline and consumed by these endpoints.
from backend.ws import hitl_events, hitl_decisions, cancel_events

router = APIRouter(prefix="/api/runs", tags=["hitl"])


class HITLApproveRequest(BaseModel):
    approved: bool = True
    reason: str = ""  # used when rejecting — optional user feedback


@router.post("/{run_id}/approve")
async def hitl_approve(run_id: str, body: HITLApproveRequest):
    """
    Signal the pipeline to continue (approved=True) or abort (approved=False).

    Called by the frontend when the user clicks Approve or Reject in the
    architecture review modal.
    """
    event = hitl_events.get(run_id)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active HITL checkpoint found for run '{run_id}'. "
                   "The pipeline may have already passed the approval stage."
        )

    # Record the decision before unblocking the pipeline thread
    hitl_decisions[run_id] = {
        "approved": body.approved,
        "reason": body.reason,
    }
    event.set()  # Unblock the waiting pipeline thread

    return {
        "run_id": run_id,
        "approved": body.approved,
        "message": "Pipeline will continue." if body.approved else "Pipeline will be aborted.",
    }


@router.get("/{run_id}/hitl_status")
async def hitl_status(run_id: str):
    """Check whether a run is currently waiting for HITL approval."""
    waiting = run_id in hitl_events and not hitl_events[run_id].is_set()
    decision = hitl_decisions.get(run_id)
    return {
        "run_id": run_id,
        "waiting_for_approval": waiting,
        "decision": decision,
    }


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str):
    """
    Gracefully stop a running pipeline.

    Sets the cancel event which the pipeline checks at the boundary between
    every stage. The pipeline will raise PipelineCancelledError at the next
    checkpoint, mark the run as 'failed', and emit a 'cancelled' WebSocket event.

    Safe to call even if the run has already completed — returns 200 with
    a 'not_running' message in that case.
    """
    from backend.store.run_store import run_store
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    if run.get("status") not in ("running", "pending"):
        return {
            "run_id": run_id,
            "cancelled": False,
            "message": f"Run is already in terminal state: {run.get('status')}",
        }

    event = cancel_events.get(run_id)
    if event:
        event.set()   # Signal the pipeline thread to stop at next stage boundary

    # Also unblock any HITL wait so it doesn't hang
    hitl_ev = hitl_events.get(run_id)
    if hitl_ev and not hitl_ev.is_set():
        hitl_decisions[run_id] = {"approved": False, "reason": "Cancelled by user."}
        hitl_ev.set()

    return {
        "run_id": run_id,
        "cancelled": True,
        "message": "Cancel signal sent — pipeline will stop at the next stage boundary.",
    }

