"""
CLI entry point for FUZZWISE.

Subcommands:
    run     — execute a fuzzing campaign against a live API
    analyze — compute metrics from saved JSONL logs (stub for Config A)

Usage:
    fuzzwise run --spec data/specs/petstore.yaml --target http://localhost:8080/api/v3
    fuzzwise run --spec data/specs/petstore.yaml --strategy dictionary --explorer bfs
    fuzzwise analyze --logs-dir logs/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from fuzzwise.fuzzer.engine import FuzzEngine
from fuzzwise.fuzzer.explorer import BFSExplorer
from fuzzwise.models.types import CampaignConfig, FuzzResult
from fuzzwise.spec.dependencies import build_dependency_graph
from fuzzwise.spec.parser import parse_spec
from fuzzwise.strategies.dictionary import DictionaryStrategy

console = Console()


# ---------------------------------------------------------------------------
# CLI builder
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuzzwise",
        description="LLM-augmented stateful REST API fuzzer",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- run ----
    run_p = sub.add_parser("run", help="Execute a fuzzing campaign")
    run_p.add_argument("--spec", required=True, help="Path to OpenAPI YAML/JSON spec")
    run_p.add_argument("--target", default=None, help="Target API base URL")
    run_p.add_argument(
        "--strategy", choices=["dictionary", "llm"], default="dictionary",
        help="Payload generation strategy",
    )
    run_p.add_argument(
        "--explorer", choices=["bfs", "bfs_fast", "llm_guided"], default="bfs",
        help="Sequence selection strategy",
    )
    run_p.add_argument("--max-requests", type=int, default=None)
    run_p.add_argument("--time-budget", type=float, default=None, help="Seconds")
    run_p.add_argument("--max-sequence-length", type=int, default=3)
    run_p.add_argument("--seed", type=int, default=42)
    run_p.add_argument("--log-dir", default="./logs")
    run_p.add_argument("--config-label", default="A")
    run_p.add_argument("--min-confidence", type=float, default=0.5)
    run_p.add_argument(
        "--dictionaries-dir", default="./data/dictionaries",
        help="Path to fuzz dictionary JSON files",
    )
    run_p.add_argument(
        "--auth-header", action="append", dest="auth_headers", default=[],
        metavar="HEADER:VALUE",
        help="Extra HTTP header(s) sent with every request (e.g. 'Authorization:Bearer tok'). "
             "Can be repeated.",
    )
    run_p.add_argument("--verbose", action="store_true")

    # ---- analyze ----
    ana_p = sub.add_parser("analyze", help="Analyze campaign logs")
    ana_p.add_argument("--logs-dir", required=True)
    ana_p.add_argument("--output", default=None, help="Output markdown path")

    return parser


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    load_dotenv()

    # Resolve env var overrides
    target = args.target or os.getenv("TARGET_API_URL", "http://localhost:8080")
    max_requests = args.max_requests or int(os.getenv("FUZZ_MAX_REQUESTS", "500"))
    time_budget = args.time_budget or float(os.getenv("FUZZ_TIME_BUDGET_SECONDS", "300"))

    _setup_logging(args.verbose)

    # Parse --auth-header flags: "Key:Value" or "Key: Value"
    extra_headers: dict[str, str] = {}
    for h in args.auth_headers:
        if ":" not in h:
            console.print(f"[red]Invalid --auth-header '{h}' — expected 'Key:Value'[/]")
            return 1
        k, _, v = h.partition(":")
        extra_headers[k.strip()] = v.strip()

    config = CampaignConfig(
        campaign_id=str(uuid.uuid4()),
        spec_path=args.spec,
        target_base_url=target,
        strategy=args.strategy,
        explorer=args.explorer,
        max_requests=max_requests,
        time_budget_seconds=time_budget,
        max_sequence_length=args.max_sequence_length,
        seed=args.seed,
        log_dir=args.log_dir,
        min_confidence=args.min_confidence,
        config_label=args.config_label,
        extra_headers=extra_headers,
    )

    log_path = Path(args.log_dir) / f"campaign_{config.campaign_id}.jsonl"

    # ---- Parse spec ----
    console.rule("[bold blue]FUZZWISE")
    console.print(f"[cyan]Spec:[/]        {args.spec}")
    console.print(f"[cyan]Target:[/]      {target}")
    console.print(f"[cyan]Strategy:[/]    {args.strategy}")
    console.print(f"[cyan]Explorer:[/]    {args.explorer}")
    console.print(f"[cyan]Budget:[/]      {max_requests} requests / {time_budget}s")
    console.print(f"[cyan]Config label:[/ ] {args.config_label}")
    console.print()

    try:
        with console.status("Parsing spec..."):
            endpoints = parse_spec(args.spec)
        console.print(f"[green]✓[/] Parsed {len(endpoints)} endpoints")

        with console.status("Inferring dependencies..."):
            graph = build_dependency_graph(endpoints, min_confidence=args.min_confidence)
        console.print(f"[green]✓[/] Inferred {len(graph.edges)} dependency edges")

        if args.verbose:
            console.print()
            console.print(graph.summary())
            console.print()

    except Exception as exc:
        console.print(f"[red]Error during setup:[/] {exc}")
        return 1

    # ---- Build strategy and explorer ----
    if args.strategy == "dictionary":
        strategy = DictionaryStrategy(
            dictionaries_dir=args.dictionaries_dir,
            seed=args.seed,
        )
    else:
        console.print("[red]LLM strategy not yet implemented — use --strategy dictionary[/]")
        return 1

    if args.explorer == "bfs":
        explorer = BFSExplorer(max_sequence_length=args.max_sequence_length, bfs_fast=False)
    elif args.explorer == "bfs_fast":
        explorer = BFSExplorer(max_sequence_length=args.max_sequence_length, bfs_fast=True)
    else:
        console.print("[red]LLM-guided explorer not yet implemented — use --explorer bfs[/]")
        return 1

    # ---- Run campaign ----
    try:
        async def _run() -> FuzzResult:
            async with httpx.AsyncClient(timeout=10.0) as client:
                engine = FuzzEngine(
                    config=config,
                    endpoints=endpoints,
                    graph=graph,
                    strategy=strategy,
                    explorer=explorer,
                    http_client=client,
                    log_path=log_path,
                )
                return await engine.run()

        console.print(f"\n[bold]Starting campaign[/] [dim]{config.campaign_id}[/]\n")
        result = asyncio.run(_run())

    except KeyboardInterrupt:
        console.print("\n[yellow]Campaign interrupted by user.[/]")
        return 0
    except Exception as exc:
        console.print(f"[red]Campaign failed:[/] {exc}")
        logging.exception("Campaign error")
        return 1

    # ---- Print results ----
    _print_results(result)
    console.print(f"\n[dim]Log:[/] {log_path}")
    console.print(f"[dim]Result:[/] {log_path.with_suffix('.result.json')}")
    return 0


# ---------------------------------------------------------------------------
# analyze subcommand (basic for Config A)
# ---------------------------------------------------------------------------


def cmd_analyze(args: argparse.Namespace) -> int:
    logs_dir = Path(args.logs_dir)
    result_files = list(logs_dir.glob("*.result.json"))

    if not result_files:
        console.print(f"[yellow]No result files found in {logs_dir}[/]")
        return 0

    results: list[FuzzResult] = []
    for rf in sorted(result_files):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            results.append(FuzzResult(**data))
        except Exception as exc:
            console.print(f"[yellow]Skipping {rf.name}: {exc}[/]")

    if not results:
        return 0

    console.rule("[bold blue]FUZZWISE Analysis")
    table = Table(title="Campaign Comparison")
    table.add_column("Campaign", style="dim")
    table.add_column("Config")
    table.add_column("Strategy")
    table.add_column("Requests", justify="right")
    table.add_column("Coverage", justify="right")
    table.add_column("Unique 500s", justify="right")
    table.add_column("Error Types", justify="right")
    table.add_column("Schema Violations", justify="right")
    table.add_column("Max Depth", justify="right")
    table.add_column("Duration", justify="right")

    for r in results:
        coverage = f"{r.endpoints_hit}/{r.total_endpoints}"
        table.add_row(
            r.campaign_id[:8],
            r.config_label,
            r.strategy,
            str(r.total_requests),
            coverage,
            str(r.unique_500_count),
            str(r.error_type_count),
            str(r.schema_violation_count),
            str(r.max_depth_reached),
            f"{r.duration_seconds:.1f}s",
        )

    console.print(table)

    if args.output:
        _write_markdown_report(results, Path(args.output))
        console.print(f"\n[green]✓[/] Report written to [cyan]{args.output}[/]")

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # Suppress noisy httpx logs unless verbose
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _print_results(result: FuzzResult) -> None:
    console.print()
    console.rule("[bold green]Campaign Complete")

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("Key", style="cyan")
    summary.add_column("Value")
    summary.add_row("Campaign ID", result.campaign_id)
    summary.add_row("Config Label", result.config_label)
    summary.add_row("Strategy", result.strategy)
    summary.add_row("Explorer", result.explorer)
    summary.add_row("Total Requests", str(result.total_requests))
    summary.add_row("Duration", f"{result.duration_seconds:.1f}s")
    summary.add_row("Max Sequence Depth", str(result.max_depth_reached))
    summary.add_row("Sequences Explored", str(result.sequences_explored))
    console.print(summary)

    console.print()
    results_table = Table(show_header=False, box=None, padding=(0, 2))
    results_table.add_column("Metric", style="cyan")
    results_table.add_column("Value")
    coverage_pct = (
        f"{result.endpoints_hit}/{result.total_endpoints}"
        f" ({100 * result.endpoints_hit / result.total_endpoints:.0f}%)"
        if result.total_endpoints > 0 else "0/0"
    )
    results_table.add_row("Endpoint Coverage", coverage_pct)
    results_table.add_row("Unique 500 Errors", str(result.unique_500_count))
    results_table.add_row("Schema Violations", str(result.schema_violation_count))

    dist = ", ".join(
        f"{k}={v}" for k, v in sorted(result.status_code_distribution.items())
    )
    results_table.add_row("Status Distribution", dist or "—")
    console.print(results_table)

    if result.bugs:
        console.print()
        bug_table = Table(title="Bugs Found", show_lines=True)
        bug_table.add_column("Type", style="red")
        bug_table.add_column("Endpoint")
        bug_table.add_column("Status", justify="right")
        bug_table.add_column("Sequence")
        for bug in result.bugs[:20]:  # cap display at 20
            bug_table.add_row(
                bug.bug_type,
                bug.operation_id,
                str(bug.status_code),
                " → ".join(bug.full_sequence),
            )
        console.print(bug_table)


def _write_markdown_report(results: list[FuzzResult], output: Path) -> None:
    """Write a markdown report comparing all campaign results."""
    lines: list[str] = []
    lines.append("# FUZZWISE Campaign Report\n")

    # --- Summary comparison table ---
    lines.append("## Summary\n")
    lines.append("| Config | Strategy | Requests | Coverage | Unique 500s | Error Types | Schema Violations | Max Depth | Duration |")
    lines.append("|--------|----------|----------|----------|-------------|-------------|-------------------|-----------|----------|")
    for r in results:
        coverage = f"{r.endpoints_hit}/{r.total_endpoints}"
        if r.total_endpoints > 0:
            coverage += f" ({100 * r.endpoints_hit // r.total_endpoints}%)"
        lines.append(
            f"| {r.config_label} | {r.strategy} | {r.total_requests} | {coverage} "
            f"| {r.unique_500_count} | {r.error_type_count} | {r.schema_violation_count} "
            f"| {r.max_depth_reached} | {r.duration_seconds:.1f}s |"
        )
    lines.append("")

    # --- Per-campaign detail ---
    for r in results:
        lines.append(f"## Campaign: {r.config_label} (`{r.campaign_id[:8]}`)\n")
        lines.append(f"- **Spec:** `{r.spec_path}`")
        lines.append(f"- **Strategy:** {r.strategy}  **Explorer:** {r.explorer}")
        lines.append(f"- **Seed:** {r.seed}  **Max sequence length:** {r.max_sequence_length}")
        lines.append(f"- **Requests:** {r.total_requests}  **Duration:** {r.duration_seconds:.1f}s")
        lines.append(f"- **Sequences explored:** {r.sequences_explored}  **Max depth:** {r.max_depth_reached}")
        lines.append("")

        dist = "  ".join(f"`{k}`: {v}" for k, v in sorted(r.status_code_distribution.items()))
        lines.append(f"**Status distribution:** {dist or '—'}\n")

        if r.bugs:
            lines.append("### Bugs Found\n")
            lines.append("| Type | Endpoint | Status | Sequence |")
            lines.append("|------|----------|--------|----------|")
            for bug in r.bugs:
                seq = " → ".join(bug.full_sequence)
                lines.append(f"| {bug.bug_type} | `{bug.operation_id}` | {bug.status_code} | {seq} |")
            lines.append("")
        else:
            lines.append("*No bugs found.*\n")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        sys.exit(cmd_run(args))
    elif args.command == "analyze":
        sys.exit(cmd_analyze(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
