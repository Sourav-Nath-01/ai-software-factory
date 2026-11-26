"""Human-in-the-Loop (HITL) REST endpoints.

After the Planner stage completes, the pipeline pauses and emits a
`hitl_checkpoint` event over WebSocket. The frontend renders an approval
modal showing the proposed architecture (tech stack, files, modules).

The user can (all via POST /api/runs/{run_id}/approve):
  - approve → the pipeline continues to code generation
  - revise  → the Planner rewrites the plan using the user's feedback, and the
              pipeline pauses again with the new plan (up to 3 rounds)
  - abort   → the pipeline stops

Implementation uses threading.Event to block the pipeline thread without
busy-waiting, keeping CPU usage at zero during the wait.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# hitl_events: run_id -> threading.Event (set when user approves/rejects)
# hitl_decisions: run_id -> {"action": "approve" | "revise" | "abort", "feedback": str, "reason": str}
# Both are populated by the pipeline and consumed by these endpoints.
from backend.ws import hitl_events, hitl_decisions, hitl_waiting, cancel_events

router = APIRouter(prefix="/api/runs", tags=["hitl"])


class HITLApproveRequest(BaseModel):
    # "revise" sends `feedback` to the Planner; without `action`, `approved` picks approve/abort
    action: Optional[Literal["approve", "revise", "abort"]] = None
    feedback: str = Field("", max_length=2000)
    approved: bool = True
    reason: str = ""


@router.post("/{run_id}/approve")
async def hitl_approve(run_id: str, body: HITLApproveRequest):
    """
    Answer the architecture review: approve, revise with feedback, or abort.

    Called by the frontend's review modal. `{"approved": true/false}` alone still
    works (approve / abort) for older clients.
    """
    event = hitl_events.get(run_id)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active HITL checkpoint found for run '{run_id}'. "
                   "The pipeline may have already passed the approval stage."
        )
    if run_id not in hitl_waiting or event.is_set():
        raise HTTPException(status_code=409, detail="The run isn't waiting for a review right now "
                                                    "(a decision is being processed or the plan is being revised).")

    action = body.action or ("approve" if body.approved else "abort")
    feedback = body.feedback.strip()
    if action == "revise" and not feedback:
        raise HTTPException(status_code=400, detail="Describe what should change in the plan.")

    # Record the decision before unblocking the pipeline thread
    hitl_decisions[run_id] = {"action": action, "feedback": feedback, "reason": body.reason}
    event.set()  # Unblock the waiting pipeline thread

    messages = {"approve": "Pipeline will continue.", "revise": "The plan will be revised.",
                "abort": "Pipeline will be aborted."}
    return {"run_id": run_id, "action": action, "message": messages[action]}


@router.get("/{run_id}/hitl_status")
async def hitl_status(run_id: str):
    """Check whether a run is currently waiting for HITL approval."""
    waiting = run_id in hitl_waiting and not hitl_events[run_id].is_set()
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
        hitl_decisions[run_id] = {"action": "abort", "reason": "Cancelled by user."}
        hitl_ev.set()

    return {
        "run_id": run_id,
        "cancelled": True,
        "message": "Cancel signal sent — pipeline will stop at the next stage boundary.",
    }

