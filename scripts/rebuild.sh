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

if docker info >/dev/null 2>&1; then
    compose() { docker compose "$@"; }
else
    echo "docker socket needs root, using sudo"
    compose() { sudo docker compose "$@"; }
fi

echo "building $branch at $hash"
compose build collector

echo "recreating the containers"
compose up -d --force-recreate collector web

compose ps
