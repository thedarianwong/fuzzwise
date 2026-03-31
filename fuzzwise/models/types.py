"""
Core Pydantic data models for FUZZWISE.

These models are the shared data contract between all modules. Every request
sent and response received is represented as a RequestLog + ResponseLog pair,
written as JSON lines to the campaign log file.

Import graph position: nothing in fuzzwise imports this module — everything
imports FROM it. Never import other fuzzwise modules here.

RESTler alignment:
    - Sequence is a list of operation_ids (RESTler's seqSet entries)
    - RequestLog.sequence captures the full prefix that was executed
    - Bug bucketization uses the minimal suffix of the triggering sequence
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ParameterLocation(str, Enum):
    """Where a parameter lives in the HTTP request."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    BODY = "body"  # pseudo-location for flattened requestBody fields


# ---------------------------------------------------------------------------
# Spec models  (populated by fuzzwise/spec/parser.py)
# ---------------------------------------------------------------------------


class Parameter(BaseModel):
    """
    One parameter declared in an OpenAPI operation.

    Covers all four locations (path, query, header, body). Body parameters
    are synthetic — the parser flattens requestBody.properties into Parameter
    objects with location=BODY for uniform handling by the engine.

    schema_type mirrors the JSON Schema 'type' keyword. For parameters with
    no explicit type (e.g., a bare $ref), defaults to "string" as RESTler does.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    location: ParameterLocation
    schema_type: str = "string"  # string|integer|number|boolean|array|object
    required: bool = False
    description: str | None = None
    default: Any = None
    enum_values: list[Any] = Field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    format: str | None = None    # "int64", "email", "uuid", "date-time", …
    item_type: str | None = None  # for array schemas: element type


class Endpoint(BaseModel):
    """
    One API operation (path + HTTP method) parsed from an OpenAPI spec.

    operation_id is the primary key used throughout the system:
      - keys the resource_pool in FuzzState
      - keys the dependency graph nodes
      - appears in RequestLog and sequence lists

    If the spec omits operationId, the parser synthesizes one via:
        f"{method.upper()}_{path.replace('/', '_').replace('{','').replace('}','').strip('_')}"

    This synthesis is deterministic and stable — do not use a counter.

    response_schemas stores raw JSON Schema dicts (already $ref-resolved by the
    parser) keyed by status code string ("200", "404", etc.).
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str
    method: str    # "GET" | "POST" | "PUT" | "DELETE" | "PATCH"
    path: str      # URL template, e.g. "/pets/{petId}"
    path_params: list[Parameter] = Field(default_factory=list)
    query_params: list[Parameter] = Field(default_factory=list)
    header_params: list[Parameter] = Field(default_factory=list)
    body_params: list[Parameter] = Field(default_factory=list)
    response_schemas: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None

    @property
    def all_params(self) -> list[Parameter]:
        """Flat list of all parameters across all locations."""
        return self.path_params + self.query_params + self.header_params + self.body_params


class DependencyEdge(BaseModel):
    """
    A directed producer → consumer dependency between two endpoints.

    Example:
        POST /pets (producer) returns {"id": 1}
        GET  /pets/{petId} (consumer) needs petId in the path

        DependencyEdge(
            producer_operation_id = "addPet",
            consumer_operation_id = "getPetById",
            producer_response_field = "id",
            consumer_param_name = "petId",
            consumer_param_location = ParameterLocation.PATH,
            field_type = "integer",
            confidence = 0.9,
        )

    confidence scoring (RESTler-faithful heuristic):
        1.0  exact case-insensitive name match (param.name == field_name)
        0.9  normalized match (strip Id/id suffix, lowercase)
        0.8  producer field is literally "id" (generic match)
        ×0.5 type mismatch penalty
    """

    model_config = ConfigDict(frozen=True)

    producer_operation_id: str
    consumer_operation_id: str
    producer_response_field: str
    consumer_param_name: str
    consumer_param_location: ParameterLocation
    field_type: str = "string"
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


# ---------------------------------------------------------------------------
# Campaign configuration
# ---------------------------------------------------------------------------


class CampaignConfig(BaseModel):
    """
    All settings for one fuzzing campaign.

    Embedded in the campaign log header so any log file is self-describing.

    campaign_id is assigned at startup (UUID4) and is the filename stem
    for the JSONL log: logs/campaign_{campaign_id}.jsonl
    """

    campaign_id: str
    spec_path: str
    target_base_url: str
    strategy: str = "dictionary"   # "dictionary" | "llm"
    explorer: str = "bfs"          # "bfs" | "llm_guided"
    max_requests: int = 500
    time_budget_seconds: float = 300.0
    max_sequence_length: int = 3   # RESTler default maxLength
    seed: int = 42
    log_dir: str = "./logs"
    min_confidence: float = 0.5
    config_label: str = "A"        # "A" | "B" | "C"
    extra_headers: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runtime log models  (written to JSONL during a campaign)
