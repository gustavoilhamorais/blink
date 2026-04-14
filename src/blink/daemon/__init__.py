"""Blink background daemon.

The daemon runs as a long-lived process that:
- Maintains a Unix socket for IPC with the Kitty kitten
- Manages AI provider connections and streaming sessions
- Persists shell history and block data to SQLite
- Handles completion requests from the shell integration scripts
"""
