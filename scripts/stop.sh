#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

docker compose stop

docker compose ps
