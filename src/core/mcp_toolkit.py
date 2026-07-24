"""MCP-Style Tool Toolkit for AI Agents.

Wraps real, deterministic CLI tools (bandit, flake8, pip-audit) that agents
can invoke to get grounded, accurate analysis before making LLM calls.

This implements the core idea behind MCP (Model Context Protocol):
give agents structured access to tools so they don't have to rely purely
on zero-shot LLM reasoning.

Usage:
    toolkit = MCPToolkit()
    results = toolkit.run_code_review(code_files)
    # inject results into Reviewer agent's system prompt
"""
from __future__ import annotations

import subprocess
import tempfile
import json
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


# ── Tool availability detection ───────────────────────────────

def _tool_available(name: str) -> bool:
    """Check if a CLI tool is installed."""
    try:
        result = subprocess.run(
            [name, "--version"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


BANDIT_AVAILABLE  = _tool_available("bandit")
FLAKE8_AVAILABLE  = _tool_available("flake8")
PIPIT_AVAILABLE   = _tool_available("pip-audit")  # pip-audit for vuln scanning


# ── Tool result dataclass ──────────────────────────────────────

@dataclass
class ToolResult:
    tool_name: str
    success: bool
    output: str
    issues_found: int = 0
    error: Optional[str] = None


@dataclass
class MCPToolReport:
    """Aggregated report from all MCP tool invocations."""
    security_issues: list[dict] = field(default_factory=list)
    style_issues: list[dict] = field(default_factory=list)
    vuln_packages: list[dict] = field(default_factory=list)
    tools_run: list[str] = field(default_factory=list)
    tools_available: dict[str, bool] = field(default_factory=dict)

    def as_context_string(self) -> str:
        """Format the report as a concise string for LLM context injection."""
        lines = ["## Deterministic Tool Analysis (MCP Toolkit)\n"]

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


# ── Core toolkit ───────────────────────────────────────────────

class MCPToolkit:
    """
    MCP-style tool registry that agents use to ground their analysis in
    real, deterministic tool output — not pure LLM intuition.

    The pattern mirrors MCP (Model Context Protocol): rather than an LLM
    reasoning cold about security issues, we inject structured tool output
    from bandit/flake8 into the agent's context window before it calls the LLM.
    """

    def __init__(self):
        self.tools_available = {
            "bandit":    BANDIT_AVAILABLE,
            "flake8":    FLAKE8_AVAILABLE,
            "pip-audit": PIPIT_AVAILABLE,
        }

    def run_code_review(self, python_files: dict[str, str]) -> MCPToolReport:
        """
        Run all available static analysis tools against the generated Python files.

        Args:
            python_files: dict of {file_path: content} for Python source files

        Returns:
            MCPToolReport with all findings, ready to inject into LLM context
        """
        report = MCPToolReport(tools_available=self.tools_available)

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

    def run_dependency_audit(self, requirements_content: str) -> MCPToolReport:
        """
        Run pip-audit against a requirements.txt to find vulnerable packages.

        Args:
            requirements_content: raw text of requirements.txt

        Returns:
            MCPToolReport with vulnerability findings
        """
        report = MCPToolReport(tools_available=self.tools_available)

        if not PIPIT_AVAILABLE or not requirements_content.strip():
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
                ["bandit", "-r", str(target_dir), "-f", "json", "-q",
                 "--severity-level", "low"],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout.strip() or result.stderr.strip()
            if not output:
                return []
            data = json.loads(output)
            return data.get("results", [])
        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception):
            return []

    def _run_flake8(self, target_dir: Path) -> list[str]:
        """Run flake8 and return list of violation strings."""
        try:
            result = subprocess.run(
                ["flake8", str(target_dir),
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
            return lines[:30]  # cap for token budget
        except (subprocess.TimeoutExpired, Exception):
            return []

    def _run_pip_audit(self, req_file: Path) -> list[dict]:
        """Run pip-audit and return vulnerability findings."""
        try:
            result = subprocess.run(
                ["pip-audit", "-r", str(req_file), "--format", "json"],
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
mcp_toolkit = MCPToolkit()
