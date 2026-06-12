"""
Test suite for AI Software Factory — pipeline, API, and data layer.
Run:  pytest tests/ -v
"""
from __future__ import annotations
import json, sys, threading, time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ─── 1. JSON extraction ───────────────────────────────────────

class TestExtractJson:
    def _call(self, text):
        from src.core.pipeline import _extract_json
        return _extract_json(text)

    def test_plain_json(self):
        assert self._call('{"project_name":"foo"}')["project_name"] == "foo"

    def test_json_in_code_fence(self):
        data = self._call('```json\n{"files":[{"file_path":"main.py"}]}\n```')
        assert data["files"][0]["file_path"] == "main.py"

    def test_json_surrounded_by_prose(self):
        data = self._call('Here is the plan:\n\n{"project_name":"bar"}\n\nDone!')
        assert data["project_name"] == "bar"

    def test_trailing_comma_repaired(self):
        data = self._call('{"a":1,"b":2,}')
        assert data["a"] == 1

    def test_safe_json_fallback(self):
        from src.core.pipeline import _safe_json
        result = _safe_json("not json at all", fallback={"ok": True})
        assert result.get("ok") is True


# ─── 2. RunStore ──────────────────────────────────────────────

class TestRunStore:
    @pytest.fixture
    def store(self, tmp_path):
        import backend.store.run_store as rs_mod
        orig = rs_mod.STORE_DIR
        rs_mod.STORE_DIR = tmp_path
        s = rs_mod.RunStore()
        yield s
        rs_mod.STORE_DIR = orig

    def test_create_and_get(self, store):
        rid = store.create_run("Build a todo app", "demo")
        run = store.get_run(rid)
        assert run["status"] == "running"
        assert run["prompt"] == "Build a todo app"

    def test_update_complete(self, store):
        rid = store.create_run("Test", "demo")
        metrics = {"files_generated": 5, "lines_of_code": 120, "duration_seconds": 42.0}
        store.update_complete(rid, metrics=metrics, files=[{"file_path":"a.py","content":"x=1","language":"python"}])
        run = store.get_run(rid)
        assert run["status"] == "complete"
        assert run["metrics"]["files_generated"] == 5
        assert len(run["files"]) == 1

    def test_update_failed(self, store):
        rid = store.create_run("Fail test", "demo")
        store.update_failed(rid, "boom")
        run = store.get_run(rid)
        assert run["status"] == "failed"
        assert "boom" in run["error"]

    def test_list_runs(self, store):
        for i in range(3):
            store.create_run(f"Project {i}", "demo")
        assert len(store.list_runs(limit=10)) == 3

    def test_nonexistent_returns_none(self, store):
        assert store.get_run("no-such-id") is None

    def test_thread_safety(self, store):
        errors = []
        def write(i):
            try:
                rid = store.create_run(f"Thread {i}", "demo")
                store.update_complete(rid, metrics={}, files=[])
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=write, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors
        assert len(store.list_runs(limit=20)) == 10


# ─── 3. MockLLM stage routing ────────────────────────────────

class TestMockLLM:
    @pytest.fixture
    def llm(self):
        from src.core.mock_llm import MockLLM
        return MockLLM()

    def test_planner(self, llm):
        d = json.loads(llm.call(system="You are a world-class software architect with 20 years of experience designing"))
        assert "project_name" in d

    def test_coder(self, llm):
        d = json.loads(llm.call(system="You are an elite software engineer who writes clean, production-ready"))
        assert "files" in d

    def test_reviewer(self, llm):
        d = json.loads(llm.call(system="You are a meticulous senior code reviewer with deep expertise"))
        assert d["overall_quality"] in {"excellent","good","needs_improvement","poor"}

    def test_tester(self, llm):
        d = json.loads(llm.call(system="You are a QA test engineer who writes comprehensive"))
        assert "files" in d

    def test_deployer(self, llm):
        d = json.loads(llm.call(system="You are a senior devops engineer"))
        assert "files" in d

    def test_fallback(self, llm):
        d = json.loads(llm.call(system="Unknown agent"))
        assert isinstance(d, dict)


# ─── 4. Pipeline with MockLLM (demo mode) ────────────────────

