# fuzzwise/strategies/llm.py
"""
LLM-based payload generation strategy (Config B).

Uses a locally-hosted LLM via Ollama to generate adversarial test values.
This implements Config B from the research: LLM-generated payloads + BFS exploration.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fuzzwise.llm.client import OllamaClient
from fuzzwise.models.types import Endpoint, Parameter
from fuzzwise.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class LLMStrategy(BaseStrategy):
    """Config B: LLM-generated adversarial payloads."""

    def __init__(
        self,
        dictionaries_dir: str | Path,
        seed: int = 42,
        model: str = "qwen2.5:7b",
        ollama_host: str = "http://localhost:11434",
        temperature: float = 0.8,
        top_p: float = 0.95,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        fallback_to_dictionary: bool = True,
    ) -> None:
        super().__init__(seed)

        # Initialize Ollama client
        self._client = OllamaClient(
            host=ollama_host,
            model=model,
            temperature=temperature,
            top_p=top_p,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

        # Load fallback dictionary
        self._dictionary = None
        self._fallback_to_dictionary = fallback_to_dictionary
        if fallback_to_dictionary:
            from fuzzwise.strategies.dictionary import DictionaryStrategy
            self._dictionary = DictionaryStrategy(dictionaries_dir, seed)

        # Per-parameter state
        self._indices: dict[str, int] = {}
        self._cached_values: dict[str, list[Any]] = {}

        # Metrics
        self.metrics = {
            "llm_calls": 0,
            "llm_failures": 0,
            "llm_successes": 0,
            "fallback_uses": 0,
            "cache_hits": 0,
            "unique_values_generated": 0,
        }

        if not self._client.is_available:
            logger.warning(
                f"Ollama not available at {ollama_host}. "
                f"LLMStrategy will fall back to dictionary. "
                f"Run: ollama serve && ollama pull {model}"
            )

    def generate(self, endpoint: Endpoint, param: Parameter) -> Any:
        """Generate a test value for the parameter."""
        key = f"{endpoint.operation_id}::{param.name}"
        idx = self._indices.get(key, 0)

        # Check cache
        if key in self._cached_values:
            cached = self._cached_values[key]
            if idx < len(cached):
                value = cached[idx]
                self._indices[key] = idx + 1
                self.metrics["cache_hits"] += 1
                return value

        # Generate new value
        value = self._generate_with_llm(endpoint, param)

        # Cache it
        if key not in self._cached_values:
            self._cached_values[key] = []
        self._cached_values[key].append(value)
        self._indices[key] = idx + 1
        self.metrics["unique_values_generated"] += 1

        return value

    def _generate_with_llm(self, endpoint: Endpoint, param: Parameter) -> Any:
        """Generate a value using LLM, with fallback."""
        # Enums: use directly
        if param.enum_values:
            return self._rng.choice(param.enum_values)

        # No LLM available? fallback
        if not self._client.is_available:
            self.metrics["fallback_uses"] += 1
            return self._fallback_generate(endpoint, param)

        # Build prompt and call LLM
        prompt = self._build_prompt(endpoint, param)

        try:
            response = self._client.generate(prompt, max_tokens=150)
            self.metrics["llm_calls"] += 1
            self.metrics["llm_successes"] += 1
            return self._parse_response(response, param)
        except Exception as e:
            self.metrics["llm_failures"] += 1
            self.metrics["fallback_uses"] += 1
            logger.debug(f"LLM failed for {param.name}: {e}")
            return self._fallback_generate(endpoint, param)

    def _build_prompt(self, endpoint: Endpoint, param: Parameter) -> str:
        """Build context-aware prompt for the LLM."""
        schema_desc = self._describe_schema(param.model_json_schema()) if param.model_json_schema else "no constraints"
        param_type = param.schema_type if param.schema_type else "string"
        location = param.location.value if hasattr(param.location, 'value') else str(param.location)

        prompt = f"""You are an API fuzzing tool. Generate a SINGLE test value for this API parameter.

API Endpoint: {endpoint.method} {endpoint.path}
Parameter Name: {param.name}
Parameter Location: {location}
Required: {param.required}
Parameter Type: {param_type}
Schema Constraints: {schema_desc}

Generate a test value that is interesting from a testing perspective (edge cases, boundaries, invalid values, security-relevant).

Return ONLY the value, nothing else. No explanations, no markdown."""

        # Add specific guidance
        param_lower = param.name.lower()
        if param_lower in ["id", "petid", "userid"]:
            prompt += "\n\nConsider: 0, -1, 999999999"
        elif "name" in param_lower:
            prompt += "\n\nConsider: empty string, very long string, ' OR '1'='1"
        elif "email" in param_lower:
            prompt += "\n\nConsider: invalid formats, test@example.com<script>alert(1)</script>"

        return prompt

    def _describe_schema(self, schema: dict) -> str:
        """Convert JSON schema to readable description."""
        if not schema:
            return "no constraints"

        schema_type = schema.get("type", "unknown")

        if schema_type == "string":
            parts = []
            if schema.get("format"):
                parts.append(f"format: {schema['format']}")
            if schema.get("enum"):
                parts.append(f"enum: {schema['enum']}")
            if schema.get("pattern"):
                parts.append(f"pattern: {schema['pattern']}")
            if schema.get("minLength"):
                parts.append(f"minLength: {schema['minLength']}")
            if schema.get("maxLength"):
                parts.append(f"maxLength: {schema['maxLength']}")
            return f"string ({', '.join(parts)})" if parts else "string"

        elif schema_type in ["integer", "number"]:
            parts = []
            if schema.get("minimum") is not None:
                parts.append(f"min: {schema['minimum']}")
            if schema.get("maximum") is not None:
                parts.append(f"max: {schema['maximum']}")
            if schema.get("enum"):
                parts.append(f"enum: {schema['enum']}")
            return f"{schema_type} ({', '.join(parts)})" if parts else schema_type

        elif schema_type == "array":
            items = schema.get("items", {})
            return f"array of {self._describe_schema(items) if items else 'any'}"
        elif schema_type == "object":
            props = list(schema.get("properties", {}).keys())
            required = schema.get("required", [])
            if props:
                return f"object with fields: {props[:3]}, required: {required}"
            return "object"
        else:
            return schema_type

    def _parse_response(self, response: str, param: Parameter) -> Any:
        """Parse LLM response into appropriate Python type."""
        if not response:
            return self._fallback_generate(None, param)

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

    def _fallback_generate(self, endpoint: Endpoint | None, param: Parameter) -> Any:
        """Fallback to dictionary or simple defaults."""
        if self._dictionary and endpoint:
            try:
                return self._dictionary.generate(endpoint, param)
            except Exception:
                pass

        # Ultimate fallback
        param_type = param.schema_type if param.schema_type else "string"
        if param_type in ["integer", "int"]:
            return self._rng.choice([0, 1, -1])
        elif param_type == "number":
            return self._rng.choice([0.0, 1.0, -1.0])
        elif param_type == "boolean":
            return self._rng.choice([True, False])
        else:
            return self._rng.choice(["", "test"])

    def reset(self, seed: int | None = None) -> None:
        """Reset internal state."""
        super().reset(seed)
        self._indices = {}
        self._cached_values = {}
        if self._dictionary:
            self._dictionary.reset(seed)
        self.metrics = {k: 0 for k in self.metrics}

    def get_metrics(self) -> dict:
        """Return metrics for research."""
        return {
            **self.metrics,
            "model": self._client.model,
            "ollama_available": self._client.is_available,
        }