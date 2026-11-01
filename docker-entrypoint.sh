#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/reports
    chown -R 1000:1000 /app/reports
    exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
fi

exec "$@"
