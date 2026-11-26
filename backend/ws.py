"""WebSocket endpoint — streams live pipeline events to the browser."""
from __future__ import annotations

import asyncio
import json
import queue as q_module
import threading
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["websocket"])

# run_id -> Queue[dict | None]  (None = sentinel = done)
active_queues: Dict[str, q_module.Queue] = {}

# ── Human-in-the-Loop coordination ──────────────────────────
# run_id -> threading.Event  (set when user approves/rejects)
hitl_events: Dict[str, threading.Event] = {}

# run_id -> {"approved": bool, "reason": str}
hitl_decisions: Dict[str, dict] = {}

# ── Pipeline cancellation ───────────────────────────────
# run_id -> threading.Event  (set when user clicks Stop)
cancel_events: Dict[str, threading.Event] = {}


@router.websocket("/ws/{run_id}")
async def websocket_run(ws: WebSocket, run_id: str):
    """Stream pipeline events for a run via WebSocket."""
    await ws.accept()

    # Wait up to 10s for the run queue to appear
    for _ in range(100):
        if run_id in active_queues:
            break
        await asyncio.sleep(0.1)
    else:
        await ws.send_text(json.dumps({"type": "error", "message": "Run not found or already finished"}))
        await ws.close()
        return

    event_queue = active_queues[run_id]
    loop = asyncio.get_running_loop()
    last_sent = loop.time()

    try:
        while True:
            # Non-blocking poll. (Blocking queue.get() in run_in_executor would
            # hold a thread-pool thread per connected browser for the whole run,
            # starving the pool once a dozen or so clients are connected.)
            try:
                event = event_queue.get_nowait()
            except q_module.Empty:
                if loop.time() - last_sent > 30:
                    await ws.send_text(json.dumps({"type": "ping"}))  # keep proxies from idling us out
                    last_sent = loop.time()
                await asyncio.sleep(0.1)
                continue

            if event is None:  # sentinel — pipeline finished
                break

            await ws.send_text(json.dumps(event))
            last_sent = loop.time()

    except WebSocketDisconnect:
        pass
    finally:
        active_queues.pop(run_id, None)
        try:
            await ws.close()
        except Exception:
            pass
