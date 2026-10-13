#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export FASHION_FINDER_DATA_DIR="${FASHION_FINDER_DEMO_DATA_DIR:-$PWD/data/demo}"
export FASHION_FINDER_API_PORT="${FASHION_FINDER_DEMO_API_PORT:-8300}"
export FASHION_FINDER_WEB_PORT="${FASHION_FINDER_DEMO_WEB_PORT:-3300}"
export FASHION_FINDER_UID="$(id -u)"
export FASHION_FINDER_GID="$(id -g)"
if [[ -z "${FASHION_FINDER_SECRET_KEY:-}" ]]; then
  FASHION_FINDER_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  export FASHION_FINDER_SECRET_KEY
fi

mkdir -p "$FASHION_FINDER_DATA_DIR"
docker compose -f compose.yaml -f compose.demo.yaml build api web
docker compose -f compose.yaml -f compose.demo.yaml run --rm demo-seed
docker compose -f compose.yaml -f compose.demo.yaml up -d --no-build

printf 'Local synthetic demo: http://127.0.0.1:%s\n' "$FASHION_FINDER_WEB_PORT"
printf 'API health: http://127.0.0.1:%s/api/v1/health\n' "$FASHION_FINDER_API_PORT"
