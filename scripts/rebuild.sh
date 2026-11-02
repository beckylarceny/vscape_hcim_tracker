#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker/compose.yaml
export COMPOSE_FILE

branch=$(git rev-parse --abbrev-ref HEAD)
hash=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain -- . ':(exclude)version')" ]; then
    hash="$hash-dirty"
fi
stamp=$(date -u '+%Y-%m-%d %H:%M UTC')

printf '%s %s %s\n' "$hash" "$branch" "$stamp" > version
mkdir -p reports
cp version reports/version

echo "building $branch at $hash"
docker compose build collector

echo "recreating the containers"
docker compose up -d --force-recreate collector web

docker compose ps
