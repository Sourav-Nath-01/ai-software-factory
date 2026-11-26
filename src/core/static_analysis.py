"""Deterministic static analysis: bandit, flake8 and pip-audit.

Used in two ways:
  - the pipeline runs it before every review and injects the findings into
    the Reviewer's prompt (grounding), then re-runs it to verify fixes;
  - the MCP server (src/mcp_server.py) exposes it as tools that external
    coding agents (Claude Code, Cursor, ...) can call on their own code.

Usage:
    report = analyzer.run_code_review({"app/main.py": source, ...})
    report.as_context_string()     # text for an LLM prompt
    report.code_finding_keys()     # line-independent ids, for before/after diffs
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import json
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


# ── Tool availability detection ───────────────────────────────

_TOOL_MODULES = {"bandit": "bandit", "flake8": "flake8", "pip-audit": "pip_audit"}


def _tool_cmd(name: str) -> list[str]:
    """Command prefix for a tool: `python -m <module>` if installed for this interpreter, else PATH.

    `python -m` keeps working in unactivated or relocated venvs, where
    console-script shebangs can point at a stale interpreter path.
    """
    if importlib.util.find_spec(_TOOL_MODULES[name]) is not None:
        return [sys.executable, "-m", _TOOL_MODULES[name]]
    return [shutil.which(name) or name]


def _tool_available(name: str) -> bool:
    """Check if a CLI tool is installed."""
    try:
        result = subprocess.run(
            [*_tool_cmd(name), "--version"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


BANDIT_AVAILABLE  = _tool_available("bandit")
FLAKE8_AVAILABLE  = _tool_available("flake8")
PIP_AUDIT_AVAILABLE   = _tool_available("pip-audit")  # pip-audit for vuln scanning


# ── Tool result dataclass ──────────────────────────────────────

@dataclass
class ToolResult:
    tool_name: str
    success: bool
    output: str
    issues_found: int = 0
    error: Optional[str] = None


@dataclass
class AnalysisReport:
    """Aggregated findings from one run of the analysis tools."""
    security_issues: list[dict] = field(default_factory=list)
    style_issues: list[dict] = field(default_factory=list)
    vuln_packages: list[dict] = field(default_factory=list)
    tools_run: list[str] = field(default_factory=list)
    tools_available: dict[str, bool] = field(default_factory=dict)

    def as_context_string(self) -> str:
        """Format the report as a concise string for LLM context injection."""
        lines = ["## Deterministic Tool Analysis (bandit, flake8, pip-audit)\n"]

        if self.security_issues:
            lines.append(f"### Security (bandit): {len(self.security_issues)} issues found")
            for issue in self.security_issues[:10]:  # cap at 10 for token budget
                lines.append(
                    f"- [{issue.get('issue_severity','?')}/{issue.get('issue_confidence','?')}] "
                    f"{issue.get('filename','?')}:{issue.get('line_number','?')} — "
                    f"{issue.get('issue_text','?')} (test: {issue.get('test_id','?')})"
                )
        else:
            lines.append("### Security (bandit): No issues found ✓")

        if self.style_issues:
            lines.append(f"\n### Style (flake8): {len(self.style_issues)} violations")
            for issue in self.style_issues[:10]:
                lines.append(f"- {issue}")
        else:
            lines.append("\n### Style (flake8): No violations ✓")

        if self.vuln_packages:
            lines.append(f"\n### Vulnerable Dependencies (pip-audit): {len(self.vuln_packages)} found")
            for pkg in self.vuln_packages:
                lines.append(f"- {pkg.get('name','?')} {pkg.get('version','?')}: {pkg.get('id','?')}")
        else:
            lines.append("\n### Vulnerable Dependencies (pip-audit): None found ✓")

        lines.append(f"\n_Tools run: {', '.join(self.tools_run) or 'none (install bandit, flake8, pip-audit)' }_")
        return "\n".join(lines)

    @property
    def total_issues(self) -> int:
        return len(self.security_issues) + len(self.style_issues) + len(self.vuln_packages)

    def code_finding_keys(self) -> set[tuple[str, str, str]]:
        """Line-number-independent identities for bandit/flake8 findings.

        Used to verify fixes: a finding counts as fixed only if its
        (file, rule, message) disappears after the Improver runs.
        """
        keys = set()
        for issue in self.security_issues:
            keys.add((str(issue.get("filename", "")), str(issue.get("test_id", "")),
                      str(issue.get("issue_text", ""))))
        for line in self.style_issues:
            # flake8 format: "path:line:col: CODE message"
            parts = line.split(":", 3)
            if len(parts) == 4:
                keys.add((parts[0], "flake8", parts[3].strip()))
        return keys


# ── Core toolkit ───────────────────────────────────────────────

class StaticAnalyzer:
    """
    Runs the analysis tools on code held in memory (written to a temp folder).

    The pipeline always calls it directly — these checks should run on every
    review, so there is no decision for an LLM to make. External agents reach
    the same checks through the MCP server.
    """

    def __init__(self):
        self.tools_available = {
            "bandit":    BANDIT_AVAILABLE,
            "flake8":    FLAKE8_AVAILABLE,
            "pip-audit": PIP_AUDIT_AVAILABLE,
        }

    def run_code_review(self, python_files: dict[str, str]) -> AnalysisReport:
        """
        Run all available static analysis tools against the generated Python files.

        Args:
            python_files: dict of {file_path: content} for Python source files

        Returns:
            AnalysisReport with all findings, ready to inject into LLM context
        """
        report = AnalysisReport(tools_available=self.tools_available)

        if not python_files:
            return report

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Write all Python files to temp dir
            for rel_path, content in python_files.items():
                dest = tmp / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")

            # ── Run bandit (security) ─────────────────────────
            if BANDIT_AVAILABLE:
                bandit_result = self._run_bandit(tmp)
                report.security_issues = bandit_result
                report.tools_run.append("bandit")

            # ── Run flake8 (style) ────────────────────────────
            if FLAKE8_AVAILABLE:
                flake8_result = self._run_flake8(tmp)
                report.style_issues = flake8_result
                report.tools_run.append("flake8")

        return report

    def run_dependency_audit(self, requirements_content: str) -> AnalysisReport:
        """
        Run pip-audit against a requirements.txt to find vulnerable packages.

        Args:
            requirements_content: raw text of requirements.txt

        Returns:
            AnalysisReport with vulnerability findings
        """
        report = AnalysisReport(tools_available=self.tools_available)

        if not PIP_AUDIT_AVAILABLE or not requirements_content.strip():
            return report

        with tempfile.TemporaryDirectory() as tmpdir:
            req_file = Path(tmpdir) / "requirements.txt"
            req_file.write_text(requirements_content, encoding="utf-8")

            result = self._run_pip_audit(req_file)
            report.vuln_packages = result
            report.tools_run.append("pip-audit")

        return report

    # ── Private tool runners ──────────────────────────────────

    def _run_bandit(self, target_dir: Path) -> list[dict]:
        """Run bandit and return structured findings."""
        try:
            result = subprocess.run(
                [*_tool_cmd("bandit"), "-r", str(target_dir), "-f", "json", "-q",
                 "--severity-level", "low"],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout.strip() or result.stderr.strip()
            if not output:
                return []
            data = json.loads(output)
            results = data.get("results", [])
            # Report project-relative paths, not the throwaway temp dir
            for issue in results:
                try:
                    issue["filename"] = str(Path(issue["filename"]).relative_to(target_dir))
                except (KeyError, ValueError):
                    pass
            return results
        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception):
            return []

    def _run_flake8(self, target_dir: Path) -> list[str]:
        """Run flake8 and return list of violation strings."""
        try:
            result = subprocess.run(
                [*_tool_cmd("flake8"), str(target_dir),
                 "--max-line-length=120",
                 "--ignore=E501,W503,E302,E303,W291,W293,W292",
                 "--count"],
                capture_output=True, text=True, timeout=30
            )
            lines = [
                l.replace(str(target_dir) + "/", "")
                for l in result.stdout.strip().splitlines()
                if l.strip() and not l.strip().isdigit()
            ]
            return lines  # as_context_string() caps what reaches the LLM
        except (subprocess.TimeoutExpired, Exception):
            return []

    def _run_pip_audit(self, req_file: Path) -> list[dict]:
        """Run pip-audit and return vulnerability findings."""
        try:
            result = subprocess.run(
                [*_tool_cmd("pip-audit"), "-r", str(req_file), "--format", "json"],
                capture_output=True, text=True, timeout=60
            )
            output = result.stdout.strip()
            if not output:
                return []
            data = json.loads(output)
            vulns = []
            for dep in data.get("dependencies", []):
                for vuln in dep.get("vulns", []):
                    vulns.append({
                        "name": dep.get("name"),
                        "version": dep.get("version"),
                        "id": vuln.get("id"),
                        "description": vuln.get("description", "")[:200],
                    })
            return vulns
        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception):
            return []


# Singleton toolkit instance
analyzer = StaticAnalyzer()
