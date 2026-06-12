#!/usr/bin/env bash
# ── AI Software Factory — Dev Launcher ──────────────────────
# Starts backend API + opens frontend dev server
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Python venv ───────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "🔧 Creating Python virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate

# Install/update Python deps
echo "📦 Installing Python dependencies..."
pip install -q -e "." 2>&1 | tail -5

# ── Node deps ─────────────────────────────────────────────────
if [ ! -d "frontend/node_modules" ]; then
  echo "📦 Installing Node dependencies..."
  cd frontend && npm install --legacy-peer-deps && cd ..
fi

# ── Kill any stale processes on our ports ────────────────────
echo "🧹 Cleaning up stale processes..."
fuser -k 8000/tcp 2>/dev/null || true
fuser -k 3000/tcp 2>/dev/null || true
sleep 1

echo ""
echo "🚀 Starting FastAPI backend on http://localhost:8000"
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir backend --reload-dir src &
BACKEND_PID=$!

# Wait for backend
sleep 2

# ── Start frontend ────────────────────────────────────────────
echo "🎨 Starting React dev server on http://localhost:3000"
cd frontend && npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  🏭 AI Software Factory is running!      ║"
echo "║                                          ║"
echo "║  Frontend:  http://localhost:3000        ║"
echo "║  API Docs:  http://localhost:8000/docs   ║"
echo "║                                          ║"
echo "║  Press Ctrl+C to stop                   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Wait for Ctrl+C
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo ''; echo 'Stopped.'" EXIT
wait
