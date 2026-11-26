"""Evaluation harness — run the pipeline over a prompt set under ablation configs.

Every number is measured by the harness on the final generated code, never
taken from an agent's own claims:
  - pipeline completion
  - syntax validity of every generated .py file (ast.parse)
  - generated test results (pytest exit code + parsed pass/fail counts)
  - bandit findings by severity and flake8 findings on non-test source files
    (flake8 F-codes are pyflakes checks: undefined names, unused imports, ...)
  - LLM calls, tokens, list-price cost and wall-clock time

Configs are interleaved per prompt, so even a partial run yields paired
comparisons. Results are appended to results.jsonl after every run; a run
stopped by a daily quota can be resumed with --out <same dir>.

Usage:
    python -m evals.run_eval --model groq/openai/gpt-oss-120b      # key from .env
    python -m evals.run_eval --demo --limit 2                  # smoke test, no API key
    python -m evals.run_eval --out evals/results/<run> ...     # resume
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_PROMPTS = Path(__file__).resolve().parent / "prompts.json"


@dataclass(frozen=True)
class EvalConfig:
    name: str
    description: str
    max_review_iterations: int
    use_static_analysis: bool
    max_test_fix_iterations: int
    use_memory: bool = False


# Each config adds exactly one component to the one it is compared with (see report.COMPARISONS)
CONFIGS: dict[str, EvalConfig] = {c.name: c for c in [
    EvalConfig("no_review", "Coder output goes straight to tests", 0, False, 1),
    EvalConfig("llm_review", "+ Reviewer/Improver loop, LLM judgement only", 2, False, 1),
    EvalConfig("grounded_review", "+ Reviewer grounded in bandit/flake8/pip-audit output", 2, True, 1),
    EvalConfig("full", "+ test-fix loop (production config)", 2, True, 2),
    EvalConfig("grounded_memory", "grounded_review + retrieval of issues from similar past projects "
               "(memory builds up across prompts in run order)", 2, True, 1, use_memory=True),
]}
# grounded_memory is opt-in: its memory needs earlier prompts to learn from
DEFAULT_CONFIGS = ["no_review", "llm_review", "grounded_review", "full"]


class StopEval(RuntimeError):
    """An infrastructure problem (quota, bad model/key, outage) — stop without recording."""


# Errors that say nothing about the pipeline's quality: recording them as
# failed runs would corrupt the results, so the eval stops instead.
_INFRA_ERROR_MARKERS = (
    "daily api quota exhausted", "model_not_found", "does not exist or you do not have access",
    "invalid api key", "invalid_api_key", "authentication", "permission denied", "unauthorized",
    "llm call failed after",  # retries exhausted on rate limits / provider outage
    "request too large",      # a hard per-request cap of this model/tier
)


def is_infra_error(error: str | None) -> bool:
    return bool(error) and any(m in error.lower() for m in _INFRA_ERROR_MARKERS)


# ── Measurement ───────────────────────────────────────────────

def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return (p.startswith("tests/") or "/tests/" in p or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except (SyntaxError, ValueError):
        return False


def measure_codebase(files: list[dict]) -> dict:
    """Independent static measurements of a generated codebase."""
    from src.core.static_analysis import AnalysisReport, analyzer

    py_files = [f for f in files if f["file_path"].endswith(".py")]
    source = {f["file_path"]: f["content"] for f in py_files if not is_test_path(f["file_path"])}
    report = analyzer.run_code_review(source) if source else AnalysisReport()

    severities = Counter(str(i.get("issue_severity", "")).upper() for i in report.security_issues)
    flake8_codes = [line.split(":", 3)[3].strip().split(" ", 1)[0]
                    for line in report.style_issues if line.count(":") >= 3]
    return {
        "files": len(files),
        "py_files": len(py_files),
        "test_files": sum(is_test_path(f["file_path"]) for f in py_files),
        "syntax_error_files": sum(not _parses(f["content"]) for f in py_files),
        "source_loc": sum(c.count("\n") + 1 for c in source.values()),
        "bandit_high": severities["HIGH"],
        "bandit_medium": severities["MEDIUM"],
        "bandit_low": severities["LOW"],
        "flake8_total": len(flake8_codes),
        "flake8_pyflakes": sum(c.startswith("F") for c in flake8_codes),
    }


def parse_pytest(output: str) -> dict:
    """Pass/fail/error counts from pytest's summary line."""
    from src.tools.code_executor import pytest_counts
    c = pytest_counts(output)
    return {"tests_passed_count": c["passed"], "tests_failed_count": c["failed"],
            "tests_error_count": c["errors"]}


