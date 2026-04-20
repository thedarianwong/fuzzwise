![Fuzzwise Github Banner](https://github.com/user-attachments/assets/bc5d0578-6379-4cf4-b68f-bc672fdb95a7)

# FUZZWISE

**LLM-Augmented Stateful Fuzz Testing for REST APIs**
CMPT 479 Graduate Research Project — Spring 2026

FUZZWISE reads an OpenAPI 3.0 specification, infers producer-consumer dependencies between endpoints, and performs **stateful** fuzz testing by chaining dependent requests (e.g., `POST /pets` → `GET /pets/{id}`). It evaluates four configurations that isolate the contribution of LLM-generated payloads and LLM-guided sequence exploration against a RESTler-style static dictionary baseline, using a locally-hosted LLM (Qwen 2.5 via Ollama).

---

## Configurations

| Config | Payload Strategy | Explorer | Notes |
|--------|-----------------|----------|-------|
| **A** | Static dictionary (RESTler-style) | BFS | Baseline |
| **B_batch** | LLM pre-generated — batch (Qwen 2.5 7B) | BFS | Fast pre-gen (~6 min) |
| **B_ryan** | LLM pre-generated — iterative (Qwen 2.5 7B) | BFS | Higher diversity (~6h) |
| **C** | Static dictionary | LLM-guided | Isolates exploration quality |
| **D** | LLM pre-generated — batch | LLM-guided | Full LLM augmentation |

Config A is the baseline. B isolates payload quality. C isolates exploration strategy. D combines both.

---

## Setup

### Prerequisites

- Python 3.11+
- Docker Desktop (for local target APIs)
- [Ollama](https://ollama.com) (required for Config B/C/D only — skip for Config A)

### 1. Clone and install

```bash
git clone https://github.com/<org>/fuzzwise.git
cd fuzzwise
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

This installs the `fuzzwise` CLI and all dependencies.

### 2. Configure environment

```bash
cp .env.example .env
```

Key variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_API_URL` | `http://localhost:8080` | Base URL of the API to fuzz |
| `FUZZ_MAX_REQUESTS` | `500` | Request budget per campaign |
| `FUZZ_TIME_BUDGET_SECONDS` | `300` | Time budget per campaign |
| `FUZZ_STRATEGY` | `dictionary` | `dictionary` or `llm_pregenerated` |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose HTTP logs |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server (Config B/C/D only) |
| `LLM_MODEL` | `qwen2.5:7b` | Model name (Config B/C/D only) |

All `.env` values can be overridden at the command line.

### 3. Start a local target API

The repo includes a Docker Compose file with two pre-configured targets:

```bash
# Petstore (port 8080)
docker compose -f docker/docker-compose.yaml up -d petstore

# Gitea (port 3000)
docker compose -f docker/docker-compose.yaml up -d gitea
```

Wait a few seconds, then verify Petstore is up:

```bash
curl http://localhost:8080/api/v3/openapi.json | python3 -m json.tool | head -5
# Windows:
curl.exe -s http://localhost:8080/api/v3/openapi.json | python -m json.tool | Select-Object -First 5
```

### 4. (Config B/C/D only) Start Ollama

```bash
ollama serve &
ollama pull qwen2.5:7b
```

---

## Running the Fuzzer

### Config A — static dictionary + BFS (baseline)

```bash
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy dictionary \
  --explorer bfs \
  --config-label A
```

### Config B — LLM pre-generated payloads + BFS

Config B requires a one-time pre-generation step before running the campaign.
Two variants are available — both produce identical campaign speed to Config A.

#### Step 1 — Start Ollama and pull the model (first time only)

```bash
ollama serve &
ollama pull qwen2.5:7b
```

#### Step 2 — Pre-generate payloads

**Batch** (faster, ~6 min on any hardware, slightly lower parse rate):
```bash
python -m fuzzwise.llm.pregenerate_batch \
  --spec data/specs/petstore.yaml \
  --model qwen2.5:7b \
  --num-payloads 20 \
  --output-dir data/llm_payloads
```

**Iterative** (higher value diversity, ~6h on M1):
```bash
python -m fuzzwise.llm.pregenerate \
  --spec data/specs/petstore.yaml \
  --model qwen2.5:7b \
  --num-payloads 20 \
  --output-dir data/llm_payloads
```

Both scripts save a JSON payload file to `data/llm_payloads/`. Pre-generated payload
files for Petstore are already committed to the repo — skip this step if they exist.

#### Step 3 — Run the campaign

```bash
# Using batch payloads
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy llm_pregenerated \
  --llm-payloads data/llm_payloads/llm_payloads_qwen2.5_7b_batch.json \
  --config-label B_batch

# Using iterative payloads
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy llm_pregenerated \
  --llm-payloads data/llm_payloads/llm_payloads_qwen2.5_7b.json \
  --config-label B_ryan
```

### Config C — static dictionary + LLM-guided exploration

Config C uses the dictionary payload strategy but replaces BFS with an LLM that chooses
which sequences to explore next based on coverage state. Ollama must be running.

```bash
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy dictionary \
  --explorer llm_guided \
  --config-label C
```

Note: the LLM-guided explorer makes one Ollama call per batch of 5 sequences. On large
APIs (100+ endpoints), each call can take several minutes — see the Scalability section below.

### Config D — LLM pre-generated payloads + LLM-guided exploration

Config D combines LLM payloads (pre-generated) with LLM-guided exploration. This is the
full augmentation configuration. Requires both a pre-generated payload file and Ollama running.

```bash
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy llm_pregenerated \
  --llm-payloads data/llm_payloads/llm_payloads_qwen2.5_7b_batch.json \
  --explorer llm_guided \
  --config-label D
```

### Run the fair comparison (recommended)

Reproduce all paper results in one command:

```bash
# Petstore — all 5 configs (A, B_batch, B_ryan, C, D)
chmod +x run_petstore_fair.sh && ./run_petstore_fair.sh

# Gitea — Configs A and B_batch (C/D hit time budget at this scale — see below)
chmod +x run_gitea_fair.sh && ./run_gitea_fair.sh
```

Reports are written to `results/petstore_fair_comparison.md` and `results/gitea_fair_comparison.md`.

### Scalability note for Config C and D on large APIs

The LLM-guided explorer is not feasible at scale without a time budget cap. On Gitea
(145 endpoints), each LLM call takes several minutes, so C and D are run with a 300-second
budget. In that window they explore ~30 sequences and cover ~47% of endpoints, compared to
BFS-Fast reaching 100% in 11 seconds. This is documented as a scalability limitation.

The `run_gitea_fair.sh` script runs C and D with `--time-budget 300` for empirical evidence.

### Testing against any API with auth

```bash
fuzzwise run \
  --spec /path/to/openapi.yaml \
  --target https://api.example.com/v1 \
  --auth-header "Authorization:Bearer <your-token>" \
  --auth-header "X-Tenant-ID:acme"
```

`--auth-header` can be repeated for multiple headers. Format is `Key:Value` (whitespace around `:` is stripped).

### All flags

```
--spec PATH              Path to OpenAPI 3.0 YAML or JSON spec file (required)
--target URL             Target API base URL (default: $TARGET_API_URL or localhost:8080)
--strategy STR           dictionary | llm_pregenerated  (default: dictionary)
--explorer STR           bfs | bfs_fast | llm_guided  (default: bfs)
--max-requests INT       Request budget (default: $FUZZ_MAX_REQUESTS or 500)
--time-budget FLOAT      Time budget in seconds (default: 300)
--max-sequence-length INT  Max BFS depth (default: 3)
--seed INT               RNG seed for reproducibility (default: 42)
--config-label STR       Label for this run, appears in reports (default: A)
--log-dir PATH           Where to write JSONL logs (default: ./logs)
--llm-payloads PATH      Path to pre-generated payload JSON (required for llm_pregenerated)
--dictionaries-dir PATH  Path to fuzz value dictionaries (default: ./data/dictionaries)
--auth-header KEY:VALUE  Extra HTTP header sent with every request — repeatable
--verbose                Enable DEBUG-level logging (shows every HTTP request)
```

---

## Viewing Results

### During / after a run

Campaign logs are written to `logs/` as two files per run:
- `campaign_<id>.jsonl` — full request/response log (one JSON object per line)
- `campaign_<id>.result.json` — aggregated metrics summary

### Terminal comparison table

```bash
fuzzwise analyze --logs-dir logs/
```

Shows a side-by-side table of all campaigns in `logs/` including: config, strategy, requests, coverage, unique 500s, error types, schema violations, max depth, and duration.

### Markdown report

```bash
fuzzwise analyze --logs-dir logs/ --output results/report.md
```

Writes a structured markdown file containing:
1. Summary comparison table across all campaigns
2. Per-campaign detail: spec, strategy, seed, request count, status distribution
3. Full bug listing per campaign (type, endpoint, status code, triggering sequence)

---

## Metrics

| Metric | Definition | Source |
|--------|------------|--------|
| `unique_500_count` | Distinct endpoints that returned 5xx | RESTler (ICSE 2019) |
| `error_type_count` | Distinct `(endpoint, bug_type)` pairs | FSE 2020 |
| `schema_violation_count` | Responses that don't match the declared schema | FUZZWISE |
| `endpoints_hit` | Fraction of spec endpoints reached | Standard |
| `max_depth_reached` | Deepest sequence successfully executed | FUZZWISE |
| `sequences_explored` | Size of BFS seqSet at campaign end | RESTler |

---

## Development

```bash
# Lint
ruff check fuzzwise/ tests/

# Type check
mypy fuzzwise/

# Tests
pytest
```

### Project layout

```
fuzzwise/
├── fuzzwise/
│   ├── cli.py                  # Entry point — run / analyze subcommands
│   ├── models/types.py         # All Pydantic data models (no internal imports)
│   ├── spec/
│   │   ├── parser.py           # OpenAPI → Endpoint list
│   │   └── dependencies.py     # Producer-consumer inference (RESTler heuristic)
│   ├── fuzzer/
│   │   ├── engine.py           # RENDER + EXECUTE loop; wires LLM call counts into FuzzResult
│   │   ├── explorer.py         # BFS / BFS-Fast sequence selection
│   │   ├── llm_explorer.py     # LLM-guided explorer (Config C/D); batched, coverage-aware
│   │   └── state.py            # Mutable campaign state (resource pool, seqSet)
│   ├── strategies/
│   │   ├── base.py             # Abstract interfaces
│   │   ├── dictionary.py       # Config A/C: static dictionary payloads
│   │   ├── llm.py              # Online LLM strategy (per-request, experimental)
│   │   └── llm_pregenerated.py # Config B/D: pre-generated payload strategy
│   ├── llm/
│   │   ├── client.py           # Ollama HTTP wrapper
│   │   ├── prompts.py          # Prompt templates
│   │   ├── pregenerate.py      # Iterative pre-generation script (1 call/value)
│   │   └── pregenerate_batch.py # Batch pre-generation script (1 call/param)
│   └── analysis/               # Campaign log analysis and report generation
├── data/
│   ├── specs/                  # OpenAPI spec files (petstore.yaml, gitea_user_issue.json)
│   ├── dictionaries/           # Fuzz value lists (strings, integers, numbers, booleans)
│   └── llm_payloads/           # Pre-generated payload JSON files
├── docker/
│   └── docker-compose.yaml     # Petstore + Gitea local targets
├── results/                    # Comparison reports (gitignored except committed baselines)
├── run_petstore_fair.sh        # Reproduces all Petstore campaign results
├── run_gitea_fair.sh           # Reproduces all Gitea campaign results
├── tests/
└── logs/                       # Campaign output (gitignored)
```

### Key design decisions

- **Strategy-agnostic engine**: `FuzzEngine` only depends on `BaseStrategy` and `BaseExplorer` interfaces. Concrete implementations (dictionary, LLM) are injected at the CLI layer.
- **`_body` sentinel for array requestBodies**: When an OpenAPI requestBody schema has `type: array`, the parser creates a synthetic `_body` parameter. The engine detects this and sends the value directly as the JSON array rather than wrapping it in an object.
- **Bug deduplication**: Bugs are deduplicated by `(operation_id, bug_type)` before being stored. The first triggering sequence is kept as the canonical report.
- **JSONL logging**: Every request and response is logged as two JSON lines. The campaign config is written as the first line, making every log file self-describing.
- **LLM explorer batching**: The LLM-guided explorer requests 5 sequences per Ollama call and queues results, so LLM overhead is amortized across multiple fuzzing iterations rather than paid per sequence.
