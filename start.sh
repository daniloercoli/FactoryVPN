#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -f .env ]]; then
  echo 'Create the deployment first: python -m scripts.bootstrap init --host <server-hostname>' >&2
  exit 1
fi
docker compose config --quiet
docker compose up --build -d
docker compose ps
