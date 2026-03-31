<img width="2914" height="1440" alt="Fuzzwise Github Banner" src="https://github.com/user-attachments/assets/bc5d0578-6379-4cf4-b68f-bc672fdb95a7" />

# FUZZWISE

**LLM-Augmented Stateful Fuzz Testing for REST APIs**
CMPT 479 Graduate Research Project — Spring 2026

FUZZWISE reads an OpenAPI 3.0 specification, infers producer-consumer dependencies between endpoints, and performs **stateful** fuzz testing by chaining dependent requests (e.g., `POST /pets` → `GET /pets/{id}`). Its key contribution is using a locally-hosted LLM (Qwen 2.5 via Ollama) to generate semantically meaningful adversarial payloads, compared against a RESTler-style static dictionary baseline.

---

## Three Configurations

| Config | Payload Strategy | Exploration | Status |
|--------|-----------------|-------------|--------|
| **A** | Static dictionary (RESTler-style) | BFS | **Implemented** |
| **B** | LLM-generated | BFS | Not yet implemented |
| **C** | LLM-generated | LLM-guided | Not yet implemented |

Config A is the fully working baseline. Configs B and C are the planned contributions — running `--strategy llm` or `--explorer llm_guided` will exit with an error until implemented.

---

## Setup

### Prerequisites

- Python 3.11+
- Docker Desktop (for local target APIs)
- [Ollama](https://ollama.com) (required for Config B/C only — skip for Config A)

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
| `FUZZ_STRATEGY` | `dictionary` | `dictionary` or `llm` |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose HTTP logs |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server (Config B/C only) |
| `LLM_MODEL` | `qwen2.5:7b` | Model name (Config B/C only) |

All `.env` values can be overridden at the command line.

### 3. Start a local target API

The repo includes a Docker Compose file with two pre-configured targets:

```bash
# Petstore (port 8080) — recommended for baseline testing
docker compose -f docker/docker-compose.yaml up -d petstore

# Gitea (port 3000) — for future expansion
docker compose -f docker/docker-compose.yaml up -d gitea
```

Wait a few seconds, then verify Petstore is up:

```bash
curl http://localhost:8080/api/v3/openapi.json | python3 -m json.tool | head -5
```

### 4. (Config B/C only) Start Ollama

```bash
ollama serve &
ollama pull qwen2.5:7b
```

---

## Running the Fuzzer

### Basic usage

```bash
# Config A — static dictionary payloads + BFS (fully working)
fuzzwise run \
  --spec data/specs/petstore.yaml \
  --target http://localhost:8080/api/v3 \
  --strategy dictionary \
  --explorer bfs \
  --config-label config_a_baseline
```

### All flags

```
--spec PATH              Path to OpenAPI 3.0 YAML or JSON spec file (required)
--target URL             Target API base URL (default: $TARGET_API_URL or localhost:8080)
--strategy STR           dictionary | llm  (default: dictionary)
--explorer STR           bfs | bfs_fast | llm_guided  (default: bfs)
--max-requests INT       Request budget (default: $FUZZ_MAX_REQUESTS or 500)
--time-budget FLOAT      Time budget in seconds (default: 300)
--max-sequence-length INT  Max BFS depth (default: 3)
--seed INT               RNG seed for reproducibility (default: 42)
--config-label STR       Label for this run, appears in reports (default: A)
--log-dir PATH           Where to write JSONL logs (default: ./logs)
--dictionaries-dir PATH  Path to fuzz value dictionaries (default: ./data/dictionaries)
--auth-header KEY:VALUE  Extra HTTP header sent with every request — repeatable
--verbose                Enable DEBUG-level logging (shows every HTTP request)
```

### Testing against any API with auth

For APIs that require authentication:

```bash
fuzzwise run \
  --spec /path/to/openapi.yaml \
  --target https://api.example.com/v1 \
  --auth-header "Authorization:Bearer <your-token>" \
  --auth-header "X-Tenant-ID:acme"
```

`--auth-header` can be repeated for multiple headers. Format is `Key:Value` (whitespace around `:` is stripped).

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

Writes a structured markdown file to `results/report.md` containing:
1. Summary comparison table across all campaigns
2. Per-campaign detail: spec, strategy, seed, request count, status distribution
3. Full bug listing per campaign (type, endpoint, status code, triggering sequence)

When Configs A, B, and C have all been run and their logs are in `logs/`, this produces the paper comparison table in one command.

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
│   │   ├── engine.py           # RENDER + EXECUTE loop
│   │   ├── explorer.py         # BFS sequence selection
│   │   └── state.py            # Mutable campaign state (resource pool, seqSet)
│   ├── strategies/
│   │   ├── base.py             # Abstract interfaces
│   │   └── dictionary.py       # Config A: static dictionary payloads
│   ├── llm/                    # Config B/C stubs — not yet implemented
│   └── analysis/               # Analysis stubs — not yet implemented
├── data/
│   ├── specs/                  # OpenAPI spec files (petstore.yaml, petstore.json)
│   └── dictionaries/           # Fuzz value lists (strings, integers, numbers, booleans)
├── docker/
│   └── docker-compose.yaml     # Petstore + Gitea local targets
├── tests/
└── logs/                       # Campaign output (gitignored)
```

### Key design decisions

- **Strategy-agnostic engine**: `FuzzEngine` only depends on `BaseStrategy` and `BaseExplorer` interfaces. Concrete implementations (dictionary, LLM) are injected at the CLI layer.
- **`_body` sentinel for array requestBodies**: When an OpenAPI requestBody schema has `type: array`, the parser creates a synthetic `_body` parameter. The engine detects this and sends the value directly as the JSON array rather than wrapping it in an object.
- **Bug deduplication**: Bugs are deduplicated by `(operation_id, bug_type)` before being stored. The first triggering sequence is kept as the canonical report.
- **JSONL logging**: Every request and response is logged as two JSON lines. The campaign config is written as the first line, making every log file self-describing.
