#!/bin/sh
set -eu

cd "$(dirname "$0")"

backup_dir=${BACKUP_DIR:-backups}
mkdir -p "$backup_dir"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
db=$(docker compose exec -T db sh -c 'printf %s "$POSTGRES_DB"')
file="${backup_dir}/${db}_${stamp}.dump"
tmp="${file}.part"

cleanup() {
    rm -f "$tmp"
}
trap cleanup EXIT

docker compose exec -T db sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$tmp"

if [ ! -s "$tmp" ]; then
    echo "backup is empty" >&2
    exit 1
fi

mv "$tmp" "$file"
trap - EXIT
echo "wrote $file ($(du -h "$file" | cut -f1))"
