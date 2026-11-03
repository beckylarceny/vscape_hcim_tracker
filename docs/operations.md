# Operations

This page covers installation, normal operation, backups, restores and upgrades.

## Prerequisites

- Docker Engine with the Compose plugin.
- A Docker named volume for PostgreSQL data, named `pgdata`.
- Network access to `vidyaplace.org`.
- Linux is assumed by the container entrypoint. It uses `setpriv` and numeric
  uid/gid ownership.

## First start

```sh
cp docker/.env.example docker/.env
# edit docker/.env and set POSTGRES_PASSWORD
docker compose -f docker/compose.yaml up -d --build
```

The compose file is `docker/compose.yaml`. Its build context is the repository
root, so the image can copy `config/`, `src/`, `tests/` and
`scripts/internal/docker-entrypoint.sh`.

The compose project loads `docker/.env` automatically. To use compose commands
from the repository root without `-f`, export the file once per shell:

```sh
export COMPOSE_FILE=docker/compose.yaml
docker compose ps
```

Scripts that call compose set `COMPOSE_FILE` themselves.

## What starts

Compose starts three services:

| Service | Port | Notes |
| --- | --- | --- |
| `db` | internal 5432 | PostgreSQL 17. Waits for a healthy database before the app starts. |
| `collector` | none | Runs `python -m src.collector --loop`. |
| `web` | host 8000 | Runs `python -m src.web`, serves `reports/` and rebuilds reports. |

The collector and web service both create the schema if needed. The first
collector run can take hours because it backfills about 644 days. The web
service serves empty or partial reports immediately.

URLs:

- general report: <http://localhost:8000/>
- status report: <http://localhost:8000/status>
- priority report: <http://localhost:8000/priority/>
- text versions: `reports/latest.txt`, `reports/status.txt`

## Renaming from vscape_hcim_tracker

The project and Compose project are now `vscape_tracker`. Existing data lives in
the old `vscape_hcim_tracker_pgdata` volume. Copy it before the first start:

```sh
docker compose -p vscape_hcim_tracker -f docker/compose.yaml down
docker volume create vscape_tracker_pgdata
docker run --rm \
  -v vscape_hcim_tracker_pgdata:/from:ro \
  -v vscape_tracker_pgdata:/to \
  alpine:3 sh -c 'cp -a /from/. /to/'
```

Do not use `docker compose down -v`. It removes the old data volume.

Update `docker/.env` to the new names:

| Old | New |
| --- | --- |
| `VSCAPE_HCIM_TRACKER_LOG_LEVEL` | `VSCAPE_TRACKER_LOG_LEVEL` |
| `VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS` | `VSCAPE_TRACKER_PRIORITY_PLAYERS` |
| `VSCAPE_HCIM_TRACKER_BASE_URL` | `VSCAPE_TRACKER_BASE_URL` |
| `COMPOSE_PROJECT_NAME=vscape_hcim_tracker` | `COMPOSE_PROJECT_NAME=vscape_tracker` |

Build the new image and start the renamed project:

```sh
./scripts/rebuild.sh
```

## Daily commands

```sh
./scripts/start.sh
./scripts/stop.sh
./scripts/restart.sh
./scripts/rebuild.sh
./scripts/logs.sh
./scripts/logs.sh web
./scripts/ps.sh
```

`logs.sh` follows one service and defaults to `collector`. `ps.sh` accepts the
same options as `docker compose ps`.

With `COMPOSE_FILE` exported, drop the `-f` argument.

### Script layout

Basic service commands stay in `scripts/`:

- `start.sh`, `stop.sh`, `restart.sh`
- `update.sh`, `rebuild.sh`
- `logs.sh`, `ps.sh`

Specialized commands are in `scripts/maintenance/`:

- `setup.sh`, `reset-config.sh`
- `backup.sh`, `restore.sh`
- `install-cron.sh`

The container entrypoint is `scripts/internal/docker-entrypoint.sh`.

## Updates

```sh
./scripts/update.sh
```

The script:

1. Runs `git pull --ff-only`.
2. Writes `version` with the short commit hash and UTC time.
3. Copies it to `reports/version`.
4. Rebuilds the collector image.
5. Recreates `collector` and `web`.
6. Leaves `db` running.

The version string appears in every page footer and at the end of
`reports/latest.txt`.

To install a daily 04:00 update job:

```sh
./scripts/maintenance/install-cron.sh
./scripts/maintenance/install-cron.sh --remove
```

The cron line calls `scripts/update.sh` and appends output to `update.log` in the
repository root.

## Testing a branch

```sh
./scripts/rebuild.sh
```

The script:

1. Skips `git pull`.
2. Writes `version` with the short commit hash, branch name and UTC time.
3. Rebuilds the collector image from the current checkout.
4. Recreates `collector` and `web`.
5. Leaves `db` running.