class TestPipelineDemo:
    @pytest.fixture
    def pipeline(self, tmp_path):
        from src.core.pipeline import Pipeline
        from src.core import config as cfg
        cfg.settings.output_dir = str(tmp_path)
        cfg.settings.max_review_iterations = 1
        cfg.settings.max_test_fix_iterations = 1
        return Pipeline(demo=True)

    def test_completes_successfully(self, pipeline):
        from src.core.models import PipelineStage
        state = pipeline.run("Build a simple todo REST API")
        assert state.current_stage == PipelineStage.COMPLETE

    def test_plan_populated(self, pipeline):
        state = pipeline.run("Build a CLI calculator")
        assert state.plan is not None
        assert len(state.plan.tech_stack) > 0

    def test_codebase_has_files(self, pipeline):
        state = pipeline.run("Build a URL shortener")
        assert state.codebase and len(state.codebase.files) > 0

    def test_review_ran(self, pipeline):
        state = pipeline.run("Build a weather CLI")
        assert state.review_report is not None

    def test_events_emitted(self, pipeline):
        events = []
        pipeline.event_callback = lambda e: events.append(e)
        pipeline.run("Build a markdown converter")
        types = {e["type"] for e in events}
        assert {"stage_start", "stage_complete", "complete"} <= types

    def test_complete_event_has_real_metrics(self, pipeline):
        """Core regression: complete event must carry non-zero metrics."""
        events = []
        pipeline.event_callback = lambda e: events.append(e)
        pipeline.run("Build a todo API")
        complete = [e for e in events if e["type"] == "complete"]
        assert len(complete) == 1
        m = complete[0]["metrics"]
        assert m["files_generated"] > 0
        assert m["lines_of_code"] > 0
        assert "duration_seconds" in m

    def test_output_written_to_disk(self, pipeline, tmp_path):
        pipeline.run("Build a hello world API")
        project_dirs = [d for d in tmp_path.iterdir() if not d.name.startswith("_")]
        assert len(project_dirs) >= 1


# ─── 5. FastAPI endpoints ─────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    import backend.store.run_store as rs_mod
    orig = rs_mod.STORE_DIR
    rs_mod.STORE_DIR = tmp_path
    rs_mod.run_store = rs_mod.RunStore()
    from backend.main import app
    with TestClient(app) as c:
        yield c
    rs_mod.STORE_DIR = orig
    rs_mod.run_store = rs_mod.RunStore()


class TestAPI:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_stats_empty(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        assert r.json()["total_runs"] == 0

    def test_list_runs_empty(self, client):
        assert client.get("/api/runs").json() == []

    def test_get_missing_run_404(self, client):
        assert client.get("/api/runs/no-such-id").status_code == 404

    def test_start_demo_run(self, client):
        r = client.post("/api/run", json={
            "prompt": "Build a simple calculator REST API with FastAPI",
            "provider": "demo", "model": "demo",
            "max_review_iterations": 1, "max_test_fix_iterations": 1,
        })
        assert r.status_code == 200
        data = r.json()
        assert "run_id" in data and data["status"] == "running"

    def _wait_complete(self, client, run_id: str, timeout: int = 30):
        for _ in range(timeout * 2):
            time.sleep(0.5)
            r = client.get(f"/api/runs/{run_id}")
            if r.status_code == 200 and r.json()["status"] in ("complete","failed"):
                return r.json()
        return None

    def test_metrics_not_empty_after_demo_run(self, client):
        """Regression test for the metrics={} bug."""
        r = client.post("/api/run", json={
            "prompt": "Build a hello world REST API",
            "provider": "demo", "model": "demo",
            "max_review_iterations": 1, "max_test_fix_iterations": 1,
        })
        run_id = r.json()["run_id"]
        run = self._wait_complete(client, run_id)
        assert run is not None
        assert run["status"] == "complete", f"Pipeline failed: {run.get('error')}"
        assert run["metrics"]["files_generated"] > 0, "Bug: files_generated is still 0"
        assert run["metrics"]["lines_of_code"] > 0, "Bug: lines_of_code is still 0"

    def test_download_zip(self, client):
        r = client.post("/api/run", json={
            "prompt": "Build a simple todo list API",
            "provider": "demo", "model": "demo",
            "max_review_iterations": 1, "max_test_fix_iterations": 1,
        })
        run_id = r.json()["run_id"]
        self._wait_complete(client, run_id)
        zr = client.get(f"/api/runs/{run_id}/download")
        assert zr.status_code == 200
        assert zr.headers["content-type"] == "application/zip"

    def test_stats_after_completed_run(self, client):
        r = client.post("/api/run", json={
            "prompt": "Build a REST API for managing tasks",
            "provider": "demo", "model": "demo",
            "max_review_iterations": 1, "max_test_fix_iterations": 1,
        })
        self._wait_complete(client, r.json()["run_id"])
        stats = client.get("/api/stats").json()
        assert stats["total_runs"] >= 1
        assert stats["success_rate"] > 0
