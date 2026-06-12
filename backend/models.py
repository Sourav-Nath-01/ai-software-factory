"""API request/response models."""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class Provider(str, Enum):
    OPENAI  = "openai"
    GEMINI  = "gemini"
    GROQ    = "groq"
    DEMO    = "demo"


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=10, description="What to build")
    api_key: Optional[str] = Field(None, description="API key for the selected provider")
    provider: Provider = Field(Provider.DEMO, description="LLM provider")
    model: str = Field("gemini/gemini-1.5-flash", description="Full LiteLLM model string")
    max_review_iterations: int = Field(2, ge=1, le=5)
    max_test_fix_iterations: int = Field(2, ge=1, le=5)


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class RunMetrics(BaseModel):
    files_generated: int = 0
    lines_of_code: int = 0
    review_iterations: int = 0
    test_fix_iterations: int = 0
    issues_found: int = 0
    issues_fixed: int = 0
    tests_passed: bool = False
    duration_seconds: float = 0.0


class CodeFileResponse(BaseModel):
    file_path: str
    content: str
    language: str = "python"


class RunSummary(BaseModel):
    run_id: str
    prompt: str
    status: RunStatus
    model: str
    created_at: str
    metrics: Optional[RunMetrics] = None


class RunResult(BaseModel):
    run_id: str
    prompt: str
    status: RunStatus
    model: str
    created_at: str
    metrics: Optional[RunMetrics] = None
    files: list[CodeFileResponse] = []
    error: Optional[str] = None
