# Grader — portable deployment

This is the handoff guide for running the same Grader image on another machine.
It assumes Docker Engine with Compose v2. The service has no dependency on this
Hermes installation, on LDR, or on any host-specific path.

## What you receive

At minimum:

```text
grader-0.2.8.tar.gz
docker-compose.release.yml
.env.example
README-DOCKER.md
INTEGRATION.md
PORTABLE.md
```

`config.json` is optional; the service starts with built-in defaults.

## First run

```bash
mkdir -p ~/grader
cd ~/grader

# copy the image and files here, then:
gunzip -c grader-0.2.8.tar.gz | docker load

cp .env.example .env
# Edit .env:
#   OPENROUTER_API_KEY=...
#   GRADER_API_TOKEN=...       (openssl rand -hex 32)

# If your user is not UID/GID 1000:
#   echo "GRADER_UID=$(id -u)" >> .env
#   echo "GRADER_GID=$(id -g)" >> .env

mkdir -p state
docker compose -f docker-compose.release.yml up -d
curl -sS http://127.0.0.1:9131/health/ready
```

The service is ready when `/health/ready` returns `database=ok` and
`worker=ok`.

## Required configuration

Only two values are strictly required:

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | One key for all three judge roles |
| `GRADER_API_TOKEN` | Token for your clients; generate with `openssl rand -hex 32` |

Everything else has a default. `config.json` is optional; `{}` is sufficient.

## Your own LDR

LDR is disabled by default. If you want automatic LDR grading:

```dotenv
GRADER_LDR_ENABLED=true
GRADER_LDR_BASE=http://127.0.0.1:5000
LDR_USER=your-ldr-user
LDR_PASS=<your-ldr-password>
```

If LDR runs in another container/host, use its reachable URL instead of
`127.0.0.1`. The adapter baselines existing history on first start and grades
jobs completed afterward. You can also submit an LDR job manually:

```bash
curl -sS http://127.0.0.1:9131/v1/papers/from-ldr/JOB_ID \
  -H "X-API-Token: $GRADER_API_TOKEN"
```

## Different pipelines

Nothing needs to know about LDR. Submit any report:

```bash
curl -sS http://127.0.0.1:9131/v1/papers \
  -H "X-API-Token: $GRADER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "source_type": "text",
    "title": "My report",
    "content": "# Report\n...",
    "grade": true,
    "wait": true
  }'
```

Supported inputs: `text`, `url`, `file` path, `file` with
`content_base64` + `filename`, and optional `ldr`. See `INTEGRATION.md`.

## Custom rubrics

Mount a read-only directory:

```yaml
volumes:
  - "./state:/data"
  - "./custom-rubrics:/config/rubrics:ro"
```

```dotenv
GRADER_RUBRIC_OVERLAY=/config/rubrics
```

Add `custom-rubrics/<rubric_id>.md`, then submit with
`"rubric_id":"<rubric_id>"`. The default is `generic_research_v1`.

## Hermes plugin

If this Grader is used by a Hermes agent, install/enable the `grader` plugin and
point it at the service:

```dotenv
GRADER_BASE_URL=http://<grader-host>:9131
GRADER_API_TOKEN=<same token as the service>
```

The plugin reads `GRADER_BASE_URL` and `GRADER_API_TOKEN` from its environment.
The CLI reads `GRADER_BASE_URL` and `GRADER_TOKEN_FILE`.

## Updating

Keep the old image tag loaded, then:

```bash
gunzip -c grader-0.2.8.tar.gz | docker load
# change GRADER_IMAGE in .env to grader:0.2.8
docker compose -f docker-compose.release.yml up -d
```

Rollback by changing `GRADER_IMAGE` back to `grader:0.2.8`. State remains in
`state/`; take a backup before schema-changing releases:

```bash
docker compose -f docker-compose.release.yml exec -T grader python backup.py
```

## Architecture

The published image is built for the architecture of the machine that built it.
For an ARM host, build a multi-arch image:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t grader:0.2.8 .
```

or build directly on the target host.

## Ports and security

The default publish is `0.0.0.0:9131`; set `GRADER_BIND_IP=127.0.0.1` for
loopback only. No TLS is included; use a trusted LAN, Tailscale, or a reverse
proxy for anything wider.
