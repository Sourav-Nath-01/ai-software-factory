"""Central configuration using pydantic settings."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- LLM Configuration (all providers) ---
    openai_api_key: str = ""
    openai_model_name: str = "gpt-4o"
    openai_temperature: float = 0.2

    gemini_api_key: str = ""
    google_api_key: str = ""
    groq_api_key: str = ""

    # --- Pipeline Settings ---
    max_review_iterations: int = 3
    max_test_fix_iterations: int = 3

    # --- Output ---
    output_dir: str = "output"

    # --- Dry Run ---
    dry_run: bool = False

    # --- Demo Mode ---
    demo_delay_scale: float = 1.0  # multiplier for simulated LLM latency (0 = instant, used by tests)

    # --- Agent Memory ---
    enable_memory: bool = True  # ChromaDB persistence; failures never break a run

    # --- Code Execution Sandbox ---
    use_docker_sandbox: bool = True       # run generated tests in Docker when available
    allow_unsandboxed_exec: bool = True   # fall back to a plain subprocess when Docker is unavailable

    # --- Server capacity ---
    max_concurrent_runs: int = 4  # extra runs get HTTP 429 instead of exhausting a small server

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def output_path(self) -> Path:
        """Return the resolved output directory path."""
        return Path(self.output_dir).resolve()


def save_api_key(api_key: str) -> None:
    """Save or update the OpenAI API key in the .env file."""
    env_path = Path(".env")
    lines: list[str] = []

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    # Update existing key or append
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("OPENAI_API_KEY"):
            lines[i] = f"OPENAI_API_KEY={api_key}"
            found = True
            break

    if not found:
        lines.append(f"OPENAI_API_KEY={api_key}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Global settings instance
settings = Settings()
