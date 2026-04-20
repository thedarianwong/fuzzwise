"""
Producer-consumer dependency inference between API endpoints.

Implements the RESTler naming heuristic from ICSE 2019:
    For each path/body parameter consumed by endpoint B, search all 2xx
    response schemas of POST/PUT endpoints for fields with matching names.
    Confidence is based on how well names match after normalization.

The resulting DependencyGraph drives the BFS sequence generation in the engine:
    - roots() → endpoints with no dependencies (safe to call first)
    - bfs_order() → topological BFS ordering for sequence generation
    - producers_for(endpoint, param) → candidate producer edges for a param

Usage:
    from fuzzwise.spec.dependencies import build_dependency_graph
    graph = build_dependency_graph(endpoints, min_confidence=0.5)
"""

from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any

from fuzzwise.models.types import DependencyEdge, Endpoint, Parameter, ParameterLocation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------


class DependencyGraph:
    """
    Directed graph of producer-consumer dependencies between endpoints.

    Nodes are Endpoint objects. Edges are DependencyEdge objects.
    Edges only exist for inferred dependencies above min_confidence.
    """

    def __init__(self, endpoints: list[Endpoint], edges: list[DependencyEdge]) -> None:
        self._endpoints = endpoints
        self._endpoint_map: dict[str, Endpoint] = {e.operation_id: e for e in endpoints}
        self._edges = edges
        self._bfs_order_cache: list[Endpoint] | None = None

    @property
    def edges(self) -> list[DependencyEdge]:
        return list(self._edges)

    @property
    def endpoints(self) -> list[Endpoint]:
        return list(self._endpoints)

    def producers_for(self, consumer: Endpoint, param: Parameter) -> list[DependencyEdge]:
        """
        Return all dependency edges where this param is the consumer side,
        sorted by confidence descending (highest confidence first).
        """
        matches = [
            e for e in self._edges
            if e.consumer_operation_id == consumer.operation_id
            and e.consumer_param_name == param.name
        ]
        return sorted(matches, key=lambda e: e.confidence, reverse=True)

    def roots(self) -> list[Endpoint]:
        """
        Endpoints with no incoming dependency edges.
        These can be executed first in any sequence (no prerequisites).
        """
        consumers = {e.consumer_operation_id for e in self._edges}
        return [ep for ep in self._endpoints if ep.operation_id not in consumers]

    def bfs_order(self) -> list[Endpoint]:
        """
        Topological BFS ordering via Kahn's algorithm.

        Returns endpoints in layers: roots first, then endpoints whose
        dependencies are all roots, etc. Within each layer, sorted by
        operation_id for determinism.

        If the graph contains cycles (unusual but possible), remaining nodes
        are appended in sorted order with a one-time warning.

        Result is cached — safe to call repeatedly with no performance penalty.
        """
        if self._bfs_order_cache is not None:
            return self._bfs_order_cache

        # Build in-degree map and adjacency list (producer → set of consumers)
        in_degree: dict[str, int] = {e.operation_id: 0 for e in self._endpoints}
        adj: dict[str, set[str]] = {e.operation_id: set() for e in self._endpoints}

        for edge in self._edges:
            p = edge.producer_operation_id
            c = edge.consumer_operation_id
            if p != c:  # skip self-loops
                adj[p].add(c)
                in_degree[c] = in_degree.get(c, 0) + 1

        queue: deque[str] = deque(
            sorted(op_id for op_id, deg in in_degree.items() if deg == 0)
        )
        order: list[Endpoint] = []

        while queue:
            op_id = queue.popleft()
            if op_id in self._endpoint_map:
                order.append(self._endpoint_map[op_id])
            for consumer_id in sorted(adj.get(op_id, set())):
                in_degree[consumer_id] -= 1
                if in_degree[consumer_id] == 0:
                    queue.append(consumer_id)

        # Handle cycles: append remaining endpoints in sorted order
        remaining_ids = {
            op_id for op_id, deg in in_degree.items() if deg > 0
        }
        if remaining_ids:
            logger.warning(
                "Dependency cycle detected among: %s — appending in sorted order",
                sorted(remaining_ids),
            )
            for op_id in sorted(remaining_ids):
                if op_id in self._endpoint_map:
                    order.append(self._endpoint_map[op_id])

        self._bfs_order_cache = order
        return order

    def summary(self) -> str:
        """Human-readable summary for CLI output."""
        lines = [
            f"Dependency graph: {len(self._endpoints)} endpoints, {len(self._edges)} edges",
            f"Roots: {[e.operation_id for e in self.roots()]}",
        ]
        for edge in sorted(self._edges, key=lambda e: e.confidence, reverse=True):
            lines.append(
                f"  {edge.producer_operation_id}.{edge.producer_response_field}"
                f" → {edge.consumer_operation_id}.{edge.consumer_param_name}"
                f" (confidence={edge.confidence:.2f})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_dependency_graph(
    endpoints: list[Endpoint],
    min_confidence: float = 0.5,
) -> DependencyGraph:
    """
    Infer producer-consumer dependencies and return a DependencyGraph.

    Implements the RESTler ICSE 2019 dependency inference heuristic.

    Args:
        endpoints:      All endpoints parsed from the spec.
        min_confidence: Edges below this threshold are excluded.

    Returns:
        DependencyGraph with all inferred edges above min_confidence.
    """
    edges: list[DependencyEdge] = []

    for consumer in endpoints:
        for param in consumer.all_params:
            # RESTler only links PATH parameters to producers (dynamic resource IDs).
            # Body params (name, status, etc.) are always supplied by the dictionary
            # or LLM — never inferred from responses. This matches RESTler's CONSUMES
            # definition which refers to dynamic objects (IDs), not primitive values.
            if param.location != ParameterLocation.PATH:
                continue

            for producer in endpoints:
                if producer.operation_id == consumer.operation_id:
                    continue  # no self-dependencies
                if producer.method not in ("POST", "PUT", "PATCH"):
                    continue  # only mutating methods create new resources

                for status_code, schema in producer.response_schemas.items():
                    if not (200 <= int(status_code) < 300):
                        continue
                    for field_name, field_schema in _flatten_schema_fields(schema):
                        confidence = _compute_confidence(param, field_name, field_schema)
                        if confidence >= min_confidence:
                            edges.append(DependencyEdge(
                                producer_operation_id=producer.operation_id,
                                consumer_operation_id=consumer.operation_id,
                                producer_response_field=field_name,
                                consumer_param_name=param.name,
                                consumer_param_location=param.location,
                                field_type=param.schema_type,
                                confidence=confidence,
                            ))

    # De-duplicate: keep only the highest-confidence edge per
    # (producer, consumer, consumer_param) triple
    edges = _deduplicate_edges(edges)
    logger.info("Inferred %d dependency edges (min_confidence=%.2f)", len(edges), min_confidence)
    return DependencyGraph(endpoints, edges)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """
    Normalize a field/param name for fuzzy matching.

    Strips common ID suffixes, lowercases, removes underscores.

    Examples:
        "petId"    → "pet"
        "pet_id"   → "pet"
        "PET_ID"   → "pet"
        "id"       → "id"      (kept as-is for generic matching)
        "orderId"  → "order"
    """
    name = name.lower()
    # Strip trailing _id, id (camelCase or snake_case)
    name = re.sub(r"_?id$", "", name)
    name = re.sub(r"id$", "", name)
    name = name.replace("_", "")
    return name or "id"  # fallback to "id" if everything was stripped


