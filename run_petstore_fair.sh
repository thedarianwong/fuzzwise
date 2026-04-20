#!/bin/bash
# run_petstore_fair.sh — canonical A/B/C/D comparison for Petstore
#
# Uniform parameters across all configs:
#   --max-requests 500   primary budget (all configs)
#   --time-budget 3600   generous wall-clock ceiling (LLM explorer needs extra headroom)
#   --seed 42            reproducibility
#   --max-sequence-length 3
#
# docker compose restarts petstore between configs to reset server state.
#
# Usage:
#   chmod +x run_petstore_fair.sh
#   ./run_petstore_fair.sh

set -e

SPEC="data/specs/petstore.yaml"
TARGET="http://localhost:8080/api/v3"
PAYLOADS="data/llm_payloads/llm_payloads_qwen2.5_7b_batch.json"
RYAN_PAYLOADS="data/llm_payloads/llm_payloads_qwen2.5_7b.json"  # Ryan's pre-generated payload file
DICTS="data/dictionaries"
SEED=42
MAX_REQ=500
TIME_BUDGET=3600
SEQ_LEN=3
LOG_DIR="logs/petstore_fair"

mkdir -p "$LOG_DIR"

restart_petstore() {
    echo "--- Restarting Petstore to reset server state ---"
    docker compose -f docker/docker-compose.yaml restart petstore
    sleep 3
    echo "--- Petstore ready ---"
}

# ------------------------------------------------------------------
# Config A — Dictionary payloads + BFS explorer (baseline)
# ------------------------------------------------------------------
restart_petstore
echo ""
echo "=== Config A — Dictionary + BFS (baseline) ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$TARGET" \
  --strategy dictionary \
  --explorer bfs \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --dictionaries-dir "$DICTS" \
  --log-dir "$LOG_DIR" \
  --config-label A

# ------------------------------------------------------------------
# Config B-batch — LLM pre-generated payloads + BFS (canonical B)
# ------------------------------------------------------------------
restart_petstore
echo ""
echo "=== Config B-batch — LLM Pregenerated + BFS ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$TARGET" \
  --strategy llm_pregenerated \
  --llm-payloads "$PAYLOADS" \
  --explorer bfs \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --log-dir "$LOG_DIR" \
  --config-label B_batch

# ------------------------------------------------------------------
# Config B-ryan — Ryan's pre-generated payloads + BFS
# Uses Ryan's payload file (llm_payloads_qwen2.5_7b.json, 1020 payloads,
# generated iteratively offline). Avoids 6-hour live LLM campaign runtime.
# ------------------------------------------------------------------
restart_petstore
echo ""
echo "=== Config B-ryan — Ryan's Pregenerated Payloads + BFS ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$TARGET" \
  --strategy llm_pregenerated \
  --llm-payloads "$RYAN_PAYLOADS" \
  --explorer bfs \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --log-dir "$LOG_DIR" \
  --config-label B_ryan

# ------------------------------------------------------------------
# Config C — Dictionary payloads + LLM-guided explorer
# ------------------------------------------------------------------
restart_petstore
echo ""
echo "=== Config C — Dictionary + LLM Guided ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$TARGET" \
  --strategy dictionary \
  --explorer llm_guided \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --dictionaries-dir "$DICTS" \
  --log-dir "$LOG_DIR" \
  --config-label C

# ------------------------------------------------------------------
# Config D — LLM pre-generated payloads + LLM-guided explorer
# ------------------------------------------------------------------
restart_petstore
echo ""
echo "=== Config D — LLM Pregenerated + LLM Guided ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$TARGET" \
  --strategy llm_pregenerated \
  --llm-payloads "$PAYLOADS" \
  --explorer llm_guided \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --log-dir "$LOG_DIR" \
  --config-label D

# ------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------
echo ""
echo "=== Generating comparison report ==="
fuzzwise analyze \
  --logs-dir "$LOG_DIR" \
  --output "results/petstore_fair_comparison.md"

echo ""
echo "Done. Report: results/petstore_fair_comparison.md"
