"""Completion broker.

Three-stage pipeline: Retrieval → Generation → Validation.

Stage 1 — Retrieval (fast path, ≤120 ms):
    Query the local history ranker. If a high-confidence match is found
    (confidence > *fast_path_threshold*) return immediately without waiting
    for the LLM.

Stage 2 — Generation (remote LLM, 300-400 ms):
    If the fast path did not yield a confident enough result, call the
    configured provider.  If no provider is configured the broker still works
    in history-only mode.

Stage 3 — Validation:
    All candidates (LLM + history) are passed through the validator before
    being returned to the caller.

Debouncing / cancellation:
    ``get_completions`` accepts an ``anyio.CancelScope`` so the caller can
    cancel in-flight requests when a new keystroke arrives.  The method also
    sleeps for *debounce_ms* milliseconds before doing any real work — callers
    that create a fresh scope per keystroke get automatic debouncing.
"""

from __future__ import annotations

import anyio

from blink.completions.context import CompletionContext
from blink.completions.ranker import Completion, HistoryRanker
from blink.completions.validator import Validator
from blink.providers.base import CompletionProvider

# Minimum confidence for the fast-path shortcut (skip LLM).
_FAST_PATH_THRESHOLD = 0.8

# Default debounce delay in seconds (350 ms — middle of 250-400 ms range).
_DEFAULT_DEBOUNCE_S = 0.35

# Maximum completions to return.
_MAX_COMPLETIONS = 5


class CompletionBroker:
    """Orchestrates the retrieval → generation → validation pipeline."""

    def __init__(
        self,
        ranker: HistoryRanker,
        provider: CompletionProvider | None = None,
        validator: Validator | None = None,
        debounce_s: float = _DEFAULT_DEBOUNCE_S,
        fast_path_threshold: float = _FAST_PATH_THRESHOLD,
    ) -> None:
        self._ranker = ranker
        self._provider = provider
        self._validator = validator or Validator()
        self._debounce_s = debounce_s
        self._fast_path_threshold = fast_path_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_completions(
        self,
        context: CompletionContext,
        *,
        cancel_scope: anyio.CancelScope | None = None,
    ) -> list[Completion]:
        """Return up to *_MAX_COMPLETIONS* validated completions.

        Parameters
        ----------
        context:
            Snapshot of the current shell state.
        cancel_scope:
            Optional ``anyio.CancelScope``.  Pass a scope that you cancel when
            a new keystroke arrives to abort in-flight work.
        """
        async with anyio.create_task_group() as _tg:
            if cancel_scope is not None:
                # Link lifetime to the caller's cancel scope
                _tg.cancel_scope.deadline = cancel_scope.deadline

            # Debounce: sleep before doing work so rapid keystrokes collapse.
            await anyio.sleep(self._debounce_s)

            completions = await self._pipeline(context)
            return completions

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    async def _pipeline(self, context: CompletionContext) -> list[Completion]:
        # ---- Stage 1: Retrieval (history, fast path) ----
        history_completions = await self._ranker.rank(context)

        if history_completions and history_completions[0].confidence > self._fast_path_threshold:
            # Fast path: high-confidence history match, skip LLM entirely.
            validated = await self._validator.validate_all(
                history_completions[:3], context
            )
            return validated[:_MAX_COMPLETIONS]

        # ---- Stage 2: LLM generation ----
        llm_completions: list[Completion] = []
        if self._provider is not None:
            try:
                with anyio.move_on_after(5.0):  # per-request LLM timeout
                    llm_completions = await self._provider.complete(context)
            except Exception:  # noqa: BLE001
                # Provider errors must not crash the completion pipeline.
                pass

        # ---- Stage 3: Validation ----
        combined = llm_completions + history_completions
        validated = await self._validator.validate_all(combined, context)

        # De-duplicate by text, preserve order.
        seen: set[str] = set()
        unique: list[Completion] = []
        for comp in validated:
            if comp.text not in seen:
                seen.add(comp.text)
                unique.append(comp)

        return unique[:_MAX_COMPLETIONS]


__all__ = ["CompletionBroker"]
