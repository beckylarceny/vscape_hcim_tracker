#!/bin/sh
set -eu

cd "$(dirname "$0")/../.."

if command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    PYTHON=python3
fi

OLD_PROJECT=${OLD_PROJECT:-}
NEW_PROJECT=${NEW_PROJECT:-vscape_tracker}
replace_new_volume=0
skip_backup=0
backup=
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

usage() {
    cat >&2 <<'EOF'
usage: ./scripts/maintenance/migrate.sh [--replace-new-volume] [--skip-backup]

Migrate a running vscape_hcim_tracker Compose instance to vscape_tracker.

Options:
  --replace-new-volume  Remove an existing vscape_tracker_pgdata volume.
                        Use only after a failed first start, and only when the
                        new volume has no data worth keeping.
  --skip-backup         Skip the database dump. Use only when a recent backup
                        already exists.

Environment:
  OLD_PROJECT  Old Compose project. Defaults to COMPOSE_PROJECT_NAME in
               docker/.env, then vscape_hcim_tracker.
  NEW_PROJECT  New Compose project. Defaults to vscape_tracker.
  OLD_VOLUME   Old volume. Defaults to ${OLD_PROJECT}_pgdata.
  NEW_VOLUME   New volume. Defaults to ${NEW_PROJECT}_pgdata.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --replace-new-volume)
            replace_new_volume=1
            ;;
        --skip-backup)
            skip_backup=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
    shift
done

if [ ! -f docker/compose.yaml ] ||
        [ ! -f config/collector.yaml ] ||
        [ ! -f config/web.yaml ]; then
    echo "error: run this script from the repository root" >&2
    exit 1
fi

if [ -z "$OLD_PROJECT" ] && [ -f docker/.env ]; then
    OLD_PROJECT=$(
        sed -n 's/^COMPOSE_PROJECT_NAME=//p' docker/.env | tail -n 1
    )
fi
OLD_PROJECT=${OLD_PROJECT:-vscape_hcim_tracker}

if [ "$OLD_PROJECT" = "$NEW_PROJECT" ]; then
    echo "error: OLD_PROJECT and NEW_PROJECT are both $NEW_PROJECT" >&2
    exit 1
fi

OLD_VOLUME=${OLD_VOLUME:-${OLD_PROJECT}_pgdata}
NEW_VOLUME=${NEW_VOLUME:-${NEW_PROJECT}_pgdata}

if docker info >/dev/null 2>&1; then
    SUDO=
    compose() { docker compose "$@"; }
    docker_cmd() { docker "$@"; }
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo "error: the Docker socket needs root and sudo is unavailable" >&2
        exit 1
    fi
    echo "the Docker socket needs root, using sudo"
    SUDO="sudo "
    compose() { sudo docker compose "$@"; }
    docker_cmd() { sudo docker "$@"; }
fi

update_job="$PWD/scripts/update.sh"
if command -v crontab >/dev/null 2>&1 &&
        crontab -l 2>/dev/null | grep -Fq "$update_job"; then
    echo "error: the daily update job is still installed" >&2
    echo "remove it first: ./scripts/maintenance/install-cron.sh --remove" >&2
    exit 1
fi

if ! docker_cmd volume inspect "$OLD_VOLUME" >/dev/null 2>&1; then
    echo "error: old volume not found: $OLD_VOLUME" >&2
    echo "volumes containing pgdata:" >&2
    docker_cmd volume ls --format '{{.Name}}' | grep pgdata >&2 || true
    exit 1
fi

if docker_cmd volume inspect "$NEW_VOLUME" >/dev/null 2>&1; then
    if [ "$replace_new_volume" != 1 ]; then
        echo "error: new volume already exists: $NEW_VOLUME" >&2
        echo "if it is empty, rerun with --replace-new-volume" >&2
        exit 1
    fi
    echo "stopping the new project before replacing its volume"
    compose -p "$NEW_PROJECT" -f docker/compose.yaml down >/dev/null 2>&1 ||
        true
    docker_cmd volume rm "$NEW_VOLUME" >/dev/null
fi

if compose -p "$NEW_PROJECT" -f docker/compose.yaml ps -q 2>/dev/null |
        grep -q .; then
    if [ "$replace_new_volume" != 1 ]; then
        echo "error: the new project already has containers" >&2
        echo "stop it first or rerun with --replace-new-volume" >&2
        exit 1
    fi
    echo "stopping the new project before continuing"
    compose -p "$NEW_PROJECT" -f docker/compose.yaml down >/dev/null
fi

if [ "$skip_backup" = 1 ]; then
    echo "skipping the database backup"
