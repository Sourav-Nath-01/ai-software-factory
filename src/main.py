"""AI Software Factory — Interactive Terminal Application.

Launch:
    python3 -m src.main
    python3 -m src.main "Create a REST API for a todo app"
    ./run.sh
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.text import Text
from rich.columns import Columns

from src.core.config import settings, save_api_key
from src.core.pipeline import Pipeline

console = Console()

# ──────────────────────────────────────────────
# Banner & Branding
# ──────────────────────────────────────────────

BANNER = r"""
    _    ___   ____         __ _                          
   / \  |_ _| / ___|  ___  / _| |___      ____ _ _ __ ___ 
  / _ \  | |  \___ \ / _ \| |_| __\ \ /\ / / _` | '__/ _ \
 / ___ \ | |   ___) | (_) |  _| |_ \ V  V / (_| | | |  __/
/_/   \_\___| |____/ \___/|_|  \__| \_/\_/ \__,_|_|  \___|
                 _____ _    ____ _____ ___  ______   __
                |  ___/ \  / ___|_   _/ _ \|  _ \ \ / /
                | |_ / _ \| |     | || | | | |_) \ V / 
                |  _/ ___ \ |___  | || |_| |  _ < | |  
                |_|/_/   \_\____| |_| \___/|_| \_\|_|  
"""

TAGLINE = "Multiple specialized AI agents collaborating like a real engineering team."

MODEL_CHOICES = {
    "1": ("gemini/gemini-2.0-flash",      "Gemini 2.0 Flash  — FREE, recommended (Google AI Studio)"),
    "2": ("gemini/gemini-2.0-flash-lite",  "Gemini 2.0 Flash Lite — FREE, fastest (Google AI Studio)"),
    "3": ("groq/llama-3.1-8b-instant",     "Llama 3.1 8B (Groq) — FREE, near-instant (console.groq.com)"),
    "4": ("groq/llama-3.3-70b-versatile",  "Llama 3.3 70B (Groq) — FREE, more capable (console.groq.com)"),
    "5": ("gpt-4o-mini",                   "GPT-4o Mini (OpenAI) — PAID, fast & cheap"),
    "6": ("gpt-4o",                        "GPT-4o (OpenAI)     — PAID, best quality"),
}

PROVIDER_KEY_HINTS = {
    "gemini": ("AIza...", "https://aistudio.google.com/apikey", "GEMINI_API_KEY"),
    "groq":   ("gsk_...",  "https://console.groq.com/keys",       "GROQ_API_KEY"),
    "openai": ("sk-...",   "https://platform.openai.com/api-keys", "OPENAI_API_KEY"),
}


def print_banner():
    """Display the welcome banner."""
    console.print()
    banner_text = Text(BANNER, style="bold cyan")
    console.print(banner_text)
    console.print(
        Panel(
            f"[bold white]{TAGLINE}[/bold white]",
            border_style="dim cyan",
            padding=(0, 2),
        )
    )
    console.print()


def print_divider(label: str = ""):
    """Print a styled divider."""
    if label:
        console.print(f"\n[bold cyan]{'─' * 3} {label} {'─' * (50 - len(label))}[/bold cyan]")
    else:
        console.print(f"[dim]{'─' * 60}[/dim]")


# ──────────────────────────────────────────────
# Setup Wizard
# ──────────────────────────────────────────────

def _graceful_exit():
    """Print a clean exit message and terminate."""
    console.print("\n\n  [dim]Goodbye! Run again anytime.[/dim]\n")
    sys.exit(0)


def _save_provider_key(api_key: str, env_var: str) -> None:
    """Save an API key to .env under the given env var name."""
    env_path = Path(".env")
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(env_var):
            lines[i] = f"{env_var}={api_key}"
            found = True
            break
    if not found:
        lines.append(f"{env_var}={api_key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup_api_key(model_name: str = "") -> tuple[str, str]:
    """Prompt for an API key appropriate to the selected model."""
    # Determine provider from model string
    provider = "openai"
    if model_name.startswith("gemini/"):
        provider = "gemini"
    elif model_name.startswith("groq/"):
        provider = "groq"

    placeholder, key_url, env_var = PROVIDER_KEY_HINTS.get(provider, ("sk-...", "", "OPENAI_API_KEY"))

    print_divider("API Key Setup")
    console.print()
    console.print(f"  [dim]Provider: [bold]{provider.upper()}[/bold][/dim]")
    console.print(f"  [dim]Get your free key at: [cyan]{key_url}[/cyan][/dim]")
    console.print(f"  [dim]Format: {placeholder}[/dim]")
    console.print(f"  [dim]Leave blank to run in Demo Mode (Mock LLM responses).[/dim]")
    console.print()

    while True:
        try:
            api_key = Prompt.ask(
                f"  [bold yellow]Paste your {provider.upper()} API key[/bold yellow]",
                password=True,
            )
        except (KeyboardInterrupt, EOFError):
            _graceful_exit()

        api_key = api_key.strip()

        if not api_key:
            try:
                if Confirm.ask("  [yellow]Run in Demo Mode (Mock LLM)?[/yellow]", default=True):
                    return "", provider
            except (KeyboardInterrupt, EOFError):
                _graceful_exit()
            continue

        # Save to .env under the correct env var name
        _save_provider_key(api_key, env_var)
        console.print(f"  [green]✓ API key saved to .env as {env_var}[/green]")
        return api_key, provider


def setup_model() -> str:
    """Let user pick an LLM model."""
    print_divider("Model Selection")
    console.print()
    console.print("  [dim]Models 1–4 are completely FREE. Models 5–6 require a paid OpenAI account.[/dim]")
    console.print()

    for key, (model, desc) in MODEL_CHOICES.items():
        is_free = not model.startswith("gpt-")
        marker = " [green]◀ recommended[/green]" if model == "gemini/gemini-2.0-flash" else ""
        free_badge = " [green][FREE][/green]" if is_free else " [yellow][PAID][/yellow]"
        console.print(f"  [bold cyan]{key}[/bold cyan]  {desc}{free_badge}{marker}")

    console.print()
    try:
        choice = Prompt.ask(
            "  [bold yellow]Select model[/bold yellow]",
            choices=list(MODEL_CHOICES.keys()),
            default="1",
        )
    except (KeyboardInterrupt, EOFError):
        _graceful_exit()

    model_name = MODEL_CHOICES[choice][0]
    settings.openai_model_name = model_name
    console.print(f"  [green]✓ Using {model_name}[/green]")
    return model_name


def setup_iterations():
    """Let user configure iteration limits."""
    print_divider("Pipeline Settings")
    console.print()
    console.print("  [dim]How many times should agents retry to improve code?[/dim]")
    console.print()

    try:
        review_max = IntPrompt.ask(
            "  [bold yellow]Max review-improve cycles[/bold yellow]",
            default=settings.max_review_iterations,
        )
        settings.max_review_iterations = max(1, min(review_max, 10))

        test_max = IntPrompt.ask(
            "  [bold yellow]Max test-fix cycles[/bold yellow]",
            default=settings.max_test_fix_iterations,
        )
        settings.max_test_fix_iterations = max(1, min(test_max, 10))
    except (KeyboardInterrupt, EOFError):
        _graceful_exit()

    console.print(
        f"  [green]✓ Review loops: {settings.max_review_iterations}, "
        f"Test-fix loops: {settings.max_test_fix_iterations}[/green]"
    )


def get_project_prompt() -> str:
    """Get the project description from the user."""
    print_divider("What do you want to build?")
    console.print()
    console.print("  [dim]Describe the software project you want the AI team to build.[/dim]")
    console.print("  [dim]Be as specific as possible for best results.[/dim]")
    console.print()
    console.print("  [dim italic]Examples:[/dim italic]")
    console.print('  [dim]  • "Create a REST API for a todo app with FastAPI and PostgreSQL"[/dim]')
    console.print('  [dim]  • "Build a CLI weather tool that fetches data from OpenWeatherMap"[/dim]')
    console.print('  [dim]  • "Create a URL shortener service with analytics tracking"[/dim]')
    console.print()

    while True:
        try:
            prompt = Prompt.ask("  [bold yellow]Your project[/bold yellow]")
        except (KeyboardInterrupt, EOFError):
            _graceful_exit()

        prompt = prompt.strip()
        if prompt:
            return prompt
        console.print("  [red]Please describe what you want to build.[/red]")


# ──────────────────────────────────────────────
# Interactive Mode
# ──────────────────────────────────────────────

def run_interactive():
    """Run the full interactive terminal experience."""
    try:
        print_banner()

        # Step 1: Pick model (before API key — so we know which key to ask for)
        try:
            if Confirm.ask("  [dim]Choose model / provider?[/dim]", default=True):
                model_name = setup_model()
            else:
                model_name = "gemini/gemini-2.0-flash"  # default free model
                console.print(f"  [green]\u2713 Using default:[/green] [dim]{model_name}[/dim]")
        except (KeyboardInterrupt, EOFError):
            _graceful_exit()

        is_demo = model_name == "demo"
        api_key = ""
        provider = "gemini"

        # Step 2: API key (provider-aware)
        if not is_demo:
            api_key, provider = setup_api_key(model_name)
            if not api_key:
                is_demo = True

        if is_demo:
            console.print("  [green]\u2713 Demo Mode:[/green] [dim]Mock LLM (No API Key)[/dim]")
        else:
            try:
                if Confirm.ask("\n  [dim]Configure pipeline iteration limits?[/dim]", default=False):
                    setup_iterations()
            except (KeyboardInterrupt, EOFError):
                _graceful_exit()

        # Step 3: Project prompt
        user_request = get_project_prompt()

        # Step 4: Confirm & launch
        print_divider("Ready to Build")
        console.print()

        model_display = "Demo Mode (Mock LLM)" if is_demo else model_name
        summary_panel = Panel(
            f"[bold white]{user_request}[/bold white]\n\n"
            f"[dim]Model:[/dim] {model_display}  "
            f"[dim]Review loops:[/dim] {settings.max_review_iterations}  "
            f"[dim]Test-fix loops:[/dim] {settings.max_test_fix_iterations}",
            title="[bold cyan]Project Summary[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
        console.print(summary_panel)

        try:
            if not Confirm.ask("\n  [bold yellow]Launch the AI team?[/bold yellow]", default=True):
                console.print("\n  [dim]Cancelled. Run again anytime![/dim]\n")
                sys.exit(0)
        except (KeyboardInterrupt, EOFError):
            _graceful_exit()

        # Step 5: Run pipeline
        console.print()
        pipeline = Pipeline(model=model_name, api_key=api_key, demo=is_demo)

        state = pipeline.run(user_request)

        if state.errors:
            console.print(f"\n[yellow]⚠ {len(state.errors)} warning(s):[/yellow]")
            for err in state.errors:
                console.print(f"  [dim]• {err}[/dim]")

        # Final message
        console.print(
            Panel(
                "[bold green]Your project is ready![/bold green]\n\n"
                f"[dim]Check the output directory for your generated project.[/dim]",
                border_style="green",
                padding=(1, 2),
            )
        )

    except KeyboardInterrupt:
        console.print("\n\n  [yellow]⚠ Interrupted. Partial output may be in output/.[/yellow]")
        console.print("  [dim]Run again anytime![/dim]\n")
        sys.exit(130)
    except Exception as e:
        console.print(
            Panel(
                f"[red bold]Pipeline Error[/red bold]\n\n{e}",
                border_style="red",
                padding=(1, 2),
            )
        )
        sys.exit(1)


# ──────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────

def main():
    """Main entry point — supports both interactive and direct modes."""
    load_dotenv()

    # If called with a prompt argument, run directly (power-user mode)
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        user_request = " ".join(sys.argv[1:])
        _run_direct(user_request)
    elif "--help" in sys.argv or "-h" in sys.argv:
        _print_help()
    else:
        run_interactive()


def _run_direct(user_request: str):
    """Run pipeline directly with a prompt (no wizard)."""
    # Detect best available provider
    model = "demo"
    api_key = ""
    is_demo = True

    if settings.groq_api_key:
        model = "groq/llama-3.3-70b-versatile"
        api_key = settings.groq_api_key
        is_demo = False
    elif settings.gemini_api_key or settings.google_api_key:
        model = "gemini/gemini-2.0-flash"
        api_key = settings.gemini_api_key or settings.google_api_key
        is_demo = False
    elif settings.openai_api_key and settings.openai_api_key != "sk-your-api-key-here":
        model = settings.openai_model_name
        api_key = settings.openai_api_key
        is_demo = False

    if is_demo:
        console.print("[yellow]No API keys found. Running in Demo Mode (Mock LLM).[/yellow]")
        console.print("[dim]To use a real model, set GROQ_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY in .env.[/dim]\n")
    else:
        console.print(f"[green]✓ Using model:[/green] [dim]{model}[/dim]\n")

    pipeline = Pipeline(model=model, api_key=api_key, demo=is_demo)
    try:
        state = pipeline.run(user_request)
        if state.errors:
            for err in state.errors:
                console.print(f"  [dim]• {err}[/dim]")
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red bold]Error: {e}[/red bold]")
        sys.exit(1)


def _print_help():
    """Print usage help."""
    print_banner()
    console.print("[bold]Usage:[/bold]")
    console.print()
    console.print("  [cyan]python3 -m src.main[/cyan]")
    console.print("    Launch interactive mode (recommended)")
    console.print()
    console.print('  [cyan]python3 -m src.main "Create a REST API for todos"[/cyan]')
    console.print("    Run directly with a prompt")
    console.print()
    console.print("  [cyan]./run.sh[/cyan]")
    console.print("    One-command launch (auto-activates virtualenv)")
    console.print()
    console.print("[bold]Environment:[/bold]")
    console.print()
    console.print("  OPENAI_API_KEY     Your OpenAI API key")
    console.print("  OPENAI_MODEL_NAME  Model to use (default: gpt-4o)")
    console.print()


if __name__ == "__main__":
    main()
