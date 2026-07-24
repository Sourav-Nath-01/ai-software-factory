---
title: AI Software Factory
emoji: 🏭
colorFrom: blue
colorTo: purple
sdk: docker
pinned: true
license: mit
short_description: 7 AI agents collaborate to build your software end-to-end
---

# 🏭 AI Software Factory

> **A production-ready, multi-agent AI pipeline that automatically plans, codes, reviews, tests, and deploys software — powered by free-tier LLMs (Gemini / Groq).**

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Try%20it%20on%20Hugging%20Face-ffcc00?logo=huggingface)](https://huggingface.co/spaces/SouravNath/ai-software-factory)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18+-61DAFB?logo=react)](https://reactjs.org)
[![Tests](https://img.shields.io/badge/Tests-pytest-orange?logo=pytest)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🌟 Enterprise-Grade Features

We recently upgraded this project from a prototype to a production-ready architecture, solving the most common issues in multi-agent systems:

1. **Model Context Protocol (MCP) Grounding:** The pipeline runs real deterministic CLI tools (`bandit` for security, `flake8` for syntax) and injects the output into the AI's context. This eliminates LLM hallucination during code reviews.
2. **Proactive Token Bucket Rate Limiting:** Prevent `429 Too Many Requests` API errors. The system estimates payload tokens and sleeps *before* making the API call if the bucket capacity is low, dynamically handling Free-Tier API limits.
3. **Human-in-the-Loop (HITL) Checkpoints:** The pipeline gracefully pauses via zero-CPU `threading.Event()` after the Architecture Planning stage, presenting a UI modal for the user to Approve or Reject the blueprint before expensive code generation begins.
4. **Graceful Pipeline Cancellation:** Click "Stop" at any time. The background thread checks for a cancellation signal at stage boundaries and aborts cleanly without leaving corrupted state.
5. **Interactive Diff Viewer:** The backend stores snapshots of the raw generated codebase, allowing users to view side-by-side git-style diffs of exactly what the Reviewer Agent fixed.

---

## 📊 Benchmark Results

**One full real pipeline run on Groq Llama 3.3 70B (free tier)**

Prompt: *"Build a REST API for a todo app with FastAPI, SQLite, JWT auth, CRUD operations, and proper error handling"*

| Metric | Measured Value |
|---|---|
| **Files generated** | **24 files** (12 source + 8 tests + 3 deployment + 1 config) |
| **Lines of code** | **480 LOC** |
| **Security issues found** | **13 issues** across 2 review iterations |
| **Issues fixed** | **13 issues fixed** by the Improver agent |
| **Review iterations** | **2 cycles** (Reviewer → Improver → Reviewer loop) |
| **Total pipeline duration** | **3 minutes 42 seconds** |

---

## 🏗️ Architecture

```
User Prompt
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│         FastAPI Backend + Custom Orchestrator           │
│                                                         │
│  Planner ──→ Coder ──→ Reviewer ──┐                    │
│                               ↕   │  (Pydantic State)  │
│                           Improver ┘                    │
│                               │                         │
│  Deployer ←── TestRunner ←── Tester                     │
│                   │                                     │
│            (Docker Sandbox / subprocess)                │
└─────────────────────────────────────────────────────────┘
    │                           │
    ▼                           ▼
React Dashboard         queue.Queue → asyncio WS
(Live WebSocket)        (thread/async bridge)
```

**Key design decision:** The blocking pipeline runs in a daemon `threading.Thread`. Events are pushed to a `queue.Queue`. The async WebSocket handler polls via `asyncio.run_in_executor` — bridging the sync/async boundary without a message broker like Redis.

---

## 🤖 The 7 Agents

| Agent | Role | Responsibility |
|---|---|---|
| **Planner** | Software Architect | Designs architecture, file structure, API contracts |
| **Coder** | Software Engineer | Writes complete code based *only* on the Planner's blueprint |
| **Reviewer** | Code Reviewer | Finds bugs, OWASP Top 10 vulnerabilities, N+1 queries, anti-patterns |
| **Improver** | Improvement Specialist | Applies targeted fixes for every issue the Reviewer found |
| **Tester** | QA Engineer | Writes comprehensive pytest suites with fixtures and edge cases |
| **Test Runner** | QA Analyst | Executes tests in sandboxed environment, analyzes failures |
| **Deployer** | DevOps Engineer | Generates `Dockerfile`, `docker-compose.yml`, CI/CD pipeline |

---

## 🚀 Quick Start

### Web UI (Recommended)

```bash
git clone https://github.com/Sourav-Nath-01/ai-software-factory.git
cd ai-software-factory
./run.sh
```

- **Frontend:** http://localhost:3000
- **API Docs:** http://localhost:8000/docs

### CLI Mode

```bash
source .venv/bin/activate
python3 -m src.main
```

The interactive wizard will guide you through provider selection (Gemini/Groq/OpenAI) and API key setup.

### Supported Providers

| Provider | Model | Cost | Limit |
|---|---|---|---|
| **Google AI Studio** | `gemini-2.0-flash` | **FREE** | 1M TPM |
| **Google AI Studio** | `gemini-2.0-flash-lite` | **FREE** | Fastest |
| **Groq** | `llama-3.1-8b-instant` | **FREE** | 30K TPM/min |
| **Groq** | `llama-3.3-70b-versatile` | **FREE** | 12K TPM/min |
| OpenAI | `gpt-4o-mini` | ~$0.01/run | Paid |

> **Demo Mode** — runs the full pipeline locally using stage-aware mock responses. No API key required. Perfect for portfolio demonstrations.

---

## 🧪 Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

The test suite covers:
- **JSON repair** — edge cases for malformed LLM output (`_extract_json`, `_safe_json`)
- **RunStore** — thread-safety, CRUD, file persistence
- **MockLLM** — stage-aware routing verification
- **Pipeline (demo mode)** — all 7 stages, event emission, metrics accuracy
- **FastAPI REST endpoints** — run lifecycle, metrics regression, ZIP download

---

## 🛠️ Project Structure

```
ai-software-factory/
├── src/
│   ├── core/
│   │   ├── pipeline.py     # Custom orchestration engine & rate-limiting
│   │   ├── models.py       # Pydantic state contracts (inter-agent memory)
│   │   ├── memory.py       # ChromaDB vector memory (with fallback)
│   │   ├── mock_llm.py     # Stage-aware mock for demo mode
│   │   └── sandbox.py      # Docker sandbox (subprocess fallback)
│   ├── agents/             # Isolated system prompts for all 7 agents
│   └── tools/              # file_writer.py, code_executor.py
├── backend/
│   ├── main.py             # FastAPI app (serves React + API)
│   ├── ws.py               # WebSocket event streaming
│   ├── routes/runs.py      # REST: run lifecycle, download ZIP
│   └── store/run_store.py  # Thread-safe JSON persistence
├── frontend/               # React 18 + Vite + TypeScript
│   └── src/pages/          # Landing, Build, Result, History
├── tests/
│   └── test_pipeline.py    # Full test suite (pytest)
├── Dockerfile              # Production container
├── docker-compose.yml      # One-command deploy
└── run.sh                  # Dev launcher (backend + frontend)
```

---

## 📜 License
MIT
