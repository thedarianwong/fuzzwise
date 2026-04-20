"""
Prompt templates for LLM-based payload generation (Config B) and
sequence reasoning (Config C — stubs only).

All templates are module-level string constants with {placeholder} slots.
No logic here — rendering happens in pregenerate_batch.py and strategies/llm.py.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Config B: per-field adversarial value generation
# ---------------------------------------------------------------------------

PAYLOAD_GENERATION_PROMPT = """\
You are a REST API security testing assistant generating adversarial test values.

Endpoint: {method} {path}
{summary_line}Field: {param_name}
Type: {schema_type}
{format_line}{description_line}{constraints_line}{examples_line}{response_values_line}
Generate {n} test values that include:
- Boundary values (at/just beyond declared min/max/length constraints)
- Semantically meaningful edge cases for a field named "{param_name}"
- Values that bypass weak validation (empty string, whitespace-only, null-like)
- Values that may cause server-side errors (special chars, unicode, very long strings)
- Adversarial inputs relevant to this field type (injection, traversal, format strings)

Respond with ONLY a valid JSON array of {n} string or number literals.
Rules:
- Every value must be a valid JSON literal (string, number, true, false, null)
- Do NOT use any programming expressions like "a".repeat(N), "a"*100, or str*N
- For long strings, write the actual characters — no shortcuts
- The entire response must be parseable by JSON.parse()
Example output: ["value1", "value2", "value3"]"""


def render_payload_prompt(
    method: str,
    path: str,
    param_name: str,
    schema_type: str,
    n: int,
    summary: str | None = None,
    fmt: str | None = None,
    description: str | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    enum_values: list | None = None,
    spec_examples: list | None = None,
    response_values: list | None = None,
) -> str:
    """Render PAYLOAD_GENERATION_PROMPT with conditional line inclusion."""

    summary_line = f"Summary: {summary}\n" if summary else ""
    format_line = f"Format: {fmt}\n" if fmt else ""
    description_line = f"Description: {description}\n" if description else ""

    constraints: list[str] = []
    if enum_values:
        constraints.append(f"Allowed values: {enum_values}")
    if minimum is not None:
        constraints.append(f"min={minimum}")
    if maximum is not None:
        constraints.append(f"max={maximum}")
    if min_length is not None:
        constraints.append(f"minLength={min_length}")
    if max_length is not None:
        constraints.append(f"maxLength={max_length}")
    constraints_line = f"Constraints: {', '.join(constraints)}\n" if constraints else ""

    examples_line = f"Spec examples: {spec_examples}\n" if spec_examples else ""
    response_values_line = (
        f"Recent response values (values the API actually returned): {response_values[:3]}\n"
        if response_values else ""
    )

    return PAYLOAD_GENERATION_PROMPT.format(
        method=method,
        path=path,
        summary_line=summary_line,
        param_name=param_name,
        schema_type=schema_type,
        format_line=format_line,
        description_line=description_line,
        constraints_line=constraints_line,
        examples_line=examples_line,
        response_values_line=response_values_line,
        n=n,
    )


# ---------------------------------------------------------------------------
# Config C stubs
# ---------------------------------------------------------------------------

RESPONSE_ANALYSIS_PROMPT = """\
You are analyzing HTTP responses from a REST API fuzzing campaign.

Response status: {status_code}
Endpoint: {method} {path}
Response body (truncated): {body_snippet}

Classify this response:
- "interesting": unexpected error, information leak, or anomalous behaviour
- "normal": expected success or documented error

Respond with ONLY one word: interesting or normal."""

SEQUENCE_REASONING_PROMPT = """\
You are guiding a REST API fuzzing campaign. Choose the next sequence to explore.

Current state:
- Sequences tested so far: {sequences_tested}
- Endpoints with 2xx responses: {covered_endpoints}
- Bugs found: {bug_count}
- Resource pool (available IDs): {resource_pool_summary}

Valid next sequences to extend:
{candidate_sequences}

Pick the sequence most likely to uncover new bugs or reach untested code paths.
Respond with ONLY the index number of your choice (0-based). No explanation."""
