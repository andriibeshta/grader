# Grader Docker deployment

## What this is

The grader is a source-agnostic paper-grading service. It runs as one Docker
container with two bounded worker threads. LDR is an optional adapter; the
core API works without LDR or its credentials. Mutable state is bind-mounted
under the service user's home:

```text
~/grader/
├── docker-compose.yml          # source build
├── docker-compose.release.yml  # image-only deployment
├── Dockerfile
├── .env                        # secrets/config, mode 600; never commit
├── config.json                 # optional in the image-only deployment
├── state/                      # bind mount -> container /data
│   ├── grades.db
│   ├── reports/
│   ├── backups/
│   ├── watcher_state.json
│   └── ldr_cookies
└── dist/                       # docker save artifacts
```

The container runs as a configurable UID/GID (`GRADER_UID`/`GRADER_GID`,
default 1000), uses a read-only root filesystem, drops all capabilities, and
publishes port 9131.

## Image-only deployment

For a machine that receives only the image, use `PORTABLE.md` and
`docker-compose.release.yml`. They do not require the source tree or a
`build:` context.

## First run

```bash
cd ~/.hermes/workspace/grader
cp .env.example .env   # fill required secrets, then chmod 600
mkdir -p state/reports state/backups
chmod 700 state

docker compose build
docker compose up -d
docker compose ps
curl -sS http://127.0.0.1:9131/health/live
curl -sS http://127.0.0.1:9131/health/ready
```

Detailed health and all data endpoints require the token from `.env`:

```bash
TOKEN=$(awk -F= '/^GRADER_API_TOKEN=/{print $2}' .env)
curl -sS -H "X-API-Token: $TOKEN" http://127.0.0.1:9131/health
```

## Configuration

All routine settings live in `.env`; they are passed explicitly by
`docker-compose.yml`. Edit `.env` and recreate the container:

```bash
docker compose up -d --force-recreate
```

| Variable | Meaning | Default |
|---|---|---|
| `GRADER_BIND_IP` | Host interface to publish on | `0.0.0.0` |
| `GRADER_PUBLISH_PORT` | Host port | `9131` |
| `GRADER_HOST` | Listen address inside the container | `0.0.0.0` |
| `GRADER_PORT` | Internal API port | `9131` |
| `GRADER_STATE_DIR` | Host state path | `./state` |
| `GRADER_CONFIG_FILE` | Host config file | `./config.json` |
| `GRADER_WORKER_THREADS` | Grading workers | `2` |
| `GRADER_JUDGE_ATTEMPTS` | Retries per judge call | `4` |
| `GRADER_LDR_BASE` | LDR API base (used only when enabled) | `http://127.0.0.1:5000` |
| `GRADER_LDR_ENABLED` | Enable the optional LDR watcher | `false` |
| `GRADER_OPENROUTER_BASE` | OpenRouter API base | `https://openrouter.ai/api/v1` |
| `GRADER_PRIMARY_MODEL` | Primary judge model | `deepseek/deepseek-v4-flash` |
| `GRADER_SECONDARY_MODEL` | Second judge model | `z-ai/glm-5.3-flash` |
| `GRADER_TIEBREAKER_MODEL` | Tiebreaker model | `deepseek/deepseek-v4-pro` |
| `OPENROUTER_API_KEY` | One key for all three models | required |
| `GRADER_API_TOKEN` | Local API token | required |
| `LDR_USER` / `LDR_PASS` | LDR login; only needed when LDR is enabled | optional |
| `GRADER_RUBRIC_OVERLAY` | Read-only custom rubric directory | unset |
| `GRADER_QUERIES_FILE` | Optional custom query map | bundled map |

There is intentionally no `DEEPSEEK_API_KEY`: all three models use
OpenRouter. Failures are visible in `docker compose logs`, `/health`, and the
job record.

## Custom rubrics and non-LDR pipelines

The bundled rubric is `generic_research_v1`; no LDR content is required. To
add a rubric without rebuilding:

```bash
mkdir -p custom-rubrics
# custom-rubrics/<rubric_id>.md
```

Mount the directory and point the service at it in `.env`:

```dotenv
GRADER_RUBRIC_OVERLAY=/config/rubrics
```

```yaml
volumes:
  - "./state:/data"
  - "./config.json:/config/config.json:ro"
  - "./custom-rubrics:/config/rubrics:ro"
```

A custom `queries.json` can also be mounted and used with
`GRADER_QUERIES_FILE=/config/rubrics/queries.json`. Unknown queries fall back
to `generic_research_v1`, so arbitrary pipelines work without a query map.

`POST /v1/papers` is the integration surface for non-LDR pipelines. Use
`source_type=text`, `url`, or `file` (path or `content_base64` + `filename`).
See `INTEGRATION.md` for the request/response contract.

## LAN access

Compose publishes `9131:9131`. Use the token for everything except
`/health/live` and `/health/ready`. The host firewall is intentionally left to
the operator; restrict 9131 to trusted VLANs.

No TLS is configured for the trusted LAN. Add Tailscale Serve or Caddy if the
service is ever exposed outside it.

## Self-healing

- `restart: always` handles process exit and daemon restarts.
- Docker's `HEALTHCHECK` marks a hung container unhealthy.
- `grader-docker-healthcheck.timer` checks every 60 seconds, restarts after
  three consecutive unhealthy checks, and starts an exited container.
- For planned maintenance, use `docker compose down` (or stop the timer first);
  plain `docker stop` leaves an exited container that the watchdog will restart.
  Use `docker compose stop` or `docker stop -t 30` when you want the 30-second
  drain window; a bare `docker stop` uses Docker's shorter default.
- `docker pause` is not a maintenance mode: clients hang and the watchdog will
  restart the container after three unhealthy checks.

Inspect:

```bash
docker compose ps
docker inspect --format '{{.State.Health.Status}}' grader-grader-1
docker compose logs --tail=50
systemctl --user status grader-docker-healthcheck.timer
journalctl --user -u grader-docker-healthcheck.service
```

## Backups

`grader-docker-backup.timer` runs at 03:30 and calls the SQLite online backup
inside the container. Snapshots are written to `state/backups/`.

Manual backup and verification:

```bash
docker compose exec -T grader python backup.py
ls -lh state/backups
```

`backup.py` verifies the core tables and keeps the newest 30 snapshots.

## Move to the remote host

Use `docker save`/`docker load` for now:

```bash
# current VM
docker save grader:0.2.10 | gzip > dist/grader-0.2.10.tar.gz
sha256sum dist/grader-0.2.10.tar.gz

# copy the image and these files to the remote host
scp dist/grader-0.2.10.tar.gz remote-host:/tmp/
scp docker-compose.yml config.json .env remote-host:~/grader/

# remote host
gunzip -c /tmp/grader-0.2.10.tar.gz | docker load
cd ~/.hermes/workspace/grader
mkdir -p state/reports state/backups
# copy state/ from the old VM or restore the latest backup
docker compose up -d
```

The image contains no secrets. The bind mount path is under the service user's
home on both hosts. Add a private registry only when there are multiple hosts or
automated releases.

## Rollback

The native systemd unit remains installed. For the current VM:

```bash
docker compose down
systemctl --user enable --now grader.service
systemctl --user disable --now grader-docker-backup.timer grader-docker-healthcheck.timer
```

For the remote host, use the same image with the remote host data directory.
