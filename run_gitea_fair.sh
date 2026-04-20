#!/bin/bash
# run_gitea_fair.sh — fair A vs B comparison on Gitea (user+issue scope)
#
# WHY only A and B (no C/D):
#   Config C and D use LLM-guided exploration. On Gitea (145 endpoints),
#   LLM context window is too large — requests take >5 min each even with
#   windowing to 30 endpoints. Documented in the paper as a scalability
#   limitation. C and D are evaluated on Petstore only.
#
# WHY no B-ryan:
#   Ryan's iterative pre-generation takes ~6h for Petstore (51 params).
#   Gitea has 492 parameters — estimated ~60h. Not feasible.
#   Only the batch-generated payload file is available for Gitea.
#
# WHY no server reset between configs:
#   Gitea uses a persistent Docker volume (gitea_data). Recreating the volume
#   would invalidate the auth token. Server state from Config A may persist
#   into Config B — documented as a threat to validity. In practice, the
#   BFS explorer reruns all sequences independently, so residual state has
#   minimal effect.
#
# Prerequisites:
#   1. Docker running
#   2. Gitea container up:
#        docker compose -f docker/docker-compose.yaml up -d gitea
#        (wait ~30s for Gitea to initialize)
#   3. Gitea token provisioned (see setup note below)
#   4. Payload file exists: data/llm_payloads/gitea/llm_payloads_qwen2.5_7b_batch.json
#   5. Ollama NOT needed (payloads are pre-generated)
#
# Token setup (one-time, if Gitea volume was wiped):
#   curl -s http://localhost:3000/api/v1/users/search?q=admin | python3 -m json.tool
#   # If no admin user: create one via Gitea web UI at http://localhost:3000
#   # Then generate a token:
#   curl -s -X POST http://localhost:3000/api/v1/users/admin/tokens \
#     -u admin:admin \
#     -H "Content-Type: application/json" \
#     -d '{"name":"fuzzwise"}' | python3 -m json.tool
#   # Copy the sha1 value into GITEA_TOKEN below.
#
# Usage:
#   chmod +x run_gitea_fair.sh
#   ./run_gitea_fair.sh

set -e

GITEA_TOKEN="ca5b99a16d5325322bd8aef1f16d28cf5ecfdb3a"
GITEA_URL="http://localhost:3000/api/v1"
SPEC="data/specs/gitea_user_issue.json"
AUTH="Authorization:token ${GITEA_TOKEN}"
PAYLOADS="data/llm_payloads/gitea/llm_payloads_qwen2.5_7b_batch.json"
LOG_DIR="logs/gitea_fair"

# Shared campaign settings — identical across all configs for fair comparison
MAX_REQ=1000
TIME_BUDGET=600      # generous ceiling; bfs_fast is fast, extra headroom for slow endpoints
SEQ_LEN=2            # Gitea is large; depth-2 is the practical ceiling with bfs_fast
SEED=42
EXPLORER="bfs_fast"  # bfs_fast scales to 145 endpoints; plain bfs would OOM on seqSet

mkdir -p "$LOG_DIR"

echo "=== Verifying Gitea is reachable ==="
curl -sf "${GITEA_URL}/version" > /dev/null || {
  echo "ERROR: Gitea not responding at ${GITEA_URL}"
  echo "Run: docker compose -f docker/docker-compose.yaml up -d gitea && sleep 30"
  exit 1
}
echo "Gitea OK"

echo ""
echo "=== Verifying auth token ==="
curl -sf -H "${AUTH}" "${GITEA_URL}/user" > /dev/null || {
  echo "ERROR: Auth token invalid. Re-provision token (see script header)."
  exit 1
}
echo "Token OK"

echo ""

# ------------------------------------------------------------------
# Config A — Dictionary payloads + BFS-Fast (baseline)
# ------------------------------------------------------------------
echo "=== Config A — Dictionary + BFS-Fast (baseline) ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$GITEA_URL" \
  --strategy dictionary \
  --explorer "$EXPLORER" \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --auth-header "$AUTH" \
  --log-dir "$LOG_DIR" \
  --config-label A_gitea

# ------------------------------------------------------------------
# Config B-batch — LLM pre-generated payloads + BFS-Fast
# ------------------------------------------------------------------
echo ""
echo "=== Config B-batch — LLM Pregenerated + BFS-Fast ==="
fuzzwise run \
  --spec "$SPEC" \
  --target "$GITEA_URL" \
  --strategy llm_pregenerated \
  --llm-payloads "$PAYLOADS" \
  --explorer "$EXPLORER" \
  --max-requests "$MAX_REQ" \
  --time-budget "$TIME_BUDGET" \
  --max-sequence-length "$SEQ_LEN" \
  --seed "$SEED" \
  --auth-header "$AUTH" \
  --log-dir "$LOG_DIR" \
  --config-label B_gitea_batch

# ------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------
echo ""
echo "=== Generating comparison report ==="
fuzzwise analyze \
  --logs-dir "$LOG_DIR" \
  --output "results/gitea_fair_comparison.md"

echo ""
echo "Done. Report: results/gitea_fair_comparison.md"