else
    if ! compose -p "$OLD_PROJECT" -f docker/compose.yaml \
            ps --status running --services | grep -qx db; then
        echo "error: the old db service is not running under $OLD_PROJECT" >&2
        echo "start the old stack or rerun with --skip-backup" >&2
        exit 1
    fi

    mkdir -p backups
    dbname=$(
        compose -p "$OLD_PROJECT" -f docker/compose.yaml exec -T db \
            sh -c 'printf %s "$POSTGRES_DB"'
    )
    dbname=${dbname:-xp_tracker}
    backup="backups/${dbname}_${STAMP}.dump"
    tmp="${backup}.part"
    trap 'rm -f "$tmp"' EXIT
    compose -p "$OLD_PROJECT" -f docker/compose.yaml exec -T db sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
        > "$tmp"
    if [ ! -s "$tmp" ]; then
        echo "error: the database backup is empty" >&2
        exit 1
    fi
    mv "$tmp" "$backup"
    trap - EXIT
    echo "wrote $backup"
fi

mkdir -p backups/upgrade
cp -a config "backups/upgrade/config-${STAMP}"
if [ -f docker/.env ]; then
    cp -a docker/.env "backups/upgrade/docker-${STAMP}.env"
    sed -i 's/^VSCAPE_HCIM_TRACKER_/VSCAPE_TRACKER_/' docker/.env
    sed -i \
        "s/^COMPOSE_PROJECT_NAME=$OLD_PROJECT$/COMPOSE_PROJECT_NAME=$NEW_PROJECT/" \
        docker/.env
else
    echo "warning: docker/.env not found; update external environment variables"
    echo "         with the VSCAPE_TRACKER_ prefix manually" >&2
fi

"$PYTHON" - config/collector.yaml <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
original = list(lines)


def drop_block(lines, key):
    result = []
    skipping = False
    for line in lines:
        if skipping:
            if not line.strip():
                continue
            if line[:1].isspace():
                continue
            skipping = False
        if line.startswith(f"{key}:"):
            skipping = True
            continue
        result.append(line)
    return result


lines = drop_block(lines, "account_filter_codes")
lines = drop_block(lines, "urls")
if not any(line.startswith("account_types:") for line in lines):
    index = next(
        (i for i, line in enumerate(lines) if line.startswith("defaults:")),
        len(lines),
    )
    lines.insert(index, "account_types: [HARDCORE_IRONMAN]")
    if index < len(lines) - 1:
        lines.insert(index + 1, "")

if lines != original:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"updated {path}")
else:
    print(f"no changes needed for {path}")
PY

"$PYTHON" - config/web.yaml <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()

if not any(line.strip().startswith("site_title:") for line in lines):
    index = next(
        (i for i, line in enumerate(lines) if line.startswith("defaults:")),
        None,
    )
    if index is None:
        lines[0:0] = ["defaults:", '  site_title: "vidyascape tracker"', ""]
    else:
        lines.insert(index + 1, '  site_title: "vidyascape tracker"')
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"updated {path}")
else:
    print(f"no changes needed for {path}")
PY

echo "stopping the old project: $OLD_PROJECT"
compose -p "$OLD_PROJECT" -f docker/compose.yaml down

echo "copying $OLD_VOLUME to $NEW_VOLUME"
docker_cmd volume create "$NEW_VOLUME" >/dev/null
docker_cmd run --rm \
    -v "$OLD_VOLUME:/from:ro" \
    -v "$NEW_VOLUME:/to" \
    postgres:17-alpine \
    sh -c 'cp -a /from/. /to/'

echo "building and starting the new project: $NEW_PROJECT"
./scripts/rebuild.sh

if command -v curl >/dev/null 2>&1; then
    echo "waiting for the web service"
    count=0
    while [ "$count" -lt 30 ]; do
        if curl -fsS --max-time 5 \
                http://localhost:8000/status >/dev/null 2>&1; then
            echo "the web service is responding"
            break
        fi
        count=$((count + 1))
        sleep 2
    done
    if [ "$count" -eq 30 ]; then
        echo "warning: the web service did not respond within 60 seconds" >&2
    fi
fi

echo
echo "migration complete"
echo "old project: $OLD_PROJECT"
echo "old volume kept: $OLD_VOLUME"
echo "new volume: $NEW_VOLUME"
if [ -n "$backup" ]; then
    echo "database backup: $backup"
fi
echo
echo "verify:"
echo "  ${SUDO}./scripts/ps.sh"
echo "  curl -fsS http://localhost:8000/status"
echo
echo "after verification, remove the old resources:"
echo "  ${SUDO}docker image rm vscape_hcim_tracker-app:latest"
echo "  ${SUDO}docker volume rm $OLD_VOLUME"
echo
echo "rollback before cleanup:"
echo "  ${SUDO}docker compose -p $NEW_PROJECT -f docker/compose.yaml down"
echo "  git checkout <old-commit>"
echo "  ${SUDO}docker compose -p $OLD_PROJECT -f docker/compose.yaml up -d"
