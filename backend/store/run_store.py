"""Thread-safe JSON-based run persistence."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import settings

# Under OUTPUT_DIR, so a deployment can keep run history on a persistent volume
STORE_DIR = settings.output_path / "runs"


class RunStore:
    """Persists pipeline runs as JSON files.

    Writes are atomic (temp file + os.replace), so a reader never sees a
    half-written file, and every read-modify-write holds the lock, so two
    threads updating the same run can't lose each other's changes.
    """

    def __init__(self):
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def cleanup_stale_runs(self) -> int:
        """Mark any 'running' runs as 'failed' on server startup.

        When the server crashes or gets OOM-killed, pipeline threads die
        instantly without updating their run status. This ensures history
        never shows ghost 'running' entries after a restart.

        Returns the number of runs cleaned up.
        """
        cleaned = 0
        with self._lock:
            for f in STORE_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    if data.get("status") == "running":
                        data["status"] = "failed"
                        data["error"] = "Server restarted — run did not complete."
                        self._save(data["run_id"], data)
                        cleaned += 1
                except Exception:
                    continue
        return cleaned

    def create_run(self, prompt: str, model: str) -> str:
        run_id = str(uuid.uuid4())[:8]
        data = {
            "run_id": run_id,
            "prompt": prompt,
            "status": "running",
            "model": model,
            "created_at": datetime.now().isoformat(),
            "metrics": None,
            "files": [],
            "initial_files": [],  # snapshot before Reviewer/Improver runs
            "error": None,
        }
        self._save(run_id, data)
        return run_id

    def update_initial_codebase(self, run_id: str, files: list[dict]):
        """Persist the initial generated codebase snapshot (before Improver runs)."""
        self._update(run_id, initial_files=files)

    def update_complete(self, run_id: str, metrics: dict, files: list[dict]):
        self._update(run_id, status="complete", metrics=metrics, files=files)

    def update_failed(self, run_id: str, error: str):
        self._update(run_id, status="failed", error=error)

    def get_run(self, run_id: str) -> Optional[dict]:
        return self._load(run_id)

    def list_runs(self, limit: int = 20) -> list[dict]:
        files = sorted(
            STORE_DIR.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        runs = []
        for f in files[:limit]:
            try:
                data = json.loads(f.read_text())
                runs.append({
                    "run_id": data.get("run_id", f.stem),
                    "prompt": data.get("prompt", "")[:100],
                    "status": data.get("status", "unknown"),
                    "model": data.get("model", "unknown"),
                    "created_at": data.get("created_at", ""),
                    "metrics": data.get("metrics", {}),
                })
            except Exception:
                continue
        return runs

    def _update(self, run_id: str, **fields):
        with self._lock:
            run = self._load(run_id) or {}
            run.update(fields)
            self._save(run_id, run)

    def _save(self, run_id: str, data: dict):
        path = STORE_DIR / f"{run_id}.json"
        tmp = path.with_suffix(f".json.{threading.get_ident()}.tmp")
        with self._lock:
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, path)  # atomic on POSIX and Windows

    def _load(self, run_id: str) -> Optional[dict]:
        path = STORE_DIR / f"{run_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())


run_store = RunStore()
