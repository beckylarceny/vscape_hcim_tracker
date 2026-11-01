#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

backup_dir=${BACKUP_DIR:-backups}
service=db
staged=/tmp/restore

if [ ! -d "$backup_dir" ]; then
    echo "no backup directory: $backup_dir" >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    name=$(basename "$1")
else
    name=$(find "$backup_dir" -maxdepth 1 -type f -name '*.dump' \
        -printf '%T@ %f\n' | sort -rn | head -n 1 | cut -d' ' -f2-)
fi

if [ -z "$name" ]; then
    echo "no .dump file in $backup_dir" >&2
    exit 1
fi

if [ ! -s "$backup_dir/$name" ]; then
    echo "missing backup file: $backup_dir/$name" >&2
    exit 1
fi

if ! docker compose ps --status running --services | grep -qx "$service"; then
    echo "the $service service is not running" >&2
    exit 1
fi

echo "restoring $backup_dir/$name"

# Copy the whole backup directory into the container, then restore from it.
docker compose cp "$backup_dir" "$service:$staged"

docker compose exec -T -e DUMP="$staged/$name" "$service" sh -c \
    'test -s "$DUMP"' </dev/null || {
    echo "backup not found in the container: $staged/$name" >&2
    exit 1
}

paused=0
cleanup() {
    docker compose exec -T "$service" rm -rf "$staged" </dev/null >/dev/null 2>&1 || true
    if [ "$paused" = 1 ]; then
        docker compose start collector web >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# Stop writers so the restore is not racing live collection.
if [ "${RESTORE_KEEP_SERVICES:-0}" != "1" ]; then
    docker compose stop collector web >/dev/null
    paused=1
fi

docker compose exec -T \
    -e DUMP="$staged/$name" \
    -e TARGET_DB="${RESTORE_DB:-}" \
    "$service" sh -c '
    db=${TARGET_DB:-$POSTGRES_DB}
    PGPASSWORD="$POSTGRES_PASSWORD" pg_restore \
        -U "$POSTGRES_USER" -d "$db" \
        --clean --if-exists --no-owner --no-privileges "$DUMP"
    echo "restored $DUMP into $db"
    PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$db" -Atc \
        "SELECT '"'"'players='"'"' || count(*) FROM players
         UNION ALL
         SELECT '"'"'snapshots='"'"' || count(*) FROM snapshots"
' </dev/null
trap - EXIT
cleanup