The hash gets a `-dirty` suffix when the checkout has uncommitted changes,
ignoring `version` itself. The branch name appears in the page footer version
line. If the Docker socket requires root, the script runs the compose commands
through `sudo`.

## Backups

```sh
./scripts/maintenance/backup.sh
```

The script needs the `db` service running. It reads `POSTGRES_DB` from the
container, writes `pg_dump -Fc` output to `backups/`, and names the file with a
UTC timestamp. `BACKUP_DIR` changes the destination.

The file is written as a `.part` file and renamed only after a non-empty dump
exists.

## Restores

```sh
./scripts/maintenance/restore.sh                 # newest dump in backups/
./scripts/maintenance/restore.sh backups/file.dump
```

The script:

1. Finds the newest `.dump` file when no path is given.
2. Checks that `db` is running.
3. Copies the whole backup directory into `/tmp/restore` in `db`.
4. Stops `collector` and `web` unless `RESTORE_KEEP_SERVICES=1`.
5. Runs `pg_restore --clean --if-exists --no-owner --no-privileges`.
6. Prints player and snapshot counts.
7. Removes the staged files and restarts the stopped services.

`RESTORE_DB` selects another database name. Without it, the dump is restored into
`POSTGRES_DB`.

Restoring drops everything written after the dump. Take a fresh backup before a
restore if the current data matters.

## Schema creation

The services create the schema at startup. To run it once manually:

```sh
docker compose -f docker/compose.yaml exec collector python -m src.db
```

Outside Docker:

```sh
python -m src.db --dsn "postgresql://user:pass@host:5432/xp_tracker"
```

Schema creation is idempotent. It uses `CREATE TABLE IF NOT EXISTS`, `CREATE
INDEX IF NOT EXISTS` and guarded `ALTER TABLE` statements.

## Password rotation

`POSTGRES_PASSWORD` only initializes the `pgdata` volume. Changing `docker/.env`
after the volume exists does not change the role password.

Option 1, recreate the volume and lose all xp:

```sh
docker compose -f docker/compose.yaml down -v
# update docker/.env
docker compose -f docker/compose.yaml up -d --build
```

Option 2, change the role password in place:

```sh
docker compose -f docker/compose.yaml exec db psql -U xp_tracker -d xp_tracker \
  -c "ALTER ROLE xp_tracker PASSWORD 'new-password';"
```

Then update `POSTGRES_PASSWORD` in `docker/.env` and recreate the app services:

```sh
docker compose -f docker/compose.yaml up -d --force-recreate collector web
```

## Reports directory

`reports/` is bind-mounted from the host. The container entrypoint creates it if
missing and changes ownership to uid 1000 at startup. The web service writes
files as uid 1000.

To clear generated reports, stop `web` and remove its contents:

```sh
docker compose -f docker/compose.yaml stop web
rm -rf reports/*
docker compose -f docker/compose.yaml start web
```

`version` is maintained by `scripts/update.sh` and `scripts/rebuild.sh`, and is
recreated on the next rebuild.

## Docker socket access

`web` mounts `/var/run/docker.sock` read-only so the status page can list compose
containers. Read access to the Docker socket is equivalent to host root. Use
this only on trusted hosts.

To disable the mount, remove it from `docker/compose.yaml`. The status page
degrades to `docker api unavailable`.

`VSCAPE_TRACKER_DOCKER_SOCKET` changes the socket path.
`VSCAPE_TRACKER_COMPOSE_PROJECT` changes the project name used when the
container cannot identify its own project.

## Health checks

Run these in order when something looks wrong:

```sh
docker compose -f docker/compose.yaml ps
docker compose -f docker/compose.yaml logs --tail=100 db
docker compose -f docker/compose.yaml logs --tail=100 collector
docker compose -f docker/compose.yaml logs --tail=100 web
curl -fsS http://localhost:8000/status >/dev/null
```

Check `reports/status.txt` for:

- database connection and latency.
- newest checkpoints and next fetch times.
- recent run status, requests and failures.
- table row estimates.
- coverage across the lookback window.

Common problems:

| Symptom | Check |
| --- | --- |
| Web responds but pages are empty | Collector has not discovered players yet, or the database has no snapshots. Check collector logs. |
| Status says database unreachable | `db` health, `POSTGRES_PASSWORD`, and `PG*` values. |
| Status says docker api unavailable | Socket mount, socket permissions, or the `VSCAPE_TRACKER_COMPOSE_PROJECT` fallback. |
| Collector logs many failures | API limits, rate, network access, or HTTP 503 responses. Lower `--rate` or `--workers`. |
| Reports are stale | `web` log, report interval, and disk permissions on `reports/`. |
| No priority pages | Priority thread only runs with `--loop`; check the collector command. |
| Container cannot write reports | Host `reports/` ownership. The entrypoint fixes ownership only when started as root. |

## Related documentation

- [Configuration](configuration.md)
- [Architecture](architecture.md)
- [Reports](reports.md)
