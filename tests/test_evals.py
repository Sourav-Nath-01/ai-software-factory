"""Tests for the evaluation harness (evals/)."""
from __future__ import annotations
import json

import pytest


class TestMeasurement:
    def test_test_paths_are_excluded_from_source(self):
        from evals.run_eval import is_test_path
        assert is_test_path("tests/test_api.py")
        assert is_test_path("app/tests/helpers.py")
        assert is_test_path("conftest.py")
        assert not is_test_path("app/testing_utils.py")

    def test_measure_codebase(self):
        from evals.run_eval import measure_codebase
        from src.core.static_analysis import BANDIT_AVAILABLE, FLAKE8_AVAILABLE
        m = measure_codebase([
            # shell=True with a non-literal command is HIGH severity (a literal one is only LOW)
            {"file_path": "app/a.py", "content": "import os\nimport subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n"},
            {"file_path": "app/broken.py", "content": "def f(:\n"},
            {"file_path": "tests/test_a.py", "content": "def test_x():\n    assert True\n"},
            {"file_path": "README.md", "content": "# hi"},
        ])
        assert (m["files"], m["py_files"], m["test_files"], m["syntax_error_files"]) == (4, 3, 1, 1)
        if BANDIT_AVAILABLE and FLAKE8_AVAILABLE:
            assert m["bandit_high"] == 1          # shell=True on a variable
            assert m["bandit_low"] == 1           # import subprocess
            assert m["flake8_pyflakes"] == 1      # unused import os

    def test_parse_pytest_summary(self):
        from evals.run_eval import parse_pytest
        out = "test_a.py::test_x PASSED\n===== 3 passed, 1 failed, 2 errors in 0.5s ====="
        assert parse_pytest(out) == {"tests_passed_count": 3, "tests_failed_count": 1, "tests_error_count": 2}
        assert parse_pytest("") == {"tests_passed_count": 0, "tests_failed_count": 0, "tests_error_count": 0}


class TestInfraErrors:
    def test_setup_errors_are_not_pipeline_failures(self):
        from evals.run_eval import is_infra_error
        assert is_infra_error('litellm.NotFoundError: {"code":"model_not_found"}')
        assert is_infra_error("Daily API quota exhausted for groq/x.")
        assert is_infra_error("LLM call failed after 4 retries (groq/x). Last error: 503")
        assert is_infra_error("Request too large for groq/qwen/qwen3.8-27b's limits (prompt + 5365 output tokens).")
        assert not is_infra_error("1 validation error for ProjectPlan")
        assert not is_infra_error(None)

    def test_resume_retries_infra_failures_and_report_keeps_latest(self, tmp_path):
        from evals.run_eval import main
        from evals.report import load_records
        out = tmp_path / "run"
        out.mkdir()
        (out / "meta.json").write_text(json.dumps({"model": "demo"}))
        stale = {"prompt_id": "api_todo_jwt", "config": "no_review", "repeat": 0, "completed": False,
                 "error": "LLM call failed (x): model_not_found", "tests_passed_count": 0,
                 "tests_failed_count": 0, "tests_error_count": 0}
        (out / "results.jsonl").write_text(json.dumps(stale) + "\n")
        main(["--demo", "--limit", "1", "--configs", "no_review", "--out", str(out)])
        assert len((out / "results.jsonl").read_text().splitlines()) == 2   # re-ran, appended
        records = load_records(out)
        assert len(records) == 1 and records[0]["completed"]

    def test_resume_refuses_a_different_model(self, tmp_path):
        from evals.run_eval import main
        out = tmp_path / "run"
        out.mkdir()
        (out / "meta.json").write_text(json.dumps({"model": "groq/some-other-model"}))
        with pytest.raises(SystemExit):
            main(["--demo", "--out", str(out)])


class TestReportStats:
    def _records(self):
        rows = []
        for i, (a_bandit, b_bandit) in enumerate([(4, 1), (3, 3), (5, 2), (2, 0)]):
            for config, bandit in (("llm_review", a_bandit), ("grounded_review", b_bandit)):
                rows.append({"prompt_id": f"p{i}", "repeat": 0, "config": config, "completed": True,
                             "bandit_hm": bandit, "test_pass_rate": 1.0})
        return rows

    def test_paired_comparison_counts_and_direction(self):
        from evals.report import paired
        res = paired(self._records(), "llm_review", "grounded_review", "bandit_hm", higher_is_better=False)
        assert res["n"] == 4
        assert res["mean_diff"] == pytest.approx(-2.0)
        assert (res["better"], res["tie"], res["worse"]) == (3, 1, 0)
        assert res["ci"][1] <= 0

    def test_bootstrap_is_deterministic(self):
        from evals.report import bootstrap_ci
        assert bootstrap_ci([1.0, -2.0, 3.0, 0.5]) == bootstrap_ci([1.0, -2.0, 3.0, 0.5])
        assert bootstrap_ci([1.0]) is None


class TestEndToEnd:
    def test_demo_eval_writes_results_report_and_resumes(self, tmp_path):
        from evals.run_eval import main
        out = tmp_path / "run"
        main(["--demo", "--limit", "1", "--configs", "no_review,full", "--out", str(out)])

        records = [json.loads(l) for l in (out / "results.jsonl").read_text().splitlines()]
        assert [r["config"] for r in records] == ["no_review", "full"]
        assert all(r["completed"] and r["tests_all_passed"] and r["llm_calls"] > 0 for r in records)
        assert records[0]["review_iterations"] == 0
        trace = json.loads((out / records[0]["trace"]).read_text())
        assert trace["llm_calls"] and trace["llm_calls"][0]["stage"] == "planning"
        assert trace["final_files"] and any(e["type"] == "complete" for e in trace["events"])
        report = (out / "report.md").read_text()
        assert "## Results by configuration" in report and "`full`" in report

        # Re-running the same command resumes: nothing new is executed
        main(["--demo", "--limit", "1", "--configs", "no_review,full", "--out", str(out)])
        assert len((out / "results.jsonl").read_text().splitlines()) == 2
