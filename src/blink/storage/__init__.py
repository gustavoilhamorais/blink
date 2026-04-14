"""Persistent storage layer.

Uses aiosqlite for async SQLite access. Stores:
- Shell command history with metadata
- Output blocks and their AI annotations
- AI provider configurations (encrypted credentials handled by security module)
- User preferences and session state
"""
