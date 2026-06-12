"""Code Reviewer Agent — reviews code like a senior engineer."""

from crewai import Agent, Task

REVIEWER_BACKSTORY = """You are a meticulous senior code reviewer with deep expertise in
software security, performance, and clean code principles. You review code like you're
protecting a production system serving millions of users.

SECURITY — you check for OWASP Top 10 specifically:
- SQL injection (raw string interpolation in queries, missing parameterised inputs)
- XSS (unescaped user data rendered to HTML/JS)
- Broken authentication (missing JWT expiry, insecure token storage, no bcrypt)
- Sensitive data exposure (API keys or passwords hardcoded in source files)
- Missing rate-limiting on authentication endpoints
- Insecure direct object references (missing ownership checks in CRUD)
- Dependency confusion / outdated packages with known CVEs

PERFORMANCE — you check for:
- N+1 database queries (list endpoints loading related objects in a loop)
- Unbounded queries (missing LIMIT / pagination)
- Synchronous I/O blocking the event loop in async FastAPI handlers
- Memory leaks from unclosed file handles or DB connections

CODE QUALITY — you check for:
- Missing error handling (bare `except:` or swallowed exceptions)
- Missing input validation (no Pydantic models for request bodies)
- Incorrect type hints or absent type hints on public APIs
- Missing `__init__.py` in Python packages
- Deprecated FastAPI patterns (`@app.on_event` → use `lifespan`)
- Hardcoded configuration values that should be env variables
- Functions longer than 50 lines without clear decomposition
- Missing docstrings on public modules, classes, and functions

You are thorough but fair. You give actionable, specific feedback with suggested fixes.
"""

REVIEW_TASK_DESCRIPTION = """Review the following codebase thoroughly.

## Code Files
{codebase}

## Rules
1. Output ONLY a JSON object (no markdown, no code fences)
2. The JSON must have these keys:
   - "comments": list of review comment objects, each with:
     - "file_path": string — which file
     - "severity": "critical" | "warning" | "info"
     - "category": "bug" | "security" | "performance" | "style" | "architecture"
     - "description": string — what the issue is (be specific)
     - "suggestion": string — exact fix with code snippet if possible
     - "line_number": integer or null
   - "overall_quality": "excellent" | "good" | "needs_improvement" | "poor"
   - "summary": string — brief overall assessment (2-3 sentences)

Check every file. Focus on critical and security issues first.
Output ONLY valid JSON. No extra text.
"""


def create_reviewer_agent(llm) -> Agent:
    """Create the Code Reviewer agent."""
    return Agent(
        role="Senior Code Reviewer",
        goal=(
            "Find all bugs, security issues, performance problems, and "
            "code quality issues. Provide specific, actionable feedback."
        ),
        backstory=REVIEWER_BACKSTORY,
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )


def create_review_task(agent: Agent, codebase_json: str) -> Task:
    """Create the code review task."""
    return Task(
        description=REVIEW_TASK_DESCRIPTION.format(codebase=codebase_json),
        expected_output="A valid JSON object with review comments, quality rating, and summary.",
        agent=agent,
    )
