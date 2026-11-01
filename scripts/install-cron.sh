#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
project=$(pwd)
update="$project/scripts/update.sh"
log="$project/update.log"
line="0 4 * * * PATH=/usr/local/bin:/usr/bin:/bin $update >> $log 2>&1"

if ! command -v crontab >/dev/null 2>&1; then
    echo "crontab not found; install cron first" >&2
    exit 1
fi

existing=$(crontab -l 2>/dev/null || true)

if [ "${1:-}" = "--remove" ]; then
    printf '%s\n' "$existing" | grep -vF "$update" | crontab - || true
    echo "removed the daily update job"
    exit 0
fi

{
    printf '%s\n' "$existing" | grep -vF "$update" || true
    printf '%s\n' "$line"
} | sed '/^$/d' | crontab -

echo "installed the daily update job:"
crontab -l | grep -F "$update"
