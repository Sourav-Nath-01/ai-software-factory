"""Tests for the MCP server (src/mcp_server.py) and the dependency check behind it."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.static_analysis import BANDIT_AVAILABLE, FLAKE8_AVAILABLE

ROOT = Path(__file__).resolve().parent.parent
needs_tools = pytest.mark.skipif(not (BANDIT_AVAILABLE and FLAKE8_AVAILABLE), reason="bandit/flake8 not installed")

# shell=True on a variable = bandit B602 (HIGH); `os` unused = pyflakes F401
VULNERABLE = "import os\nimport subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n"
SAFER = "import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd)\n"


def call(tool: str, args: dict) -> tuple[bool, str]:
    """Call one tool through a real MCP client connected in-process."""
    from mcp import Client
    from src.mcp_server import server

    async def go():
        async with Client(server) as client:
            result = await client.call_tool(tool, args)
            return result.is_error, (result.content[0].text if result.content else "")
    return asyncio.run(go())


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIFIER_ROOTS", str(tmp_path))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "runner.py").write_text(VULNERABLE)
    return tmp_path


class TestTools:
    def test_four_read_only_tools_are_listed(self):
        from mcp import Client
        from src.mcp_server import server

        async def go():
            async with Client(server) as client:
                return (await client.list_tools()).tools
        tools = asyncio.run(go())
        assert sorted(t.name for t in tools) == ["analyze_code", "check_dependencies", "diff_findings", "run_tests"]
        assert all(t.annotations.read_only_hint for t in tools)

    @needs_tools
    def test_analyze_code_reports_tool_findings(self, project):
        is_error, text = call("analyze_code", {"path": "."})
        data = json.loads(text)
        assert not is_error and data["files_analyzed"] == 2
        assert data["security_findings"][0]["rule"] == "B602"          # most severe first
        assert data["security_findings"][0]["file"] == "app/runner.py"
        assert any("F401" in line for line in data["lint_findings"])

    def test_check_dependencies_flags_hallucinated_packages(self, project, monkeypatch):
        import src.mcp_server as mcp_server
        from src.core.dependency_check import check_requirements
        from src.core.static_analysis import AnalysisReport
        fake_pypi = {"fastapi": {"status": "exists", "first_release": "2018-12-08T08:14:13Z"}}
        lookup = lambda name: fake_pypi.get(name, {"status": "missing"})  # noqa: E731
        monkeypatch.setattr(mcp_server, "check_requirements", lambda text: check_requirements(text, lookup=lookup))
        monkeypatch.setattr(mcp_server.analyzer, "run_dependency_audit", lambda text: AnalysisReport())
        (project / "requirements.txt").write_text("fastapi==0.115.0\nfastapi-jwt-secure-helpers-pro>=1.0\n")
        is_error, text = call("check_dependencies", {"path": "."})
        data = json.loads(text)
        assert not is_error and data["checked"] == 2
        assert data["missing_on_pypi"] == ["fastapi-jwt-secure-helpers-pro"]

    def test_run_tests_refuses_without_a_sandbox(self, project, monkeypatch):
        monkeypatch.delenv("VERIFIER_ALLOW_UNSANDBOXED", raising=False)   # conftest turns Docker off
        is_error, text = call("run_tests", {"path": "."})
        assert is_error and "Docker is not available" in text

    def test_run_tests_runs_the_suite_on_a_copy(self, project, monkeypatch):
        monkeypatch.setenv("VERIFIER_ALLOW_UNSANDBOXED", "1")
        (project / "tests").mkdir()
        (project / "tests" / "test_ok.py").write_text(
            "def test_math():\n    assert 1 + 1 == 2\n\n\n"
            "def test_writes_a_file():\n    open('marker.txt', 'w').write('x')\n")
        is_error, text = call("run_tests", {"path": "."})
        data = json.loads(text)
        assert not is_error
        assert (data["all_passed"], data["passed"], data["failed"], data["collected"]) == (True, 2, 0, True)
        assert not (project / "marker.txt").exists()     # ran on a copy, the project is untouched

    @needs_tools
    def test_diff_findings_reports_introduced_and_fixed(self, project):
        git = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        (project / "app" / "runner.py").write_text(SAFER)
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "safer version"], check=True)
        (project / "app" / "runner.py").write_text(VULNERABLE)   # the "change" under review

        is_error, text = call("diff_findings", {"path": ".", "base": "HEAD"})
        data = json.loads(text)
        assert not is_error
        assert "B602" in {f["rule"] for f in data["introduced"]}          # shell=True added
        assert any(f["rule"] == "flake8" and "F401" in f["issue"] for f in data["introduced"])
        assert "B603" in {f["rule"] for f in data["fixed"]}               # the old finding went away

    def test_paths_outside_the_roots_are_refused(self, project):
        is_error, text = call("analyze_code", {"path": str(project.parent)})
        assert is_error and "outside the allowed roots" in text

    def test_git_option_injection_is_refused(self, project):
        is_error, text = call("diff_findings", {"path": ".", "base": "--output=/tmp/pwned"})
        assert is_error and "not a valid git revision" in text


class TestDependencyCheck:
    def test_requirement_names(self):
        from src.core.dependency_check import requirement_names
        text = ("fastapi[all]==0.115.0  # web\nFlask_Login>=0.6\n-r base.txt\n"
                "git+https://github.com/x/y\nuvicorn ; python_version>'3.8'\nflask-login\n")
        assert requirement_names(text) == ["fastapi", "Flask_Login", "uvicorn"]   # flask-login == Flask_Login

    def test_missing_recent_and_unverified(self):
        from src.core.dependency_check import check_requirements
        pypi = {"old": {"status": "exists", "first_release": "2019-01-01T00:00:00Z"},
                "brandnew": {"status": "exists", "first_release": "2026-11-01T00:00:00Z"},
                "offline": {"status": "unknown", "first_release": None}}
        result = check_requirements("old\nbrandnew\noffline\ninvented\n",
                                    lookup=lambda n: pypi.get(n, {"status": "missing", "first_release": None}),
                                    now=datetime(2026, 11, 26, tzinfo=timezone.utc))
        assert result["missing_on_pypi"] == ["invented"]
        assert [p["name"] for p in result["recently_registered"]] == ["brandnew"]
        assert result["unverified"] == ["offline"]

    def test_pipeline_tells_the_reviewer_about_hallucinated_packages(self, monkeypatch):
        import src.core.pipeline as pipeline_mod
        from src.core.dependency_check import check_requirements
        from src.core.models import CodeBase, PipelineState
        from src.core.static_analysis import AnalysisReport
        monkeypatch.setattr(pipeline_mod, "check_requirements",
                            lambda text: check_requirements(text, lookup=lambda n: {"status": "missing"}))
        monkeypatch.setattr(pipeline_mod.analyzer, "run_dependency_audit", lambda text: AnalysisReport())
        p = pipeline_mod.Pipeline(model="groq/x")
        p.state = PipelineState(user_request="x")
        p.state.codebase = CodeBase()
        p.state.codebase.set_file("requirements.txt", "flask-jwt-auth-helper\n", "text")
        p._run_security_scan()
        assert "do not exist on PyPI" in p._security_audit and "flask-jwt-auth-helper" in p._security_audit


def test_real_stdio_session(tmp_path):
    """Launch the server as a subprocess and talk MCP over stdin/stdout, as Claude Code does."""
    from mcp import Client, StdioServerParameters
    (tmp_path / "app.py").write_text(VULNERABLE)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "src.mcp_server"], cwd=str(ROOT),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(tmp_path)),
             "VERIFIER_ROOTS": str(tmp_path), "VERIFIER_CACHE_DIR": str(tmp_path / "cache"),
             "ENABLE_MEMORY": "false", "USE_DOCKER_SANDBOX": "false"})

    async def go():
        async with Client(params) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            result = await client.call_tool("analyze_code", {"path": "."})
            return names, result
    names, result = asyncio.run(go())
    assert names == ["analyze_code", "check_dependencies", "diff_findings", "run_tests"]
    assert not result.is_error
    if BANDIT_AVAILABLE:
        assert json.loads(result.content[0].text)["security_findings"][0]["rule"] == "B602"
