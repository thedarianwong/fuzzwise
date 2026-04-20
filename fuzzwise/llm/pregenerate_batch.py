"""
Batch pre-generation of LLM payloads (Config B variant 2).

One LLM call per (endpoint, param) pair asks for all N values at once.
This is ~20x faster than the iterative approach (pregenerate.py) which makes
one call per value.

Comparison:
    pregenerate.py       — 1 call/value  → 1,020 calls, ~6h on slow hardware
    pregenerate_batch.py — 1 call/param  →    51 calls, ~4 min on slow hardware

Output format is identical to pregenerate.py so LLMPregeneratedStrategy
loads either file without changes.

Usage:
    python -m fuzzwise.llm.pregenerate_batch \
        --spec data/specs/petstore.yaml \
        --model qwen2.5:7b \
        --num-payloads 20 \
        --output-dir data/llm_payloads
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from fuzzwise.llm.client import OllamaClient
from fuzzwise.llm.prompts import render_payload_prompt
from fuzzwise.models.types import Endpoint, Parameter
from fuzzwise.spec.parser import parse_spec

console = Console()
logger = logging.getLogger(__name__)

# Need more tokens than the default 150 to fit 20 values in one response
_BATCH_MAX_TOKENS = 800


def _sanitize_llm_json(text: str) -> str:
    """
    Fix common non-JSON patterns that LLMs emit when asked for arrays.

    - "a".repeat(N)  → "aaa..." (N chars)
    - "a" * N        → "aaa..." (N chars)
    - Bare Infinity / -Infinity → removed
    """
    # "char".repeat(N) — JS pattern
    def expand_repeat(m: re.Match) -> str:
        char = m.group(1)
        try:
            n = min(int(m.group(2)), 512)  # cap at 512 chars
        except ValueError:
            return f'"{char}"'
        return f'"{char * n}"'

    text = re.sub(r'"([^"]*)"\.?repeat\((\d+)\)', expand_repeat, text)

    # "char" * N — Python pattern
    def expand_multiply(m: re.Match) -> str:
        char = m.group(1)
        try:
            n = min(int(m.group(2)), 512)
        except ValueError:
            return f'"{char}"'
        return f'"{char * n}"'

    text = re.sub(r'"([^"]*?)"\s*\*\s*(\d+)', expand_multiply, text)

    # Bare Infinity / -Infinity (not quoted)
    text = re.sub(r'(?<!["\w])-?Infinity(?!["\w])', 'null', text)

    return text


def _parse_json_array(text: str) -> list[Any] | None:
    """
    Extract a JSON array from LLM output.

    Stage 1: sanitize common non-JSON expressions, then direct parse.
    Stage 2: extract first [...] block via regex (handles stray prose/markdown).
    Returns None if both stages fail.
    """
    stripped = _sanitize_llm_json(text.strip())

    try:
        result = json.loads(stripped)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    match = re.search(r'\[.*\]', stripped, re.DOTALL)
    if match:
        candidate = _sanitize_llm_json(match.group())
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None


def _fallback_values(schema_type: str) -> list[Any]:
    """RESTler-faithful fallback values when LLM parse fails."""
    defaults: dict[str, list[Any]] = {
        "string":  ["sampleString", ""],
        "integer": [0, 1],
        "number":  [0.0, 1.0],
        "boolean": ["true", "false"],
        "array":   [[]],
        "object":  [{}],
    }
    return defaults.get(schema_type, defaults["string"])


def generate_batch(
    endpoints: list[Endpoint],
    client: OllamaClient,
    n: int = 20,
) -> tuple[dict[str, list[Any]], dict]:
    """
    Generate n values per (endpoint, param) pair using one LLM call each.

    Returns:
        payloads  — dict mapping "op_id::param_name" → list of values
        stats     — generation statistics
    """
    param_pairs = [
        (ep, param)
        for ep in endpoints
        for param in ep.all_params
    ]
    total = len(param_pairs)

    payloads: dict[str, list[Any]] = {}
    successes = 0
    failures = 0
    fallbacks = 0
    t_start = time.monotonic()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[cyan]Batch generating {total} params × {n} values...",
            total=total,
        )

        for ep, param in param_pairs:
            key = f"{ep.operation_id}::{param.name}"

            prompt = render_payload_prompt(
                method=ep.method,
                path=ep.path,
                param_name=param.name,
                schema_type=param.schema_type,
                n=n,
                summary=ep.summary,
                fmt=param.format,
                description=param.description,
                minimum=param.minimum,
                maximum=param.maximum,
                min_length=param.min_length,
                max_length=param.max_length,
                enum_values=list(param.enum_values) if param.enum_values else None,
            )

            values: list[Any] | None = None
            try:
                raw = client.generate(prompt, max_tokens=_BATCH_MAX_TOKENS)
                values = _parse_json_array(raw)
                if values is not None:
                    successes += 1
                    logger.debug("%-40s → %d values", key, len(values))
                else:
                    logger.warning("Parse failed for %s — raw: %.120s", key, raw)
                    failures += 1
            except Exception as exc:
                logger.warning("LLM call failed for %s: %s", key, exc)
                failures += 1

            if values is None:
                values = _fallback_values(param.schema_type)
                fallbacks += 1

            payloads[key] = values
            progress.update(task, advance=1)

    elapsed = time.monotonic() - t_start
    stats = {
        "total_params": total,
        "successes": successes,
        "failures": failures,
        "fallbacks": fallbacks,
        "success_rate": round(100 * successes / total, 1) if total else 0,
        "elapsed_seconds": round(elapsed, 1),
        "llm_calls": successes + failures,  # one call per param
    }
    return payloads, stats


def save_payloads(
    payloads: dict[str, list[Any]],
    stats: dict,
    model: str,
    spec_path: str,
    n: int,
    output_dir: Path,
) -> Path:
    """Save payloads in the same JSON format as pregenerate.py."""
    output_dir.mkdir(parents=True, exist_ok=True)
    model_slug = model.replace(":", "_").replace("/", "_")
    output_path = output_dir / f"llm_payloads_{model_slug}_batch.json"

    metadata = {
        "model": model,
        "generation_method": "batch",   # distinguishes from iterative
        "num_payloads_per_param": n,
        "spec": spec_path,
        "total_parameters": len(payloads),
        "total_payloads": sum(len(v) for v in payloads.values()),
        **stats,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"metadata": metadata, "payloads": payloads}, f, indent=2, default=str)

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch pre-generate LLM payloads (1 LLM call per parameter)"
    )
    parser.add_argument("--spec", required=True, help="OpenAPI spec file")
    parser.add_argument("--model", default="qwen2.5:7b", help="Ollama model")
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--num-payloads", type=int, default=20, help="Values per parameter")
    parser.add_argument("--output-dir", default="./data/llm_payloads")
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    client = OllamaClient(
        host=args.ollama_host,
        model=args.model,
        temperature=args.temperature,
        timeout_seconds=60.0,   # longer timeout: model generating 20 values at once
    )
    if not client.is_available:
        console.print("[red]Ollama not available. Run: ollama serve[/]")
        sys.exit(1)

    console.rule("[bold cyan]FUZZWISE — Batch Pre-generation")
    console.print(f"[cyan]Spec:[/]   {args.spec}")
    console.print(f"[cyan]Model:[/]  {args.model}")
    console.print(f"[cyan]Values:[/] {args.num_payloads} per parameter")
    console.print(f"[cyan]Method:[/] batch (1 LLM call per parameter)\n")

    endpoints = parse_spec(args.spec)
    total_params = sum(len(ep.all_params) for ep in endpoints)
    console.print(f"[green]✓[/] Parsed {len(endpoints)} endpoints, {total_params} parameters")
    console.print(f"[dim]Estimated LLM calls: {total_params}[/]")
    console.print()

    payloads, stats = generate_batch(endpoints, client, n=args.num_payloads)

    output_path = save_payloads(
        payloads=payloads,
        stats=stats,
        model=args.model,
        spec_path=args.spec,
        n=args.num_payloads,
        output_dir=Path(args.output_dir),
    )

    console.print(f"\n[green]✓[/] Saved to [cyan]{output_path}[/]")
    console.print(f"  Parameters:   {stats['total_params']}")
    console.print(f"  LLM calls:    {stats['llm_calls']}")
    console.print(f"  Successes:    {stats['successes']} ({stats['success_rate']}%)")
    console.print(f"  Fallbacks:    {stats['fallbacks']}")
    console.print(f"  Time:         {stats['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
