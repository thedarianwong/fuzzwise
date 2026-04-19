#!/bin/bash
set -e

# Config A — baseline
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy dictionary \
  --config-label A

# Config B — iterative (Kevin's 6h pre-gen)
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy llm_pregenerated \
  --llm-payloads data/llm_payloads/llm_payloads_qwen2.5_7b.json \
  --config-label B_iterative

# Config B — batch (4 min pre-gen)
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy llm_pregenerated \
  --llm-payloads data/llm_payloads/llm_payloads_qwen2.5_7b_batch.json \
  --config-label B_batch

# Compare
fuzzwise analyze --logs-dir logs/ --output results/petstore_comparison.md
