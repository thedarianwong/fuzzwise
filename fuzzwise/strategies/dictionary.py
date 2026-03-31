"""
RESTler-faithful static dictionary payload strategy (Config A).

Implements the exact dictionary values from the RESTler ICSE 2019 paper (Section V.A):
    string:  "sampleString", ""
    integer: 0, 1
    boolean: "true", "false"

Values are cycled deterministically per (operation_id, param_name) key using an
instance-level RNG, so:
    - Two runs with the same seed produce identical sequences.
    - Cycling petId does not advance the index for name (independent per key).
    - If param.enum_values is non-empty, cycles through those instead of the
      dictionary (enum-constrained params always send valid values — bugs come
      from stateful chains, not rejected enum values which add noise).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fuzzwise.models.types import Endpoint, Parameter
from fuzzwise.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class DictionaryStrategy(BaseStrategy):
    """
    Config A payload generation: RESTler-faithful static dictionary.

    Loads dictionary files from a directory on startup. Falls back to
    hard-coded RESTler values if the files are missing.
    """

    # RESTler paper (ICSE 2019) Section V.A — exact values
    _FALLBACK_STRINGS = ["sampleString", ""]
    _FALLBACK_INTEGERS = [0, 1]
    _FALLBACK_BOOLEANS = ["true", "false"]
    _FALLBACK_NUMBERS = [0.0, 1.0]

    def __init__(self, dictionaries_dir: str | Path, seed: int = 42) -> None:
        super().__init__(seed)
        d = Path(dictionaries_dir)
        self._strings = self._load(d / "strings.json", self._FALLBACK_STRINGS)
        self._integers = self._load(d / "integers.json", self._FALLBACK_INTEGERS)
        self._booleans = self._load(d / "booleans.json", self._FALLBACK_BOOLEANS)
        self._numbers = self._load(d / "numbers.json", self._FALLBACK_NUMBERS)
        # Per-(operation_id, param_name) cycling index
        self._indices: dict[str, int] = {}

    @staticmethod
    def _load(path: Path, fallback: list[Any]) -> list[Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("Could not load dictionary %s (%s) — using fallback", path, exc)
        return fallback

    def generate(self, endpoint: Endpoint, param: Parameter) -> Any:
        """
        Return the next fuzz value for this (endpoint, param) pair.

        Cycles deterministically through the appropriate dictionary list.
        If param has enum_values, cycles through those instead.
        """
        key = f"{endpoint.operation_id}::{param.name}"

        if param.enum_values:
            candidates = param.enum_values
        else:
            candidates = self._get_candidates(param.schema_type)

        if not candidates:
            return None

        idx = self._indices.get(key, 0)
        value = candidates[idx % len(candidates)]
        self._indices[key] = idx + 1
        return value

    def _get_candidates(self, schema_type: str) -> list[Any]:
        mapping = {
            "string": self._strings,
            "integer": self._integers,
            "number": self._numbers,
            "boolean": self._booleans,
            "array": [[]],
            "object": [{}],
        }
        return mapping.get(schema_type, self._strings)

    def reset(self, seed: int | None = None) -> None:
        """Reset cycling indices and RNG for a fresh campaign."""
        super().reset(seed)
        self._indices = {}
