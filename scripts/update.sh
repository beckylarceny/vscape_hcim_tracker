#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

COMPOSE_FILE=docker/compose.yaml
export COMPOSE_FILE

echo "pulling from origin"
git pull --ff-only

hash=$(git rev-parse --short HEAD)
stamp=$(date -u '+%Y-%m-%d %H:%M UTC')
printf '%s %s\n' "$hash" "$stamp" > version
mkdir -p reports
cp version reports/version

echo "building the image"
docker compose build collector

echo "restarting the containers"
docker compose up -d --force-recreate collector web

docker compose ps
