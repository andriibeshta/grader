# Grader

![Grader — a multi-judge paper-grading service for research reports](docs/assets/grader-banner.jpg)

A source-agnostic, multi-judge paper-grading service for research reports.

Grader accepts a report from text, a URL, an uploaded file, or an optional LDR
job; sends it through independent LLM judges; validates and combines their
scores; and stores every grade in SQLite. It exposes an HTTP API, a CLI, and an
optional Hermes plugin.

The core does **not** require LDR. LDR is one optional adapter; any pipeline can
submit a report directly.

**Status:** 0.2.13 — Docker-first, bearer-token API, per-judge OpenRouter
provider chains, 32 network-free tests, CI on every push.

[Quick start](#quick-start) · [Architecture](#architecture) ·
[Provider routing](#provider-routing) · [Configuration](#configuration) ·
[API](#api) · [Deployment](#deployment) · [Storage](#storage) ·
[Reliability](#reliability) · [Tests](#tests) ·
[Security](#security-and-limitations)

## Quick start

You need Docker with Compose v2, a reachable OpenRouter account, and two values
in `.env`: `OPENROUTER_API_KEY` and `GRADER_API_TOKEN`. LDR is not required.

```bash
git clone https://github.com/andriibeshta/grader.git
cd grader
cp .env.example .env
$EDITOR .env                  # set the two required values
mkdir -p state                # must be writable by the container user
docker compose up -d --build
```

Generate the service token with `openssl rand -hex 32`. The API binds to
`0.0.0.0:9131` by default; change `GRADER_BIND_IP` and `GRADER_PUBLISH_PORT` if
needed.

Verify the service:

```bash
curl -fsS http://127.0.0.1:9131/health/ready

TOKEN=$(grep ^GRADER_API_TOKEN= .env | cut -d= -f2-)
curl -sS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:9131/health
```

`/health` shows the configured judge models and provider chains. `/health/live`
and `/health/ready` require no token.

Submit a report:

```bash
curl -sS http://127.0.0.1:9131/v1/papers \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "source_type": "text",
    "title": "My report",
    "content": "# Report\n...",
    "grade": true,
    "wait": true
  }'
```

Grading is asynchronous by default. The response contains a `job_id`; poll
`GET /v1/jobs/{job_id}` for the result.

### Image-only release

The release image is self-contained and needs no source tree or `config.json`:

```bash
mkdir -p ~/grader && cd ~/grader
gunzip -c grader-0.2.13.tar.gz | docker load
cp .env.example .env
# set OPENROUTER_API_KEY and GRADER_API_TOKEN
mkdir -p state
docker compose -f docker-compose.release.yml up -d
```

The current release is
[v0.2.13](https://github.com/andriibeshta/grader/releases/tag/v0.2.13).

## Architecture

```text
text / URL / file / base64 upload / LDR job
                    │
              ingest adapter
                    │
             usability gate
                    │
        rubric resolution + prompt build
                    │
        ┌───────────┴───────────┐
   primary judge          secondary judge
        └───────────┬───────────┘
                    │
       validation + median per criterion
                    │
          disagreement detected?
             │              │
            no             yes
             │              │
             │        tiebreaker judge
             └──────┬───────┘
                    │
        weighted overall computed by service
                    │
       SQLite + judge runs + audit trail
                    │
       API / CLI / plugin / CSV / chart
```

The service calculates the weighted overall itself. Judge-provided totals are
never trusted.

### Grading pipeline

1. **Ingest** text, a URL, a file, an uploaded file, or a completed LDR job.
2. **Gate** the report: empty reports, failure pages, and reports below the
   minimum useful length are stored but not judged.
3. **Resolve** the rubric and build a blind scoring prompt.
4. **Judge** with the primary and secondary models in parallel.
5. **Validate** JSON, retry malformed or empty responses, and take the median
   per criterion.
6. **Adjudicate** with the tiebreaker when overall disagreement exceeds `1.0`
   or any criterion disagreement exceeds `2.0`.
7. **Score** with service-side weights.
8. **Persist** the paper, grade, judge runs, served providers, cost, latency,
   and audit trail.

## Scoring

| Criterion | Weight |
|---|---:|
| Factual accuracy | 25% |
| Coverage | 20% |
| Critical analysis | 20% |
| Source quality and citations | 15% |
| Structure and readability | 10% |
| Actionable conclusion | 10% |

Each criterion is scored `0–10`. The default rubric is
`generic_research_v1`, a neutral research-report rubric. Use a custom rubric
and optional query map when you have a domain-specific checklist.

## Provider routing

Grader supports an ordered OpenRouter provider chain for **each judge role**.
The routing model matches Curator:

- the first entry is preferred;
- later entries are fallback tiers;
- the same list is the whitelist;
- a provider outside the chain cannot serve the request.

The request uses:

```json
{
  "provider": {
    "order": ["deepseek", "pearl-research", "..."],
    "only":  ["deepseek", "pearl-research", "..."]
  }
}
```

`allow_fallbacks` is intentionally not set to `false`. With `order == only`,
the chain entries are the permitted fallback tiers.

A chain only applies when the model's vendor prefix appears in that role's
chain. This prevents a DeepSeek chain from being sent to an Anthropic or OpenAI
model. Leave a role's chain unset to use OpenRouter default routing. A
one-entry chain means primary-only for that role.

Do not add `quantizations` or `max_price` filters. DeepSeek's official endpoint
reports `quantization: unknown`; a quantization filter would silently exclude
it.

### Current reference deployment

The chains used by the reference deployment are:

```dotenv
GRADER_PRIMARY_PROVIDER_CHAIN=deepseek,pearl-research,tenstorrent,morph,pareto-inference,deepinfra,streamlake,cohere,siliconflow
GRADER_SECONDARY_PROVIDER_CHAIN=anthropic
GRADER_TIEBREAKER_PROVIDER_CHAIN=openai
```

| Role | Model |
|---|---|
| Primary | `deepseek/deepseek-v4.1-flash-1017` |
| Secondary | `anthropic/claude-haiku-5.5` |
| Tiebreaker | `openai/gpt-6-luna` |

These are deployment choices, not hard-coded limits. Every role accepts its own
chain and model.

### Served-provider audit

Every judge run records `served_provider` in `judge_runs`. When a fallback
provider serves a call, Grader logs it:

```text
[judge] primary preferred deepseek, served by pearl-research
```

`GET /v1/grades/{grade_id}` returns the judge runs, including the served
provider, latency, usage, cost, and error state.

## Configuration

Everything is environment-driven. `.env` is read by Compose; exported shell
variables override `.env` during interpolation.

### Required

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | — | One OpenRouter key for all judge models |
| `GRADER_API_TOKEN` | — | Bearer token clients must present |

### Optional

| Variable | Default | Meaning |
|---|---|---|
| `GRADER_BIND_IP` | `0.0.0.0` | Host interface to publish on |
| `GRADER_PUBLISH_PORT` | `9131` | Host port |
| `GRADER_STATE_DIR` | `./state` | Persistent SQLite and backup directory |
| `GRADER_WORKER_THREADS` | `2` | Grading workers |
| `GRADER_JUDGE_ATTEMPTS` | `4` | Retries per judge call |
| `GRADER_PRIMARY_MODEL` | `deepseek/deepseek-v4-flash` | Primary judge model |
| `GRADER_SECONDARY_MODEL` | `z-ai/glm-5.3-flash` | Secondary judge model |
| `GRADER_TIEBREAKER_MODEL` | `deepseek/deepseek-v4-pro` | Tiebreaker model |
| `GRADER_PRIMARY_PROVIDER_CHAIN` | unset | Ordered primary provider chain |
| `GRADER_SECONDARY_PROVIDER_CHAIN` | unset | Ordered secondary provider chain |
| `GRADER_TIEBREAKER_PROVIDER_CHAIN` | unset | Ordered tiebreaker provider chain |
| `GRADER_LDR_ENABLED` | `false` | Enable the optional LDR watcher |
| `GRADER_LDR_BASE` | `http://127.0.0.1:5000` | LDR API base |
| `GRADER_RUBRIC_OVERLAY` | unset | Custom rubric directory |
| `GRADER_QUERIES_FILE` | unset | Optional custom query map |

`config.json` is optional; `{}` is sufficient. All model roles use OpenRouter
by default and can be changed to another compatible gateway and model IDs.

### LDR adapter

LDR is disabled by default. Enable it only when this deployment should poll an
LDR instance:

```dotenv
GRADER_LDR_ENABLED=true
GRADER_LDR_BASE=http://ldr-host:5000
LDR_USER=...
LDR_PASS=...
```

The watcher baselines existing history on first start, then grades new
completed jobs. Reports that are too short, empty, or recognizable failure
pages are stored but not sent to judges.

### Custom rubrics

Mount a read-only directory and point the service at it:

```yaml
volumes:
  - "./state:/data"
  - "./custom-rubrics:/config/rubrics:ro"
```

```dotenv
GRADER_RUBRIC_OVERLAY=/config/rubrics
```

Add `custom-rubrics/<rubric_id>.md`, then submit with
`"rubric_id":"<rubric_id>"`. A custom `queries.json` can be mounted through
`GRADER_QUERIES_FILE`; unknown queries fall back to `generic_research_v1`.

## API

Every route except `/health/live` and `/health/ready` requires:

```http
X-API-Token: ***
```

or:

```http
Authorization: Bearer ***
```

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/papers` | Ingest text/file/URL/upload and optionally grade |
| `POST` | `/v1/papers/from-ldr/{job_id}` | Optional LDR ingestion |
| `POST` | `/v1/grades` | Grade or force-regrade a paper |
| `POST` | `/v1/batches` | Grade a batch of paper IDs |
| `GET` | `/v1/jobs/{job_id}` | Job status and result |
| `GET` | `/v1/jobs?status=failed` | List failures |
| `POST` | `/v1/jobs/{job_id}/retry` | Retry a failed job |
| `GET` | `/v1/papers/{paper_id}` | Paper metadata |
| `GET` | `/v1/papers/{paper_id}/current-grade` | Canonical current grade |
| `GET` | `/v1/grades/{grade_id}` | Grade plus judge runs |
| `GET` | `/v1/scores?format=json\|csv` | Score history |
| `GET` | `/v1/batches/{id}` | Batch status and cost |
| `GET` | `/v1/batches/{id}/export?format=csv` | Batch export |
| `GET` | `/v1/chart.png` | Quality chart |
| `GET` | `/health/live` | Liveness, no token |
| `GET` | `/health/ready` | DB/worker readiness, no token |
| `GET` | `/health` | Detailed diagnostics, token required |

Upload a file without a host mount:

```json
{
  "source_type": "file",
  "filename": "report.pdf",
  "content_base64": "...",
  "source_ref": "pipeline:report-42",
  "grade": true
}
```

Force a fresh judgment:

```json
{
  "paper_id": "...",
  "force": true,
  "wait": true
}
```

See [`INTEGRATION.md`](INTEGRATION.md) for the full contract.

## Deployment

The repository's `docker-compose.yml` is the source-build stack. The
image-only `docker-compose.release.yml` is the portable release stack.

Minimal deployment:

```yaml
name: grader

services:
  grader:
    image: grader:0.2.13
    restart: unless-stopped
    user: "${GRADER_UID:-1000}:${GRADER_GID:-1000}"
    env_file: .env
    environment:
      GRADER_DATA_DIR: /data
      GRADER_LDR_ENABLED: "${GRADER_LDR_ENABLED:-false}"
    ports:
      - "9131:9131"
    volumes:
      - "./state:/data"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9131/health/ready', timeout=3)"
      interval: 30s
      timeout: 5s
      retries: 3
```

For a source build, replace `image: grader:0.2.13` with `build: .` and run
`docker compose up -d --build`.

The release Compose file adds read-only root, dropped capabilities, logging
limits, and configurable bind address/port/UID/GID.

### Deploying elsewhere

Build and ship the image:

```bash
docker save grader:0.2.13 | gzip > dist/grader-0.2.13.tar.gz
scp dist/grader-0.2.13.tar.gz user@host:/tmp/
```

On the target:

```bash
gunzip -c /tmp/grader-0.2.13.tar.gz | docker load
docker compose -f docker-compose.release.yml up -d
curl -fsS http://127.0.0.1:9131/health/ready
```

For a deployment where Grader must reach a containerised LDR, see
[`PORTABLE.md`](PORTABLE.md).

## Storage

```text
$GRADER_STATE_DIR/
  grades.db                 SQLite: papers, grades, jobs, judge runs, audit
  backups/                  verified SQLite snapshots
  chart.png                 generated quality chart
  watcher_state.json        LDR watcher checkpoint
  ldr_cookies/              optional LDR session cache
```

Papers, failed attempts, and superseded grades are not deleted. Forced
regrades preserve history, and the newest grade is marked `is_current`.

## Reliability

- Each judge call retries up to 4 times with exponential backoff.
- Empty responses retry with a larger token budget and structured-output mode
  removed.
- Parse failures retry with a JSON-only correction prompt.
- A failed primary judge triggers the tiebreaker as a replacement.
- A disagreement above `1.0` overall or `2.0` on any criterion triggers
  adjudication.
- Three consecutive judge failures trigger a five-minute cooldown.
- Failed jobs retry up to 3 times, then remain visible with their error.
- Interrupted `running` jobs are requeued on startup.
- Daily SQLite backups are verified with `PRAGMA integrity_check` and retained
  for 30 snapshots.

Provider chains add an availability layer inside a judge call. If every
provider in a role's chain fails, the existing judge retry and cooldown logic
still applies; the grading pipeline does not silently switch to an unrelated
model.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

**32 network-free tests** cover validation, retries, fallback judging, circuit
breaking, provider-chain routing, served-provider capture, rubric resolution,
custom rubrics, ingestion, schema setup, cache invalidation, forced regrades,
and canonical current grades. CI runs the suite on every push to `main`.

## Project layout

```text
grader/                 service package
  adapters/ldr.py       optional LDR adapter
  judges.py             judge calls, retries, provider routing
  pipeline.py           registration and grading pipeline
  server.py             HTTP API and worker queue
  db.py                 SQLite storage
  rubrics.py            rubric resolution
  charts.py             chart rendering
  cli.py                CLI client
tests/                  network-free tests
rubrics/                generic rubric
backup.py               verified SQLite backup
Dockerfile
docker-compose.yml      source build
docker-compose.release.yml image-only deployment
```

## Security and limitations

- Keep the API on a trusted LAN or put it behind TLS/reverse proxy if exposed.
- Use a dedicated `GRADER_API_TOKEN`; never reuse the OpenRouter key as the API
  token.
- URL ingestion blocks obvious private/link-local targets, but does not pin DNS
  against rebinding.
- `generic_research_v1` is editorial scoring, not independent fact-checking.
- LLM judge scores are not a substitute for human calibration.
- Provider fallback can change quantization and cost. Grader intentionally
  does not filter by quantization or price; the model ID stays pinned.
- The LDR watcher only auto-grades jobs completed after its baseline.
- The image architecture is the build host architecture; use `docker buildx`
  for multi-arch releases.

## License

MIT. See [`LICENSE`](LICENSE).
