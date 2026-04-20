"""
LLM-Guided Sequence Explorer using LangChain + Ollama.

Improvements over baseline:
- Batched LLM calls: asks for N sequences per call, queues them, only calls
  LLM again when the queue is empty. Reduces LLM overhead by ~5x.
- Coverage-aware prompt: tells the LLM which endpoints are uncovered, which
  have returned 5xx errors, and how many valid sequences exist. This gives
  the LLM a meaningful basis for prioritizing unexplored paths.
- Fallback: if LLM fails or returns invalid op IDs, falls back to a random
  uncovered root endpoint (prioritizing coverage).
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from typing import TYPE_CHECKING

from langchain_core.prompts import PromptTemplate
from langchain_ollama import ChatOllama

from fuzzwise.models.types import Endpoint
from fuzzwise.strategies.base import BaseExplorer

if TYPE_CHECKING:
    from fuzzwise.fuzzer.state import FuzzState
    from fuzzwise.spec.dependencies import DependencyGraph

logger = logging.getLogger(__name__)

_BATCH_SIZE = 5          # sequences to request per LLM call
_MAX_UNCOVERED_SHOWN = 15  # cap how many uncovered ops we list in the prompt


class LLMGuidedExplorer(BaseExplorer):
    """
    Config C explorer: uses an LLM to select the next sequence of operations.

    Instead of BFS's systematic generation, the LLM reads current coverage
    state and proposes sequences targeting gaps or interesting paths.

    LLM is called in batches (_BATCH_SIZE sequences per call) and results are
    queued, so LLM overhead is amortized over multiple fuzzing iterations.
    """

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        batch_size: int = _BATCH_SIZE,
    ):
        self.model = model
        self.base_url = base_url
        self.batch_size = batch_size
        self._endpoint_map: dict[str, Endpoint] = {}
        self._queue: deque[tuple[list[Endpoint], Endpoint]] = deque()
        self.llm_call_count: int = 0  # incremented each time chain.invoke() succeeds

        logger.info("Initializing LangChain ChatOllama: model=%s url=%s", model, base_url)
        self.llm = ChatOllama(
            model=model,
            base_url=base_url,
            temperature=0.7,
        )

        self.prompt = PromptTemplate.from_template(
            "You are an intelligent API fuzzer choosing sequences of operations to test.\n\n"
            "Available operations (name: required params):\n"
            "{operations}\n\n"
            "Current fuzzing state:\n"
            "{coverage}\n\n"
            "Your goal: generate {batch_size} diverse sequences to maximize coverage and find bugs. "
            "Prioritize uncovered endpoints. "
            "For stateful operations (e.g. GET after POST), include the dependency in the sequence. "
            "Each sequence is a JSON array of operation IDs. "
            "Return ONLY a JSON array of arrays. "
            "Example: [[\"createUser\", \"getUser\"], [\"addPet\"], [\"placeOrder\", \"getOrderById\"]].\n"
            "Do not use markdown or include explanations."
        )

    def select_next(
        self,
        state: "FuzzState",
        graph: "DependencyGraph",
    ) -> tuple[list[Endpoint], Endpoint]:
        if not self._endpoint_map:
            self._endpoint_map = {e.operation_id: e for e in graph.endpoints}

        # Serve from queue; only call LLM when queue is empty
        if not self._queue:
            self._fill_queue(state, graph)

        if self._queue:
            return self._queue.popleft()

        # Final fallback
        return self._fallback(state, graph)

    def reset(self) -> None:
        self._endpoint_map.clear()
        self._queue.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fill_queue(self, state: "FuzzState", graph: "DependencyGraph") -> None:
        """Call LLM once for a batch of sequences and enqueue valid ones."""
        operations_summary = self._build_operations_summary(graph)
        coverage_summary = self._build_coverage_summary(state, graph)

        chain = self.prompt | self.llm
        try:
            logger.debug("Calling LLM for %d sequences...", self.batch_size)
            response = chain.invoke({
                "operations": operations_summary,
                "coverage": coverage_summary,
                "batch_size": self.batch_size,
            })
            self.llm_call_count += 1

            raw = response.content.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1]) if len(lines) >= 3 else raw

            batch = json.loads(raw)
            if not isinstance(batch, list):
                raise ValueError("LLM did not return a JSON array")

            # Each item should be a list of op IDs; accept a flat list too
            # (LLM sometimes returns a single sequence instead of a batch)
            if batch and not isinstance(batch[0], list):
                batch = [batch]

            queued = 0
            for seq_ids in batch:
                result = self._parse_sequence(seq_ids)
                if result is not None:
                    self._queue.append(result)
                    queued += 1

            logger.info("LLM Explorer queued %d sequences from LLM batch", queued)

        except Exception as exc:
            logger.warning("LLM Explorer batch call failed: %s — falling back", exc)
            # On failure, enqueue one fallback entry so the loop keeps moving
            self._queue.append(self._fallback(state, graph))

    def _parse_sequence(
        self, seq_ids: list
    ) -> tuple[list[Endpoint], Endpoint] | None:
        """Validate and convert a list of op ID strings to (prefix, target)."""
        if not isinstance(seq_ids, list) or not seq_ids:
            return None
        valid_ids = [op for op in seq_ids if op in self._endpoint_map]
        if not valid_ids:
            return None
        target_id = valid_ids[-1]
        prefix_ids = valid_ids[:-1]
        prefix = [self._endpoint_map[op] for op in prefix_ids]
        target = self._endpoint_map[target_id]
        return prefix, target

    def _build_operations_summary(self, graph: "DependencyGraph") -> str:
        lines = []
        for e in graph.endpoints:
            params = [p.name for p in e.all_params] if e.all_params else ["none"]
            lines.append(f"- {e.operation_id} ({e.method} {e.path}): {', '.join(params)}")
        return "\n".join(lines)

    def _build_coverage_summary(
        self, state: "FuzzState", graph: "DependencyGraph"
    ) -> str:
        all_ops = [e.operation_id for e in graph.endpoints]
        covered = state.coverage
        uncovered = [op for op in all_ops if op not in covered]
        buggy = list(state.error_counts.keys())

        parts = [
            f"Endpoints tested: {len(covered)}/{len(all_ops)}.",
            f"Valid sequences in pool: {len(state.valid_sequences)}.",
        ]
        if uncovered:
            shown = uncovered[:_MAX_UNCOVERED_SHOWN]
            suffix = f" (+{len(uncovered) - _MAX_UNCOVERED_SHOWN} more)" if len(uncovered) > _MAX_UNCOVERED_SHOWN else ""
            parts.append(f"NOT YET TESTED (prioritize these): {shown}{suffix}.")
        if buggy:
            parts.append(f"Endpoints with 5xx errors (interesting — try in sequences): {buggy}.")

        return " ".join(parts)

    def _fallback(
        self, state: "FuzzState", graph: "DependencyGraph"
    ) -> tuple[list[Endpoint], Endpoint]:
        """Return a random uncovered root, or any root if all covered."""
        roots = graph.roots()
        uncovered_roots = [r for r in roots if r.operation_id not in state.coverage]
        pool = uncovered_roots if uncovered_roots else (roots if roots else graph.endpoints)
        target = random.choice(pool)
        return [], target
