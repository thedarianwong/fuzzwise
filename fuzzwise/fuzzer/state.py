"""
Mutable campaign state for a fuzzing campaign.

FuzzState tracks:
    - resource_pool: dynamic objects (IDs etc.) extracted from 2xx responses.
      This is RESTler's "memoized dynamic objects" — the engine uses these to
      resolve path/body parameters for dependent requests.
    - seq_pool: the set of valid request sequences (RESTler's seqSet).
      A sequence is valid if all its prefixes returned 2xx.
    - request_history: ordered list of all (RequestLog, ResponseLog) pairs.
    - coverage: set of operation_ids hit at least once.
    - bug_reports: list of BugReport objects emitted during the campaign.

RESTler alignment:
    resource_pool[operation_id] = list of response body dicts from 2xx responses.
    When consumer needs a value produced by an operation, it looks here.
    Pool is capped at 10 entries per operation to bound memory.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fuzzwise.models.types import (
    BugReport,
    CampaignConfig,
    Endpoint,
    FuzzResult,
    Parameter,
    ParameterLocation,
    RequestLog,
    ResponseLog,
)
from fuzzwise.spec.dependencies import DependencyEdge

logger = logging.getLogger(__name__)

_POOL_CAP = 10  # max entries per operation_id in resource_pool


class FuzzState:
    """
    Mutable campaign state. Updated after every request by the engine.

    Not thread-safe — fuzzing campaigns are single-threaded.
    """

    def __init__(self, config: CampaignConfig, total_endpoints: int) -> None:
        self.config = config
        self.total_endpoints = total_endpoints

        # RESTler's "memoized dynamic objects"
        # key   = producer operation_id
        # value = list of response body dicts (capped at _POOL_CAP)
        self.resource_pool: dict[str, list[Any]] = {}

        # RESTler's seqSet: set of valid sequence operation_id lists
        # Stored as list of lists (sequences are mutable during BFS extension)
        self.valid_sequences: list[list[str]] = [[]]  # starts with empty sequence ε

        # Request/response history (ordered)
        self.request_history: list[tuple[RequestLog, ResponseLog]] = []

        # Coverage tracking
        self.coverage: set[str] = set()
        self.error_counts: dict[str, int] = {}       # op_id → 5xx count
        self.status_code_counts: dict[str, int] = {} # "200" → count

        # Bug tracking
        self.bug_reports: list[BugReport] = []

        # Counters
        self.total_requests: int = 0
        self.sequences_explored: int = 0
        self.max_depth_reached: int = 0
        self._start_time: float = time.monotonic()

    # ------------------------------------------------------------------
    # Budget and timing
    # ------------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def budget_exhausted(self) -> bool:
        """True if either the request count or time limit is reached."""
        return (
            self.total_requests >= self.config.max_requests
            or self.elapsed_seconds() >= self.config.time_budget_seconds
        )

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def record_response(
        self,
        endpoint: Endpoint,
        request_log: RequestLog,
        response_log: ResponseLog,
    ) -> None:
        """
        Update all state fields after a completed request.

        Called by the engine after every HTTP request, whether successful or not.
        """
        self.request_history.append((request_log, response_log))
        self.total_requests += 1
        self.coverage.add(endpoint.operation_id)

        sc = response_log.status_code
        sc_str = str(sc)
        self.status_code_counts[sc_str] = self.status_code_counts.get(sc_str, 0) + 1

        if sc >= 500:
            self.error_counts[endpoint.operation_id] = (
                self.error_counts.get(endpoint.operation_id, 0) + 1
            )

        # Add to resource pool if 2xx and body contains data
        if 200 <= sc < 300 and response_log.body is not None:
            self._pool_add(endpoint.operation_id, response_log.body)

        # Track sequence depth: sequence is the prefix, so total depth = prefix + 1
        depth = len(request_log.sequence) + 1
        if depth > self.max_depth_reached:
            self.max_depth_reached = depth

    def mark_sequence_valid(self, sequence: list[str]) -> None:
        """
        Add a sequence to the valid sequence pool (RESTler's seqSet).

        Called by the engine when all prefixes of a sequence returned 2xx.
        """
        if sequence not in self.valid_sequences:
            self.valid_sequences.append(list(sequence))
            self.sequences_explored += 1

    def mark_sequence_invalid(self, sequence: list[str]) -> None:
        """
        Remove a sequence from the valid pool due to a non-2xx prefix response.

        This is RESTler's dynamic feedback / pruning mechanism.
        """
        try:
            self.valid_sequences.remove(sequence)
        except ValueError:
            pass

    def _pool_add(self, operation_id: str, body: Any) -> None:
        """Add a response body to the resource pool, respecting the cap."""
        pool = self.resource_pool.setdefault(operation_id, [])
        if isinstance(body, dict):
            if body not in pool:  # avoid duplicates
                pool.append(body)
        elif isinstance(body, list):
            for item in body:
                if isinstance(item, dict) and item not in pool:
                    pool.append(item)
        # Cap the pool
        if len(pool) > _POOL_CAP:
            self.resource_pool[operation_id] = pool[-_POOL_CAP:]

    # ------------------------------------------------------------------
    # Parameter resolution (RESTler's PRODUCES → CONSUMES matching)
    # ------------------------------------------------------------------

    def resolve_param(
        self,
        param: Parameter,
        edges: list[DependencyEdge],
    ) -> Any | None:
        """
        Try to satisfy a parameter from the resource pool.

        For each candidate edge (sorted by confidence), look up the pool of the
        producer operation and extract the response field value.

        Returns the first value found, or None if the pool is empty / the field
        is not present in any response body.

        Field lookup is case-insensitive to handle inconsistent naming.
        """
        for edge in edges:
            pool = self.resource_pool.get(edge.producer_operation_id, [])
            for response_body in reversed(pool):  # most recent first
                value = _extract_field(response_body, edge.producer_response_field)
                if value is not None:
                    return value
        return None

    def resolve_param_by_name(self, param_name: str) -> Any | None:
        """
        Attempt to resolve a parameter by scanning all pools for any field
        whose name case-insensitively matches param_name.

        Used as a fallback when no explicit dependency edge exists.
        """
        for pool in self.resource_pool.values():
            for body in reversed(pool):
                value = _extract_field(body, param_name)
                if value is not None:
                    return value
        return None

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> FuzzResult:
        """Compute and return a FuzzResult from the current campaign state."""
        unique_500s = len({
            b.operation_id for b in self.bug_reports if b.bug_type == "5xx"
        })
        return FuzzResult(
            campaign_id=self.config.campaign_id,
            config_label=self.config.config_label,
            spec_path=self.config.spec_path,
            strategy=self.config.strategy,
            explorer=self.config.explorer,
            seed=self.config.seed,
            max_sequence_length=self.config.max_sequence_length,
            total_requests=self.total_requests,
            total_endpoints=self.total_endpoints,
            endpoints_hit=len(self.coverage),
            unique_500_count=unique_500s,
            error_type_count=len({(b.operation_id, b.bug_type) for b in self.bug_reports}),
            schema_violation_count=sum(
                1 for _, r in self.request_history if r.schema_valid is False
            ),
            status_code_distribution=dict(self.status_code_counts),
            bugs=list(self.bug_reports),
            duration_seconds=self.elapsed_seconds(),
            sequences_explored=self.sequences_explored,
            max_depth_reached=self.max_depth_reached,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_field(body: Any, field_name: str) -> Any | None:
    """
    Case-insensitive field extraction from a response body dict.

    Only looks at the top level (RESTler also only uses top-level fields).
    Returns None if field not found or body is not a dict.
    """
    if not isinstance(body, dict):
        return None
    field_lower = field_name.lower()
    for key, value in body.items():
        if key.lower() == field_lower and value is not None:
            return value
    return None
