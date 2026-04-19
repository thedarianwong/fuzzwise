"""
LLM-Guided Sequence Explorer using Langchain.
"""

from __future__ import annotations

import json
import logging
import random
from typing import TYPE_CHECKING

from langchain_core.prompts import PromptTemplate
from langchain_ollama import ChatOllama

from fuzzwise.models.types import Endpoint
from fuzzwise.strategies.base import BaseExplorer

if TYPE_CHECKING:
    from fuzzwise.fuzzer.state import FuzzState
    from fuzzwise.spec.dependencies import DependencyGraph

logger = logging.getLogger(__name__)

class LLMGuidedExplorer(BaseExplorer):
    def __init__(self, model: str = "qwen2.5:7b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url
        self._endpoint_map: dict[str, Endpoint] = {}
        
        logger.info(f"Initializing LangChain ChatOllama with model {model} at {base_url}")
        self.llm = ChatOllama(
            model=model,
            base_url=base_url,
            temperature=0.7,
        )
        
        self.prompt = PromptTemplate.from_template(
            "You are an intelligent API fuzzer choosing the next sequence of operations to test.\n\n"
            "Here are the available operations:\n"
            "{operations}\n\n"
            "Here is the fuzzing state overview:\n"
            "{coverage}\n\n"
            "Your task is to generate ONE logical sequence of operations to test next. "
            "For example, if you want to test getting a user, you must first create a user. "
            "Return ONLY a JSON array of operation IDs. Example: [\"createUser\", \"getUser\"].\n"
            "Do not use markdown formatting or include explanations."
        )

    def select_next(
        self,
        state: "FuzzState",
        graph: "DependencyGraph",
    ) -> tuple[list[Endpoint], Endpoint]:
        
        if not self._endpoint_map:
            self._endpoint_map = {e.operation_id: e for e in graph.endpoints}
            
        # Keep operations summary tight to avoid huge context usage
        operations_summary = []
        for e in graph.endpoints:
            params = [p.name for p in e.all_params]
            operations_summary.append(f"- {e.operation_id} (params: {', '.join(params)})")
            
        coverage_summary = f"Total valid sequences tested: {len(state.valid_sequences)}."
            
        chain = self.prompt | self.llm
        
        try:
            logger.debug("Prompting LLM for next sequence...")
            response = chain.invoke({
                "operations": "\n".join(operations_summary),
                "coverage": coverage_summary,
            })
            
            raw_content = response.content.strip()
            if raw_content.startswith("```"):
                lines = raw_content.split("\n")
                raw_content = "\n".join(lines[1:-1]) if len(lines) >= 3 else raw_content
                
            sequence_ids = json.loads(raw_content)
            
            if not isinstance(sequence_ids, list) or not sequence_ids:
                raise ValueError("LLM did not return a valid list of operations.")
                
            # Filter to valid ops
            sequence_ids = [op for op in sequence_ids if op in self._endpoint_map]
            if not sequence_ids:
                raise ValueError("LLM returned nonexistent operation IDs.")
                
            # Target is the last item, prefix is all items before it
            target_id = sequence_ids[-1]
            prefix_ids = sequence_ids[:-1]
            
            prefix = [self._endpoint_map[op] for op in prefix_ids]
            target = self._endpoint_map[target_id]
            
            logger.info(f"LLM Explorer selected sequence: {sequence_ids}")
            return prefix, target
            
        except Exception as e:
            logger.warning(f"LLM Explorer failed to generate sequence: {e}. Falling back to random root.")
            roots = graph.roots()
            target = random.choice(roots) if roots else random.choice(graph.endpoints)
            return [], target

    def reset(self) -> None:
        self._endpoint_map.clear()
