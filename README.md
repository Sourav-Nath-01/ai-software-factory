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

> **A multi-agent LLM pipeline that plans, codes, reviews, tests, and packages software for deployment — with tool-grounded review, sandboxed test execution, and a reproducible eval harness. Runs on free-tier LLMs (Gemini / Groq).**

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Try%20it%20on%20Hugging%20Face-ffcc00?logo=huggingface)](https://huggingface.co/spaces/SouravNath/ai-software-factory)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18+-61DAFB?logo=react)](https://reactjs.org)
[![CI](https://github.com/Sourav-Nath-01/ai-software-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/Sourav-Nath-01/ai-software-factory/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🌟 Key Features

1. **Tool-Grounded Review:** Before the Reviewer agent runs, the pipeline runs real deterministic tools — `bandit` (security), `flake8` (pyflakes/style), `pip-audit` (vulnerable dependencies) and a PyPI existence check (hallucinated packages) — and injects their findings into its context, so the review starts from facts rather than pure LLM judgement. The pipeline calls these tools directly, because they should run on every review.
2. **MCP Server for Coding Agents:** The same verification layer is exposed as a real [Model Context Protocol](https://modelcontextprotocol.io) server, so Claude Code, Cursor, VS Code or Claude Desktop can check the code *they* write: static analysis, dependency checks, tests in the sandbox, and findings introduced since a git revision ([setup below](#-mcp-server-verify-code-from-claude-code-cursor-or-vs-code)).
3. **Verified Metrics:** "Issues fixed" is only counted when the tools agree — findings present before the review loop must be gone after it. "Tests passed" comes from pytest's exit code, never from an agent's claim.
4. **Sandboxed Test Execution:** Generated tests run in Docker with `--network none` and CPU/memory/process limits; dependencies are installed in a separate container and cached by requirements hash. Without Docker it falls back to a subprocess with secrets scrubbed from its environment.
5. **Proactive Token Bucket Rate Limiting:** Estimates request tokens and waits *before* calling the API when the bucket (tokens/min + requests/min) is low, instead of reacting to `429` errors. Reactive retry remains as a fallback.
6. **Human-in-the-Loop Checkpoint:** The pipeline pauses on a `threading.Event` after Planning so the user can approve or reject the architecture before code generation.
7. **Graceful Cancellation:** "Stop" aborts at the next stage or loop boundary, and immediately interrupts rate-limit and retry waits.
8. **Retrieval-Augmented Review:** Serious issues from past reviews are stored in a ChromaDB vector store and retrieved for similar new projects (by project description and stack), so recurring mistakes get checked first.
9. **Diff Viewer:** Snapshots of the Coder's original output let you see exactly what the review loop changed.

---

## 📊 Benchmark Results

**One full real pipeline run on Groq Llama 3.3 70B (free tier)**

Prompt: *"Build a REST API for a todo app with FastAPI, SQLite, JWT auth, CRUD operations, and proper error handling"*

| Metric | Measured Value |
|---|---|
| **Files generated** | **24 files** (12 source + 8 tests + 3 deployment + 1 config) |
| **Lines of code** | **480 LOC** |
| **Security issues found** | **13 issues** across 2 review iterations |
| **Review iterations** | **2 cycles** (Reviewer → Improver → Reviewer loop) |
| **Total pipeline duration** | **3 minutes 42 seconds** |

> This is a single run. "Issues fixed" is no longer reported from the agent's own claims — it is now verified by re-running bandit + flake8 (see Evaluation below).

---

## 📏 Evaluation

`evals/` contains a reproducible harness that runs the pipeline over **25 prompts** (REST APIs, CLIs, data processing, algorithms, libraries, games) under four **ablation configs**, each adding one component:

| Config | Adds |
|---|---|
| `no_review` | Coder output goes straight to tests |
| `llm_review` | Reviewer/Improver loop, LLM judgement only |
| `grounded_review` | Reviewer grounded in bandit / flake8 / pip-audit output |
| `full` | Test-fix loop (production config) |

Every metric is **measured by the harness on the final code**, never taken from agent output: syntax validity, generated-test pass rate (pytest in the sandbox), bandit high/medium findings, pyflakes findings, verified fixes, tokens, cost and latency. The report compares configs **paired on the same prompts** with 95% bootstrap confidence intervals.

```bash
python -m evals.run_eval --demo --limit 2                                  # smoke test, no API key
python -m evals.run_eval --model groq/openai/gpt-oss-120b                  # key from .env
python -m evals.run_eval --out evals/results/<run>                         # resume after a quota stop
python -m evals.report evals/results/<run>                                 # re-render report.md
```

Results are appended after every run, so free-tier daily quota stops lose nothing. Add `--configs ...,grounded_memory` to also measure retrieval from past reviews (opt-in, since its memory builds up across prompts in run order).

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

**Key design decision:** The blocking pipeline runs in a daemon `threading.Thread`. Events are pushed to a per-run `queue.Queue`, which the async WebSocket handler drains with non-blocking polls — bridging the sync/async boundary without a message broker like Redis. The trade-off: all run state lives in one process, so scaling out would need a broker (e.g. Redis pub/sub) and a task queue.

The seven "agents" are specialised LLM roles (separate system prompts and output schemas) orchestrated as a fixed pipeline with two feedback loops — not autonomous agents that choose their own tools.

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
| **Google AI Studio** | `gemini-2.5-flash` | **FREE** | Large token budget, ~10 RPM |
| **Google AI Studio** | `gemini-2.5-flash-lite` | **FREE** | Fastest |
| **Groq** | `openai/gpt-oss-120b` | **FREE** | 8K TPM, 200K tokens/day |
| **Groq** | `openai/gpt-oss-20b` | **FREE** | 8K TPM, 200K tokens/day |
| OpenAI | `gpt-4o-mini` | ~$0.01/run | Paid |

> Groq's free-tier quotas are per model, so running different models in parallel multiplies throughput. Some models also cap output tokens per minute (qwen3.8-27b was rejected at ~1K), which rules them out for multi-file code generation. Free-tier model lists and limits change often — Groq reports your current limits in its `x-ratelimit-*` response headers. The rate limiter sizes each request's output budget to fit the tier.

> **Demo Mode** — runs the full pipeline locally using stage-aware mock responses. No API key required. Perfect for portfolio demonstrations.

---

## 🔌 MCP Server: verify code from Claude Code, Cursor or VS Code

The verification layer runs as an MCP server, so a coding agent can check its own work with deterministic tools instead of judging it itself.

| Tool | What it returns |
|---|---|
| `analyze_code(path)` | bandit security findings (most severe first) and flake8/pyflakes findings |
| `check_dependencies(path)` | packages that **don't exist on PyPI** (likely hallucinated), packages first published < 90 days ago, and known vulnerabilities from pip-audit |
| `run_tests(path)` | the project's pytest results, run on a copy in the network-isolated Docker sandbox |
| `diff_findings(path, base="HEAD")` | findings your changes **introduced** and **fixed** compared with a git revision |

```bash
pip install -e ".[analysis,mcp]"          # creates .venv/bin/ai-factory-mcp

# Claude Code (run inside the project you want verified)
claude mcp add verifier --env VERIFIER_ROOTS="$PWD" -- /abs/path/to/.venv/bin/ai-factory-mcp
```

Cursor (`.cursor/mcp.json`) or Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "verifier": {
      "command": "/abs/path/to/.venv/bin/ai-factory-mcp",
      "env": { "VERIFIER_ROOTS": "/abs/path/to/your/project" }
    }
  }
}
```

**Safety:** tools only read inside `VERIFIER_ROOTS` (default: the directory the server starts in — set it explicitly); `run_tests` executes a *copy* of the project in Docker with no network, and refuses to run without Docker unless `VERIFIER_ALLOW_UNSANDBOXED=1`; git revisions are validated before they reach git. The sandbox's dependency cache lives in `~/.cache/ai-software-factory` (`VERIFIER_CACHE_DIR`), never in your project.

Example: in a small project pinning `requests==2.32.3`, `check_dependencies` reports the published advisory fixed in 2.32.4, and `run_tests` installs the dependency in one container and runs the tests offline in another.

---

## ☁️ Deploy (Azure VM with the full sandbox)

One command in Azure Cloud Shell creates a small Ubuntu VM running the app behind Caddy (automatic HTTPS), with generated code still executed in the network-isolated Docker sandbox:

```bash
git clone https://github.com/Sourav-Nath-01/ai-software-factory.git
bash ai-software-factory/deploy/azure/provision.sh
```

Why a VM rather than App Service or Container Apps, what it costs, and how to update or delete it: [deploy/azure/README.md](deploy/azure/README.md).

---

## 🧪 Running Tests

```bash
pip install -e ".[dev,analysis,mcp]"
pytest tests/ -v
```

The test suite covers:
- **JSON repair** — edge cases for malformed LLM output (`_extract_json`, `_safe_json`)
- **RunStore** — thread-safety, CRUD, file persistence
- **MockLLM** — stage-aware routing verification
- **Pipeline (demo mode)** — all 7 stages, event emission, metrics accuracy
- **FastAPI REST endpoints** — run lifecycle, metrics regression, ZIP download
- **Robustness regressions** — prompt budgeting, file merging, per-run limits, fail-safe memory, verified fixes, secret scrubbing
- **Eval harness** — measurement, pytest parsing, paired bootstrap stats, end-to-end demo run with resume
- **MCP server** — every tool through a real MCP client, path and git-revision safety rules, sandbox refusal, and a full stdio session with the server as a subprocess
- **Dependency check** — requirement parsing, hallucinated / brand-new / unverifiable packages, and the Reviewer being told about them

---

## 🛠️ Project Structure

```
ai-software-factory/
├── src/
│   ├── core/
│   │   ├── pipeline.py     # Custom orchestration engine & rate-limiting
│   │   ├── models.py       # Pydantic state contracts (inter-agent memory)
│   │   ├── memory.py       # ChromaDB memory of past review issues (fail-safe fallback)
│   │   ├── static_analysis.py # bandit / flake8 / pip-audit (grounding + verification)
│   │   ├── dependency_check.py # packages that don't exist on PyPI (hallucinated) or are brand new
│   │   ├── rate_limiter.py # proactive token bucket
│   │   ├── mock_llm.py     # Stage-aware mock for demo mode
│   │   └── sandbox.py      # Docker sandbox (subprocess fallback)
│   ├── mcp_server.py       # MCP server exposing the verification tools to coding agents
│   ├── agents/             # Isolated system prompts for all 7 agents
│   └── tools/              # file_writer.py, code_executor.py
├── backend/
│   ├── main.py             # FastAPI app (serves React + API)
│   ├── ws.py               # WebSocket event streaming
│   ├── routes/runs.py      # REST: run lifecycle, download ZIP
│   └── store/run_store.py  # Thread-safe JSON persistence
├── frontend/               # React 18 + Vite + TypeScript
│   └── src/pages/          # Landing, Build, Result, History
├── evals/                  # Eval harness: prompts, ablation runner, report
├── tests/                  # pipeline, eval harness and MCP server tests (pytest)
├── deploy/azure/           # One-command Azure VM deployment (cloud-init, compose, Caddy)
├── .github/workflows/      # CI: tests on Python 3.10 + 3.12, frontend build
├── Dockerfile              # Production container
├── docker-compose.yml      # One-command deploy
└── run.sh                  # Dev launcher (backend + frontend)
```

---

## 📜 License
MIT
