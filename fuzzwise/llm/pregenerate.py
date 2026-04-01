"""
Pre-generate LLM payloads for Config B.

This module generates a corpus of LLM-generated payloads for all parameters
in an OpenAPI spec. The payloads are saved to a JSON file that can be used
by LLMPregeneratedStrategy during fuzzing.

Can be run as a script:
    python -m fuzzwise.llm.pregenerate \
        --spec data/specs/petstore.yaml \
        --model qwen2.5:7b \
        --num-payloads 20 \
        --output-dir data/llm_payloads
"""

import argparse
import json
import logging
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
from fuzzwise.spec.parser import parse_spec
from fuzzwise.models.types import Endpoint, Parameter

console = Console()
logger = logging.getLogger(__name__)


class LLMPayloadGenerator:
    """Generate LLM payloads for all parameters in a spec."""
    
    def __init__(
        self,
        spec_path: Path,
        model: str = "qwen2.5:7b",
        ollama_host: str = "http://localhost:11434",
        temperature: float = 0.8,
        num_payloads_per_param: int = 20,
        output_dir: Path = Path("./data/llm_payloads"),
        delay_between_calls: float = 0.5,
    ):
        self.spec_path = spec_path
        self.model = model
        self.num_payloads = num_payloads_per_param
        self.output_dir = output_dir
        self.delay = delay_between_calls
        
        self.client = OllamaClient(
            host=ollama_host,
            model=model,
            temperature=temperature,
            timeout_seconds=30.0,
        )
        
        self.endpoints: list[Endpoint] = []
        self.payloads: dict[str, list[Any]] = {}
        
        # Track stats
        self.successes = 0
        self.failures = 0
        
    def load_spec(self) -> None:
        """Load and parse the OpenAPI spec."""
        console.print(f"[cyan]Loading spec:[/] {self.spec_path}")
        self.endpoints = parse_spec(str(self.spec_path))
        console.print(f"[green]✓[/] Loaded {len(self.endpoints)} endpoints")
        
        total_params = sum(len(e.all_params) for e in self.endpoints)
        console.print(f"[cyan]Total parameters:[/] {total_params}")
        console.print(f"[cyan]Total payloads to generate:[/] {total_params * self.num_payloads:,}")
        
    def generate_payloads(self) -> None:
        """Generate payloads for all parameters."""
        console.print(f"\n[bold yellow]Generating payloads...[/]")
        console.print(f"[dim]Model: {self.model}[/]")
        console.print(f"[dim]Payloads per parameter: {self.num_payloads}[/]")
        console.print()
        
        # Count total work
        total_work = 0
        param_list = []
        for endpoint in self.endpoints:
            for param in endpoint.all_params:
                param_list.append((endpoint, param))
                total_work += self.num_payloads
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(
                f"[cyan]Generating {total_work:,} payloads...",
                total=total_work
            )
            
            for endpoint, param in param_list:
                param_key = f"{endpoint.operation_id}::{param.name}"
                self.payloads[param_key] = []
                
                console.print(f"\n[dim]Generating for {param.name} ({endpoint.operation_id})[/]")
                
                for i in range(self.num_payloads):
                    # Cycle through prompt types for variety
                    prompt_type = i % 3  # 0: valid, 1: edge, 2: adversarial
                    
                    payload = self._generate_single_payload(
                        endpoint, param, i, prompt_type
                    )
                    
                    if payload is not None:
                        self.payloads[param_key].append(payload)
                        self.successes += 1
                    else:
                        self.failures += 1
                    
                    progress.update(task, advance=1)
                    time.sleep(self.delay)
                
                console.print(
                    f"  [green]✓[/] Generated {len(self.payloads[param_key])}/{self.num_payloads}"
                )
    
    def _generate_single_payload(
        self,
        endpoint: Endpoint,
        param: Parameter,
        index: int,
        prompt_type: int,
    ) -> Any:
        """Generate a single payload for a parameter."""
        if prompt_type == 0:
            prompt = self._build_valid_prompt(endpoint, param)
        elif prompt_type == 1:
            prompt = self._build_edge_prompt(endpoint, param)
        else:
            prompt = self._build_adversarial_prompt(endpoint, param)
        
        try:
            response = self.client.generate(prompt, max_tokens=100)
            return self._parse_response(response, param)
        except Exception as e:
            logger.debug(f"Failed to generate for {param.name}: {e}")
            return None
    
    def _build_valid_prompt(self, endpoint: Endpoint, param: Parameter) -> str:
        """Prompt for valid values."""
        param_type = param.schema_type if param.schema_type else "string"
        location = param.location.value if hasattr(param.location, 'value') else str(param.location)
        
        return f"""Generate a VALID test value for this API parameter.

Endpoint: {endpoint.method} {endpoint.path}
Parameter: {param.name} ({location})
Type: {param_type}
Required: {param.required}

Generate a simple, valid value that will pass basic validation.
Return ONLY the value, no explanation."""
    
    def _build_edge_prompt(self, endpoint: Endpoint, param: Parameter) -> str:
        """Prompt for edge cases."""
        param_type = param.schema_type if param.schema_type else "string"
        location = param.location.value if hasattr(param.location, 'value') else str(param.location)
        
        return f"""Generate an EDGE CASE test value for this API parameter.

Endpoint: {endpoint.method} {endpoint.path}
Parameter: {param.name} ({location})
Type: {param_type}

Generate a value that tests boundaries, limits, or edge cases.
Examples: for integers use 0, -1, max+1; for strings use empty, very long, special chars.
Return ONLY the value, no explanation."""
    
    def _build_adversarial_prompt(self, endpoint: Endpoint, param: Parameter) -> str:
        """Prompt for adversarial/security values."""
        param_type = param.schema_type if param.schema_type else "string"
        location = param.location.value if hasattr(param.location, 'value') else str(param.location)
        
        return f"""Generate an ADVERSARIAL test value for this API parameter.

Endpoint: {endpoint.method} {endpoint.path}
Parameter: {param.name} ({location})
Type: {param_type}

Generate a value that tests for security vulnerabilities:
- SQL injection: ' OR '1'='1
- XSS: <script>alert(1)</script>
- Path traversal: ../../../etc/passwd
- Command injection: ; rm -rf /

Return ONLY the value, no explanation."""
    
    def _parse_response(self, response: str, param: Parameter) -> Any:
        """Parse LLM response into appropriate Python type."""
        if not response:
            return None
        
        # Try JSON
        if response.startswith(('{', '[')):
            try:
                return json.loads(response)
            except json.JSONDecodeError:
                pass
        
        # Remove quotes
        cleaned = response.strip()
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        elif cleaned.startswith("'") and cleaned.endswith("'"):
            cleaned = cleaned[1:-1]
        
        # Type conversion
        expected_type = param.schema_type if param.schema_type else "string"
        
        try:
            if expected_type in ["integer", "int"]:
                return int(float(cleaned))
            elif expected_type == "number":
                return float(cleaned)
            elif expected_type == "boolean":
                return cleaned.lower() in ["true", "1", "yes"]
            else:
                return cleaned
        except (ValueError, TypeError):
            return cleaned
    
    def save(self) -> Path:
        """Save generated payloads to JSON file."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename
        model_slug = self.model.replace(':', '_').replace('/', '_')
        filename = f"llm_payloads_{model_slug}.json"
        output_path = self.output_dir / filename
        
        # Prepare metadata
        metadata = {
            "model": self.model,
            "num_payloads_per_param": self.num_payloads,
            "spec": str(self.spec_path),
            "total_parameters": len(self.payloads),
            "total_payloads": sum(len(v) for v in self.payloads.values()),
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.successes / (self.successes + self.failures) * 100,
        }
        
        # Prepare output
        output_data = {
            "metadata": metadata,
            "payloads": self.payloads,
        }
        
        # Save
        with open(output_path, 'w') as f:
            json.dump(output_data, f, indent=2, default=str)
        
        console.print(f"\n[green]✓[/] Saved payloads to {output_path}")
        console.print(f"  Total parameters: {metadata['total_parameters']}")
        console.print(f"  Total payloads: {metadata['total_payloads']:,}")
        console.print(f"  Success rate: {metadata['success_rate']:.1f}%")
        
        return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Pre-generate LLM payloads for fuzzing"
    )
    parser.add_argument("--spec", required=True, help="OpenAPI spec file")
    parser.add_argument("--model", default="qwen2.5:7b", help="Ollama model")
    parser.add_argument("--ollama-host", default="http://localhost:11434", help="Ollama host")
    parser.add_argument("--num-payloads", type=int, default=20, help="Payloads per parameter")
    parser.add_argument("--output-dir", default="./data/llm_payloads", help="Output directory")
    parser.add_argument("--temperature", type=float, default=0.8, help="LLM temperature")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between calls (seconds)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    
    # Check Ollama availability
    client = OllamaClient(
        host=args.ollama_host,
        model=args.model,
        timeout_seconds=5,
    )
    if not client.is_available:
        console.print("[red]Error: Ollama not available. Run: ollama serve[/]")
        sys.exit(1)
    
    generator = LLMPayloadGenerator(
        spec_path=Path(args.spec),
        model=args.model,
        ollama_host=args.ollama_host,
        temperature=args.temperature,
        num_payloads_per_param=args.num_payloads,
        output_dir=Path(args.output_dir),
        delay_between_calls=args.delay,
    )
    
    generator.load_spec()
    generator.generate_payloads()
    generator.save()


if __name__ == "__main__":
    main()