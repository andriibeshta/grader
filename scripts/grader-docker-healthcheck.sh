#!/usr/bin/env bash
# Restart the grader container after repeated Docker healthcheck failures.
# One small host-side timer, not an autoheal container or monitoring stack.
set -u
ROOT="${GRADER_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE="${GRADER_STATE_DIR:-$ROOT/state}"
COMPOSE_FILE="${GRADER_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
mkdir -p "$STATE"
COUNT_FILE="$STATE/docker-unhealthy.count"

cid=$(docker ps -q --filter "label=com.docker.compose.project=grader" --filter "label=com.docker.compose.service=grader" | head -1)
if [ -z "$cid" ]; then
  exited=$(docker compose -f "$COMPOSE_FILE" ps -a -q 2>/dev/null | head -1)
  if [ -n "$exited" ]; then
    echo "grader container is exited; starting it"
    docker compose -f "$COMPOSE_FILE" up -d
  else
    echo "grader container is not present; nothing to do"
  fi
  exit 0
fi

status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)
echo "grader container $cid status=$status"

if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
  rm -f "$COUNT_FILE"
  exit 0
fi

count=0
[ -f "$COUNT_FILE" ] && count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT_FILE"

if [ "$count" -lt 3 ]; then
  echo "unhealthy check $count/3; waiting"
  exit 1
fi

echo "restarting grader after 3 unhealthy checks"
docker compose -f "$COMPOSE_FILE" restart grader
rm -f "$COUNT_FILE"
exit 0
