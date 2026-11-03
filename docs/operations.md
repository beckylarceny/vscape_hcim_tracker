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

## Starting without backfill

Use `start_nobackfill.sh` for a new collector that should not rebuild the full
history:

```sh
./scripts/start_nobackfill.sh
```

This keeps the recent retry window. Failed or missed checkpoints inside that
window are still retried.

For true live-only collection, fetch only the current checkpoint:

```sh
./scripts/start_nobackfill.sh --live-only
```

Both modes start `db`, `collector` and `web`. The script selects a Compose
override file and leaves the base `docker/compose.yaml` unchanged.

## Upgrading from vscape_hcim_tracker

Use this when the instance was last updated before the project rename,
configurable account types and site title changes. The database schema does not
change.

### 1. Back up and pause updates

From the old checkout:

```sh
./scripts/maintenance/backup.sh
mkdir -p backups/upgrade
cp -a docker/.env backups/upgrade/docker.env
cp -a config backups/upgrade/config
./scripts/maintenance/install-cron.sh --remove
```

Keep the backup until the new instance is verified.

### 2. Stop the old stack

```sh
docker compose -f docker/compose.yaml down
```

Do not use `down -v`. It removes the old database volume.

### 3. Copy the database volume

Find the old volume:

```sh
docker volume ls | grep pgdata
```

Expected names:

| Volume | Purpose |
| --- | --- |
| `vscape_hcim_tracker_pgdata` | Old data |
| `vscape_tracker_pgdata` | New project |

Copy the old volume to the new name:

```sh
docker volume create vscape_tracker_pgdata
docker run --rm \
  -v vscape_hcim_tracker_pgdata:/from:ro \
  -v vscape_tracker_pgdata:/to \
  alpine:3 sh -c 'cp -a /from/. /to/'
```

Use the actual old volume name when the old project name was customized.

If `vscape_tracker_pgdata` already exists from a failed first start, confirm it
is empty, stop the new stack and remove it before copying:

```sh
docker compose -p vscape_tracker -f docker/compose.yaml down
docker volume rm vscape_tracker_pgdata
```

### 4. Update the checkout

If `git status --short` lists files under `config/`, stash them first:

```sh
git stash push -- config
```

Then switch to `main` and fast-forward:

```sh
git checkout main
git pull --ff-only
```

Restore local config changes:

```sh
git stash pop
```

Resolve conflicts by keeping local values and adding the new keys from the next
step.

### 5. Update `docker/.env`

Rename these keys. Keep `POSTGRES_PASSWORD`, `POSTGRES_DB` and `POSTGRES_USER`
unchanged.

| Old | New |
| --- | --- |
| `VSCAPE_HCIM_TRACKER_LOG_LEVEL` | `VSCAPE_TRACKER_LOG_LEVEL` |
| `VSCAPE_HCIM_TRACKER_PRIORITY_PLAYERS` | `VSCAPE_TRACKER_PRIORITY_PLAYERS` |
| `VSCAPE_HCIM_TRACKER_BASE_URL` | `VSCAPE_TRACKER_BASE_URL` |
| `COMPOSE_PROJECT_NAME=vscape_hcim_tracker` | `COMPOSE_PROJECT_NAME=vscape_tracker` |

Rename the same prefix in any other environment variable you set outside
`docker/.env`:

| Old | New |
| --- | --- |
| `VSCAPE_HCIM_TRACKER_CONFIG_DIR` | `VSCAPE_TRACKER_CONFIG_DIR` |
| `VSCAPE_HCIM_TRACKER_DSN` | `VSCAPE_TRACKER_DSN` |
| `VSCAPE_HCIM_TRACKER_DOCKER_SOCKET` | `VSCAPE_TRACKER_DOCKER_SOCKET` |
| `VSCAPE_HCIM_TRACKER_COMPOSE_PROJECT` | `VSCAPE_TRACKER_COMPOSE_PROJECT` |
| `VSCAPE_HCIM_TRACKER_TEST_DSN` | `VSCAPE_TRACKER_TEST_DSN` |

If `COMPOSE_PROJECT_NAME` is not set, leave it unset. The compose file now sets
`name: vscape_tracker`.

### 6. Update `config/`

Edit `config/collector.yaml`:

- Remove `account_filter_codes`.
- Remove `urls`.
- Add:

```yaml
account_types: [HARDCORE_IRONMAN]
```

Keep `defaults` and `priority` unchanged. Do not lose `data_start_epoch` or
`priority.players`.

Edit `config/web.yaml` and add:

```yaml
defaults:
  site_title: "vidyascape tracker"
```

Do not run `setup.sh --defaults`. It overwrites active configuration values.
See [Configuration](configuration.md#collectoryaml) for other account types.

### 7. Build and start

```sh
./scripts/rebuild.sh
```

This builds `vscape_tracker-app:latest` and recreates `collector` and `web`.
PostgreSQL starts on the copied `vscape_tracker_pgdata` volume. The web service
rewrites the reports on startup, so no manual report cleanup is needed.

If the image is already built, start the services with:

```sh
./scripts/start.sh
```

### 8. Verify

```sh
./scripts/ps.sh
curl -fsS http://localhost:8000/status >/dev/null
```

Check:

- `reports/status.txt` shows the expected player and checkpoint counts.
- `reports/latest.txt` starts with your site title in uppercase.
- Page headers show the configured site title.
- The lookback still starts at your `data_start_epoch`.

Reinstall the daily update job after verification:

```sh
./scripts/maintenance/install-cron.sh
```

### 9. Port 8000 is already allocated

The old stack usually still holds the port:

```sh
sudo ss -ltnp 'sport = :8000'
sudo docker ps --filter publish=8000
```

If the listener is `vscape_hcim_tracker-web-1`, stop the old project:

```sh
docker compose -p vscape_hcim_tracker -f docker/compose.yaml down
```

Then start the new stack again.

### 10. Clean up after verification

Keep the old image and volume until the new instance is verified.

```sh
docker image rm vscape_hcim_tracker-app:latest
docker volume rm vscape_hcim_tracker_pgdata
```

### Rollback

Before cleanup, stop the new stack and start the old checkout:

```sh
docker compose -p vscape_tracker -f docker/compose.yaml down
git checkout <old-commit>
docker compose -p vscape_hcim_tracker -f docker/compose.yaml up -d
```

The old volume is unchanged until step 10. The backup from step 1 is the
fallback when the old checkout or image is no longer available.

## Daily commands

```sh
./scripts/start.sh
./scripts/start_nobackfill.sh
./scripts/start_nobackfill.sh --live-only
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

- `start.sh`, `start_nobackfill.sh`, `stop.sh`, `restart.sh`
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