# ── Running ───────────────────────────────────────────────────

def run_one(prompt: dict, config: EvalConfig, repeat: int, model: str, api_key: str, demo: bool,
            trace_dir: Path | None = None) -> dict:
    from src.core.pipeline import Pipeline

    record = {
        "prompt_id": prompt["id"], "category": prompt.get("category", ""),
        "difficulty": prompt.get("difficulty", ""), "config": config.name, "repeat": repeat,
        "model": "demo" if demo else model,
    }
    events: list[dict] = []
    pipeline = Pipeline(
        model=model, api_key=api_key, demo=demo, event_callback=events.append,
        max_review_iterations=config.max_review_iterations,
        max_test_fix_iterations=config.max_test_fix_iterations,
        use_static_analysis=config.use_static_analysis,
        use_memory=config.use_memory,
    )
    pipeline.transcript = []  # raw responses, saved below so odd results can be traced
    t0 = time.time()
    try:
        pipeline.run(prompt["prompt"])
        record.update(completed=True, error=None)
    except Exception as e:
        if is_infra_error(str(e)):
            raise StopEval(str(e)) from e
        # Keep where it failed, so pipeline bugs can be told apart from model failures
        record.update(completed=False, error=str(e)[:500], traceback=traceback.format_exc()[-2000:])
    record["duration_s"] = round(time.time() - t0, 1)
    record.update(pipeline.usage)

    state = pipeline.state
    files = ([{"file_path": f.file_path, "content": f.content} for f in state.codebase.files]
             if state and state.codebase else [])
    record.update(measure_codebase(files))

    result = state.test_result if state else None
    record["tests_all_passed"] = bool(result and result.passed)
    record.update(parse_pytest(result.stdout if result else ""))
    # False = pytest couldn't even collect the tests (e.g. the app fails to import)
    record["tests_collected"] = (record["tests_passed_count"] + record["tests_failed_count"]
                                 + record["tests_error_count"]) > 0

    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace = trace_dir / f"{prompt['id']}__{config.name}__r{repeat}.json"
        trace.write_text(json.dumps({
            "events": events,
            "llm_calls": pipeline.transcript,
            "final_files": [f["file_path"] for f in files],
            "test_output": (result.stdout[-6000:] if result else ""),
        }, indent=1), encoding="utf-8")
        record["trace"] = str(trace.relative_to(trace_dir.parent))

    metrics = next((e["metrics"] for e in events if e.get("type") == "complete"), {})
    record["review_iterations"] = metrics.get("review_iterations", state.review_iterations if state else 0)
    record["verified_fixes"] = metrics.get("issues_fixed") if metrics.get("static_issues_initial") is not None else None
    return record


