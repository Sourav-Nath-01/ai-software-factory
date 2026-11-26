"""Mock LLM for demo/dry-run mode — simulates the full pipeline without an API key.

Response selection is STAGE-AWARE: it looks at the system prompt to decide which
pre-built response to return, so it never breaks when optional stages are skipped.
"""

import json
import random
import re
import time

from src.core.config import settings


# ── Pre-built realistic mock responses ────────────────────────

MOCK_PLAN = {
    "project_name": "todo_api",
    "description": "A RESTful Todo API with CRUD operations, user authentication, and SQLite storage.",
    "tech_stack": ["Python", "FastAPI", "SQLite", "Pydantic", "uvicorn"],
    "file_structure": [
        "app/__init__.py",
        "app/main.py",
        "app/models.py",
        "app/database.py",
        "app/routers/todos.py",
        "app/routers/auth.py",
        "requirements.txt",
    ],
    "modules": [
        "app: FastAPI application setup and routing",
        "app.models: Pydantic schemas for request/response validation",
        "app.database: SQLite connection and table creation",
        "app.routers.todos: CRUD endpoints for todo items",
        "app.routers.auth: User registration and JWT authentication",
    ],
    "endpoints": [
        {"method": "GET",    "path": "/todos",         "description": "List all todos"},
        {"method": "POST",   "path": "/todos",         "description": "Create a new todo"},
        {"method": "GET",    "path": "/todos/{id}",    "description": "Get a specific todo"},
        {"method": "PUT",    "path": "/todos/{id}",    "description": "Update a todo"},
        {"method": "DELETE", "path": "/todos/{id}",    "description": "Delete a todo"},
        {"method": "POST",   "path": "/auth/register", "description": "Register a new user"},
        {"method": "POST",   "path": "/auth/login",    "description": "Login and get JWT token"},
    ],
    "additional_notes": "Uses SQLite for simplicity. PBKDF2 password hashing and opaque bearer tokens (stdlib only).",
}

MOCK_CODE_FILES = [
    {
        "file_path": "app/__init__.py",
        "content": '"""Todo API Application."""\n',
        "language": "python",
    },
    {
        "file_path": "app/main.py",
        "content": '''"""FastAPI application entry point."""

from fastapi import FastAPI
from app.routers import todos, auth
from app.database import create_tables

app = FastAPI(title="Todo API", version="1.0.0")

app.include_router(todos.router, prefix="/todos", tags=["todos"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])


@app.on_event("startup")
async def startup():
    """Initialize database on startup."""
    create_tables()


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
''',
        "language": "python",
    },
    {
        "file_path": "app/models.py",
        "content": '''"""Pydantic models for request/response validation."""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class TodoCreate(BaseModel):
    """Schema for creating a todo."""
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    completed: bool = False


class TodoResponse(BaseModel):
    """Schema for todo response."""
    id: int
    title: str
    description: Optional[str]
    completed: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    """Schema for user registration."""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


class Token(BaseModel):
    """JWT token response."""
    access_token: str
    token_type: str = "bearer"
''',
        "language": "python",
    },
    {
        "file_path": "app/database.py",
        "content": '''"""SQLite database setup and connection management."""

import sqlite3
from pathlib import Path

DB_PATH = Path("data/todos.db")


def get_connection() -> sqlite3.Connection:
    """Get a database connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    """Create database tables if they don\'t exist."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            completed BOOLEAN DEFAULT 0,
            user_id INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.close()
''',
        "language": "python",
    },
    {
        "file_path": "app/routers/todos.py",
        "content": '''"""Todo CRUD endpoints."""

from fastapi import APIRouter, HTTPException
from app.models import TodoCreate, TodoResponse
from app.database import get_connection

router = APIRouter()


@router.get("/", response_model=list[TodoResponse])
async def list_todos():
    """List all todos."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM todos ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@router.get("/search", response_model=list[TodoResponse])
async def search_todos(q: str):
    """Search todos by title."""
    conn = get_connection()
    rows = conn.execute(f"SELECT * FROM todos WHERE title LIKE '%{q}%'").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@router.post("/", response_model=TodoResponse, status_code=201)
async def create_todo(todo: TodoCreate):
    """Create a new todo."""
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO todos (title, description, completed) VALUES (?, ?, ?)",
        (todo.title, todo.description, todo.completed),
    )
    conn.commit()
    todo_id = cursor.lastrowid
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    conn.close()
    return dict(row)


@router.delete("/{todo_id}", status_code=204)
async def delete_todo(todo_id: int):
    """Delete a todo."""
    conn = get_connection()
    result = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Todo not found")
    conn.close()
''',
        "language": "python",
    },
    {
        "file_path": "app/routers/__init__.py",
        "content": '"""API routers."""\n',
        "language": "python",
    },
    {
        "file_path": "app/routers/auth.py",
        "content": '''"""User registration and login endpoints."""

import hashlib
import hmac
import os
import secrets
import sqlite3

from fastapi import APIRouter, HTTPException
from app.models import UserCreate, Token
from app.database import get_connection

router = APIRouter()


def _hash_password(password: str, salt: bytes) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


@router.post("/register", status_code=201)
async def register(user: UserCreate):
    """Register a new user."""
    salt = os.urandom(16)
    stored = salt.hex() + "$" + _hash_password(user.password, salt)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (user.username, stored),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already taken")
    finally:
        conn.close()
    return {"username": user.username}


@router.post("/login", response_model=Token)
async def login(user: UserCreate):
    """Login and get a bearer token."""
    conn = get_connection()
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?", (user.username,)
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    salt_hex, expected = row["password_hash"].split("$", 1)
    if not hmac.compare_digest(_hash_password(user.password, bytes.fromhex(salt_hex)), expected):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return Token(access_token=secrets.token_urlsafe(32))
''',
        "language": "python",
    },
    {
        "file_path": "requirements.txt",
        "content": "fastapi==0.115.0\nuvicorn==0.30.0\npydantic==2.9.0\n",
        "language": "text",
    },
]

