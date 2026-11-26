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
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

# Agent prompt strings
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
from src.core.memory import reviewer_memory
from src.core.static_analysis import analyzer
from src.core.dependency_check import as_context_string as dependency_context, check_requirements
from src.core.rate_limiter import estimate_tokens, get_rate_limiter
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

def _extract_json(text: str, truncated: bool = False) -> dict:
    text = text.strip()
    if truncated:
        # A cut-off response has no closing brace of its own; any '}' near the
        # end belongs to the half-written content. Repair from the first '{'.
        start = text.find("{")
        text = text[start:] if start != -1 else text
        import json_repair
        result = json_repair.loads(text)
        return result if isinstance(result, dict) else {}
    # Slice from the first '{' to the last '}'. This also strips ```json fences
    # and surrounding prose. Don't search for fences instead: generated file
    # contents (READMEs, docstrings) contain ``` blocks inside the JSON strings,
    # and matching one of those would throw away the whole response.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start: end + 1]
    
    import json_repair
    # json_repair automatically fixes unescaped quotes, trailing commas, missing brackets, etc.
    result = json_repair.loads(text)
    if isinstance(result, dict):
        return result
    return json.loads(text)  # Fallback to standard error if not a dict


def _safe_json(text: str, fallback: dict | None = None, truncated: bool = False) -> dict:
    try:
        return _extract_json(text, truncated=truncated)
    except Exception as e:
        console.print(f"  [yellow]⚠ JSON parse error: {e}[/yellow]")
        return fallback or {}


def _complete_file_entries(text: str) -> list:
    """Decode the complete objects of a (possibly cut-off) `"files": [...]` array, exactly.

    Uses the strict JSON decoder one entry at a time; the entry that was cut
    off fails to decode and ends the scan.
    """
    key = text.find('"files"')
    start = text.find("[", key) if key != -1 else -1
    if start == -1:
        return []
    decoder, pos, entries = json.JSONDecoder(), start + 1, []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break
        try:
            entry, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        entries.append(entry)
    return entries


def _valid_files(files, default_language: str = "python") -> list[dict]:
    """Keep only well-formed {"file_path", "content"} entries from an LLM's "files" list.

    Models occasionally return strings, nulls or a dict instead of a list —
    calling .get() on those crashed whole runs.
    """
    valid = []
    for f in files if isinstance(files, list) else []:
        if isinstance(f, dict) and isinstance(f.get("file_path"), str) and f["file_path"].strip():
            content = f.get("content")
            valid.append({
                "file_path": f["file_path"].strip(),
                "content": content if isinstance(content, str) else "",
                "language": str(f.get("language") or default_language),
            })
    return valid


_DAILY_QUOTA_MARKERS = (
    "per_day", "perday", "per day", "daily", "day quota", "(tpd)", "(rpd)",
)


def _parse_retry_after(text: str) -> float | None:
    """Seconds from a provider hint like "try again in 11m57.5s", "in 7.2s" or "in 450ms"."""
    m = re.search(r"(?:try again in|retry after|please wait)\s*((?:\d+(?:\.\d+)?\s*(?:ms|h|m|s)\s*)+)", text)
    if not m:
        return None
    units = {"ms": 0.001, "h": 3600, "m": 60, "s": 1}
    return sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", m.group(1)))


# Output token budget per call (shrunk automatically on low-TPM tiers)
MAX_OUTPUT_TOKENS = 16384

_TRANSIENT_ERROR_MARKERS = (
    "serviceunavailable", "service unavailable", "high demand", "overloaded",
    "internalservererror", "internal server error", "bad gateway", "gateway timeout",
    "apiconnectionerror", "connection error", "timeout", "timed out",
    " 500", " 502", " 503", " 504",
)


def _model_specific_params(model: str) -> dict:
    """Extra completion params some model families need.

    gpt-oss and Gemini 2.5+ reason before answering, and reasoning tokens come
    out of max_tokens. Measured on the Coder stage with gemini-2.5-flash:
    default effort spent 7,626 of 8,188 tokens thinking and truncated the JSON
    (2 of 8 files); "low" spent 876 and returned every file.
    """
    if "gpt-oss" in model or "gemini" in model:
        return {"reasoning_effort": "low"}
    return {}


