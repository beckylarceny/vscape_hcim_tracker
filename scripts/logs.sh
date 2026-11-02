#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker/compose.yaml
export COMPOSE_FILE

service=${1:-collector}

docker compose logs -f "$service"
