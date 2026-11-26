"""Docker sandbox for safe, isolated code execution.

Generated code runs in two phases:
  1. Install — requirements.txt + test deps are pip-installed into a cache
     directory. This container has network access but no project code mounted.
  2. Execute — the command runs with --network none, the deps mounted read-only,
     and CPU / memory / process limits.

Installs are cached by a hash of the requirements, so test-fix retries and
repeat runs don't reinstall. Falls back to a plain subprocess (with secrets
scrubbed from its environment) if Docker is unavailable, unless disabled via
ALLOW_UNSANDBOXED_EXEC=false.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from src.core.config import settings
from src.tools.code_executor import ExecutionResult

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "python:3.11-slim"
# Needed to run generated test suites, on top of the project's requirements.txt
SANDBOX_TEST_DEPS = ["pytest", "httpx"]
INSTALL_TIMEOUT = 300

# Environment variable names containing these are never passed to generated code
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


DOCKER_AVAILABLE = _docker_available()
_warned_unsandboxed = False


def run_sandboxed(
    command: list[str],
    cwd: Path,
    timeout: int = 60,
) -> ExecutionResult:
    """Run command in a Docker sandbox (or subprocess fallback)."""
    if DOCKER_AVAILABLE and settings.use_docker_sandbox:
        return _run_docker(command, cwd, timeout)

    if not settings.allow_unsandboxed_exec:
        return ExecutionResult(
            return_code=-1, stdout="",
            stderr="Docker is unavailable and unsandboxed execution is disabled "
                   "(ALLOW_UNSANDBOXED_EXEC=false).",
            timed_out=False,
        )

    global _warned_unsandboxed
    if not _warned_unsandboxed:
        _warned_unsandboxed = True
        logger.warning("Docker sandbox unavailable or disabled — running generated code in an unsandboxed subprocess.")

    # Fallback: plain subprocess (dev/no-Docker environments), without secrets in its env.
    # Use this server's interpreter, not whatever "python3" is first on PATH.
    if command and command[0] in ("python", "python3"):
        command = [sys.executable, *command[1:]]
    from src.tools.code_executor import run_command
    return run_command(command, cwd=cwd, timeout=timeout, env=_scrubbed_env())


def _scrubbed_env() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if not any(marker in k.upper() for marker in _SECRET_MARKERS)
    }


def _user_args() -> list[str]:
    """Run containers as the host user so files they write stay deletable."""
    if hasattr(os, "getuid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


def _docker_exec(docker_cmd: list[str], timeout: int) -> ExecutionResult:
    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ExecutionResult(
            return_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            return_code=-1, stdout="",
            stderr=f"Sandbox timed out after {timeout}s", timed_out=True,
        )
    except Exception as e:
        return ExecutionResult(
            return_code=-1, stdout="", stderr=str(e), timed_out=False,
        )


def _ensure_deps(requirements: str) -> tuple[Path | None, ExecutionResult | None]:
    """Install requirements + test deps into a content-addressed cache directory.

    Returns (deps_dir, None) on success or (None, failure_result) on failure.
    """
    spec = requirements.strip() + "\n" + "\n".join(SANDBOX_TEST_DEPS) + "\n"
    digest = hashlib.sha256(spec.encode()).hexdigest()[:16]
    root = settings.output_path / "_sandbox_deps"
    target = root / digest
    if (target / ".complete").exists():
        return target, None

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{digest}-", dir=root))
    (staging / "_requirements.txt").write_text(spec, encoding="utf-8")

    install = _docker_exec([
        "docker", "run", "--rm",
        *_user_args(),
        "-e", "HOME=/tmp",
        "-v", f"{staging}:/deps",
        SANDBOX_IMAGE,
        "pip", "install", "--quiet", "--no-cache-dir", "--disable-pip-version-check",
        "--target", "/deps", "-r", "/deps/_requirements.txt",
    ], INSTALL_TIMEOUT)

    if not install.success:
        shutil.rmtree(staging, ignore_errors=True)
        return None, ExecutionResult(
            return_code=install.return_code,
            stdout=install.stdout,
            stderr="Sandbox dependency install failed (check requirements.txt):\n" + install.stderr,
            timed_out=install.timed_out,
        )

    (staging / ".complete").touch()
    try:
        staging.rename(target)
    except OSError:
        # Another run installed the same dependency set first — use theirs
        shutil.rmtree(staging, ignore_errors=True)
    return target, None


def _pyproject_dependencies(pyproject: Path) -> list[str]:
    """[project].dependencies from a pyproject.toml (projects may declare deps there instead)."""
    if not pyproject.exists():
        return []
    text = pyproject.read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
        deps = tomllib.loads(text).get("project", {}).get("dependencies", [])
        return [str(d) for d in deps] if isinstance(deps, list) else []
    except ImportError:
        # Python 3.10: read the `dependencies = [...]` array directly
        m = re.search(r"^\s*dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL | re.MULTILINE)
        return re.findall(r"[\"']([^\"']+)[\"']", m.group(1)) if m else []
    except Exception:
        return []  # malformed TOML — the install step just won't include these


def collect_requirements(cwd: Path) -> str:
    req_file = cwd / "requirements.txt"
    requirements = req_file.read_text(encoding="utf-8") if req_file.exists() else ""
    return "\n".join([requirements.strip(), *_pyproject_dependencies(cwd / "pyproject.toml")]).strip()


def _run_docker(command: list[str], cwd: Path, timeout: int) -> ExecutionResult:
    cwd = cwd.resolve()
    requirements = collect_requirements(cwd)

    deps_dir, failure = _ensure_deps(requirements)
    if failure:
        return failure

    docker_cmd = [
        "docker", "run", "--rm",
        "--network", "none",       # no internet access while generated code runs
        "--memory", "256m",
        "--cpus", "0.5",
        "--pids-limit", "128",
        *_user_args(),
        "-e", "PYTHONPATH=/deps",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
        "-v", f"{deps_dir}:/deps:ro",
        "-v", f"{cwd}:/sandbox",
        "--workdir", "/sandbox",
        SANDBOX_IMAGE,
        *command,
    ]
    return _docker_exec(docker_cmd, timeout + 30)