# The demo's one deliberate flaw: search_todos builds SQL with an f-string
# (SQL injection, bandit B608). The Reviewer flags it, the Improver parameterises
# the query, and the tools confirm the fix — so demo mode shows a verified fix
# without an API key.
_VULNERABLE_SEARCH = '''conn.execute(f"SELECT * FROM todos WHERE title LIKE '%{q}%'")'''
_FIXED_SEARCH = '''conn.execute("SELECT * FROM todos WHERE title LIKE ?", (f"%{q}%",))'''
_TODOS_ROUTER = next(f for f in MOCK_CODE_FILES if f["file_path"] == "app/routers/todos.py")
assert _VULNERABLE_SEARCH in _TODOS_ROUTER["content"]
MOCK_FIXED_FILES = [{**_TODOS_ROUTER,
                     "content": _TODOS_ROUTER["content"].replace(_VULNERABLE_SEARCH, _FIXED_SEARCH)}]

MOCK_REVIEW = {
    "comments": [
        {
            "file_path": "app/routers/todos.py",
            "severity": "warning",
            "category": "resource_management",
            "description": "Database connections are not using context managers.",
            "suggestion": "Wrap database calls in 'with get_connection() as conn:' blocks.",
            "line_number": None,
        },
        {
            "file_path": "app/database.py",
            "severity": "info",
            "category": "security",
            "description": "Consider adding connection pooling for better performance.",
            "suggestion": "Use a connection pool or async database library like 'databases'.",
            "line_number": None,
        },
    ],
    "overall_quality": "good",
    "summary": "Code is well-structured and follows FastAPI best practices. Minor improvements suggested for resource management.",
}

MOCK_REVIEW_FLAWED = {
    "comments": [
        {
            "file_path": "app/routers/todos.py",
            "severity": "critical",
            "category": "security",
            "description": "SQL injection: search_todos builds its query with an f-string, "
                           "so the q parameter is executed as SQL (bandit B608).",
            "suggestion": "Use a parameterised query: "
                          "conn.execute(\"... WHERE title LIKE ?\", (f\"%{q}%\",)).",
            "line_number": 22,
        },
        MOCK_REVIEW["comments"][0],
    ],
    "overall_quality": "needs_improvement",
    "summary": "One critical SQL injection in the search endpoint; otherwise well structured.",
}

