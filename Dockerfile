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

# Install Docker CLI (for sandboxed execution)
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[web]"

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
