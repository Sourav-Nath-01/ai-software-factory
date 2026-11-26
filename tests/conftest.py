"""Test-wide settings. Must run before src.core.config is imported."""
import os

# Demo-mode pipelines run instantly instead of simulating LLM latency
os.environ.setdefault("DEMO_DELAY_SCALE", "0")
# Keep tests hermetic: no ChromaDB embedding-model downloads, no Docker image pulls
os.environ.setdefault("ENABLE_MEMORY", "false")
os.environ.setdefault("USE_DOCKER_SANDBOX", "false")