_SEVERITY_ALIASES = {
    "critical": "critical", "blocker": "critical", "high": "critical", "error": "critical",
    "warning": "warning", "medium": "warning", "major": "warning", "moderate": "warning",
    "info": "info", "low": "info", "minor": "info", "suggestion": "info", "style": "info",
}


def _normalize_severity(value) -> str:
    """Map model-specific severity words ("High", "BLOCKER", ...) onto critical/warning/info.

    Without this, a model that says "high" instead of "critical" would never
    trigger another review iteration.
    """
    return _SEVERITY_ALIASES.get(str(value or "").strip().lower(), "info")


# ── Custom exceptions ─────────────────────────────────────────

class PipelineCancelledError(RuntimeError):
    """Raised when the user explicitly cancels a running pipeline."""


# ── Pipeline ──────────────────────────────────────────────────

class Pipeline:
    """Orchestrates the full software development pipeline using direct LiteLLM calls."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        demo: bool = False,
        event_callback: Optional[Callable[[dict], None]] = None,
        run_id: Optional[str] = None,
        cancel_event: Optional[object] = None,
        max_review_iterations: Optional[int] = None,
        max_test_fix_iterations: Optional[int] = None,
        use_static_analysis: bool = True,
        use_memory: Optional[bool] = None,
        # legacy param kept for backward compat (ignored when model/api_key used)
        llm=None,
    ):
        self.demo = demo
        self.event_callback = event_callback
        self._model = model
        self._api_key = api_key
        self._mock_llm = None
        self._run_id = run_id  # used for HITL coordination
        self._cancel_event = cancel_event  # threading.Event, set to abort pipeline
        # Per-run loop limits — never mutate global settings, runs execute concurrently.
        # 0 review iterations is valid (evals: "no review" ablation).
        self._max_review_iterations = (settings.max_review_iterations
                                       if max_review_iterations is None else max_review_iterations)
        self._max_test_fix_iterations = (settings.max_test_fix_iterations
                                         if max_test_fix_iterations is None else max_test_fix_iterations)
        # Ground the Reviewer in bandit/flake8/pip-audit output (off = evals: "LLM-only review" ablation)
        self._use_static_analysis = use_static_analysis
        # Cross-run memory; never in demo mode, so mock reviews don't pollute real memory
        self._use_memory = (settings.enable_memory if use_memory is None else use_memory) and not demo

        # LLM usage accounting for this run
        self._llm_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cost_usd = 0.0

        if demo:
            from src.core.mock_llm import MockLLM
            self._mock_llm = MockLLM()

        self.state: PipelineState | None = None
        self._start_time: datetime | None = None
        self._output_dir: Path | None = None
        self._issues_found = 0
        self._issues_fixed = 0
        # bandit/flake8 findings before the review loop (None = tools didn't run)
        self._static_initial_keys: set | None = None
        self._static_issues_initial: int | None = None
        self._static_issues_final: int | None = None
        self._security_audit = ""  # pip-audit report for the Reviewer
        self._last_truncated = False  # did the last LLM response hit max_tokens?
        # Set to [] to record every raw LLM response (the eval harness does, for debugging)
        self.transcript: list[dict] | None = None

    # ── Cancellation support ──────────────────────────────

    def _check_cancelled(self):
        """Raise PipelineCancelledError if the user clicked Stop.

        Called at every stage boundary — zero overhead when not cancelled
        (threading.Event.is_set() is a simple flag check).
        """
        if self._cancel_event and self._cancel_event.is_set():
            self._log("🛑 Pipeline cancelled by user.")
            self._emit({"type": "cancelled", "message": "Pipeline stopped by user."})
            raise PipelineCancelledError("Pipeline cancelled by user.")

    def _sleep(self, seconds: float):
        """Sleep that wakes up immediately (and aborts) when the user clicks Stop."""
        if self._cancel_event is None:
            time.sleep(seconds)
        elif self._cancel_event.wait(seconds):
            self._check_cancelled()

    # ── Core LLM call (bypasses CrewAI) ──────────────────────

    def _call_llm(self, system: str, user: str, max_retries: int = 4) -> str:
        """Make an LLM call; when self.transcript is a list, record the raw exchange in it."""
        response = self._complete(system, user, max_retries)
        if self.transcript is not None:
            self.transcript.append({
                "stage": self.state.current_stage.value if self.state else None,
                "agent": system.strip().splitlines()[0][:80],
                "prompt_chars": len(system) + len(user),
                "truncated": self._last_truncated,
                "response": response,
            })
        return response

    def _complete(self, system: str, user: str, max_retries: int = 4) -> str:
        """Make a direct LiteLLM call with automatic rate-limit retry."""
        self._last_truncated = False
        if self.demo:
            self._llm_calls += 1
            return self._mock_llm.call(system=system, user=user)

        try:
            import litellm  # type: ignore
            litellm.drop_params = True
            litellm.suppress_debug_info = True
        except ImportError as e:
            raise RuntimeError("litellm not installed. Run: pip install litellm") from e

        model = self._model
        bucket = get_rate_limiter(model)
        prompt_estimate = estimate_tokens(system + user)
        # Providers count prompt + max_tokens against the per-minute limit and
        # reject requests that exceed it, so size the output budget to fit:
        # big enough for the Coder's multi-file JSON, small enough for 8K-TPM tiers.
        max_tokens = max(1024, min(MAX_OUTPUT_TOKENS, bucket.capacity - prompt_estimate - 256))
        reserved = min(prompt_estimate + max_tokens, bucket.capacity)

        last_exc = None
        for attempt in range(max_retries):
            try:
                # ── Proactive rate limiting (token bucket) ──────────────
                # Reserve prompt + output budget and sleep proactively before
                # sending, to avoid 429 errors instead of reacting to them.
                # The bucket also enforces RPM (e.g. Gemini free tier).
                wait_secs = bucket.consume(system + user, sleep=self._sleep, extra_tokens=max_tokens)
                if wait_secs > 0.5:
                    self._log(f"⏳ Rate limiter: waited {wait_secs:.1f}s proactively")

                response = litellm.completion(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    temperature=0.2,
                    max_tokens=max_tokens,
                    # Key is passed per call, not via os.environ, so concurrent
                    # runs with different keys can't overwrite each other.
                    # None lets litellm fall back to env vars (CLI / .env).
                    api_key=self._api_key or None,
                    timeout=180,  # a hung request must not stall the run forever
                    **_model_specific_params(model),
                )
                actual = self._record_usage(litellm, response)
                bucket.reconcile(reserved=reserved, actual=actual or reserved)
                choice = response.choices[0]
                self._last_truncated = choice.finish_reason == "length"
                if self._last_truncated:
                    self._log(f"⚠ Response hit the {max_tokens}-token output limit and was truncated")
                return choice.message.content or ""

            except PipelineCancelledError:
                raise  # user clicked Stop during a wait — not an LLM failure

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

                # A single request above a hard cap (e.g. Groq "Request too large ...
                # output tokens per minute") fails identically on every retry
                if "request too large" in err_lower or "request entity too large" in err_lower:
                    raise RuntimeError(
                        f"Request too large for {model}'s limits (prompt + {max_tokens} output tokens). "
                        f"Use a model or tier with higher limits. Provider said: {err_str[:300]}"
                    ) from e

                if is_rate_limit:
                    # Daily quota exhausted — retrying won't help
                    # Groq: "tokens per day (TPD)"; Gemini: "...PerDayPerProject..." quota ids
                    if any(w in err_lower for w in _DAILY_QUOTA_MARKERS):
                        raise RuntimeError(
                            f"Daily API quota exhausted for {model}. "
                            "Free tier limit reached. Please use a different API key, "
                            "wait until tomorrow, or switch to another model or provider."
                        ) from e

                    # Per-minute rate limit — wait and retry
                    suggested = _parse_retry_after(err_lower)
                    wait_secs = suggested + 3 if suggested is not None else 20 * (attempt + 1)
                    self._log(f"⏳ Rate limit — waiting {wait_secs:.0f}s then retrying ({attempt+1}/{max_retries})...")
                    self._sleep(wait_secs)
                    continue

                # Temporary server-side problems (overload, 5xx, timeouts) — back off and retry
                if any(w in err_lower for w in _TRANSIENT_ERROR_MARKERS):
                    wait_secs = 10 * (attempt + 1)
                    self._log(f"⏳ Provider temporarily unavailable — retrying in {wait_secs}s "
                              f"({attempt+1}/{max_retries})...")
                    self._sleep(wait_secs)
                    continue

                # Any other error — fail immediately
                raise RuntimeError(f"LLM call failed ({model}): {e}") from e

        raise RuntimeError(
            f"LLM call failed after {max_retries} retries ({model}). "
            f"Last error: {last_exc}"
        )

    def _record_usage(self, litellm, response) -> int:
        """Accumulate usage for this run; return the call's total tokens (0 if unknown)."""
        self._llm_calls += 1
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        self._prompt_tokens += prompt
        self._completion_tokens += completion
        try:
            # List-price equivalent — free tiers are billed at $0 but this keeps runs comparable
            self._cost_usd += litellm.completion_cost(completion_response=response) or 0.0
        except Exception:
            pass  # model missing from litellm's price table
        return prompt + completion

    @property
    def usage(self) -> dict:
        return {
            "llm_calls": self._llm_calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "cost_usd": round(self._cost_usd, 6),
        }

    @staticmethod
    def _truncate(text: str, max_chars: int = 8000) -> str:
        """Truncate large text to stay within token limits."""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + '\n... [truncated for token limit] ...'

    @staticmethod
    def _codebase_for_prompt(codebase: CodeBase, max_chars: int = 8000) -> str:
        """Serialize whole files for a prompt until the character budget is used up.

        Unlike _truncate, never cuts a file (or the JSON) in half. Files that
        don't fit are listed by path so the agent knows they exist.
        """
        included: list[dict] = []
        omitted: list[str] = []
        used = 0
        for f in codebase.files:
            entry = f.model_dump()
            size = len(json.dumps(entry, indent=2))
            if included and used + size > max_chars:
                omitted.append(f.file_path)
                continue
            included.append(entry)
            used += size
        text = json.dumps({"files": included}, indent=2)
        if omitted:
            text += ("\n\n[Omitted for token limit — these files also exist and are kept as-is "
                     "unless you return them: " + ", ".join(omitted) + "]")
        return text

    def _python_files(self) -> dict[str, str]:
        return {
            f.file_path: f.content
            for f in self.state.codebase.files
            if f.file_path.endswith(".py")
        }

    def _response_files(self, result: str, default_language: str = "python") -> list[dict]:
        """Well-formed file entries from an agent's JSON response.

        A response truncated at max_tokens is never "repaired": json_repair would
        close the last file mid-statement (seen in real runs: files ending in
        `parents[1`) and also shifts whitespace inside strings. Instead, only the
        entries that are complete JSON objects are kept, decoded exactly.
        """
        if self._last_truncated:
            entries = _complete_file_entries(result)
            self._log(f"⚠ Truncated response: kept {len(entries)} complete file(s), dropped the cut-off one")
            return _valid_files(entries, default_language)
        data = _safe_json(result, {"files": []})
        return _valid_files(data.get("files"), default_language)

    def _merge_files(self, files: list) -> int:
        """Merge agent-returned files into the codebase; return how many changed.

        Merging (instead of replacing the codebase) means files the agent
        didn't return — e.g. ones omitted from its prompt — are never lost.
        """
        changed = 0
        for f in _valid_files(files):
            existing = self.state.codebase.get_file(f["file_path"])
            if existing is None or existing.content != f["content"]:
                changed += 1
            self.state.codebase.set_file(f["file_path"], f["content"], f["language"])
        return changed

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
            self._check_cancelled()           # ✔ stop check after Planning
            self._run_hitl_checkpoint()       # ✔ HITL: pause for user to approve architecture
            self._check_cancelled()           # ✔ stop check after HITL
            self._run_coding()
            self._check_cancelled()           # ✔ stop check after Coding
            self._run_security_scan()         # ✔ Dep scan before review
            self._run_review_improve_loop()
            self._check_cancelled()           # ✔ stop check after Review
            self._run_testing()
            self._check_cancelled()           # ✔ stop check after Testing
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
                # Verified: static-analysis findings that disappeared after the review loop
                "issues_fixed": self._issues_fixed,
                "static_issues_initial": self._static_issues_initial,
                "static_issues_final":   self._static_issues_final,
                "tests_passed": bool(self.state.test_result and self.state.test_result.passed),
                "duration_seconds": round(duration, 1),
                **self.usage,
            }
            self._emit({"type": "complete", "metrics": metrics})

        except PipelineCancelledError:
            self.state.current_stage = PipelineStage.FAILED
            self._emit({"type": "cancelled", "message": "Pipeline stopped by user."})
            console.print(Panel("[yellow bold]Pipeline Stopped[/yellow bold]\n\nCancelled by user.", border_style="yellow"))
            raise  # propagate so runs.py can mark status as 'cancelled'

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

    def _run_hitl_checkpoint(self):
        """Human-in-the-Loop: pause after Planning so the user can approve the architecture.

        Only activates for real LLM runs (non-demo) started through the API.
        - CLI mode (no run_id): skipped — no WebSocket to communicate with
        - Demo mode: skipped — automated demo runs should complete without human input
        - Real API run: emits hitl_checkpoint event and blocks until user approves
        """
        if not self._run_id or self.demo:
            return  # Skip HITL in CLI mode or Demo mode

        import threading as _threading
        from backend.ws import hitl_events, hitl_decisions

        plan = self.state.plan
        # Emit the checkpoint event to the frontend
        self._emit({
            "type": "hitl_checkpoint",
            "stage": "Architecture Approval",
            "plan": {
                "project_name":   plan.project_name,
                "description":    plan.description,
                "tech_stack":     plan.tech_stack,
                "file_structure": plan.file_structure[:20],  # cap for WS payload
                "modules":        plan.modules[:10],
                "endpoints":      [{"method": e.method, "path": e.path, "description": e.description} for e in plan.endpoints[:8]],
            }
        })
        self._log("👤 Waiting for human approval of architecture plan...")
        console.print("\n  [bold yellow]⏸ HITL Checkpoint[/bold yellow] — waiting for human to approve architecture...")

        # Block until the frontend POSTs to /api/runs/{run_id}/approve
        event: _threading.Event | None = hitl_events.get(self._run_id)
        if event:
            approved = event.wait(timeout=300)  # wait up to 5 minutes
            if not approved:
                raise RuntimeError("HITL timeout: user did not approve architecture within 5 minutes.")

            decision = hitl_decisions.get(self._run_id, {})
            if not decision.get("approved", True):
                reason = decision.get("reason", "User rejected the architecture.")
                raise RuntimeError(f"Pipeline aborted by user: {reason}")

            self._log("✅ Architecture approved by user — proceeding to code generation.")
            console.print("  [bold green]✔ Architecture approved — continuing pipeline.[/bold green]")
            self._emit({"type": "hitl_approved", "stage": "Architecture Approval"})

    def _run_coding(self):
        t0 = time.time()
        self.state.current_stage = PipelineStage.CODING
        self._print_stage(1, "Engineer agent is writing code...")
        self._emit({"type": "stage_start", "stage": "Code Generation", "icon": "💻"})

        self.state.codebase = CodeBase()
        # Every later stage depends on this one — retry once on an unusable
        # response, then fail loudly instead of reviewing an empty codebase.
        for attempt in (1, 2):
            with console.status("[bold cyan]  Engineer agent coding...[/bold cyan]", spinner="dots"):
                result = self._call_llm(
                    system=CODER_BACKSTORY,
                    user=CODE_TASK_DESCRIPTION.format(plan=self.state.plan.model_dump_json(indent=2)),
                )
            self._merge_files(self._response_files(result))
            if self.state.codebase.files:
                break
            self._log(f"⚠ Coder returned no usable files (attempt {attempt}/2)")
        else:
            raise RuntimeError("Coder agent returned no parsable files after 2 attempts.")

        count = len(self.state.codebase.files)
        console.print(f"  [green]✓ Generated {count} files[/green]")
        self._print_file_tree("Generated Files", self.state.codebase)
        self._log(f"Generated {count} source files")
        self._emit({"type": "stage_complete", "stage": "Code Generation", "icon": "💻",
                    "duration": round(time.time() - t0, 1), "data": {"files": count}})

        # ── Save initial codebase snapshot for diff viewer ───────────────
        if self._run_id:
            try:
                from backend.store.run_store import run_store
                initial_files = [
                    {"file_path": f.file_path, "content": f.content, "language": f.language}
                    for f in self.state.codebase.files
                ]
                run_store.update_initial_codebase(self._run_id, initial_files)
            except Exception:
                pass  # non-critical; diff viewer will show empty initial state

    def _run_security_scan(self):
        """Check generated requirements.txt: known vulnerabilities (pip-audit) and packages that don't exist on PyPI."""
        if self.demo:
            return  # like bandit/flake8, real tools only run on real LLM output
        # Find any requirements.txt in the codebase
        req_file = self.state.codebase.get_file("requirements.txt")
        if not req_file or not req_file.content.strip():
            return

        self._log("🔒 Scanning dependencies for known vulnerabilities (pip-audit)...")

        dep_report = analyzer.run_dependency_audit(req_file.content)
        if dep_report.vuln_packages:
            self._log(
                f"⚠ pip-audit found {len(dep_report.vuln_packages)} vulnerable packages — "
                f"Reviewer agent will be informed."
            )
            # Persist for the next review loop to consume
            self._security_audit = dep_report.as_context_string()
        else:
            self._log("✅ pip-audit: No known vulnerabilities found in dependencies.")

        # pip-audit only knows real packages; an invented name is a supply-chain risk of its own
        deps = check_requirements(req_file.content)
        if deps["missing_on_pypi"]:
            self._log(f"⚠ {len(deps['missing_on_pypi'])} dependencies don't exist on PyPI (likely hallucinated): "
                      f"{', '.join(deps['missing_on_pypi'])}")
        self._security_audit = "\n\n".join(filter(None, [self._security_audit, dependency_context(deps)]))

    def _run_review_improve_loop(self):
        for iteration in range(1, self._max_review_iterations + 1):
            self._check_cancelled()
            t0 = time.time()
            self.state.current_stage = PipelineStage.REVIEWING
            self._print_stage(2, f"Reviewer agent checking code (iteration {iteration}/{self._max_review_iterations})...")
            self._emit({"type": "stage_start", "stage": "Code Review", "icon": "🔍", "meta": f"Iteration {iteration}"})

            codebase_json = self._codebase_for_prompt(self.state.codebase)

            # ── Grounding: inject real static-analysis findings before the LLM call ──
            # Collect all Python files from the codebase to run tools against
            python_files = self._python_files()
            grounding_context = ""
            # Only run the tools when there are enough Python files to make it
            # worthwhile (avoids subprocess overhead on tiny codebases). They run in
            # demo mode too: they're local and deterministic, and the demo's
            # verified fix depends on them.
            if len(python_files) >= 2 and self._use_static_analysis:
                self._log("🔧 Static analysis: running bandit + flake8 on generated code...")
                tool_report = analyzer.run_code_review(python_files)
                if self._static_initial_keys is None and tool_report.tools_run:
                    self._static_initial_keys = tool_report.code_finding_keys()
                grounding_context = "\n\n" + tool_report.as_context_string()
                if tool_report.total_issues > 0:
                    self._log(f"🔧 Static analysis found {tool_report.total_issues} real issues to inform the Reviewer.")

            # Add security audit results from pip-audit (if available)
            if self._security_audit and self._use_static_analysis:
                grounding_context += "\n\n" + self._security_audit

            # Retrieval: issues found in similar past projects (vector memory)
            if iteration == 1:
                grounding_context += self._recall_past_issues()

            review_user_prompt = REVIEW_TASK_DESCRIPTION.format(codebase=codebase_json)
            if grounding_context:
                review_user_prompt += grounding_context

            with console.status("[bold cyan]  Reviewer agent analyzing...[/bold cyan]", spinner="dots"):
                result = self._call_llm(
                    system=REVIEWER_BACKSTORY,
                    user=review_user_prompt,
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
                    line = c.get("line_number")
                    comments.append({
                        "file_path":   str(c.get("file_path") or c.get("file") or "general"),
                        "severity":    _normalize_severity(c.get("severity")),
                        "category":    str(c.get("category") or "style"),
                        "description": str(c.get("description") or c.get("issue") or c),
                        "suggestion":  str(c.get("suggestion") or c.get("fix") or ""),
                        # "N/A", "12-15" etc. would fail model validation and crash the run
                        "line_number": int(line) if str(line).isdigit() else None,
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

            if iteration == 1:
                # Remember the first review (before any fixes) for future similar projects
                self._remember_issues(self.state.review_report)

            self._emit({"type": "stage_complete", "stage": "Code Review", "icon": "🔍",
                        "duration": round(time.time() - t0, 1),
                        "data": {"issues": self.state.review_report.issue_count,
                                 "quality": self.state.review_report.overall_quality}})

            if (self.state.review_report.overall_quality in ("excellent", "good")
                    and not self.state.review_report.has_critical_issues):
                console.print("  [green]✓ Code quality satisfactory, skipping further iterations.[/green]")
                self._log("Code quality satisfactory — skipping further review iterations")
                break
            if self.state.review_report.issue_count == 0:
                self._log("Reviewer reported no concrete issues — nothing for the Improver to fix")
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
                        codebase=codebase_json,
                        review=review_json,
                    ),
                )

            changed = self._merge_files(self._response_files(result))
            console.print(f"  [green]✓ Improver changed {changed} files[/green]")
            self._log(f"Improver changed {changed} files to address "
                      f"{self.state.review_report.issue_count} reported issues")

            self._emit({"type": "stage_complete", "stage": "Code Improvement", "icon": "✨",
                        "duration": round(time.time() - t1, 1),
                        "data": {"files_changed": changed,
                                 "issues_addressed": self.state.review_report.issue_count}})

        self._verify_static_fixes()

    # ── Vector memory (retrieval-augmented review) ────────────

    def _memory_key(self) -> str:
        plan = self.state.plan
        return f"{plan.description}\nStack: {', '.join(plan.tech_stack)}"

    def _remember_issues(self, report: ReviewReport):
        """Store the serious issues found in this project, keyed by what the project is."""
        if not self._use_memory:
            return
        serious = [c for c in report.comments if c.severity in ("critical", "warning")][:8]
        if not serious:
            return
        issues = "\n".join(f"- [{c.severity}/{c.category}] {c.description}" for c in serious)
        reviewer_memory.store(
            f"{self._memory_key()}\nIssues found:\n{issues}",
            {"project": self.state.plan.project_name, "tech": ",".join(self.state.plan.tech_stack)},
        )

    def _recall_past_issues(self) -> str:
        """Retrieve issues from the most similar past projects to steer the Reviewer."""
        if not self._use_memory:
            return ""
        past = reviewer_memory.query(self._memory_key(), n_results=2)
        if not past:
            return ""
        self._log(f"🧠 Memory: retrieved issues from {len(past)} similar past project(s)")
        blocks = [p["content"].split("Issues found:", 1)[-1].strip() for p in past]
        return ("\n\n## Issues found in similar past projects (from memory)\n"
                "Check whether any of these also apply here; do not report them unless they do.\n"
                + self._truncate("\n".join(blocks), max_chars=1500))

    def _verify_static_fixes(self):
        """Re-run bandit/flake8 after the review loop and count findings that disappeared.

        This is the only "issues fixed" number reported — an LLM saying it
        fixed something doesn't count until the tools agree.
        """
        if self._static_initial_keys is None:
            return  # tools didn't run (demo mode, too few files, or not installed)
        final_keys = analyzer.run_code_review(self._python_files()).code_finding_keys()
        self._static_issues_initial = len(self._static_initial_keys)
        self._static_issues_final = len(final_keys)
        self._issues_fixed = len(self._static_initial_keys - final_keys)
        self._log(f"🔧 Verified with bandit + flake8: {self._issues_fixed} findings fixed "
                  f"({self._static_issues_initial} → {self._static_issues_final})")

    def _run_testing(self):
        t0 = time.time()
        self.state.current_stage = PipelineStage.TESTING
        self._print_stage(4, "QA agent writing tests...")
        self._emit({"type": "stage_start", "stage": "Test Generation", "icon": "🧪"})

        with console.status("[bold cyan]  QA agent writing tests...[/bold cyan]", spinner="dots"):
            result = self._call_llm(
                system=TESTER_BACKSTORY,
                user=TEST_TASK_DESCRIPTION.format(
                    codebase=self._codebase_for_prompt(self.state.codebase),
                    plan=self._truncate(self.state.plan.model_dump_json(indent=2), max_chars=2000),
                ),
            )

        test_files = self._response_files(result)
        self._merge_files(test_files)
        test_count = len(test_files)

        console.print(f"  [green]✓ Generated {test_count} test files[/green]")
        self._log(f"Generated {test_count} test files")
        self._emit({"type": "stage_complete", "stage": "Test Generation", "icon": "🧪",
                    "duration": round(time.time() - t0, 1), "data": {"test_files": test_count}})

    def _run_test_fix_loop(self):
        # One directory per run so concurrent runs never share test files
        output_dir = settings.output_path / "_test_run" / (self._run_id or "cli")
        try:
            self._test_fix_attempts(output_dir)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def _test_fix_attempts(self, output_dir: Path):
        for iteration in range(1, self._max_test_fix_iterations + 1):
            self._check_cancelled()
            t0 = time.time()
            self.state.current_stage = PipelineStage.TEST_RUNNING
            self._print_stage(5, f"Running tests (attempt {iteration}/{self._max_test_fix_iterations})...")
            self._emit({"type": "stage_start", "stage": "Test Execution", "icon": "▶️", "meta": f"Attempt {iteration}"})

            # Start clean so files from earlier attempts can't affect this one
            shutil.rmtree(output_dir, ignore_errors=True)
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
            # Keep the end of the output — pytest puts the failure summary last
            test_output = (f"STDOUT:\n{exec_result.stdout[-6000:]}\n\n"
                           f"STDERR:\n{exec_result.stderr[-2000:]}")

            with console.status("[bold cyan]  QA analyst investigating failures...[/bold cyan]", spinner="dots"):
                runner_result = self._call_llm(
                    system=TEST_RUNNER_BACKSTORY,
                    user=TEST_RUNNER_TASK_DESCRIPTION.format(
                        test_output=test_output,
                        codebase=self._codebase_for_prompt(self.state.codebase),
                    ),
                )

            data = _safe_json(runner_result, {"passed": False})
            self.state.test_result = TestResult(
                # pytest's exit code is the ground truth — the analyst LLM only explains failures
                passed=False,
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

            if iteration < self._max_test_fix_iterations:
                console.print("  [cyan]→ Sending back to Improver agent...[/cyan]")
                codebase_json = self._codebase_for_prompt(self.state.codebase)
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
                self._merge_files(self._response_files(fix_result))

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
                    codebase=self._codebase_for_prompt(self.state.codebase),
                ),
            )

        data = _safe_json(result, {"files": [], "instructions": ""}, truncated=self._last_truncated)
        deploy_files = self._response_files(result, default_language="yaml")
        self._merge_files(deploy_files)
        self.state.deployment = DeploymentArtifact(files=deploy_files, instructions=str(data.get("instructions") or ""))

        count = len(deploy_files)
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
