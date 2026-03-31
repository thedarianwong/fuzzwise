"""
Endpoint/sequence selection strategies.

BFSExplorer implements RESTler's BFS algorithm from Figure 3 (ICSE 2019):
    - Maintains seqSet: the set of valid request sequences
    - At each generation n, extends all valid sequences by one endpoint
    - A sequence is only extended if DEPENDENCIES are satisfied
    - Dynamic feedback: sequences are pruned when any prefix returns non-2xx

The engine calls select_next() to get the next (prefix, target) pair to execute.
After executing target, the engine calls:
    - mark_valid(prefix + [target])   if all prefixes returned 2xx
    - mark_invalid(prefix + [target]) if a prefix returned non-2xx

BFS-Fast variant: each endpoint appended to at most one sequence per generation.
This scales better on large APIs (avoids exponential seqSet growth).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from fuzzwise.models.types import Endpoint
from fuzzwise.strategies.base import BaseExplorer

if TYPE_CHECKING:
    from fuzzwise.fuzzer.state import FuzzState
    from fuzzwise.spec.dependencies import DependencyGraph

logger = logging.getLogger(__name__)


class BFSExplorer(BaseExplorer):
    """
    RESTler-faithful BFS over request sequences of increasing length.

    Sequence generation follows Figure 3 of the RESTler paper:
        Generation 1: all single-request sequences (roots first, then dependents)
        Generation 2: extend valid gen-1 sequences by one request
        Generation 3: extend valid gen-2 sequences by one request
        ...up to max_sequence_length

    The work queue is a deque of (prefix, candidate_endpoint) pairs.
    On exhaustion, refills from the current valid_sequences × reqSet cross-product.
    """

    def __init__(self, max_sequence_length: int = 3, bfs_fast: bool = False) -> None:
        self._max_length = max_sequence_length
        self._bfs_fast = bfs_fast
        self._queue: deque[tuple[list[str], str]] = deque()  # (prefix op_ids, target op_id)
        self._endpoint_map: dict[str, Endpoint] = {}
        self._initialized = False
        self._current_generation = 1

    def select_next(
        self,
        state: "FuzzState",
        graph: "DependencyGraph",
    ) -> tuple[list[Endpoint], Endpoint]:
        """
        Return the next (prefix_sequence, target_endpoint) to execute.

        Fills the queue from the BFS expansion if empty.
        """
        if not self._initialized:
            self._initialize(graph)

        if not self._queue:
            self._refill(state, graph)

        if not self._queue:
            # Fallback: nothing satisfiable — return first root endpoint with empty prefix
            roots = graph.roots()
            target = roots[0] if roots else graph.endpoints[0]
            logger.warning("BFS queue empty — falling back to root endpoint %s", target.operation_id)
            return [], target

        prefix_ids, target_id = self._queue.popleft()
        prefix = [self._endpoint_map[op_id] for op_id in prefix_ids if op_id in self._endpoint_map]
        target = self._endpoint_map[target_id]
        return prefix, target

    def reset(self) -> None:
        self._queue.clear()
        self._initialized = False
        self._current_generation = 1

    # ------------------------------------------------------------------
    # Internal BFS machinery
    # ------------------------------------------------------------------

    def _initialize(self, graph: "DependencyGraph") -> None:
        """Populate endpoint map and seed the queue with generation-1 sequences."""
        self._endpoint_map = {e.operation_id: e for e in graph.endpoints}
        self._initialized = True
        self._seed_generation_1(graph)

    def _seed_generation_1(self, graph: "DependencyGraph") -> None:
        """
        Seed queue with all single-endpoint sequences.

        Order: roots first (no dependencies), then dependents.
        This matches RESTler's BFS starting from the empty sequence ε.
        """
        bfs_order = graph.bfs_order()
        for endpoint in bfs_order:
            self._queue.append(([], endpoint.operation_id))
        logger.debug("BFS generation 1: %d candidates", len(self._queue))

    def _refill(self, state: "FuzzState", graph: "DependencyGraph") -> None:
        """
        Refill queue by extending valid sequences by one endpoint (EXTEND step).

        Equivalent to RESTler's EXTEND(seqSet, reqSet) from Figure 3.
        Only extends sequences shorter than max_sequence_length.
        With bfs_fast=True, each endpoint is appended to at most one sequence.
        """
        self._current_generation += 1
        if self._current_generation > self._max_length:
            # Reached max depth — cycle back to generation 1
            logger.debug("BFS max depth %d reached — restarting from generation 1", self._max_length)
            self._current_generation = 1
            self._seed_generation_1(graph)
            return

        bfs_order = graph.bfs_order()
        appended_endpoints: set[str] = set()
        new_entries: list[tuple[list[str], str]] = []

        for seq in state.valid_sequences:
            if len(seq) >= self._max_length:
                continue
            for endpoint in bfs_order:
                if self._bfs_fast and endpoint.operation_id in appended_endpoints:
                    continue
                if not self._dependencies_satisfied(seq, endpoint, graph):
                    continue
                new_entries.append((list(seq), endpoint.operation_id))
                appended_endpoints.add(endpoint.operation_id)

        if new_entries:
            self._queue.extend(new_entries)
            logger.debug(
                "BFS generation %d: %d new candidates from %d valid sequences",
                self._current_generation, len(new_entries), len(state.valid_sequences),
            )
        else:
            # No new sequences possible at this depth — restart
            logger.debug("BFS generation %d: no new candidates — restarting", self._current_generation)
            self._current_generation = 0
            self._seed_generation_1(graph)

    def _dependencies_satisfied(
        self,
        sequence: list[str],
        endpoint: Endpoint,
        graph: "DependencyGraph",
    ) -> bool:
        """
        Check if all required path params of endpoint can be produced by the sequence.

        Implements RESTler's DEPENDENCIES check (Figure 3, lines 39-43):
            CONSUMES(req) ⊆ PRODUCES(seq)

        A param is satisfiable if:
        - No dependency edge exists (dictionary will supply a value), OR
        - A dependency edge exists AND the producer is in the sequence
        """
        produced_op_ids = set(sequence)
        for param in endpoint.path_params:
            if not param.required:
                continue
            edges = graph.producers_for(endpoint, param)
            if not edges:
                continue  # no known producer → dictionary supplies value
            # At least one producer must be in the sequence
            producers_in_seq = {
                e.producer_operation_id for e in edges
                if e.producer_operation_id in produced_op_ids
            }
            if not producers_in_seq:
                return False
        return True
