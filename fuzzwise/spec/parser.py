"""
OpenAPI 3.0 spec parser.

Reads a YAML or JSON OpenAPI spec file and produces a list of Endpoint objects
with fully resolved parameters and response schemas.

RESTler alignment:
    - $ref resolution matches RESTler's compiler module behaviour
    - operationId synthesis uses a deterministic path-derived formula
    - allOf merges sub-schemas; oneOf/anyOf takes the first branch (with warning)
    - Body parameters are flattened from requestBody.properties into Parameter objects

Usage:
    from fuzzwise.spec.parser import parse_spec
    endpoints = parse_spec("data/specs/petstore.yaml")
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from fuzzwise.models.types import Endpoint, Parameter, ParameterLocation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_spec(spec_path: str | Path) -> list[Endpoint]:
    """
    Parse an OpenAPI 3.0 YAML or JSON file into a list of Endpoint objects.

    Args:
        spec_path: Path to the OpenAPI spec file (.yaml, .yml, or .json).

    Returns:
        List of Endpoint objects, one per (path, method) pair in the spec.

    Raises:
        FileNotFoundError: If the spec file does not exist.
        ValueError: If the file cannot be parsed as valid OpenAPI.
    """
    path = Path(spec_path)
    if not path.exists():
        raise FileNotFoundError(f"Spec file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        spec = json.loads(raw)
    else:
        spec = yaml.safe_load(raw)

    if not isinstance(spec, dict):
        raise ValueError(f"Spec file did not parse to a dict: {path}")

    schema_registry = spec.get("components", {}).get("schemas", {})
    endpoints: list[Endpoint] = []

    for path_str, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            operation = path_item.get(method)
            if operation is None:
                continue
            endpoint = _parse_operation(path_str, method.upper(), operation, schema_registry)
            endpoints.append(endpoint)

    logger.info("Parsed %d endpoints from %s", len(endpoints), path)
    return endpoints


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _synthesize_operation_id(method: str, path: str) -> str:
    """
    Deterministic operationId synthesis used when spec omits operationId.

    Formula: "{METHOD}_{path_segments_without_braces}"
    Example: GET /pets/{petId} → "GET_pets_petId"
    """
    clean = re.sub(r"[{}]", "", path)          # remove { }
    clean = re.sub(r"[^a-zA-Z0-9_/]", "_", clean)  # non-alphanumeric → _
    segments = [s for s in clean.split("/") if s]
    return f"{method.upper()}_{'_'.join(segments)}" if segments else method.upper()


def resolve_ref(ref: str, registry: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve an internal $ref of the form '#/components/schemas/Foo'.

    Only internal refs are supported. External file refs raise NotImplementedError.

    Args:
        ref:      The $ref string, e.g. '#/components/schemas/Pet'.
        registry: The components.schemas dict from the spec.

    Returns:
        The resolved schema dict.
    """
    if not ref.startswith("#/components/schemas/"):
        raise NotImplementedError(f"Only internal $refs are supported, got: {ref!r}")
    schema_name = ref.split("/")[-1]
    if schema_name not in registry:
        logger.warning("$ref '%s' not found in components/schemas — treating as empty object", ref)
        return {"type": "object"}
    return dict(registry[schema_name])