def preflight(model: str, api_key: str):
    """One tiny call to check the model name and key before spending hours on runs."""
    from src.core.pipeline import Pipeline
    try:
        Pipeline(model=model, api_key=api_key)._call_llm(
            "You are a connectivity check.", 'Reply with exactly: {"ok": true}', max_retries=2)
    except Exception as e:
        raise StopEval(f"Pre-flight call to {model} failed — fix this before running the eval:\n{e}") from e


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="groq/openai/gpt-oss-120b", help="LiteLLM model string")
    parser.add_argument("--api-key", default="", help="defaults to the provider's env var / .env")
    parser.add_argument("--demo", action="store_true", help="use the mock LLM (smoke test)")
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                        help="comma-separated, from: " + ", ".join(CONFIGS) + " (default: all but grounded_memory)")
    parser.add_argument("--limit", type=int, default=0, help="only the first N prompts")
    parser.add_argument("--repeats", type=int, default=1, help="runs per (prompt, config) — LLMs are stochastic")
    parser.add_argument("--out", type=Path, default=None, help="results dir (existing dir = resume)")
    parser.add_argument("--verbose", action="store_true", help="show the pipeline's console output")
    args = parser.parse_args(argv)

    configs = [CONFIGS[name.strip()] for name in args.configs.split(",") if name.strip()]
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    if args.limit:
        prompts = prompts[: args.limit]

    model_label = "demo" if args.demo else args.model
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_label)
    out = args.out or ROOT / "evals" / "results" / f"{datetime.now():%Y%m%d_%H%M%S}_{tag}"
    meta_path = out / "meta.json"
    if meta_path.exists():
        previous = json.loads(meta_path.read_text(encoding="utf-8")).get("model")
        if previous != model_label:
            sys.exit(f"{out} holds results for {previous!r}, not {model_label!r} — "
                     "use a new --out so models aren't mixed in one report.")

    # Without --api-key, litellm reads the provider's env var (GROQ_API_KEY, GEMINI_API_KEY, ...)
    load_dotenv(ROOT / ".env")
    api_key = args.api_key
    if not args.demo:
        try:
            preflight(args.model, api_key)
        except StopEval as e:
            sys.exit(f"🛑 {e}")
    out.mkdir(parents=True, exist_ok=True)

    from src.core.config import settings
    from src.core import pipeline as pipeline_module
    from src.tools import file_writer
    from src.core.static_analysis import analyzer

    settings.enable_memory = False            # only configs with use_memory=True get memory
    settings.output_dir = str(out / "projects")  # also isolates this eval's memory store
    pipeline_module.console.quiet = not args.verbose
    file_writer.console.quiet = not args.verbose
    if not all(analyzer.tools_available[t] for t in ("bandit", "flake8")):
        msg = "bandit/flake8 not installed — install with: pip install -e '.[analysis]'"
        if not args.demo:
            # Static metrics would all read 0 and grounded_review would silently equal llm_review
            sys.exit(f"🛑 {msg}")
        print(f"⚠ {msg}")

    results_path = out / "results.jsonl"
    done = set()
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                # Infra failures from older harness versions are retried, not kept
                if not is_infra_error(r.get("error")):
                    done.add((r["prompt_id"], r["config"], r["repeat"]))
    if not meta_path.exists():
        meta_path.write_text(json.dumps({
            "model": model_label,
            "configs": [asdict(c) for c in configs],
            "prompts_file": str(args.prompts), "n_prompts": len(prompts), "repeats": args.repeats,
            "git_commit": _git_commit(), "started_at": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")

    jobs = [(r, p, c) for r in range(args.repeats) for p in prompts for c in configs]
    todo = [j for j in jobs if (j[1]["id"], j[2].name, j[0]) not in done]
    print(f"📊 {len(jobs)} runs ({len(prompts)} prompts × {len(configs)} configs × {args.repeats}), "
          f"{len(jobs) - len(todo)} already done → {out}")

    try:
        for i, (repeat, prompt, config) in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {prompt['id']} · {config.name} · repeat {repeat} ...", end=" ", flush=True)
            record = run_one(prompt, config, repeat, args.model, api_key, args.demo, trace_dir=out / "runs")
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            status = "✓" if record["completed"] else f"✗ {record['error'][:80]}"
            print(f"{status} tests={'pass' if record['tests_all_passed'] else 'fail'} "
                  f"bandit H/M={record['bandit_high']}/{record['bandit_medium']} {record['duration_s']}s")
    except StopEval as e:
        print(f"\n🛑 Stopped (nothing recorded for this run): {e}\n"
              f"Resume later with: python -m evals.run_eval --out {out} [same options]")
    except KeyboardInterrupt:
        print(f"\n⏸ Interrupted. Resume with: python -m evals.run_eval --out {out} [same options]")

    from evals.report import write_report
    report_path = write_report(out)
    print(f"📄 Report: {report_path}")
    return out


if __name__ == "__main__":
    main()
