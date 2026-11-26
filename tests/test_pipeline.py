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

    def test_code_fences_inside_json_strings_are_kept(self):
        """Regression: a README with a ```bash block used to make the parser drop every file."""
        payload = json.dumps({"files": [
            {"file_path": "README.md", "content": "# Tool\n\n```bash\npip install .\n```\n"},
            {"file_path": "app.py", "content": "x = 1"}]}, indent=2)
        assert len(self._call(payload)["files"]) == 2
        assert len(self._call("```json\n" + payload + "\n```")["files"]) == 2

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

    def test_generated_tests_really_pass(self, pipeline):
        """tests_passed comes from pytest's exit code, not the analyst LLM's claim."""
        state = pipeline.run("Build a todo API")
        assert state.test_result.passed, state.test_result.stdout[-2000:]

    def test_demo_shows_a_verified_security_fix(self, pipeline):
        """The demo's story: bandit finds the SQL injection, the Improver fixes it, the tools confirm it."""
        from src.core.static_analysis import BANDIT_AVAILABLE
        if not BANDIT_AVAILABLE:
            pytest.skip("bandit not installed")
        events = []
        pipeline.event_callback = events.append
        state = pipeline.run("Build a todo API")
        metrics = next(e["metrics"] for e in events if e["type"] == "complete")
        assert metrics["issues_fixed"] >= 1
        assert metrics["static_issues_final"] < metrics["static_issues_initial"]
        router = state.codebase.get_file("app/routers/todos.py").content
        assert "LIKE '%{q}%'" not in router and 'LIKE ?' in router
        assert state.test_result.passed

    def test_output_written_to_disk(self, pipeline, tmp_path):
        pipeline.run("Build a hello world API")
        project_dirs = [d for d in tmp_path.iterdir() if not d.name.startswith("_")]
        assert len(project_dirs) >= 1


# ─── 5. Regression tests for pipeline robustness fixes ────────

