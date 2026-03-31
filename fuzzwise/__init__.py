"""
FUZZWISE — LLM-Augmented Stateful Fuzz Testing for REST APIs.

This package orchestrates OpenAPI spec parsing, dependency inference,
stateful fuzzing campaigns, and post-hoc metric analysis.

Three fuzzing configurations are supported:
    - Config A: Static dictionary payloads + BFS endpoint exploration (baseline)
    - Config B: LLM-generated payloads + BFS endpoint exploration
    - Config C: LLM-generated payloads + LLM-guided endpoint exploration (stretch)
"""

__version__ = "0.1.0"
__author__ = "Kevin Shi, Darian Wong, Ryan Kwan"
