"""First-run onboarding kitten.

Guides users through:
1. Provider setup (API keys)
2. Shell integration installation
3. Feature tour

Invoked by Kitty as: kitty +kitten blink_onboarding
Or automatically on first launch when ~/.blink/onboarding_complete is absent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Kitty kitten API
# ---------------------------------------------------------------------------

_BLINK_DIR = Path(os.environ.get("BLINK_DIR", Path.home() / ".blink"))
_ONBOARDING_FLAG = _BLINK_DIR / "onboarding_complete"


def main(args: list[str]) -> str:
    """Entry point called by Kitty.

    Runs the interactive onboarding wizard and returns a JSON string
    summarising the user's choices (forwarded to :func:`handle_result`).
    """
    force = "--force" in args or "-f" in args

    if _ONBOARDING_FLAG.exists() and not force:
        _print_info("Onboarding already complete. Use --force to re-run.")
        return "skipped"

    _clear_screen()
    _print_banner()

    results: dict[str, Any] = {}

    # Step 1: Provider setup
    results["provider"] = _step_provider_setup()

    # Step 2: Shell integration
    results["shell"] = _step_shell_integration()

    # Step 3: Feature tour
    _step_feature_tour()

    # Mark onboarding complete
    _BLINK_DIR.mkdir(parents=True, exist_ok=True)
    _ONBOARDING_FLAG.touch()

    _clear_screen()
    _print_success()

    import json

    return json.dumps(results)


def handle_result(
    args: list[str],
    answer: str,
    target_window_id: int,
    boss: Any,
) -> None:
    """Handle the result of onboarding.

    Persists provider configuration if the user entered API keys.
    """
    import json

    try:
        data = json.loads(answer)
    except (ValueError, TypeError):
        return

    provider = data.get("provider", {})
    api_key = provider.get("api_key", "")
    provider_name = provider.get("name", "")

    if api_key and provider_name:
        # Persist the API key via the blink CLI
        _persist_provider(provider_name, api_key)


# ---------------------------------------------------------------------------
# Onboarding steps
# ---------------------------------------------------------------------------

_BANNER_TEXT = r"""
  ____  _ _       _
 | __ )| (_)_ __ | | __
 |  _ \| | | '_ \| |/ /
 | |_) | | | | | |   <
 |____/|_|_|_| |_|_|\_\

  An open-source Warp.dev alternative built on Kitty.
"""

_OK = "\033[1;32m"
_WARN = "\033[1;33m"
_CYAN = "\033[1;36m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"

_PROVIDERS = [
    ("anthropic", "Anthropic (Claude)", "ANTHROPIC_API_KEY"),
    ("openai", "OpenAI (GPT-4)", "OPENAI_API_KEY"),
    ("ollama", "Ollama (local)", None),
    ("none", "Skip for now", None),
]


def _print_banner() -> None:
    print(_CYAN + _BANNER_TEXT + _RESET)
    print(f"  Welcome to {_BOLD}Blink{_RESET}! This wizard will get you up and running.\n")
    print(f"  {_DIM}(Press Ctrl-C at any time to skip this step){_RESET}\n")


def _step_provider_setup() -> dict[str, Any]:
    """Guide user through choosing and configuring an AI provider."""
    _section_header("Step 1 of 3: AI Provider Setup")
    print("  Blink uses AI providers for completions and inline agents.\n")
    print("  Available providers:\n")

    for i, (_key, label, env_var) in enumerate(_PROVIDERS, 1):
        env_hint = f" {_DIM}(reads {env_var}){_RESET}" if env_var else ""
        print(f"    [{i}] {label}{env_hint}")

    print()
    choice = _prompt_choice(
        "  Choose a provider",
        options=[str(i) for i in range(1, len(_PROVIDERS) + 1)],
        default="4",
    )

    idx = int(choice) - 1
    provider_key, provider_label, env_var = _PROVIDERS[idx]

    if provider_key == "none":
        print(f"\n  {_DIM}Skipping provider setup. Run 'blink provider add' later.{_RESET}\n")
        return {"name": "", "api_key": ""}

    if provider_key == "ollama":
        print(f"\n  {_OK}Ollama selected.{_RESET} No API key needed — make sure Ollama is running.")
        print(f"  {_DIM}Default endpoint: http://localhost:11434{_RESET}\n")
        return {"name": "ollama", "api_key": ""}

    # Check if key is already in env
    existing = os.environ.get(env_var or "", "")
    if existing:
        masked = existing[:4] + "..." + existing[-4:] if len(existing) > 8 else "****"
        print(f"\n  Found existing {env_var}: {_DIM}{masked}{_RESET}")
        use_existing = _prompt_yn("  Use this key?", default="y")
        if use_existing:
            return {"name": provider_key, "api_key": existing}

    print(f"\n  Enter your {provider_label} API key:")
    print(f"  {_DIM}(The key will be stored securely in your system keyring){_RESET}")
    api_key = _prompt_secret(f"  {env_var or 'API Key'}: ")

    if not api_key:
        print(f"  {_WARN}No key entered. You can add it later with: blink provider add {provider_key}{_RESET}\n")
        return {"name": provider_key, "api_key": ""}

    print(f"\n  {_OK}API key saved.{_RESET}\n")
    return {"name": provider_key, "api_key": api_key}


def _step_shell_integration() -> dict[str, Any]:
    """Guide user through installing shell integration hooks."""
    _section_header("Step 2 of 3: Shell Integration")
    print("  Shell integration enables block tracking, completions, and agent context.\n")

    detected_shell = _detect_shell()
    print(f"  Detected shell: {_BOLD}{detected_shell}{_RESET}\n")

    install = _prompt_yn("  Install shell integration now?", default="y")
    if not install:
        print(f"\n  {_DIM}Skipping. Run 'blink shell install' later to set up manually.{_RESET}\n")
        return {"shell": detected_shell, "installed": False}

    success = _install_shell_integration(detected_shell)
    if success:
        print(f"\n  {_OK}Shell integration installed.{_RESET}")
        print(f"  {_DIM}Restart your shell or run 'source ~/.bashrc' to activate.{_RESET}\n")
    else:
        print(f"\n  {_WARN}Could not auto-install. See docs/shell-integration.md for manual steps.{_RESET}\n")

    return {"shell": detected_shell, "installed": success}


def _step_feature_tour() -> None:
    """Show a brief overview of Blink's key features."""
    _section_header("Step 3 of 3: Feature Tour")

    features = [
        ("AI Completions", "Tab-complete commands with AI suggestions as you type."),
        ("Block Inspector", "Every command is captured as a block — view, search, re-run."),
        ("Inline Agent", "Run 'blink agent ask <prompt>' to get AI help in context."),
        ("MCP Server", "Expose terminal state to external AI tools via MCP protocol."),
        ("Session Picker", "kitty +kitten blink_session_picker to switch sessions."),
    ]

    for name, desc in features:
        print(f"  {_BOLD}{_CYAN}{name}{_RESET}")
        print(f"    {_DIM}{desc}{_RESET}\n")

    _prompt_enter("  Press Enter to continue...")


