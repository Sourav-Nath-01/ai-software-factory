"""Docker sandbox for safe, isolated code execution.

Falls back gracefully to subprocess if Docker is unavailable (local dev).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from src.tools.code_executor import ExecutionResult


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


DOCKER_AVAILABLE = _docker_available()


def run_sandboxed(
    command: list[str],
    cwd: Path,
    timeout: int = 60,
) -> ExecutionResult:
    """Run command in a Docker sandbox (or subprocess fallback)."""
    if DOCKER_AVAILABLE:
        return _run_docker(command, cwd, timeout)
    # Fallback: plain subprocess (dev/no-Docker environments)
    from src.tools.code_executor import run_command
    return run_command(command, cwd=cwd, timeout=timeout)


def _run_docker(command: list[str], cwd: Path, timeout: int) -> ExecutionResult:
    docker_cmd = [
        "docker", "run", "--rm",
        "--network", "none",       # no internet access
        "--memory", "256m",
        "--cpus", "0.5",
        "--workdir", "/sandbox",
        "-v", f"{cwd.resolve()}:/sandbox:ro",
        "python:3.11-slim",
        "sh", "-c",
        # install deps if requirements.txt exists, then run command
        "pip install -q -r requirements.txt 2>/dev/null; " + " ".join(command),
    ]
    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
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
