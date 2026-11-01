#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

echo "pulling from origin"
git pull --ff-only

echo "building the image"
docker compose build collector

echo "restarting the containers"
docker compose up -d --force-recreate collector web

docker compose ps
