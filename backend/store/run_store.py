"""Thread-safe JSON-based run persistence."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

STORE_DIR = Path("output/runs")


class RunStore:
    """Persists pipeline runs as JSON files."""

    def __init__(self):
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

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
            "error": None,
        }
        self._save(run_id, data)
        return run_id

    def update_complete(self, run_id: str, metrics: dict, files: list[dict]):
        run = self._load(run_id) or {}
        run["status"] = "complete"
        run["metrics"] = metrics
        run["files"] = files
        self._save(run_id, run)

    def update_failed(self, run_id: str, error: str):
        run = self._load(run_id) or {}
        run["status"] = "failed"
        run["error"] = error
        self._save(run_id, run)

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

    def _save(self, run_id: str, data: dict):
        with self._lock:
            (STORE_DIR / f"{run_id}.json").write_text(json.dumps(data, indent=2))

    def _load(self, run_id: str) -> Optional[dict]:
        path = STORE_DIR / f"{run_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())


run_store = RunStore()
