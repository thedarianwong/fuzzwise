"""
Abstract base classes for payload generation strategies and endpoint explorers.

Both interfaces are injected into the fuzzing engine as constructor arguments.
The engine never imports concrete implementations directly — only these base classes.
This keeps the engine strategy-agnostic and prevents circular imports.

Import graph: base.py imports only from fuzzwise.models.types.
              Concrete strategies import from base.py.
              engine.py imports from base.py only.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from fuzzwise.models.types import Endpoint, Parameter

if TYPE_CHECKING:
    from fuzzwise.spec.dependencies import DependencyGraph
    from fuzzwise.fuzzer.state import FuzzState


class BaseStrategy(ABC):
    """
    Abstract payload generation strategy.

    generate() is called by the engine for every parameter of every request.
    Concrete implementations may use a static dictionary (Config A) or
    call an LLM (Config B/C).

    Uses an instance-level random.Random(seed) — never the global random module —
    so that two strategies with different seeds can coexist in the same process
    without interfering with each other.
    """

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed
        self._rng = random.Random(seed)

    @abstractmethod
    def generate(self, endpoint: Endpoint, param: Parameter) -> Any:
        """
        Generate one fuzz value for param in the context of endpoint.

        Args:
            endpoint: The endpoint being fuzzed (provides operation_id and context).
            param:    The specific parameter to generate a value for.

        Returns:
            A value appropriate for param.schema_type. May be any JSON-serializable
            Python value (str, int, float, bool, list, dict, or None).
        """

    def reset(self, seed: int | None = None) -> None:
        """
        Reset internal cycling state for reproducibility.

        Call this between campaigns to ensure the same seed produces
        the same sequence of values.

        Args:
            seed: If provided, use this seed. Otherwise re-use the original seed.
        """
        self._rng = random.Random(seed if seed is not None else self._seed)


class BaseExplorer(ABC):
    """
    Abstract endpoint/sequence selection strategy.

    select_next() is called by the engine at the start of each iteration
    to decide which endpoint to fuzz next.

    Concrete implementations:
        BFSExplorer     — RESTler-faithful BFS over sequences of length n
        LLMExplorer     — LLM-guided sequence selection (Config C)
    """

    @abstractmethod
    def select_next(
        self,
        state: "FuzzState",
        graph: "DependencyGraph",
    ) -> tuple[list[Endpoint], Endpoint]:
        """
        Select the next (prefix_sequence, target_endpoint) to execute.

        The engine will:
        1. Execute prefix_sequence to establish state (resolving dynamic values)
        2. Execute target_endpoint as the new fuzzing request
        3. Check if the result is a bug

        Args:
            state: Current campaign state (resource pool, coverage, history).
            graph: The dependency graph (for satisfiability checks).

        Returns:
            (prefix, target) where prefix is a list of already-executed Endpoints
            and target is the new Endpoint to append and fuzz.
        """

    def reset(self) -> None:
        """Reset internal queue/state. Called between campaigns."""