class TestPipelineRobustness:
    @pytest.fixture
    def pipeline(self):
        from src.core.pipeline import Pipeline
        from src.core.models import CodeBase, PipelineState
        p = Pipeline(demo=True)
        p.state = PipelineState(user_request="x")
        p.state.codebase = CodeBase()
        return p

    def test_prompt_budget_never_cuts_a_file(self, pipeline):
        from src.core.pipeline import Pipeline
        for i in range(5):
            pipeline.state.codebase.set_file(f"f{i}.py", "x = 1\n" * 400)
        text = Pipeline._codebase_for_prompt(pipeline.state.codebase, max_chars=6000)
        body, _, omitted = text.partition("\n\n[Omitted")
        included = json.loads(body)["files"]   # still valid JSON
        assert all(f["content"] == "x = 1\n" * 400 for f in included)
        assert 0 < len(included) < 5
        assert all(f"f{i}.py" in omitted for i in range(len(included), 5))

    def test_improver_output_merges_instead_of_replacing(self, pipeline):
        pipeline.state.codebase.set_file("a.py", "a = 1")
        pipeline.state.codebase.set_file("b.py", "b = 1")
        changed = pipeline._merge_files([{"file_path": "a.py", "content": "a = 2"}, {"content": "no path"}])
        assert changed == 1
        assert pipeline.state.codebase.get_file("a.py").content == "a = 2"
        assert pipeline.state.codebase.get_file("b.py").content == "b = 1"
        assert len(pipeline.state.codebase.files) == 2

    def test_malformed_file_entries_are_skipped_not_fatal(self, pipeline):
        """Regression: a string inside "files" crashed the Tester/Deployer with 'str'.get."""
        from src.core.pipeline import _valid_files
        files = ["oops", None, {"content": "no path"}, {"file_path": "  "},
                 {"file_path": "Dockerfile", "content": None}, {"file_path": "a.py", "content": "x = 1"}]
        assert [f["file_path"] for f in _valid_files(files, default_language="yaml")] == ["Dockerfile", "a.py"]
        assert _valid_files("not a list") == [] and _valid_files({"a.py": "x"}) == []
        assert pipeline._merge_files(files) == 2

    def test_truncated_response_drops_the_cut_off_file(self, pipeline):
        """Regression: a response cut at max_tokens planted half-written files (ending in `parents[1`)."""
        # The cut-off content contains a '}' (an f-string), like the real failures did
        cut_off = ('{"files": [{"file_path": "a.py", "content": "x = 1"}, '
                   '{"file_path": "b.py", "content": "r = client.get(f\\"/todos/{todo_id}\\")\\nsys.path.append(p.parents[1')
        pipeline._last_truncated = True
        files = pipeline._response_files(cut_off)
        assert [f["file_path"] for f in files] == ["a.py"] and files[0]["content"] == "x = 1"

    def test_truncated_response_keeps_contents_byte_exact(self, pipeline):
        """Regression: json_repair shifted whitespace in strings when repairing a cut-off response."""
        originals = [{"file_path": f"f{i}.py", "content": f"\n    x = {{'k': {i}}}\n\ttab\n", "language": "python"}
                     for i in range(30)]
        full = json.dumps({"files": originals}, indent=2)
        pipeline._last_truncated = True
        for cut in range(200, len(full), 97):
            got = pipeline._response_files(full[:cut])
            assert got == originals[:len(got)]
            assert len(got) >= full[:cut].count("}\n    }") - 1

    def test_loop_limits_are_per_run(self):
        from src.core.pipeline import Pipeline
        from src.core.config import settings
        before = settings.max_review_iterations
        p = Pipeline(demo=True, max_review_iterations=5, max_test_fix_iterations=4)
        assert (p._max_review_iterations, p._max_test_fix_iterations) == (5, 4)
        assert settings.max_review_iterations == before

    def test_memory_write_failure_does_not_raise(self):
        from src.core.memory import AgentMemory

        class Broken:
            def upsert(self, **_):
                raise TimeoutError("The read operation timed out in upsert.")

        mem = AgentMemory("t")
        mem._initialised, mem._collection = True, Broken()
        mem.store("content", {"k": "v"})
        assert mem._fallback

    def test_finding_keys_ignore_line_numbers(self):
        from src.core.static_analysis import AnalysisReport
        before = AnalysisReport(
            security_issues=[{"filename": "app/db.py", "test_id": "B608", "issue_text": "SQL injection", "line_number": 10}],
            style_issues=["app/main.py:3:1: F401 'os' imported but unused"])
        after = AnalysisReport(
            security_issues=[{"filename": "app/db.py", "test_id": "B608", "issue_text": "SQL injection", "line_number": 42}],
            style_issues=[])
        assert len(before.code_finding_keys() - after.code_finding_keys()) == 1

    def test_verified_fixes_use_real_tools(self, pipeline):
        from src.core.static_analysis import analyzer, BANDIT_AVAILABLE, FLAKE8_AVAILABLE
        if not (BANDIT_AVAILABLE and FLAKE8_AVAILABLE):
            pytest.skip("bandit/flake8 not installed")
        pipeline.state.codebase.set_file("app/a.py", "import os\nimport subprocess\nsubprocess.call('ls', shell=True)\n")
        pipeline.state.codebase.set_file("app/b.py", "x = 1\n")
        pipeline._static_initial_keys = analyzer.run_code_review(pipeline._python_files()).code_finding_keys()
        assert pipeline._static_initial_keys
        # The "Improver" removes the unused import and the shell=True call
        pipeline.state.codebase.set_file("app/a.py", "import subprocess\nsubprocess.call(['ls'])\n")
        pipeline._verify_static_fixes()
        assert pipeline._issues_fixed > 0
        assert pipeline._static_issues_final < pipeline._static_issues_initial

    def test_oversized_request_does_not_deadlock_rate_limiter(self):
        from src.core.rate_limiter import TokenBucket
        bucket = TokenBucket(tpm=1000, rpm=30)
        slept = []
        bucket.consume("x" * 40_000, sleep=slept.append)   # ~10k tokens > 1k capacity
        assert sum(slept) == 0                              # full bucket serves it at once

    def test_limits_follow_provider_not_model_name(self):
        from src.core.rate_limiter import _resolve_limits, PROVIDER_LIMITS
        assert _resolve_limits("groq/openai/gpt-oss-120b") is PROVIDER_LIMITS["groq"]   # not OpenAI's
        assert _resolve_limits("gemini/gemini-2.5-flash") is PROVIDER_LIMITS["gemini"]
        assert _resolve_limits("gpt-4o-mini") is PROVIDER_LIMITS["openai"]

    def test_daily_quota_and_retry_hints(self):
        from src.core.pipeline import _DAILY_QUOTA_MARKERS, _parse_retry_after
        groq = "rate limit reached on tokens per day (tpd): limit 200000. please try again in 11m57.552s."
        assert any(m in groq for m in _DAILY_QUOTA_MARKERS)
        assert _parse_retry_after(groq) == pytest.approx(717.552)
        assert _parse_retry_after("please try again in 7.25s") == pytest.approx(7.25)
        assert _parse_retry_after("try again in 450ms") == pytest.approx(0.45)
        assert _parse_retry_after("no hint here") is None
        assert not any(m in "rate limit reached on tokens per minute (tpm)" for m in _DAILY_QUOTA_MARKERS)

    def test_reasoning_models_get_low_effort(self):
        from src.core.pipeline import _model_specific_params
        assert _model_specific_params("groq/openai/gpt-oss-120b") == {"reasoning_effort": "low"}
        assert _model_specific_params("gemini/gemini-2.5-flash") == {"reasoning_effort": "low"}
        assert _model_specific_params("gpt-4o-mini") == {}

    def test_unused_output_reservation_is_refunded(self):
        from src.core.rate_limiter import TokenBucket
        bucket = TokenBucket(tpm=8000, rpm=30)
        bucket.consume("x" * 4000, sleep=lambda s: None, extra_tokens=6000)  # reserves 7000
        bucket.reconcile(reserved=7000, actual=1500)
        slept = []
        bucket.consume("x" * 4000, sleep=slept.append, extra_tokens=5000)    # 6000 more fits now
        assert sum(slept) == 0

    def test_cancel_interrupts_rate_limit_wait(self):
        import threading
        from src.core.pipeline import Pipeline, PipelineCancelledError
        cancel = threading.Event()
        p = Pipeline(demo=True, cancel_event=cancel)
        threading.Timer(0.2, cancel.set).start()
        t0 = time.time()
        with pytest.raises(PipelineCancelledError):
            p._sleep(30)
        assert time.time() - t0 < 5

    def test_review_comment_normalization(self):
        from src.core.pipeline import _normalize_severity
        assert [_normalize_severity(s) for s in ("High", "BLOCKER", "medium", None, "weird")] == \
            ["critical", "critical", "warning", "info", "info"]

    def test_memory_retrieval_feeds_reviewer(self, pipeline, monkeypatch):
        from src.core import pipeline as pipeline_mod
        from src.core.memory import AgentMemory
        from src.core.models import ProjectPlan, ReviewReport, ReviewComment
        mem = AgentMemory("t")
        mem._initialised = True                       # in-memory fallback, no ChromaDB
        monkeypatch.setattr(pipeline_mod, "reviewer_memory", mem)
        pipeline._use_memory = True
        pipeline.state.plan = ProjectPlan(project_name="p", description="todo API", tech_stack=["FastAPI"],
                                          file_structure=[], modules=[])
        assert pipeline._recall_past_issues() == ""   # nothing remembered yet
        pipeline._remember_issues(ReviewReport(comments=[
            ReviewComment(file_path="a.py", severity="critical", category="security", description="SQL injection in search"),
            ReviewComment(file_path="a.py", severity="info", category="style", description="naming"),
        ]))
        recalled = pipeline._recall_past_issues()
        assert "SQL injection in search" in recalled and "naming" not in recalled

    def test_file_writer_rejects_sibling_dir_escape(self, tmp_path):
        from src.tools.file_writer import write_project_files
        out = tmp_path / "proj"
        written = write_project_files([{"file_path": "../proj2/evil.py", "content": "x"},
                                       {"file_path": "ok.py", "content": "x"}], out)
        assert written == [str((out / "ok.py").resolve())]
        assert not (tmp_path / "proj2").exists()

    def test_zip_entries_cannot_escape(self):
        from backend.routes.runs import _safe_zip_name
        assert _safe_zip_name("app/./main.py") == "app/main.py"
        assert _safe_zip_name("/etc/passwd") == "etc/passwd"
        assert _safe_zip_name("../../.bashrc") is None
        assert _safe_zip_name("C:/x.py") is None

    def test_run_store_concurrent_updates_keep_all_fields(self, tmp_path):
        import backend.store.run_store as rs_mod
        orig, rs_mod.STORE_DIR = rs_mod.STORE_DIR, tmp_path
        try:
            store = rs_mod.RunStore()
            rid = store.create_run("x" * 20, "demo")
            threads = [threading.Thread(target=store.update_initial_codebase, args=(rid, [{"f": 1}])),
                       threading.Thread(target=store.update_complete, args=(rid, {"files_generated": 1}, []))]
            for t in threads: t.start()
            for t in threads: t.join()
            run = store.get_run(rid)
            assert run["initial_files"] == [{"f": 1}] and run["status"] == "complete"
            assert not list(tmp_path.glob("*.tmp"))
        finally:
            rs_mod.STORE_DIR = orig

    def test_sandbox_installs_pyproject_dependencies(self, tmp_path):
        """Regression: deps declared only in pyproject.toml were never installed."""
        from src.core.sandbox import collect_requirements
        (tmp_path / "requirements.txt").write_text("requests==2.32.0\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = [\n  "click>=8.1",\n  \'rich\',\n]\n\n[tool.x]\nother = ["no"]\n')
        reqs = collect_requirements(tmp_path).splitlines()
        assert reqs == ["requests==2.32.0", "click>=8.1", "rich"]

    def test_unsandboxed_exec_hides_secrets(self, monkeypatch):
        from src.core import sandbox
        monkeypatch.setenv("GROQ_API_KEY", "secret")
        monkeypatch.setenv("SOME_TOKEN", "secret")
        env = sandbox._scrubbed_env()
        assert "GROQ_API_KEY" not in env and "SOME_TOKEN" not in env
        assert "PATH" in env


# ─── 6. FastAPI endpoints ─────────────────────────────────────

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

    def test_busy_server_refuses_extra_runs(self, client, monkeypatch):
        """With every run slot taken, a new run gets 429 and leaves no record behind."""
        import threading
        import backend.routes.runs as runs_mod
        full = threading.BoundedSemaphore(1)
        full.acquire()
        monkeypatch.setattr(runs_mod, "_run_slots", full)
        r = client.post("/api/run", json={"prompt": "Build a simple calculator API", "provider": "demo", "model": "demo"})
        assert r.status_code == 429
        assert client.get("/api/runs").json() == []

    def test_real_provider_without_key_is_rejected(self, client):
        """A missing key must not silently fall back to mock output."""
        r = client.post("/api/run", json={"prompt": "Build a simple calculator API", "provider": "groq",
                                          "model": "groq/openai/gpt-oss-120b"})
        assert r.status_code == 400
        assert client.get("/api/runs").json() == []

    def test_start_demo_run(self, client):
        r = client.post("/api/run", json={
            "prompt": "Build a simple calculator REST API with FastAPI",
            "provider": "demo", "model": "demo",
            "max_review_iterations": 1, "max_test_fix_iterations": 1, "require_approval": False,
        })
        assert r.status_code == 200
        data = r.json()
        assert "run_id" in data and data["status"] == "running"

    def _wait_for_review(self, client, run_id: str, timeout: float = 30):
        for _ in range(int(timeout * 10)):
            if client.get(f"/api/runs/{run_id}/hitl_status").json()["waiting_for_approval"]:
                return
            time.sleep(0.1)
        raise AssertionError("the run never paused for review")

    def test_review_can_revise_the_plan_then_approve(self, client):
        """Reject with feedback -> the Planner revises -> approve -> the run completes."""
        r = client.post("/api/run", json={"prompt": "Build a simple todo REST API", "provider": "demo", "model": "demo",
                                          "max_review_iterations": 1, "max_test_fix_iterations": 1})
        run_id = r.json()["run_id"]
        self._wait_for_review(client, run_id)
        empty = client.post(f"/api/runs/{run_id}/approve", json={"action": "revise", "feedback": "  "})
        assert empty.status_code == 400
        revise = client.post(f"/api/runs/{run_id}/approve",
                             json={"action": "revise", "feedback": "Use PostgreSQL instead of SQLite"})
        assert revise.status_code == 200
        self._wait_for_review(client, run_id)  # the revised plan is up for review
        assert client.post(f"/api/runs/{run_id}/approve", json={"action": "approve"}).status_code == 200
        run = self._wait_complete(client, run_id)
        assert run["status"] == "complete" and run["metrics"]["plan_revisions"] == 1

    def test_review_decisions_are_only_accepted_while_waiting(self, client):
        import threading
        from backend.ws import hitl_events
        assert client.post("/api/runs/no-such-run/approve", json={"action": "approve"}).status_code == 404
        hitl_events["paused-elsewhere"] = threading.Event()  # exists, but not waiting for a review
        try:
            r = client.post("/api/runs/paused-elsewhere/approve", json={"action": "approve"})
            assert r.status_code == 409
        finally:
            hitl_events.pop("paused-elsewhere", None)

    def _wait_complete(self, client, run_id: str, timeout: int = 120):
        """Poll until the run reaches a terminal state or timeout expires.

        Timeout increased to 120s to handle the full demo pipeline which
        now includes security scanning (pip-audit) and multiple review stages.
        """
        for _ in range(timeout):
            time.sleep(1)
            r = client.get(f"/api/runs/{run_id}")
            if r.status_code == 200 and r.json()["status"] in ("complete","failed"):
                return r.json()
        return None

    def test_metrics_not_empty_after_demo_run(self, client):
        """Regression test for the metrics={} bug."""
        r = client.post("/api/run", json={
            "prompt": "Build a hello world REST API",
            "provider": "demo", "model": "demo",
            "max_review_iterations": 1, "max_test_fix_iterations": 1, "require_approval": False,
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
            "max_review_iterations": 1, "max_test_fix_iterations": 1, "require_approval": False,
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
            "max_review_iterations": 1, "max_test_fix_iterations": 1, "require_approval": False,
        })
        self._wait_complete(client, r.json()["run_id"])
        stats = client.get("/api/stats").json()
        assert stats["total_runs"] >= 1
        assert stats["success_rate"] > 0



# ─── Architecture review: approve / revise / abort ────────────

class TestPlanReview:
    RUN_ID = "review-test"

    @pytest.fixture
    def pipeline(self):
        import threading
        from backend.ws import hitl_events
        from src.core.mock_llm import MOCK_PLAN
        from src.core.models import PipelineState
        from src.core.pipeline import Pipeline
        p = Pipeline(demo=True, run_id=self.RUN_ID)
        p.state = PipelineState(user_request="Build a todo API")
        p.state.plan = Pipeline._plan_from_data(MOCK_PLAN)
        p.events = []
        p.event_callback = p.events.append
        hitl_events[self.RUN_ID] = threading.Event()
        yield p
        hitl_events.pop(self.RUN_ID, None)

    def _review(self, pipeline, decisions):
        """Run the checkpoint in a thread and answer each review like the API would."""
        import threading
        from backend.ws import hitl_decisions, hitl_events, hitl_waiting
        errors = []

        def target():
            try:
                pipeline._run_hitl_checkpoint()
            except Exception as e:
                errors.append(e)
        thread = threading.Thread(target=target)
        thread.start()
        for decision in decisions:
            for _ in range(500):
                if self.RUN_ID in hitl_waiting and not hitl_events[self.RUN_ID].is_set():
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("the pipeline never paused for review")
            hitl_decisions[self.RUN_ID] = decision
            hitl_events[self.RUN_ID].set()
        thread.join(10)
        assert not thread.is_alive()
        return errors

    def test_revise_then_approve_applies_the_feedback(self, pipeline):
        feedback = "Use PostgreSQL instead of SQLite"
        errors = self._review(pipeline, [{"action": "revise", "feedback": feedback}, {"action": "approve"}])
        assert not errors
        assert pipeline._plan_revisions == 1
        assert feedback in pipeline.state.plan.description           # the Planner applied it (mock)
        assert feedback in pipeline.state.plan.additional_notes      # and the Coder will see it verbatim
        checkpoints = [e for e in pipeline.events if e["type"] == "hitl_checkpoint"]
        assert [(c["revision"], c["revisions_left"]) for c in checkpoints] == [(0, 3), (1, 2)]
        types = [e["type"] for e in pipeline.events]
        assert "hitl_revising" in types and types[-1] == "hitl_approved"

    def test_abort_stops_the_run(self, pipeline):
        errors = self._review(pipeline, [{"action": "abort", "reason": "Wrong stack"}])
        assert len(errors) == 1 and "aborted by user: Wrong stack" in str(errors[0])

    def test_legacy_approved_flag_still_works(self, pipeline):
        assert not self._review(pipeline, [{"approved": True}])
        errors = self._review(pipeline, [{"approved": False, "reason": "no"}])
        assert errors and "aborted by user" in str(errors[0])

    def test_revisions_are_capped(self, pipeline):
        from src.core.pipeline import MAX_PLAN_REVISIONS
        decisions = [{"action": "revise", "feedback": f"change {i}"} for i in range(MAX_PLAN_REVISIONS + 1)]
        errors = self._review(pipeline, decisions)
        assert pipeline._plan_revisions == MAX_PLAN_REVISIONS
        assert errors and f"revised {MAX_PLAN_REVISIONS} times" in str(errors[0])