# ---------------------------------------------------------------------------


class RequestLog(BaseModel):
    """
    One HTTP request sent during a fuzzing campaign.

    Written as a single JSON line in the campaign JSONL file.
    record_type="request" distinguishes it from ResponseLog on read-back.

    sequence is the list of operation_ids executed before this request
    (the prefix). Together with operation_id, this describes the full
    RESTler sequence being tested.

    resolved_from maps param names to the operation_id that produced
    their value from the resource pool (for dependency chain verification).
    """

    campaign_id: str
    request_id: str               # UUID4 — FK for the matching ResponseLog
    timestamp_iso: str            # UTC ISO 8601
    operation_id: str
    method: str
    url: str                      # fully-expanded URL including base
    sequence: list[str] = Field(default_factory=list)  # prefix operation_ids
    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    resolved_from: dict[str, str] = Field(default_factory=dict)
    seed: int = 42
    record_type: str = "request"  # fixed — used for JSONL parsing


class ResponseLog(BaseModel):
    """
    The HTTP response received for a corresponding RequestLog.

    Written immediately after the matching RequestLog in the JSONL file.
    request_id links back to the request.

    schema_valid:
        True  — body matches the declared schema for this status code
        False — schema validation failed (schema_errors will be non-empty)
        None  — no schema declared for this status code, or non-JSON body

    is_bug is set True for status_code >= 500.
    """

    request_id: str               # FK → RequestLog.request_id
    timestamp_iso: str
    status_code: int = 0          # 0 = network-level error (timeout, refused)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    latency_ms: float = 0.0
    schema_valid: bool | None = None
    schema_errors: list[str] = Field(default_factory=list)
    is_bug: bool = False
    bug_classification: str | None = None  # "5xx" | "schema_violation"
    record_type: str = "response"  # fixed — used for JSONL parsing


# ---------------------------------------------------------------------------
# Bug reporting
# ---------------------------------------------------------------------------


class BugReport(BaseModel):
    """
    A single bug found during a campaign.

    RESTler-faithful: a bug is a 500 status code response.
    Bug bucketization: the minimal_sequence is the shortest suffix of the
    triggering sequence that still reproduces the 500. Used to de-duplicate
    bugs triggered by different paths to the same state.
    """

    campaign_id: str
    request_id: str               # links to the RequestLog/ResponseLog pair
    operation_id: str
    bug_type: str                 # "5xx" | "schema_violation"
    status_code: int
    description: str
    timestamp_iso: str
    full_sequence: list[str]      # complete sequence of operation_ids
    minimal_sequence: list[str]   # shortest suffix that triggers the bug
    payload_snippet: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Campaign result  (written at end of campaign, consumed by analysis/)
# ---------------------------------------------------------------------------


class FuzzResult(BaseModel):
    """
    Aggregated result of a complete fuzzing campaign.

    Serialized to JSON alongside the JSONL log at campaign end.
    The analysis module loads these to build comparison tables.

    Metrics aligned with RESTler paper and FSE 2020 paper:
        unique_500_count  — distinct bug buckets (RESTler primary metric)
        error_type_count  — distinct (status_code, error_msg_sanitized) pairs
                            (FSE 2020 primary metric — more granular than 500 count)
        coverage_fraction — endpoints_hit / total_endpoints
    """

    campaign_id: str
    config_label: str             # "A" | "B" | "C"
    spec_path: str
    strategy: str
    explorer: str
    seed: int
    max_sequence_length: int
    total_requests: int = 0
    total_endpoints: int = 0
    endpoints_hit: int = 0
    unique_500_count: int = 0     # RESTler primary metric
    error_type_count: int = 0     # FSE 2020 primary metric
    schema_violation_count: int = 0
    status_code_distribution: dict[str, int] = Field(default_factory=dict)
    bugs: list[BugReport] = Field(default_factory=list)
    duration_seconds: float = 0.0
    sequences_explored: int = 0   # size of seqSet at campaign end
    max_depth_reached: int = 0    # deepest sequence length successfully executed
    llm_call_count: int = 0       # 0 for Config A
    llm_latency_total_ms: float = 0.0  # 0 for Config A