MOCK_TEST_FILES = [
    {
        "file_path": "tests/test_todos.py",
        "content": '''"""Tests for the Todo API endpoints."""

import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    """Test client that runs startup events (creates the tables)."""
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_create_todo(client):
    """Test creating a new todo."""
    response = client.post("/todos/", json={"title": "Test todo"})
    assert response.status_code == 201
    assert response.json()["title"] == "Test todo"


def test_list_todos(client):
    """Test listing all todos."""
    response = client.get("/todos/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_search_todos(client):
    """Test searching todos by title."""
    client.post("/todos/", json={"title": "Buy milk"})
    response = client.get("/todos/search", params={"q": "milk"})
    assert response.status_code == 200
    assert any("milk" in todo["title"] for todo in response.json())
''',
        "language": "python",
    },
]

MOCK_TEST_RESULT = {
    "passed": True,
    "total_tests": 3,
    "passed_tests": 3,
    "failed_tests": 0,
    "failure_analysis": "All tests passed successfully.",
}

MOCK_DEPLOYMENT = {
    "files": [
        {
            "file_path": "Dockerfile",
            "content": "FROM python:3.11-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nEXPOSE 8000\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n",
            "language": "dockerfile",
        },
        {
            "file_path": "docker-compose.yml",
            "content": "version: '3.8'\nservices:\n  api:\n    build: .\n    ports:\n      - \"8000:8000\"\n    volumes:\n      - ./data:/app/data\n    restart: unless-stopped\n",
            "language": "yaml",
        },
    ],
    "instructions": "# Deployment\n\n## Docker\n```bash\ndocker-compose up -d\n```\n\nAPI will be available at http://localhost:8000\nDocs at http://localhost:8000/docs",
}


# ── Stage-aware MockLLM ────────────────────────────────────────

def _revised_plan(prompt: str) -> dict:
    """The mock Planner's answer to a revision request: the plan, visibly updated with the feedback."""
    match = re.search(r"## User Feedback\n(.*?)\n\n## Rules", prompt, re.DOTALL)
    feedback = match.group(1).strip() if match else ""
    return {**MOCK_PLAN,
            "description": f"{MOCK_PLAN['description']} Revised per review: {feedback}",
            "additional_notes": f"{MOCK_PLAN['additional_notes']} Review feedback applied: {feedback}"}


class MockLLM:
    """Fake LLM for demo mode — selects responses by stage, not by call count.

    Uses keywords from the system prompt to identify which agent is calling,
    so skipped stages (e.g. improve skipped when quality is 'good') never
    cause the response sequence to fall out of sync.
    """

    def call(self, system: str = "", user: str = "", **kwargs) -> str:
        """Simulate an LLM call with a short delay, returning stage-appropriate JSON."""
        time.sleep(random.uniform(0.8, 2.0) * settings.demo_delay_scale)
        s = system.lower()

        # Use first-sentence phrases from each backstory — must be UNIQUE per agent
        # (e.g. CODER_BACKSTORY says "architecture plan" so "architect" is ambiguous)

        if "world-class software architect" in s or "20 years of experience designing" in s:
            if "Revise this architecture plan" in user:
                return json.dumps(_revised_plan(user))
            return json.dumps(MOCK_PLAN)

        if "qa analyst who specializes in analyzing test results" in s or "test runner" in s:
            return json.dumps(MOCK_TEST_RESULT)

        if "qa test engineer who writes comprehensive" in s:
            return json.dumps({"files": MOCK_TEST_FILES})

        if "senior devops engineer" in s:
            return json.dumps(MOCK_DEPLOYMENT)

        if "meticulous senior code reviewer" in s:
            # Judge the code actually in the prompt: keep flagging the injection until it is gone
            flawed = "LIKE '%{q}%'" in user or "B608" in user
            return json.dumps(MOCK_REVIEW_FLAWED if flawed else MOCK_REVIEW)

        if "code improvement specialist" in s or "fix every issue" in s:
            return json.dumps({"files": MOCK_FIXED_FILES})  # only the changed file

        if "elite software engineer" in s or "production-ready" in s:
            return json.dumps({"files": MOCK_CODE_FILES})

        # Fallback
        return json.dumps({"files": MOCK_CODE_FILES})

