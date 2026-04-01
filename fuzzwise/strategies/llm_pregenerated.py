"""
LLM strategy using pre-generated payloads (Config B - optimized).

This strategy loads pre-generated LLM payloads from a JSON file
and uses them during fuzzing. Same speed as dictionary strategy,
but with LLM-generated values.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fuzzwise.models.types import Endpoint, Parameter
from fuzzwise.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class LLMPregeneratedStrategy(BaseStrategy):
    """
    Config B: LLM-generated payloads from pre-generated corpus.
    
    Fair comparison to Config A - same speed, but payloads from LLM.
    """
    
    def __init__(
        self,
        payloads_path: str | Path,
        seed: int = 42,
    ) -> None:
        """Initialize with pre-generated LLM payloads."""
        super().__init__(seed)
        
        self.payloads_path = Path(payloads_path)
        self.payloads: dict[str, list[Any]] = {}
        self._indices: dict[str, int] = {}
        self.metadata: dict = {}
        
        self._load_payloads()
        
    def _load_payloads(self) -> None:
        """Load pre-generated payloads from JSON file."""
        if not self.payloads_path.exists():
            raise FileNotFoundError(f"Payloads file not found: {self.payloads_path}")
        
        with open(self.payloads_path) as f:
            data = json.load(f)
        
        self.metadata = data.get("metadata", {})
        self.payloads = data.get("payloads", {})
        
        logger.info(f"Loaded {len(self.payloads)} parameter payload sets")
        total_payloads = sum(len(v) for v in self.payloads.values())
        logger.info(f"Total payloads: {total_payloads:,}")
        
        if "model" in self.metadata:
            logger.info(f"Model used: {self.metadata['model']}")
    
    def generate(self, endpoint: Endpoint, param: Parameter) -> Any:
        """
        Return next payload for this parameter.
        
        Cycles deterministically through pre-generated LLM payloads.
        """
        key = f"{endpoint.operation_id}::{param.name}"
        
        # Get payloads for this parameter
        payloads = self.payloads.get(key, [])
        
        if not payloads:
            # No pre-generated payloads - fallback
            logger.debug(f"No payloads for {key}, using fallback")
            return self._fallback_generate(param)
        
        # Cycle through payloads deterministically
        idx = self._indices.get(key, 0)
        value = payloads[idx % len(payloads)]
        self._indices[key] = idx + 1
        
        return value
    
    def _fallback_generate(self, param: Parameter) -> Any:
        """Fallback when no payloads available."""
        param_type = param.schema_type if param.schema_type else "string"
        
        if param_type in ["integer", "int"]:
            return self._rng.choice([0, 1, -1])
        elif param_type == "number":
            return self._rng.choice([0.0, 1.0, -1.0])
        elif param_type == "boolean":
            return self._rng.choice([True, False])
        elif param_type == "array":
            return []
        elif param_type == "object":
            return {}
        else:
            return self._rng.choice(["", "test"])
    
    def reset(self, seed: int | None = None) -> None:
        """Reset cycling indices."""
        super().reset(seed)
        self._indices = {}
    
    def get_stats(self) -> dict:
        """Return statistics about loaded payloads."""
        return {
            "total_parameters": len(self.payloads),
            "total_payloads": sum(len(v) for v in self.payloads.values()),
            "payloads_file": str(self.payloads_path),
            "metadata": self.metadata,
        }