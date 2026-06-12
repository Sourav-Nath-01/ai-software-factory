"""WebSocket endpoint — streams live pipeline events to the browser."""
from __future__ import annotations

import asyncio
import json
import queue as q_module
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["websocket"])

# run_id -> Queue[dict | None]  (None = sentinel = done)
active_queues: Dict[str, q_module.Queue] = {}


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

    try:
        while True:
            # Poll queue in a non-blocking way compatible with asyncio
            try:
                event = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: event_queue.get(timeout=60)
                )
            except q_module.Empty:
                await ws.send_text(json.dumps({"type": "ping"}))
                continue

            if event is None:  # sentinel — pipeline finished
                break

            await ws.send_text(json.dumps(event))

    except WebSocketDisconnect:
        pass
    finally:
        active_queues.pop(run_id, None)
        try:
            await ws.close()
        except Exception:
            pass
