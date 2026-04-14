# Blink

An open-source [Warp.dev](https://warp.dev) alternative built on [Kitty terminal](https://sw.kovidgoyal.net/kitty/). Blink provides AI-powered autocompletions and inline agent interactions using your existing AI provider subscriptions.

## Features

- **AI-powered shell completions** — context-aware suggestions as you type
- **Inline agent interactions** — ask questions and run tasks without leaving your terminal
- **Provider abstraction** — bring your own API keys (OpenAI, Anthropic, and more)
- **Block-based output** — structured, interactive command output like Warp
- **Built on Kitty** — leverages Kitty's powerful kitten/extension system

## Quick Start

```bash
# Clone the repo
git clone https://github.com/guscost/blink
cd blink

# Install with dev dependencies
pip install -e ".[dev]"

# Verify installation
blink --version

# (Optional) Install AI provider support
pip install -e ".[ai]"
```

## Usage

```bash
# Show help
blink --help

# Start the blink daemon
blink daemon start

# Manage AI providers
blink provider list
blink provider add openai

# Manage blocks
blink block list
```

## Architecture

- **`blink.cli`** — Typer-based CLI entry point; routes to subcommand groups
- **`blink.daemon`** — Background daemon process; manages IPC with the Kitty kitten
- **`blink.kitty`** — Kitty terminal integration layer and kitten helpers
- **`blink.mcp`** — Model Context Protocol client for tool/resource discovery
- **`blink.acp`** — Agent Communication Protocol for inline agent sessions
- **`blink.completions`** — Shell completion engine (bash, zsh, fish)
- **`blink.providers`** — AI provider abstraction (OpenAI, Anthropic, etc.)
- **`blink.blocks`** — Block data model and renderer (Warp-style output blocks)
- **`blink.storage`** — Persistent storage via SQLite (aiosqlite)
- **`blink.security`** — Credential storage and sandboxing utilities

## Development

This project uses [just](https://github.com/casey/just) for common tasks:

```bash
just install       # Install dev dependencies
just test          # Run tests
just lint          # Run ruff linter
just format        # Format code with ruff
just typecheck     # Run mypy
just ci            # Run all CI checks
```

## Roadmap

See the [GitHub Issues](https://github.com/guscost/blink/issues) for the full roadmap and planned features.

## Contributing

Contributions are welcome. Please open an issue first to discuss what you'd like to change.

## License

MIT
