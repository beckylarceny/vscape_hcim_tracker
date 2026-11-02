#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker/compose.yaml
export COMPOSE_FILE

docker compose up -d

docker compose ps