def _resolve_schema(schema: Any, registry: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve $ref in a schema dict."""
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return _resolve_schema(resolve_ref(schema["$ref"], registry), registry)
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            resolved = _resolve_schema(sub, registry)
            # Merge properties
            merged.setdefault("properties", {}).update(resolved.get("properties", {}))
            # Merge required lists
            existing_required = merged.get("required", [])
            merged["required"] = list(set(existing_required + resolved.get("required", [])))
            # Take type from first sub-schema that declares it
            if "type" not in merged and "type" in resolved:
                merged["type"] = resolved["type"]
        return merged
    if "oneOf" in schema or "anyOf" in schema:
        key = "oneOf" if "oneOf" in schema else "anyOf"
        branches = schema[key]
        if branches:
            logger.warning("'%s' schema — taking first branch only", key)
            return _resolve_schema(branches[0], registry)
        return {}
    # Recursively resolve nested properties
    result = dict(schema)
    if "properties" in result:
        result["properties"] = {
            k: _resolve_schema(v, registry)
            for k, v in result["properties"].items()
        }
    if "items" in result:
        result["items"] = _resolve_schema(result["items"], registry)
    return result


def _extract_type(schema: dict[str, Any]) -> str:
    """Extract the JSON Schema 'type' string, defaulting to 'string'."""
    return schema.get("type", "string")


def _parse_parameter(param_dict: dict[str, Any], registry: dict[str, Any]) -> Parameter | None:
    """Parse a single OpenAPI parameter object into a Parameter model."""
    if "$ref" in param_dict:
        # Parameters can also be $refs (less common but valid)
        ref_name = param_dict["$ref"].split("/")[-1]
        param_dict = registry.get(ref_name, param_dict)

    name = param_dict.get("name", "")
    location_str = param_dict.get("in", "query")
    try:
        location = ParameterLocation(location_str)
    except ValueError:
        logger.warning("Unknown parameter location '%s' for '%s' — skipping", location_str, name)
        return None

    schema = _resolve_schema(param_dict.get("schema", {}), registry)
    schema_type = _extract_type(schema)

    return Parameter(
        name=name,
        location=location,
        schema_type=schema_type,
        required=param_dict.get("required", False),
        description=param_dict.get("description") or schema.get("description"),
        default=schema.get("default"),
        enum_values=list(schema.get("enum", [])),
        minimum=schema.get("minimum"),
        maximum=schema.get("maximum"),
        min_length=schema.get("minLength"),
        max_length=schema.get("maxLength"),
        pattern=schema.get("pattern"),
        format=schema.get("format"),
        item_type=_extract_type(schema.get("items", {})) if schema_type == "array" else None,
    )


def _parse_body_params(
    request_body: dict[str, Any],
    registry: dict[str, Any],
) -> list[Parameter]:
    """
    Flatten a requestBody into a list of body Parameter objects.

    Only application/json content type is processed. Each property in the
    schema becomes one Parameter with location=BODY.
    """
    if not request_body:
        return []

    content = request_body.get("content", {})
    json_content = content.get("application/json", {})
    schema = _resolve_schema(json_content.get("schema", {}), registry)

    # Array-typed requestBody: the whole body is a list, not a dict of properties.
    # Add a synthetic "_body" parameter so the engine sends a proper JSON array.
    if schema.get("type") == "array":
        items_schema = schema.get("items", {})
        return [Parameter(
            name="_body",
            location=ParameterLocation.BODY,
            schema_type="array",
            required=request_body.get("required", False),
            item_type=_extract_type(_resolve_schema(items_schema, registry)),
        )]

    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))

    params: list[Parameter] = []
    for prop_name, prop_schema in properties.items():
        resolved_prop = _resolve_schema(prop_schema, registry)
        schema_type = _extract_type(resolved_prop)
        params.append(Parameter(
            name=prop_name,
            location=ParameterLocation.BODY,
            schema_type=schema_type,
            required=prop_name in required_fields,
            description=resolved_prop.get("description"),
            default=resolved_prop.get("default"),
            enum_values=list(resolved_prop.get("enum", [])),
            minimum=resolved_prop.get("minimum"),
            maximum=resolved_prop.get("maximum"),
            min_length=resolved_prop.get("minLength"),
            max_length=resolved_prop.get("maxLength"),
            pattern=resolved_prop.get("pattern"),
            format=resolved_prop.get("format"),
            item_type=_extract_type(resolved_prop.get("items", {})) if schema_type == "array" else None,
        ))

    return params


def _parse_response_schemas(
    responses: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    """
    Extract and resolve response schemas keyed by status code string.

    Returns a dict like {"200": {json_schema}, "404": {json_schema}}.
    Status codes with no declared schema are omitted.
    """
    result: dict[str, Any] = {}
    for status_code, response_obj in responses.items():
        if not isinstance(response_obj, dict):
            continue
        if "$ref" in response_obj:
            # Response $refs are uncommon in Petstore-style specs; skip for now
            continue
        content = response_obj.get("content", {})
        json_content = content.get("application/json", {})
        schema = json_content.get("schema")
        if schema:
            result[str(status_code)] = _resolve_schema(schema, registry)
    return result


def _parse_operation(
    path: str,
    method: str,
    operation: dict[str, Any],
    registry: dict[str, Any],
) -> Endpoint:
    """Parse one OpenAPI operation object into an Endpoint."""
    operation_id = operation.get("operationId") or _synthesize_operation_id(method, path)

    # Parse parameters (path, query, header)
    path_params: list[Parameter] = []
    query_params: list[Parameter] = []
    header_params: list[Parameter] = []

    for param_dict in operation.get("parameters", []):
        param = _parse_parameter(param_dict, registry)
        if param is None:
            continue
        if param.location == ParameterLocation.PATH:
            path_params.append(param)
        elif param.location == ParameterLocation.QUERY:
            query_params.append(param)
        elif param.location == ParameterLocation.HEADER:
            header_params.append(param)

    # Parse request body
    body_params = _parse_body_params(operation.get("requestBody", {}), registry)

    # Parse response schemas
    response_schemas = _parse_response_schemas(operation.get("responses", {}), registry)

    return Endpoint(
        operation_id=operation_id,
        method=method,
        path=path,
        path_params=path_params,
        query_params=query_params,
        header_params=header_params,
        body_params=body_params,
        response_schemas=response_schemas,
        tags=operation.get("tags", []),
        summary=operation.get("summary"),
    )
