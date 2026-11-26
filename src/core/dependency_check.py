"""Dependency checks pip-audit can't do: does each package actually exist on PyPI?

LLMs sometimes invent package names ("package hallucination"). Anyone can
register such a name on PyPI and ship malware in it ("slopsquatting"), and
pip-audit only knows about packages that exist — so a non-existent name, or
one registered very recently, needs its own check before anything is installed.

Usage:
    result = check_requirements("fastapi==0.115.0\\nfastapi-security-utils\\n")
    result["missing_on_pypi"]        # ['fastapi-security-utils']
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable

PYPI_URL = "https://pypi.org/pypi/{name}/json"
RECENT_DAYS = 90  # first release newer than this = worth a human look

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(name: str) -> str:
    """PEP 503 normalised project name (Flask_Login == flask-login)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(text: str) -> list[str]:
    """Package names from requirements-style lines (pins, extras and markers stripped)."""
    names: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://", "file:")):
            continue  # options (-r, -e), VCS and URL requirements aren't PyPI names
        match = _NAME.match(line)
        if match and normalize(match.group(1)) not in {normalize(n) for n in names}:
            names.append(match.group(1))
    return names


@lru_cache(maxsize=2048)
def pypi_lookup(name: str) -> dict:
    """{"status": "exists" | "missing" | "unknown", "first_release": ISO date or None}."""
    request = urllib.request.Request(PYPI_URL.format(name=normalize(name)),
                                     headers={"User-Agent": "ai-software-factory-verifier"})
    try:
        # Always the fixed https://pypi.org URL above, never a caller-supplied scheme
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            data = json.load(response)
    except urllib.error.HTTPError as e:
        return {"status": "missing" if e.code == 404 else "unknown", "first_release": None}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {"status": "unknown", "first_release": None}  # offline: never a false "missing"

    uploads = [f.get("upload_time_iso_8601") or f.get("upload_time")
               for files in data.get("releases", {}).values() for f in files]
    uploads = [u for u in uploads if u]
    return {"status": "exists", "first_release": min(uploads) if uploads else None}


def _days_since(iso: str, now: datetime) -> float | None:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 86400


def check_requirements(text: str, lookup: Callable[[str], dict] = pypi_lookup,
                       now: datetime | None = None) -> dict:
    """Check every requirement against PyPI.

    Returns:
        checked: number of package names checked
        missing_on_pypi: names PyPI doesn't know — likely hallucinated
        recently_registered: [{"name", "first_release"}] first released < RECENT_DAYS ago
        unverified: names that couldn't be checked (offline, PyPI error)
    """
    now = now or datetime.now(timezone.utc)
    result = {"checked": 0, "missing_on_pypi": [], "recently_registered": [], "unverified": []}
    for name in requirement_names(text):
        result["checked"] += 1
        info = lookup(name)
        if info["status"] == "missing":
            result["missing_on_pypi"].append(name)
        elif info["status"] == "unknown":
            result["unverified"].append(name)
        elif info.get("first_release"):
            age = _days_since(info["first_release"], now)
            if age is not None and age < RECENT_DAYS:
                result["recently_registered"].append({"name": name, "first_release": info["first_release"]})
    return result


def as_context_string(result: dict) -> str:
    """Findings formatted for an LLM prompt (empty when there is nothing to report)."""
    lines = []
    if result["missing_on_pypi"]:
        lines.append("### Packages that do not exist on PyPI (likely hallucinated — remove or replace)")
        lines += [f"- {name}" for name in result["missing_on_pypi"]]
    if result["recently_registered"]:
        lines.append(f"### Packages first published less than {RECENT_DAYS} days ago "
                     "(verify they are the intended ones)")
        lines += [f"- {p['name']} (first release {p['first_release'][:10]})" for p in result["recently_registered"]]
    return "\n".join(lines)