def _compute_confidence(
    param: Parameter,
    field_name: str,
    field_schema: dict[str, Any],
) -> float:
    """
    Compute match confidence between a consumer param and a producer field.

    Scoring (RESTler-faithful):
        1.0  exact case-insensitive match
        0.9  normalized match (strip Id suffix, lowercase)
        0.8  field_name is literally "id" (generic match)
        × 0.5 type mismatch penalty
    """
    field_type = field_schema.get("type", "string")
    type_match = _types_compatible(param.schema_type, field_type)
    type_multiplier = 1.0 if type_match else 0.5

    # Exact case-insensitive match
    if param.name.lower() == field_name.lower():
        return 1.0 * type_multiplier

    # Normalized match
    if _normalize(param.name) == _normalize(field_name) and _normalize(param.name) != "id":
        return 0.9 * type_multiplier

    # Generic "id" field — any parameter ending in "id" can be served by an "id" field
    if field_name.lower() == "id" and param.name.lower().endswith("id"):
        return 0.8 * type_multiplier

    return 0.0


def _types_compatible(param_type: str, field_type: str) -> bool:
    """
    Check if a consumer param type is compatible with a producer field type.

    Treats integer/number as compatible (both are numeric).
    """
    if param_type == field_type:
        return True
    numeric = {"integer", "number"}
    if param_type in numeric and field_type in numeric:
        return True
    return False


def _flatten_schema_fields(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """
    Return all (field_name, field_schema) pairs from a JSON Schema dict.

    Handles:
        - Direct properties
        - allOf (merge sub-schemas)
        - Array items (extract item properties)
    """
    if not isinstance(schema, dict):
        return []

    fields: list[tuple[str, dict[str, Any]]] = []

    # Direct properties
    for name, prop_schema in schema.get("properties", {}).items():
        fields.append((name, prop_schema if isinstance(prop_schema, dict) else {}))

    # allOf: merge sub-schemas
    for sub in schema.get("allOf", []):
        fields.extend(_flatten_schema_fields(sub))

    # Array: look inside items
    if schema.get("type") == "array" and "items" in schema:
        fields.extend(_flatten_schema_fields(schema["items"]))

    return fields


def _deduplicate_edges(edges: list[DependencyEdge]) -> list[DependencyEdge]:
    """
    Keep only the highest-confidence edge per (producer, consumer, param) triple.
    """
    best: dict[tuple[str, str, str], DependencyEdge] = {}
    for edge in edges:
        key = (edge.producer_operation_id, edge.consumer_operation_id, edge.consumer_param_name)
        if key not in best or edge.confidence > best[key].confidence:
            best[key] = edge
    return list(best.values())
