#!/bin/bash
# Config A vs Config B (batch) comparison on Gitea — user+issue scope
# Pre-requisites:
#   1. Gitea running: docker compose -f docker/docker-compose.yaml up -d gitea
#   2. LLM payloads pre-generated: see step below
#   3. Ollama NOT required at campaign time (payloads are pre-generated)
#
# Pre-generate (one-time, ~40 min):
#   python -m fuzzwise.llm.pregenerate_batch \
#     --spec data/specs/gitea_user_issue.json \
#     --model qwen2.5:7b \
#     --num-payloads 20 \
#     --output-dir data/llm_payloads/gitea
#
# Then run this script.

set -e

GITEA_TOKEN="ca5b99a16d5325322bd8aef1f16d28cf5ecfdb3a"
GITEA_URL="http://localhost:3000/api/v1"
SPEC="data/specs/gitea_user_issue.json"
AUTH="Authorization:token ${GITEA_TOKEN}"
PAYLOADS="data/llm_payloads/gitea/llm_payloads_qwen2.5_7b_batch.json"

# Shared campaign settings
MAX_REQ=1000
TIME_BUDGET=300
SEQ_LEN=2
SEED=42

echo "=== Config A — RESTler-style dictionary baseline ==="
fuzzwise run \
  --spec "${SPEC}" \
  --target "${GITEA_URL}" \
  --strategy dictionary \
  --explorer bfs_fast \
  --max-requests "${MAX_REQ}" \
  --time-budget "${TIME_BUDGET}" \
  --max-sequence-length "${SEQ_LEN}" \
  --seed "${SEED}" \
  --auth-header "${AUTH}" \
  --config-label A_gitea

echo ""
echo "=== Config B — LLM pre-generated payloads (batch) ==="
fuzzwise run \
  --spec "${SPEC}" \
  --target "${GITEA_URL}" \
  --strategy llm_pregenerated \
  --llm-payloads "${PAYLOADS}" \
  --explorer bfs_fast \
  --max-requests "${MAX_REQ}" \
  --time-budget "${TIME_BUDGET}" \
  --max-sequence-length "${SEQ_LEN}" \
  --seed "${SEED}" \
  --auth-header "${AUTH}" \
  --config-label B_gitea_batch

echo ""
echo "=== Comparison Report ==="
fuzzwise analyze --logs-dir logs/ --output results/gitea_comparison.md

echo ""
echo "Report written to results/gitea_comparison.md"
