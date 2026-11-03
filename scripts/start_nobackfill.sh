#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

mode=retry
case "${1:-}" in
    "")
        ;;
    --live-only)
        mode=live
        ;;
    *)
        echo "usage: $0 [--live-only]" >&2
        exit 2
        ;;
esac

set -- -f docker/compose.yaml
if [ "$mode" = "live" ]; then
    set -- "$@" -f docker/compose.liveonly.yaml
else
    set -- "$@" -f docker/compose.nobackfill.yaml
fi

docker compose "$@" up -d

docker compose "$@" ps