def _print_success() -> None:
    print(f"\n  {_OK}{_BOLD}Setup complete!{_RESET}\n")
    print("  Quick start commands:\n")
    print(f"    {_CYAN}blink daemon start{_RESET}        — start the background daemon")
    print(f"    {_CYAN}blink agent ask 'help me'{_RESET}  — ask the agent a question")
    print(f"    {_CYAN}blink block list{_RESET}           — see recent command blocks")
    print(f"    {_CYAN}blink --help{_RESET}               — explore all commands\n")
    print(f"  {_DIM}Documentation: https://github.com/guscost/blink/docs{_RESET}\n")


# ---------------------------------------------------------------------------
# Shell integration helpers
# ---------------------------------------------------------------------------


def _detect_shell() -> str:
    """Detect the user's current shell."""
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "zsh"
    elif "fish" in shell:
        return "fish"
    elif "bash" in shell:
        return "bash"
    return Path(shell).name if shell else "bash"


def _install_shell_integration(shell: str) -> bool:
    """Write the appropriate RC snippet to the user's shell config."""
    # Find the integration script
    script_candidates = [
        Path(__file__).parent.parent.parent / "shell" / shell / f"blink.{shell}",
        Path.home() / ".local" / "share" / "blink" / "shell" / shell / f"blink.{shell}",
    ]

    script_path: Path | None = None
    for candidate in script_candidates:
        if candidate.exists():
            script_path = candidate
            break

    if script_path is None:
        return False

    rc_files = {
        "bash": Path.home() / ".bashrc",
        "zsh": Path.home() / ".zshrc",
        "fish": Path.home() / ".config" / "fish" / "config.fish",
    }
    rc_file = rc_files.get(shell)
    if rc_file is None:
        return False

    source_line = f'\n# Blink shell integration\nsource "{script_path}"\n'

    try:
        rc_content = rc_file.read_text() if rc_file.exists() else ""
        if str(script_path) in rc_content:
            return True  # Already installed
        rc_file.parent.mkdir(parents=True, exist_ok=True)
        with rc_file.open("a") as f:
            f.write(source_line)
        return True
    except OSError:
        return False


def _persist_provider(name: str, api_key: str) -> None:
    """Store provider API key via the blink CLI (best effort)."""
    import subprocess

    try:
        subprocess.run(  # noqa: S603 S607
            ["blink", "provider", "add", name],
            input=api_key.encode(),
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# UI primitives
# ---------------------------------------------------------------------------


def _section_header(title: str) -> None:
    print(f"\n  {_BOLD}{_CYAN}{title}{_RESET}\n  {'─' * (len(title) + 2)}\n")


def _prompt_choice(prompt: str, options: list[str], default: str = "") -> str:
    default_hint = f" [{default}]" if default else ""
    while True:
        try:
            sys.stdout.write(f"{prompt}{default_hint}: ")
            sys.stdout.flush()
            answer = input().strip()
        except (EOFError, KeyboardInterrupt):
            return default
        if not answer and default:
            return default
        if answer in options:
            return answer
        print(f"  Please enter one of: {', '.join(options)}")


def _prompt_yn(prompt: str, default: str = "y") -> bool:
    hint = "[Y/n]" if default == "y" else "[y/N]"
    while True:
        try:
            sys.stdout.write(f"{prompt} {hint}: ")
            sys.stdout.flush()
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return default == "y"
        if not answer:
            return default == "y"
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _prompt_secret(prompt: str) -> str:
    """Read a secret value without echoing to terminal."""
    import getpass

    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def _prompt_enter(prompt: str = "  Press Enter to continue...") -> None:
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        input()
    except (EOFError, KeyboardInterrupt):
        pass


def _print_info(msg: str) -> None:
    print(f"  {_DIM}{msg}{_RESET}")


def _clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


__all__ = ["main", "handle_result"]
