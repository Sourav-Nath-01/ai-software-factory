# ── Stage 1: Build React frontend ───────────────────────────
FROM node:20-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python backend + serve everything ───────────────
FROM python:3.11-slim
WORKDIR /app

# Docker CLI for the sandbox: it talks to the host's daemon through the mounted socket.
# Debian's docker.io client (API 1.41) is too old for current daemons, so take the
# official static CLI binary; it negotiates the API version with the daemon.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps (patient with slow package mirrors: a 15 s read timeout broke a real build)
ENV PIP_DEFAULT_TIMEOUT=100 PIP_RETRIES=10
COPY pyproject.toml README.md ./
# analysis: bandit/flake8/pip-audit for review grounding
# pytest: generated tests run in-process here (Spaces has no Docker daemon)
RUN pip install --no-cache-dir -e ".[web,analysis]" pytest

# Copy source
COPY src/ ./src/
COPY backend/ ./backend/

# Copy built frontend
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

# Create output dirs
RUN mkdir -p output/runs output/memory

# HuggingFace Spaces uses port 7860
EXPOSE 7860

ENV PORT=7860
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
