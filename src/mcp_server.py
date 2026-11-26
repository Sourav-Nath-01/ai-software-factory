"""MCP server: lets AI coding agents verify the code they write.

Exposes the project's verification layer over the Model Context Protocol, so
Claude Code, Cursor, VS Code or Claude Desktop can check their own work with
deterministic tools instead of judging it themselves:

  analyze_code        bandit (security) + flake8 (pyflakes, style) findings
  check_dependencies  packages that don't exist on PyPI, brand-new ones, known CVEs
  run_tests           the project's pytest suite, in the network-isolated Docker sandbox
  diff_findings       findings your changes introduced or fixed since a git revision

The generation pipeline calls the same checks directly — they always run, so
there is no decision for a model to make. This server is how *external*
agents reach them.

Run (stdio):    python -m src.mcp_server        (or the ai-factory-mcp script)
Claude Code:    claude mcp add verifier -- /abs/path/.venv/bin/python -m src.mcp_server

Safety:
  - tools only read inside the allowed roots: VERIFIER_ROOTS (os.pathsep-separated),
    default = the directory the server was started in;
  - run_tests executes a copy of the project in the Docker sandbox (no network,
    256 MB, 0.5 CPU); without Docker it refuses unless VERIFIER_ALLOW_UNSANDBOXED=1;
  - git revisions are validated before they reach git.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from src.core import sandbox
from src.core.config import settings
from src.core.dependency_check import check_requirements
from src.core.static_analysis import BANDIT_AVAILABLE, FLAKE8_AVAILABLE, analyzer
from src.tools.code_executor import pytest_counts

MAX_FILES = 2000
MAX_FILE_BYTES = 1_000_000
MAX_LIST = 50  # findings returned per list; the total is always reported
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "build", "dist", "site-packages", ".idea", ".vscode",
}
_SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_GIT_REV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]*$")

INSTRUCTIONS = """Deterministic checks for code you write — use them instead of judging your own code.
- After writing or editing Python: analyze_code, then diff_findings to see what your change introduced or fixed.
- After adding or changing dependencies: check_dependencies (catches packages that don't exist on PyPI).
- Before saying tests pass: run_tests (runs them in an isolated sandbox).
Findings come from bandit, flake8, pip-audit, PyPI and pytest, not from a language model."""

server = MCPServer("code-verifier", instructions=INSTRUCTIONS)


# ── Paths and files ────────────────────────────────────────────

def _roots() -> list[Path]:
    raw = os.environ.get("VERIFIER_ROOTS")
    roots = raw.split(os.pathsep) if raw else [os.getcwd()]
    return [Path(r).expanduser().resolve() for r in roots if r]


def _resolve(path: str) -> Path:
    """Resolve `path` (relative = against the first root) and refuse anything outside the roots."""
    roots = _roots()
    candidate = Path(path).expanduser()
    resolved = (candidate if candidate.is_absolute() else roots[0] / candidate).resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ToolError(f"{path!r} is outside the allowed roots ({', '.join(map(str, roots))}). "
                        "Set VERIFIER_ROOTS to allow it.")
    if not resolved.exists():
        raise ToolError(f"{path!r} does not exist.")
    return resolved


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            yield Path(dirpath) / name


def _python_sources(target: Path) -> dict[str, str]:
    """{path relative to target (or its folder, for a file): source} for Python files under target."""
    base = target if target.is_dir() else target.parent
    sources: dict[str, str] = {}
    for f in ([target] if target.is_file() else sorted(_walk(target))):
        if f.suffix == ".py" and f.stat().st_size <= MAX_FILE_BYTES:
            sources[str(f.relative_to(base))] = f.read_text(encoding="utf-8", errors="replace")
            if len(sources) >= MAX_FILES:
                break
    return sources


def _security_findings(report) -> list[dict]:
    findings = [{
        "file": i.get("filename"), "line": i.get("line_number"), "rule": i.get("test_id"),
        "severity": i.get("issue_severity"), "confidence": i.get("issue_confidence"),
        "issue": i.get("issue_text"),
    } for i in report.security_issues]
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(str(f["severity"]).upper(), 3))


# ── git helpers (diff_findings) ────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ToolError("git is not installed.") from None
    if result.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {result.stderr.strip()[:300] or 'unknown error'}")
    return result.stdout


def _extract_tar(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        try:
            tar.extractall(dest, filter="data")  # refuses absolute paths, "..", device files
        except TypeError:  # Python without extraction filters
            safe = [m for m in tar.getmembers() if (m.isfile() or m.isdir())
                    and not m.name.startswith("/") and ".." not in Path(m.name).parts]
            tar.extractall(dest, members=safe)  # nosec B202 - members validated just above


def _sources_at_revision(repo: Path, rel: Path, base: str, work: Path) -> dict[str, str]:
    """Python sources of repo/rel as they were at `base` ({} if the path didn't exist then)."""
    spec = rel.as_posix() if rel.as_posix() != "." else "."
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", base, "--", spec],
                             capture_output=True, timeout=120)
    if archive.returncode != 0:
        if b"did not match any files" in archive.stderr:
            return {}
        raise ToolError(f"git archive failed: {archive.stderr.decode(errors='replace').strip()[:300]}")
    _extract_tar(archive.stdout, work)
    old_target = work / rel
    return _python_sources(old_target) if old_target.exists() else {}


# ── Tools ──────────────────────────────────────────────────────

@server.tool(annotations=ToolAnnotations(title="Analyze code", read_only_hint=True, open_world_hint=False))
def analyze_code(path: str = ".") -> dict:
    """Run bandit (security) and flake8 (pyflakes + style) on the Python files under `path`.

    Findings come from the tools, most severe first; files are never modified.
    Use after writing or editing Python code.
    """
    if not (BANDIT_AVAILABLE or FLAKE8_AVAILABLE):
        raise ToolError("bandit and flake8 are not installed: pip install -e '.[analysis]'")
    sources = _python_sources(_resolve(path))
    if not sources:
        return {"files_analyzed": 0, "message": "No Python files found."}
    report = analyzer.run_code_review(sources)
    security = _security_findings(report)
    return {
        "files_analyzed": len(sources),
        "tools_run": report.tools_run,
        "security_total": len(security),
        "security_findings": security[:MAX_LIST],
        "lint_total": len(report.style_issues),
        "lint_findings": report.style_issues[:MAX_LIST],
    }


@server.tool(annotations=ToolAnnotations(title="Check dependencies", read_only_hint=True, open_world_hint=True))
def check_dependencies(path: str = ".") -> dict:
    """Check a project's dependencies (requirements.txt and pyproject.toml, or one requirements file).

    Flags packages that don't exist on PyPI (likely hallucinated — an attacker could
    register the name), packages first published very recently, and known
    vulnerabilities (pip-audit). Use after adding or changing dependencies.
    """
    target = _resolve(path)
    requirements = (sandbox.collect_requirements(target) if target.is_dir()
                    else target.read_text(encoding="utf-8", errors="replace"))
    if not requirements.strip():
        return {"checked": 0, "message": "No requirements.txt or pyproject.toml dependencies found."}
    result = check_requirements(requirements)
    audit = analyzer.run_dependency_audit(requirements)
    result["vulnerable"] = audit.vuln_packages
    result["pip_audit_run"] = "pip-audit" in audit.tools_run
    return result


@server.tool(annotations=ToolAnnotations(title="Run tests in sandbox", read_only_hint=True, open_world_hint=True))
def run_tests(path: str = ".", timeout_seconds: int = 120) -> dict:
    """Run the project's pytest suite on a copy of `path` in the network-isolated Docker sandbox.

    Dependencies from requirements.txt / pyproject.toml are installed first, in a
    separate container. Your files are never modified. Use before claiming tests pass.
    """
    project = _resolve(path)
    if not project.is_dir():
        raise ToolError("run_tests needs a project directory, not a file.")
    use_docker = sandbox.DOCKER_AVAILABLE and settings.use_docker_sandbox
    if not use_docker and os.environ.get("VERIFIER_ALLOW_UNSANDBOXED") != "1":
        raise ToolError("Docker is not available, and running tests outside the sandbox is disabled. "
                        "Start Docker, or set VERIFIER_ALLOW_UNSANDBOXED=1 to use a plain subprocess.")
    timeout = max(10, min(int(timeout_seconds), 600))

    with tempfile.TemporaryDirectory(prefix="verifier-tests-") as tmp:
        work = Path(tmp) / "project"
        shutil.copytree(project, work, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        result = sandbox.run_sandboxed(["python3", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
                                       cwd=work, timeout=timeout)

    counts = pytest_counts(result.stdout)
    output = result.stdout + (f"\n{result.stderr}" if result.stderr else "")
    return {
        "all_passed": result.success,
        **counts,  # passed / failed / errors (numbers of tests)
        "exit_code": result.return_code,
        "timed_out": result.timed_out,
        "collected": sum(counts.values()) > 0,  # False = the tests couldn't even be imported
        "sandbox": "docker, no network" if use_docker else "subprocess (unsandboxed)",
        "output_tail": output[-4000:],
    }


@server.tool(annotations=ToolAnnotations(title="Findings changed since a git revision",
                                         read_only_hint=True, open_world_hint=False))
def diff_findings(path: str = ".", base: str = "HEAD") -> dict:
    """Compare bandit/flake8 findings under `path` now with git revision `base` (default HEAD).

    Returns the findings your changes introduced and the ones they fixed. Line numbers
    are ignored, so moved code doesn't count as a change. Use to confirm an edit fixed
    an issue without adding new ones.
    """
    if not _GIT_REV.match(base):
        raise ToolError(f"{base!r} is not a valid git revision.")
    target = _resolve(path)
    folder = target if target.is_dir() else target.parent
    repo = Path(_git(folder, "rev-parse", "--show-toplevel").strip()).resolve()
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except ToolError:
        raise ToolError(f"Unknown git revision {base!r}.") from None

    with tempfile.TemporaryDirectory(prefix="verifier-base-") as tmp:
        before_sources = _sources_at_revision(repo, target.relative_to(repo), base, Path(tmp))
    after_sources = _python_sources(target)

    before = analyzer.run_code_review(before_sources).code_finding_keys() if before_sources else set()
    after = analyzer.run_code_review(after_sources).code_finding_keys() if after_sources else set()
    introduced, fixed = sorted(after - before), sorted(before - after)
    as_dict = lambda key: {"file": key[0], "rule": key[1], "issue": key[2]}  # noqa: E731
    return {
        "base": base,
        "introduced_total": len(introduced),
        "introduced": [as_dict(k) for k in introduced[:MAX_LIST]],
        "fixed_total": len(fixed),
        "fixed": [as_dict(k) for k in fixed[:MAX_LIST]],
        "unchanged": len(before & after),
    }


def main() -> None:
    # Keep the sandbox's dependency cache out of the projects being verified
    settings.output_dir = os.environ.get("VERIFIER_CACHE_DIR",
                                         str(Path.home() / ".cache" / "ai-software-factory"))
    server.run("stdio")


if __name__ == "__main__":
    main()
