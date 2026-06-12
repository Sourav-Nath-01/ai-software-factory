"""Pipeline — orchestration engine using direct LiteLLM calls.

Bypasses CrewAI to work reliably with ALL providers:
  Gemini (free), Groq (free), OpenAI (paid), Ollama (local)

Workflow:
  User Request → Plan → Code → Review → Improve (loop) → Test → Fix (loop) → Deploy

Events emitted via event_callback (used by WebSocket streaming):
  {"type": "stage_start",    "stage": str, "icon": str}
  {"type": "log",            "message": str}
  {"type": "stage_complete", "stage": str, "duration": float, "data": dict}
  {"type": "complete",       "metrics": dict}
  {"type": "error",          "message": str}
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

# Agent prompt strings (no CrewAI dependency needed at runtime)
from src.agents.planner  import PLANNER_BACKSTORY,     PLAN_TASK_DESCRIPTION
from src.agents.coder    import CODER_BACKSTORY,       CODE_TASK_DESCRIPTION
from src.agents.reviewer import REVIEWER_BACKSTORY,    REVIEW_TASK_DESCRIPTION
from src.agents.improver import IMPROVER_BACKSTORY,    IMPROVE_TASK_DESCRIPTION
from src.agents.tester   import TESTER_BACKSTORY,      TEST_TASK_DESCRIPTION
from src.agents.test_runner import TEST_RUNNER_BACKSTORY, TEST_RUNNER_TASK_DESCRIPTION
from src.agents.deployer import DEPLOYER_BACKSTORY,    DEPLOY_TASK_DESCRIPTION

from src.core.config import settings
from src.core.models import (
    CodeBase, DeploymentArtifact, PipelineStage, PipelineState,
    ProjectPlan, ReviewReport, TestResult,
)
from src.core.sandbox import run_sandboxed
from src.core.memory import coder_memory, reviewer_memory
from src.tools.file_writer import write_project_files

console = Console()

STAGES = [
    ("1", "Planning",         "🏗️"),
    ("2", "Code Generation",  "💻"),
    ("3", "Code Review",      "🔍"),
    ("4", "Code Improvement", "✨"),
    ("5", "Test Generation",  "🧪"),
    ("6", "Test Execution",   "▶️"),
    ("7", "Deployment",       "🚀"),
]


# ── JSON helpers ──────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    
    # Try finding the first '{' and last '}'
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start: end + 1]
    
    import json_repair
    # json_repair automatically fixes unescaped quotes, trailing commas, missing brackets, etc.
    result = json_repair.loads(text)
    if isinstance(result, dict):
        return result
    return json.loads(text)  # Fallback to standard error if not a dict


def _safe_json(text: str, fallback: dict | None = None) -> dict:
    try:
        return _extract_json(text)
    except Exception as e:
        console.print(f"  [yellow]⚠ JSON parse error: {e}[/yellow]")
        return fallback or {}


# ── Pipeline ──────────────────────────────────────────────────

class Pipeline:
    """Orchestrates the full software development pipeline using direct LiteLLM calls."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        demo: bool = False,
        event_callback: Optional[Callable[[dict], None]] = None,
        # legacy param kept for backward compat (ignored when model/api_key used)
        llm=None,
    ):
        self.demo = demo
        self.event_callback = event_callback
        self._model = model
        self._api_key = api_key
        self._mock_llm = None

        if demo:
            from src.core.mock_llm import MockLLM
            self._mock_llm = MockLLM()

        self.state: PipelineState | None = None
        self._start_time: datetime | None = None
        self._output_dir: Path | None = None
        self._issues_found = 0
        self._issues_fixed = 0

    # ── Core LLM call (bypasses CrewAI) ──────────────────────

    def _call_llm(self, system: str, user: str, max_retries: int = 4) -> str:
        """Make a direct LiteLLM call with automatic rate-limit retry."""
        if self.demo:
            import time as _time, random
            _time.sleep(random.uniform(0.5, 1.0))
            return self._mock_llm.call(system=system)

        import os, re, time as _time
        try:
            import litellm  # type: ignore
            litellm.drop_params = True
        except ImportError as e:
            raise RuntimeError("litellm not installed. Run: pip install litellm") from e

        # Set API key via env var (most reliable for all providers)
        key   = self._api_key or ""
        model = self._model
        is_gemini = model.startswith("gemini/")
        is_groq   = model.startswith("groq/")

        if is_gemini:
            os.environ["GEMINI_API_KEY"] = key
            os.environ["GOOGLE_API_KEY"] = key
        elif is_groq:
            os.environ["GROQ_API_KEY"] = key
        elif model.startswith("openai/") or "/" not in model:
            os.environ["OPENAI_API_KEY"] = key

        # Gemini free tier = 15 RPM → add a small gap between every call
        # 4s ensures we never exceed 15 req/min even in fast pipelines
        if is_gemini:
            _time.sleep(4)

        last_exc = None
        for attempt in range(max_retries):
            try:
                response = litellm.completion(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    temperature=0.2,
                    max_tokens=4096,
                )
                return response.choices[0].message.content or ""

            except Exception as e:
                last_exc = e
                err_str   = str(e)
                err_lower = err_str.lower()

                # ── Detect rate-limit / quota errors ─────────────────────────────
                # Groq:   "rate_limit_exceeded", RateLimitError
                # Gemini: "RESOURCE_EXHAUSTED", "quota exceeded", 429
                is_rate_limit = (
                    "rate_limit"         in err_lower or
                    "ratelimiterror"     in err_lower or
                    "resource_exhausted" in err_lower or
                    "resourceexhausted"  in err_lower or
                    "quota"              in err_lower or
                    "429"                in err_str
                )

                if is_rate_limit:
                    # Daily quota exhausted — retrying won't help
                    if any(w in err_lower for w in ("per_day", "daily", "day quota", "requests per day")):
                        raise RuntimeError(
                            f"Daily API quota exhausted for {model}. "
                            "Free tier allows ~200 requests/day. "
                            "Wait until midnight UTC, or switch to Groq (llama-3.1-8b-instant) which resets hourly."
                        ) from e

                    # Per-minute rate limit — wait and retry
                    wait_match = re.search(r'(?:try again in|retry after|wait)[^\d]*(\d+\.?\d*)\s*s', err_lower)
                    wait_secs  = float(wait_match.group(1)) + 3 if wait_match else (20 * (attempt + 1))
                    msg = f"⏳ Rate limit — waiting {wait_secs:.0f}s then retrying ({attempt+1}/{max_retries})..."
                    self._log(msg)
                    self._emit({"type": "log", "message": msg})
                    _time.sleep(wait_secs)
                    continue

                # Any other error — fail immediately
                raise RuntimeError(f"LLM call failed ({model}): {e}") from e

        raise RuntimeError(
            f"LLM call failed after {max_retries} retries ({model}). "
            f"Last error: {last_exc}"
        )

    @staticmethod
    def _truncate(text: str, max_chars: int = 8000) -> str:
        """Truncate large text to stay within token limits."""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + '\n... [truncated for token limit] ...'

    # ── Event helpers ─────────────────────────────────────────

    def _emit(self, event: dict):
        if self.event_callback:
            try:
                self.event_callback(event)
            except Exception:
                pass

    def _log(self, message: str):
        self._emit({"type": "log", "message": message})

    # ── Public run ────────────────────────────────────────────

    def run(self, user_request: str) -> PipelineState:
        self._start_time = datetime.now()
        self.state = PipelineState(user_request=user_request)
        self._print_header(user_request)
        self._emit({"type": "pipeline_start", "prompt": user_request})

        try:
            self._run_planning()
            self._run_coding()
            self._run_review_improve_loop()
            self._run_testing()
            self._run_test_fix_loop()
            self._run_deployment()
            self._write_output()
            self.state.current_stage = PipelineStage.COMPLETE
            self._print_summary()

            duration = (datetime.now() - self._start_time).total_seconds()
            loc = sum(f.content.count("\n") + 1 for f in self.state.codebase.files)
            metrics = {
                "files_generated":  len(self.state.codebase.files),
                "lines_of_code":    loc,
                "review_iterations":   self.state.review_iterations,
                "test_fix_iterations": self.state.test_fix_iterations,
                "issues_found": self._issues_found,
                "issues_fixed": self._issues_fixed,
                "tests_passed": bool(self.state.test_result and self.state.test_result.passed),
                "duration_seconds": round(duration, 1),
            }
            self._emit({"type": "complete", "metrics": metrics})

            if self.state.codebase and self.state.codebase.files:
                coder_memory.store(
                    self.state.codebase.summary(),
                    {"prompt": user_request, "tech": ",".join(self.state.plan.tech_stack)},
                )

        except Exception as e:
            self.state.current_stage = PipelineStage.FAILED
            self.state.errors.append(str(e))
            self._emit({"type": "error", "message": str(e)})
            console.print(Panel(f"[red bold]Pipeline Failed[/red bold]\n\n{e}", border_style="red"))
            raise

        return self.state

    # ── Stages ───────────────────────────────────────────────

    def _run_planning(self):
        t0 = time.time()
        self.state.current_stage = PipelineStage.PLANNING
        self._print_stage(0, "Architect agent is designing your system...")
        self._emit({"type": "stage_start", "stage": "Planning", "icon": "🏗️"})

        with console.status("[bold cyan]  Architect agent thinking...[/bold cyan]", spinner="dots"):
            result = self._call_llm(
                system=PLANNER_BACKSTORY,
                user=PLAN_TASK_DESCRIPTION.format(user_request=self.state.user_request),
            )

        data = _safe_json(result, {"project_name": "project", "tech_stack": [], "file_structure": [], "modules": []})

        # Normalize modules: LLMs sometimes return dicts instead of strings
        raw_modules = data.get("modules", [])
        modules: list[str] = []
        for m in raw_modules:
            if isinstance(m, str):
                modules.append(m)
            elif isinstance(m, dict):
                name = m.get("name", m.get("module", ""))
                resp = m.get("responsibility", m.get("description", m.get("desc", "")))
                modules.append(f"{name}: {resp}" if resp else name)

        # Normalize endpoints
        raw_endpoints = data.get("endpoints", [])
        endpoints: list[dict] = []
        for e in (raw_endpoints if isinstance(raw_endpoints, list) else []):
            if isinstance(e, dict):
                endpoints.append({
                    "method":      e.get("method", "GET"),
                    "path":        e.get("path", "/"),
                    "description": e.get("description", e.get("desc", "")),
                })

        # Normalize file_structure: should be list of strings
        raw_files = data.get("file_structure", [])
        file_structure: list[str] = []
        for f in (raw_files if isinstance(raw_files, list) else []):
            if isinstance(f, str):
                file_structure.append(f)
            elif isinstance(f, dict):
                file_structure.append(f.get("path", f.get("file", str(f))))

        self.state.plan = ProjectPlan(
            project_name=data.get("project_name", "project"),
            description=data.get("description", ""),
            tech_stack=[str(t) for t in data.get("tech_stack", [])],
            file_structure=file_structure,
            modules=modules,
            endpoints=endpoints,
            additional_notes=str(data.get("additional_notes", "")),
        )


        self._print_plan_summary()
        self._log(f"Architecture planned: {len(self.state.plan.file_structure)} files, stack: {', '.join(self.state.plan.tech_stack)}")
        self._emit({"type": "stage_complete", "stage": "Planning", "icon": "🏗️",
                    "duration": round(time.time() - t0, 1),
                    "data": {"tech_stack": ", ".join(self.state.plan.tech_stack[:3])}})

    def _run_coding(self):
        t0 = time.time()
        self.state.current_stage = PipelineStage.CODING
        self._print_stage(1, "Engineer agent is writing code...")
        self._emit({"type": "stage_start", "stage": "Code Generation", "icon": "💻"})

        with console.status("[bold cyan]  Engineer agent coding...[/bold cyan]", spinner="dots"):
            result = self._call_llm(
                system=CODER_BACKSTORY,
                user=CODE_TASK_DESCRIPTION.format(plan=self.state.plan.model_dump_json(indent=2)),
            )

        data = _safe_json(result, {"files": []})
        self.state.codebase = CodeBase()
        for f in data.get("files", []):
            self.state.codebase.set_file(f.get("file_path", "unknown.py"), f.get("content", ""), f.get("language", "python"))

        count = len(self.state.codebase.files)
        console.print(f"  [green]✓ Generated {count} files[/green]")
        self._print_file_tree("Generated Files", self.state.codebase)
        self._log(f"Generated {count} source files")
        self._emit({"type": "stage_complete", "stage": "Code Generation", "icon": "💻",
                    "duration": round(time.time() - t0, 1), "data": {"files": count}})

    def _run_review_improve_loop(self):
        for iteration in range(1, settings.max_review_iterations + 1):
            t0 = time.time()
            self.state.current_stage = PipelineStage.REVIEWING
            self._print_stage(2, f"Reviewer agent checking code (iteration {iteration}/{settings.max_review_iterations})...")
            self._emit({"type": "stage_start", "stage": "Code Review", "icon": "🔍", "meta": f"Iteration {iteration}"})

            codebase_json = self._truncate(self.state.codebase.model_dump_json(indent=2))
            with console.status("[bold cyan]  Reviewer agent analyzing...[/bold cyan]", spinner="dots"):
                result = self._call_llm(
                    system=REVIEWER_BACKSTORY,
                    user=REVIEW_TASK_DESCRIPTION.format(codebase=codebase_json),
                )

            data = _safe_json(result, {"comments": [], "overall_quality": "good", "summary": ""})

            # Normalize comments: some models return strings or partial objects
            raw_comments = data.get("comments", [])
            comments = []
            for c in (raw_comments if isinstance(raw_comments, list) else []):
                if isinstance(c, str):
                    comments.append({"file_path": "general", "severity": "info",
                                     "category": "style", "description": c, "suggestion": c})
                elif isinstance(c, dict):
                    comments.append({
                        "file_path":   c.get("file_path", c.get("file", "general")),
                        "severity":    c.get("severity", "info"),
                        "category":    c.get("category", "style"),
                        "description": c.get("description", c.get("issue", str(c))),
                        "suggestion":  c.get("suggestion", c.get("fix", "")),
                        "line_number": c.get("line_number"),
                    })

            # Clamp overall_quality to valid values
            valid_qualities = {"excellent", "good", "needs_improvement", "poor"}
            quality = str(data.get("overall_quality", "good")).lower().replace(" ", "_")
            if quality not in valid_qualities:
                quality = "needs_improvement"

            self.state.review_report = ReviewReport(
                comments=comments,
                overall_quality=quality,
                summary=str(data.get("summary", "")),
            )
            self.state.review_iterations = iteration

            self._issues_found += self.state.review_report.issue_count
            self._print_review_summary()

            if data.get("summary"):
                reviewer_memory.store(data["summary"], {"quality": data.get("overall_quality", ""), "iteration": iteration})

            self._emit({"type": "stage_complete", "stage": "Code Review", "icon": "🔍",
                        "duration": round(time.time() - t0, 1),
                        "data": {"issues": self.state.review_report.issue_count,
                                 "quality": self.state.review_report.overall_quality}})

            if (self.state.review_report.overall_quality in ("excellent", "good")
                    and not self.state.review_report.has_critical_issues):
                console.print("  [green]✓ Code quality satisfactory, skipping further iterations.[/green]")
                self._log("Code quality satisfactory — skipping further review iterations")
                break

            # Improve
            t1 = time.time()
            self.state.current_stage = PipelineStage.IMPROVING
            self._print_stage(3, f"Improver agent fixing {self.state.review_report.issue_count} issues...")
            self._emit({"type": "stage_start", "stage": "Code Improvement", "icon": "✨"})

            review_json = self._truncate(self.state.review_report.model_dump_json(indent=2), max_chars=3000)
            with console.status("[bold cyan]  Improver agent fixing code...[/bold cyan]", spinner="dots"):
                result = self._call_llm(
                    system=IMPROVER_BACKSTORY,
                    user=IMPROVE_TASK_DESCRIPTION.format(
                        codebase=self._truncate(codebase_json),
                        review=review_json,
                    ),
                )

            data2 = _safe_json(result, {"files": []})
            improved = CodeBase()
            for f in data2.get("files", []):
                improved.set_file(f.get("file_path", "unknown.py"), f.get("content", ""), f.get("language", "python"))

            if improved.files:
                self.state.codebase = improved
                self._issues_fixed += self.state.review_report.issue_count
                console.print(f"  [green]✓ Improved {len(improved.files)} files[/green]")
                self._log(f"Fixed {self.state.review_report.issue_count} issues across {len(improved.files)} files")

            self._emit({"type": "stage_complete", "stage": "Code Improvement", "icon": "✨",
                        "duration": round(time.time() - t1, 1),
                        "data": {"fixed": self.state.review_report.issue_count}})

    def _run_testing(self):
        t0 = time.time()
        self.state.current_stage = PipelineStage.TESTING
        self._print_stage(4, "QA agent writing tests...")
        self._emit({"type": "stage_start", "stage": "Test Generation", "icon": "🧪"})

        with console.status("[bold cyan]  QA agent writing tests...[/bold cyan]", spinner="dots"):
            result = self._call_llm(
                system=TESTER_BACKSTORY,
                user=TEST_TASK_DESCRIPTION.format(
                    codebase=self._truncate(self.state.codebase.model_dump_json(indent=2)),
                    plan=self._truncate(self.state.plan.model_dump_json(indent=2), max_chars=2000),
                ),
            )

        data = _safe_json(result, {"files": []})
        test_count = 0
        for f in data.get("files", []):
            self.state.codebase.set_file(f.get("file_path", ""), f.get("content", ""), f.get("language", "python"))
            test_count += 1

        console.print(f"  [green]✓ Generated {test_count} test files[/green]")
        self._log(f"Generated {test_count} test files")
        self._emit({"type": "stage_complete", "stage": "Test Generation", "icon": "🧪",
                    "duration": round(time.time() - t0, 1), "data": {"test_files": test_count}})

    def _run_test_fix_loop(self):
        for iteration in range(1, settings.max_test_fix_iterations + 1):
            t0 = time.time()
            self.state.current_stage = PipelineStage.TEST_RUNNING
            self._print_stage(5, f"Running tests (attempt {iteration}/{settings.max_test_fix_iterations})...")
            self._emit({"type": "stage_start", "stage": "Test Execution", "icon": "▶️", "meta": f"Attempt {iteration}"})

            output_dir = settings.output_path / "_test_run"
            files = [{"file_path": f.file_path, "content": f.content} for f in self.state.codebase.files]
            write_project_files(files, output_dir)

            with console.status("[bold cyan]  Executing tests...[/bold cyan]", spinner="dots"):
                exec_result = run_sandboxed(["python3", "-m", "pytest", "tests/", "-v", "--tb=short"], cwd=output_dir, timeout=60)

            if exec_result.success:
                console.print("  [green]✓ All tests passed![/green]")
                self._log("All tests passed!")
                self.state.test_result = TestResult(passed=True, stdout=exec_result.stdout, stderr=exec_result.stderr, failure_analysis="All tests passed.")
                self._emit({"type": "stage_complete", "stage": "Test Execution", "icon": "▶️",
                            "duration": round(time.time() - t0, 1), "data": {"passed": True}})
                break

            console.print("  [yellow]⚠ Tests failed. Analyzing...[/yellow]")
            self._log("Test failures detected — invoking QA analyst")
            test_output = f"STDOUT:\n{exec_result.stdout}\n\nSTDERR:\n{exec_result.stderr}"

            with console.status("[bold cyan]  QA analyst investigating failures...[/bold cyan]", spinner="dots"):
                runner_result = self._call_llm(
                    system=TEST_RUNNER_BACKSTORY,
                    user=TEST_RUNNER_TASK_DESCRIPTION.format(
                        test_output=test_output,
                        codebase=self.state.codebase.model_dump_json(indent=2),
                    ),
                )

            data = _safe_json(runner_result, {"passed": False})
            self.state.test_result = TestResult(
                passed=data.get("passed", False),
                total_tests=data.get("total_tests", 0),
                passed_tests=data.get("passed_tests", 0),
                failed_tests=data.get("failed_tests", 0),
                stdout=exec_result.stdout,
                stderr=exec_result.stderr,
                failure_analysis=data.get("failure_analysis", ""),
            )
            self.state.test_fix_iterations = iteration
            self._emit({"type": "stage_complete", "stage": "Test Execution", "icon": "▶️",
                        "duration": round(time.time() - t0, 1),
                        "data": {"passed": False, "failed": data.get("failed_tests", 0)}})

            if self.state.test_result.passed:
                break

            if iteration < settings.max_test_fix_iterations:
                console.print("  [cyan]→ Sending back to Improver agent...[/cyan]")
                codebase_json = self.state.codebase.model_dump_json(indent=2)
                fix_review = json.dumps({
                    "comments": [{"file_path": "tests/", "severity": "critical", "category": "bug",
                                  "description": self.state.test_result.failure_analysis,
                                  "suggestion": "Fix the failing code or tests."}],
                    "overall_quality": "needs_improvement",
                    "summary": f"Test failures: {self.state.test_result.failed_tests} tests failing.",
                })
                with console.status("[bold cyan]  Improver fixing test failures...[/bold cyan]", spinner="dots"):
                    fix_result = self._call_llm(
                        system=IMPROVER_BACKSTORY,
                        user=IMPROVE_TASK_DESCRIPTION.format(codebase=codebase_json, review=fix_review),
                    )
                fix_data = _safe_json(fix_result, {"files": []})
                for f in fix_data.get("files", []):
                    self.state.codebase.set_file(f.get("file_path", "unknown.py"), f.get("content", ""), f.get("language", "python"))

    def _run_deployment(self):
        t0 = time.time()
        self.state.current_stage = PipelineStage.DEPLOYING
        self._print_stage(6, "DevOps agent creating deployment configs...")
        self._emit({"type": "stage_start", "stage": "Deployment", "icon": "🚀"})

        with console.status("[bold cyan]  DevOps agent building infrastructure...[/bold cyan]", spinner="dots"):
            result = self._call_llm(
                system=DEPLOYER_BACKSTORY,
                user=DEPLOY_TASK_DESCRIPTION.format(
                    plan=self.state.plan.model_dump_json(indent=2),
                    codebase=self.state.codebase.model_dump_json(indent=2),
                ),
            )

        data = _safe_json(result, {"files": [], "instructions": ""})
        for f in data.get("files", []):
            self.state.codebase.set_file(f.get("file_path", ""), f.get("content", ""), f.get("language", "yaml"))
        self.state.deployment = DeploymentArtifact(files=data.get("files", []), instructions=data.get("instructions", ""))

        count = len(data.get("files", []))
        console.print(f"  [green]✓ Generated {count} deployment files[/green]")
        self._log(f"Generated {count} deployment files (Dockerfile, CI/CD, compose)")
        self._emit({"type": "stage_complete", "stage": "Deployment", "icon": "🚀",
                    "duration": round(time.time() - t0, 1), "data": {"deploy_files": count}})

    # ── Output ───────────────────────────────────────────────

    def _write_output(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        project_name = self.state.plan.project_name.lower().replace(" ", "_")
        self._output_dir = settings.output_path / f"{project_name}_{timestamp}"
        console.print(f"\n[bold]📁 Writing project to:[/bold] [cyan]{self._output_dir}[/cyan]")
        files = [{"file_path": f.file_path, "content": f.content} for f in self.state.codebase.files]
        write_project_files(files, self._output_dir)
        if self.state.deployment and self.state.deployment.instructions:
            (self._output_dir / "DEPLOYMENT.md").write_text(self.state.deployment.instructions, encoding="utf-8")

    # ── Display helpers ───────────────────────────────────────

    def _print_header(self, request: str):
        console.print()
        stages_text = "  ".join(f"[dim]{icon} {name}[/dim]" for _, name, icon in STAGES)
        console.print(Panel(
            f"[bold white]{request}[/bold white]\n\n[dim]Pipeline:[/dim] {stages_text}",
            title="[bold cyan]🏭 AI Software Factory[/bold cyan]",
            subtitle=f"[dim]Direct LiteLLM · {self._model if not self.demo else 'Demo Mode'}[/dim]",
            border_style="cyan", padding=(1, 2),
        ))
        console.print()

    def _print_stage(self, idx: int, description: str):
        num, name, icon = STAGES[idx]
        console.print(f"\n[bold blue]{icon} [{num}/7] {name}[/bold blue]")
        console.print(f"  [dim]{description}[/dim]")

    def _print_plan_summary(self):
        if not self.state.plan:
            return
        plan = self.state.plan
        table = Table(show_header=False, border_style="dim", padding=(0, 2))
        table.add_column("Key", style="bold cyan", width=14)
        table.add_column("Value")
        table.add_row("Project", plan.project_name)
        table.add_row("Description", plan.description[:120] + ("..." if len(plan.description) > 120 else ""))
        table.add_row("Stack", ", ".join(plan.tech_stack))
        table.add_row("Files", str(len(plan.file_structure)))
        if plan.endpoints:
            endpoints_str = ", ".join(f"{e.method} {e.path}" for e in plan.endpoints[:5])
            if len(plan.endpoints) > 5:
                endpoints_str += f" (+{len(plan.endpoints) - 5} more)"
            table.add_row("Endpoints", endpoints_str)
        console.print(table)
        console.print("  [green]✓ Architecture plan ready[/green]")

    def _print_file_tree(self, title: str, codebase: CodeBase):
        tree = Tree(f"[bold]{title}[/bold]", guide_style="dim")
        dirs: dict[str, Tree] = {}
        for f in sorted(codebase.files, key=lambda x: x.file_path):
            parts = f.file_path.split("/")
            parent = tree
            for i, part in enumerate(parts[:-1]):
                dir_key = "/".join(parts[: i + 1])
                if dir_key not in dirs:
                    dirs[dir_key] = parent.add(f"[bold blue]{part}/[/bold blue]")
                parent = dirs[dir_key]
            parent.add(f"[green]{parts[-1]}[/green] [dim]({f.content.count(chr(10)) + 1} lines)[/dim]")
        console.print(tree)

    def _print_review_summary(self):
        if not self.state.review_report:
            return
        report = self.state.review_report
        color_map = {"excellent": "green", "good": "green", "needs_improvement": "yellow", "poor": "red"}
        color = color_map.get(report.overall_quality, "white")
        console.print(f"  Quality: [{color}]{report.overall_quality}[/{color}]  |  Issues: {report.issue_count}")
        severity_colors = {"critical": "red", "warning": "yellow", "info": "dim"}
        severity_icons = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
        for comment in report.comments[:5]:
            sev = comment.severity if isinstance(comment.severity, str) else comment.severity.value
            c = severity_colors.get(sev, "white")
            icon = severity_icons.get(sev, "•")
            console.print(f"    {icon} [{c}]{comment.file_path}[/{c}]: {comment.description[:80]}")
        if report.issue_count - 5 > 0:
            console.print(f"    [dim]... and {report.issue_count - 5} more issues[/dim]")

    def _print_summary(self):
        elapsed = datetime.now() - self._start_time
        if self.state.codebase:
            self._print_file_tree("Final Project Files", self.state.codebase)
        console.print()
        table = Table(title="✅ Pipeline Complete", border_style="green", title_style="bold green", padding=(0, 2))
        table.add_column("Metric", style="bold")
        table.add_column("Value")
        table.add_row("Project", self.state.plan.project_name)
        table.add_row("Model", self._model if not self.demo else "Demo Mode")
        table.add_row("Files Generated", str(len(self.state.codebase.files)))
        table.add_row("Review Iterations", str(self.state.review_iterations))
        table.add_row("Test Fix Iterations", str(self.state.test_fix_iterations))
        table.add_row("Tests Passed",
                      "[green]Yes ✓[/green]" if (self.state.test_result and self.state.test_result.passed)
                      else "[yellow]No / Skipped[/yellow]")
        table.add_row("Duration", str(elapsed).split(".")[0])
        if self._output_dir:
            table.add_row("Output", str(self._output_dir))
        console.print(table)
        console.print()
