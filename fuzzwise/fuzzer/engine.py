"""
Main fuzzing engine — the RENDER + EXECUTE loop from RESTler Figure 3.

The engine is strategy-agnostic: it receives a BaseStrategy and BaseExplorer
as constructor arguments and never imports concrete implementations.

RESTler alignment:
    - RENDER: for each sequence in seqSet, concretize with strategy.generate()
    - EXECUTE: run the full sequence (prefix + target); check each prefix for 2xx
    - Dynamic feedback: prune sequences where any prefix returns non-2xx
    - Bug detection: any 500 response is a bug (RESTler primary oracle)
    - JSONL logging: two lines per request (RequestLog + ResponseLog)

Flow per iteration:
    1. explorer.select_next()           → (prefix, target)
    2. execute prefix requests          → re-establish state
    3. resolve target params            → resource_pool or strategy.generate()
    4. send target request              → HTTP response
    5. check 2xx prefix validity        → prune or retain sequence
    6. validate response schema         → flag schema violations
    7. classify bugs                    → 500 → BugReport
    8. log to JSONL                     → RequestLog + ResponseLog
    9. state.record_response()          → update pools, coverage, counts
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jsonschema

from fuzzwise.fuzzer.state import FuzzState
from fuzzwise.models.types import (
    BugReport,
    CampaignConfig,
    Endpoint,
    FuzzResult,
    RequestLog,
    ResponseLog,
)
from fuzzwise.spec.dependencies import DependencyGraph
from fuzzwise.strategies.base import BaseExplorer, BaseStrategy

logger = logging.getLogger(__name__)


class FuzzEngine:
    """
    Orchestrates the RESTler RENDER + EXECUTE fuzzing loop.

    Stateless between campaigns — all mutable state lives in FuzzState.
    """

    def __init__(
        self,
        config: CampaignConfig,
        endpoints: list[Endpoint],
        graph: DependencyGraph,
        strategy: BaseStrategy,
        explorer: BaseExplorer,
        http_client: httpx.AsyncClient,
        log_path: Path,
    ) -> None:
        self._config = config
        self._endpoints = endpoints
        self._graph = graph
        self._strategy = strategy
        self._explorer = explorer
        self._client = http_client
        self._log_path = log_path
        self._state = FuzzState(config, total_endpoints=len(endpoints))
        self._log_file: Any = None

    async def run(self) -> FuzzResult:
        """
        Execute the full campaign loop and return a FuzzResult.

        Opens the JSONL log file, runs until budget is exhausted,
        then closes the file and returns the aggregated result.
        """
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self._log_path, "w", encoding="utf-8")  # noqa: SIM115

        try:
            logger.info(
                "Campaign %s starting: strategy=%s explorer=%s budget=%ds max_requests=%d",
                self._config.campaign_id,
                self._config.strategy,
                self._config.explorer,
                self._config.time_budget_seconds,
                self._config.max_requests,
            )
            self._write_header()

            while not self._state.budget_exhausted():
                await self._step()

        finally:
            self._log_file.close()

        result = self._state.summary()

        # Wire in LLM call counts from explorer (Config C/D) and strategy (B-iterative)
        llm_calls = 0
        if hasattr(self._explorer, "llm_call_count"):
            llm_calls += self._explorer.llm_call_count
        if hasattr(self._strategy, "metrics") and isinstance(self._strategy.metrics, dict):
            llm_calls += self._strategy.metrics.get("llm_calls", 0)
        if llm_calls:
            result = result.model_copy(update={"llm_call_count": llm_calls})

        self._write_result_json(result)
        logger.info(
            "Campaign %s done: %d requests, %d bugs, %.1fs",
            self._config.campaign_id,
            result.total_requests,
            len(result.bugs),
            result.duration_seconds,
        )
        return result

    # ------------------------------------------------------------------
    # Single iteration
    # ------------------------------------------------------------------

    async def _step(self) -> None:
        """Execute one (prefix + target) iteration of the RENDER/EXECUTE loop."""
        prefix_endpoints, target = self._explorer.select_next(self._state, self._graph)

        # Re-execute prefix to establish state for this sequence.
        # In a real deployment we might cache prefix responses, but for correctness
        # we re-run the prefix. For short sequences (≤3) this is acceptable.
        prefix_ok = await self._execute_prefix(prefix_endpoints)
        if not prefix_ok:
            # Prefix failed — this sequence is invalid; prune it
            seq_ids = [e.operation_id for e in prefix_endpoints]
            self._state.mark_sequence_invalid(seq_ids)
            return

        # Resolve and send the target request
        resolved, resolved_from = self._resolve_params(target, prefix_endpoints)
        request_log, response_log = await self._send_request(
            target, resolved, resolved_from,
            sequence=[e.operation_id for e in prefix_endpoints],
        )

        # Dynamic feedback: if target itself fails (non-2xx, non-404), note it
        # 404 is expected for GET/DELETE when ID not found — not a sequence failure
        sc = response_log.status_code
        full_seq = [e.operation_id for e in prefix_endpoints] + [target.operation_id]
        if 200 <= sc < 300:
            self._state.mark_sequence_valid(full_seq)
        elif sc not in (404, 405, 0):
            # Non-2xx response that isn't "not found" — prune this sequence
            self._state.mark_sequence_invalid(full_seq)

        # Update state
        self._state.record_response(target, request_log, response_log)

        # Emit bug report for 500s and schema violations — deduplicate by (operation_id, bug_type)
        if response_log.is_bug:
            bug_type = "5xx" if sc >= 500 else "schema_violation"
            already_seen = any(
                b.operation_id == target.operation_id and b.bug_type == bug_type
                for b in self._state.bug_reports
            )
            if not already_seen:
                report = self._make_bug_report(
                    target, request_log, response_log,
                    full_sequence=[e.operation_id for e in prefix_endpoints] + [target.operation_id],
                )
                self._state.bug_reports.append(report)
                logger.warning(
                    "BUG: %s %s → %d (seq depth %d)",
                    target.method, target.path, sc, len(full_seq),
                )

        self._write_jsonl(request_log, response_log)

    async def _execute_prefix(self, prefix: list[Endpoint]) -> bool:
        """
        Execute all requests in the prefix sequence in order.

        Returns True if all returned 2xx, False if any returned non-2xx.
        Prefix requests are logged to JSONL as well.
        """
        for ep in prefix:
            resolved, resolved_from = self._resolve_params(ep, prefix[:prefix.index(ep)])
            request_log, response_log = await self._send_request(
                ep, resolved, resolved_from,
                sequence=[e.operation_id for e in prefix[:prefix.index(ep)]],
            )
            self._state.record_response(ep, request_log, response_log)
            self._write_jsonl(request_log, response_log)
            sc = response_log.status_code
            if not (200 <= sc < 300):
                return False
        return True

    # ------------------------------------------------------------------
    # Parameter resolution
    # ------------------------------------------------------------------

    def _resolve_params(
        self,
        endpoint: Endpoint,
        prefix: list[Endpoint],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """
        Resolve all parameters for endpoint.

        For each param:
        1. Check resource_pool via dependency edges (RESTler's dynamic object memoization)
        2. Fall back to strategy.generate() (RESTler's dictionary/fuzzable values)

        Returns:
            resolved:      param_name → value
            resolved_from: param_name → source operation_id (for logging)
        """
        resolved: dict[str, Any] = {}
        resolved_from: dict[str, str] = {}

        for param in endpoint.all_params:
            edges = self._graph.producers_for(endpoint, param)
            value = self._state.resolve_param(param, edges)
            if value is not None:
                resolved[param.name] = value
                if edges:
                    resolved_from[param.name] = edges[0].producer_operation_id
            else:
                resolved[param.name] = self._strategy.generate(endpoint, param)

        return resolved, resolved_from

    # ------------------------------------------------------------------
    # HTTP request/response
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        endpoint: Endpoint,
        resolved: dict[str, Any],
        resolved_from: dict[str, str],
        sequence: list[str],
    ) -> tuple[RequestLog, ResponseLog]:
        """Build, send, and log a single HTTP request."""
        url, path_p, query_p, body = self._build_request(endpoint, resolved)
        request_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        request_log = RequestLog(
            campaign_id=self._config.campaign_id,
            request_id=request_id,
            timestamp_iso=now,
            operation_id=endpoint.operation_id,
            method=endpoint.method,
            url=url,
            sequence=list(sequence),
            path_params=path_p,
            query_params=query_p,
            headers={},
            body=body,
            resolved_from=resolved_from,
            seed=self._config.seed,
        )

        t0 = time.monotonic()
        try:
            headers = dict(self._config.extra_headers)
            if body is not None:
                headers["Content-Type"] = "application/json"
            resp = await self._client.request(
                method=endpoint.method,
                url=url,
                params=query_p if query_p else None,
                json=body if body is not None else None,
                headers=headers,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            resp_body = self._parse_body(resp)
            schema_valid, schema_errors = self._validate_schema(
                endpoint, resp.status_code, resp_body
            )
            is_bug = resp.status_code >= 500
            bug_cls = "5xx" if is_bug else (
                "schema_violation" if schema_valid is False else None
            )
            response_log = ResponseLog(
                request_id=request_id,
                timestamp_iso=datetime.now(UTC).isoformat(),
                status_code=resp.status_code,
                headers=dict(resp.headers),
                body=resp_body,
                latency_ms=latency_ms,
                schema_valid=schema_valid,
                schema_errors=schema_errors,
                is_bug=is_bug or (schema_valid is False),
                bug_classification=bug_cls,
            )
        except httpx.RequestError as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.debug("Network error for %s %s: %s", endpoint.method, url, exc)
            response_log = ResponseLog(
                request_id=request_id,
                timestamp_iso=datetime.now(UTC).isoformat(),
                status_code=0,
                body={"error": str(exc)},
                latency_ms=latency_ms,
                schema_valid=None,
                is_bug=False,
            )

        return request_log, response_log

    def _build_request(
        self,
        endpoint: Endpoint,
        resolved: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        """
        Separate resolved params by location and construct URL.

        Returns: (url, path_params, query_params, body)
        """
        path_p: dict[str, Any] = {}
        query_p: dict[str, Any] = {}
        body_p: dict[str, Any] = {}

        for param in endpoint.path_params:
            val = resolved.get(param.name)
            if val is not None:
                path_p[param.name] = val

        for param in endpoint.query_params:
            val = resolved.get(param.name)
            if val is not None:
                query_p[param.name] = val

        # Check for synthetic "_body" param (array-typed requestBody from parser)
        array_body_param = next(
            (p for p in endpoint.body_params if p.name == "_body"), None
        )
        if array_body_param is not None:
            body_p = None  # not used
        else:
            for param in endpoint.body_params:
                val = resolved.get(param.name)
                if val is not None:
                    body_p[param.name] = val

        # Substitute path parameters into the URL template
        # URL-encode values so non-printable/special chars are valid in the URL
        # (the server still receives the decoded value)
        url_path = endpoint.path
        for name, value in path_p.items():
            url_path = url_path.replace(f"{{{name}}}", quote(str(value), safe=""))

        full_url = self._config.target_base_url.rstrip("/") + url_path

        if array_body_param is not None:
            body = resolved.get("_body", [])  # send the array directly
        else:
            body = body_p if body_p else None

        return full_url, path_p, query_p, body

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        """Attempt to parse the response body as JSON."""
        try:
            if response.content:
                return response.json()
        except Exception:
            pass
        text = response.text.strip()
        return text if text else None

    @staticmethod
    def _validate_schema(
        endpoint: Endpoint,
        status_code: int,
        body: Any,
    ) -> tuple[bool | None, list[str]]:
        """
        Validate body against the declared response schema for status_code.

        Returns (schema_valid, schema_errors):
            (None, [])        — no schema declared for this status code
            (True, [])        — body matches schema
            (False, [errors]) — body violates schema
        """
        schema = endpoint.response_schemas.get(str(status_code))
        if not schema or body is None:
            return None, []
        try:
            jsonschema.validate(instance=body, schema=schema)
            return True, []
        except jsonschema.ValidationError as exc:
            return False, [exc.message]
        except jsonschema.SchemaError as exc:
            logger.debug("Schema itself is malformed for %s: %s", endpoint.operation_id, exc)
            return None, []

    # ------------------------------------------------------------------
    # Bug reporting
    # ------------------------------------------------------------------

    def _make_bug_report(
        self,
        endpoint: Endpoint,
        request_log: RequestLog,
        response_log: ResponseLog,
        full_sequence: list[str],
    ) -> BugReport:
        """Create a BugReport with minimal_sequence (bug bucketization)."""
        # Minimal suffix: for now, just the target endpoint itself
        # (full bucketization via suffix analysis can be added in analysis/)
        minimal_sequence = [endpoint.operation_id]
        payload = {}
        if request_log.body and isinstance(request_log.body, dict):
            payload.update(request_log.body)
        payload.update(request_log.path_params)

        return BugReport(
            campaign_id=self._config.campaign_id,
            request_id=request_log.request_id,
            operation_id=endpoint.operation_id,
            bug_type="5xx" if response_log.status_code >= 500 else "schema_violation",
            status_code=response_log.status_code,
            description=(
                f"{endpoint.method} {endpoint.path} returned {response_log.status_code}"
            ),
            timestamp_iso=response_log.timestamp_iso,
            full_sequence=full_sequence,
            minimal_sequence=minimal_sequence,
            payload_snippet=payload,
        )

    # ------------------------------------------------------------------
    # JSONL logging
    # ------------------------------------------------------------------

    def _write_header(self) -> None:
        """Write campaign config as the first line of the JSONL file."""
        header = {"record_type": "campaign_header", **self._config.model_dump()}
        self._log_file.write(json.dumps(header) + "\n")
        self._log_file.flush()

    def _write_jsonl(self, request_log: RequestLog, response_log: ResponseLog) -> None:
        """Write one request + one response line to the JSONL log."""
        self._log_file.write(json.dumps(request_log.model_dump()) + "\n")
        self._log_file.write(json.dumps(response_log.model_dump()) + "\n")
        self._log_file.flush()

    def _write_result_json(self, result: FuzzResult) -> None:
        """Write the FuzzResult summary as a separate .json file."""
        result_path = self._log_path.with_suffix(".result.json")
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
