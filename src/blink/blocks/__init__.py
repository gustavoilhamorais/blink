"""Block-based output model.

Blocks are the fundamental unit of output in Blink, similar to Warp's
block concept. Each command execution produces a Block that captures:
- The command string
- stdout / stderr streams
- Exit code and timing
- AI-generated summaries or annotations
- Interactive widgets (e.g. diff viewers, table renderers)
"""
