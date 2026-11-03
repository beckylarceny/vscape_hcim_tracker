# Configuration

Configuration comes from YAML files, environment variables and command-line
flags. The services read YAML at import time, so restart them after changes.

## Configuration files

The default directory is `config/`:

```text
config/
  collector.yaml
  web.yaml
  db.yaml
  defaults/
    collector.yaml
    web.yaml
    db.yaml
```

`VSCAPE_TRACKER_CONFIG_DIR` replaces the whole directory. Relative overrides
are resolved from the process working directory, so an absolute path is safer in
containers.

`config/defaults/` is the reference copy. `scripts/maintenance/setup.sh` writes
editable copies into `config/`. `scripts/maintenance/reset-config.sh` copies the
defaults back.

```sh
./scripts/maintenance/setup.sh             # prompt for values
./scripts/maintenance/setup.sh --defaults  # copy defaults without prompting
./scripts/maintenance/reset-config.sh      # copy defaults over active files
```

The Docker image copies `config/` into `/app/config`. Compose also bind-mounts
the host `config/` directory read-only, so edits on the host apply after a
restart.

## collector.yaml

| Key | Type | Meaning |
| --- | --- | --- |
| `skills` | list of strings | Skills stored and reported. `overall` is included. |
| `account_filter_codes` | mapping | Named ironman codes. `HARDCORE_IRONMAN` must be `2`. |
| `urls.player_highscores` | string | Current xp URL. Supports `{username}`. |
| `urls.player_tracker` | string | Gained xp URL. Supports `{username}` and `{seconds}`. |
| `defaults.interval_seconds` | integer | Coarse backfill step. |
| `defaults.collect_interval_seconds` | integer | Live collection step. |
| `defaults.retry_window_seconds` | integer | Recent window always rechecked. |
| `defaults.data_start_epoch` | integer | Earliest known data timestamp. |
| `defaults.workers` | integer | Collector worker threads. |
| `defaults.rate` | number | Global HTTP requests per second. `0` disables pacing. |
| `priority.players` | list of strings | Extra priority players. |
| `priority.interval_seconds` | integer | Priority collection step. |
| `priority.retention_seconds` | integer | Priority history retention. |
| `priority.top` | integer | Number of top `overall` players to track. |

The shipped defaults are 24-hour backfill, 1-hour live collection, 7-day retry
window, 2 workers, 4 requests per second, 10-minute priority interval, 24-hour
priority retention and the top 5 overall players.

The priority scheduler and pruning use `priority.interval_seconds` and
`priority.retention_seconds` from the loaded config. Report labels and the feed
event cap still assume 10 minutes and 24 hours. Keep the default values unless
you also update the report code.

## web.yaml

| Key | Type | Meaning |
| --- | --- | --- |
| `defaults.report_interval_seconds` | integer | Report rebuild interval. `0` rebuilds only at startup. |
| `defaults.top_n` | integer | Rows per ranking table. |
| `defaults.base_url` | string | Public URL used in RSS links. |
| `spans` | mapping | Named report spans in seconds. |

Default spans:

| Span | Seconds |
| --- | --- |
| `day` | 86400 |
| `week` | 604800 |
| `month` | 2592000 |
| `year` | 31536000 |

## db.yaml

| Key | Type | Meaning |
| --- | --- | --- |
| `host` | string | PostgreSQL host fallback. Empty means no fallback. |
| `port` | integer | PostgreSQL port. |
| `database` | string | Database name. |
| `user` | string | Database user. |

In Compose, `PGHOST=db` and the other `PG*` variables override these values.

## Environment variables

| Variable | Used by | Meaning |
| --- | --- | --- |
| `VSCAPE_TRACKER_CONFIG_DIR` | all services | Replace the config directory. |
| `VSCAPE_TRACKER_DSN` | collector, web, db | Full PostgreSQL connection string. |
| `PGHOST` | all services | PostgreSQL host. |
| `PGPORT` | all services | PostgreSQL port. |
| `PGUSER` | all services | PostgreSQL user. |
| `PGPASSWORD` | all services | PostgreSQL password. |
| `PGDATABASE` | all services | PostgreSQL database. |
| `VSCAPE_TRACKER_PRIORITY_PLAYERS` | collector, web | Comma-separated priority players. |
| `VSCAPE_TRACKER_BASE_URL` | web | Public base URL for RSS links. |
| `VSCAPE_TRACKER_DOCKER_SOCKET` | web | Docker socket path for the status page. |
| `VSCAPE_TRACKER_COMPOSE_PROJECT` | web | Compose project name fallback for container listing. |
| `VSCAPE_TRACKER_TEST_DSN` | tests | Disposable database for database tests. |

Compose also reads these from `docker/.env` for interpolation:

| Variable | Meaning |
| --- | --- |
| `POSTGRES_PASSWORD` | Required database password. |
| `POSTGRES_DB` | Database name, default `xp_tracker`. |
| `POSTGRES_USER` | Database user, default `xp_tracker`. |
| `COMPOSE_PROJECT_NAME` | Compose project name, default `vscape_tracker`. |
| `VSCAPE_TRACKER_LOG_LEVEL` | Service log level. |
| `VSCAPE_TRACKER_PRIORITY_PLAYERS` | Priority players passed to both services. |
| `VSCAPE_TRACKER_BASE_URL` | Base URL passed to the web service. |

`POSTGRES_PASSWORD` only initializes the `pgdata` volume. Changing it later does
not change the database role. See [Operations](operations.md#password-rotation).

## Command-line flags

### Common flags

`collector`, `web` and `db` share the database flag. `collector` and `web` also
share the collection flags.

| Flag | Meaning | Default |
| --- | --- | --- |
| `--dsn` | PostgreSQL connection string. | `VSCAPE_TRACKER_DSN` or `PG*` / `db.yaml`. |
| `--interval` | Coarse backfill step. | `24h` |
| `--collect-interval` | Live collection step. | `1h` |
| `--retry-window` | Recent window always rechecked. | `7d` |
| `--lookback` | Maximum history to cover. | Since 2025-01-26. |
| `--priority` | Priority player. Repeat or comma-separate. | Environment or `priority.players`. |
| `--log-level` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. | `INFO` |

### Collector flags

| Flag | Meaning |
| --- | --- |
| `--workers N` | Concurrent player collectors. Default 2. |
| `--rate N` | Global requests per second, retries included. `0` is unlimited. Default 4. |
| `--no-backfill` | Only fetch the retry window. |
| `--loop` | Run forever and fetch a new checkpoint every collect interval. |
| `--limit N` | Process only the first N players. Useful for testing. |

### Web flags

| Flag | Meaning | Default |
| --- | --- | --- |
| `--report-dir` | Output directory. | `reports` |
| `--report-interval` | Rebuild timer. `0` builds only at startup. | `1m` |
| `--top` | Rows per ranking table. | `5` |
| `--base-url` | Public URL for RSS links. | `http://localhost:8000` |
| `--serve-port` | HTTP port. | `8000` |
| `--serve-host` | Bind interface. | `127.0.0.1` |

### Database flags

| Flag | Meaning |
| --- | --- |
| `--dsn` | Connection string used to create or update the schema. |
| `--log-level` | Log level. |

Examples:

```sh
python -m src.collector --loop --workers 4 --rate 8
python -m src.collector --no-backfill --limit 10 --log-level DEBUG
python -m src.web --report-interval 5m --top 10 --serve-host 0.0.0.0
python -m src.db --dsn "postgresql://user:pass@localhost:5432/xp_tracker"
```

## Durations

`--interval`, `--collect-interval`, `--retry-window`, `--lookback` and
`--report-interval` accept:

```text
30
90s
10m
1h
7d
1w
```

A bare number is seconds. Fractional values are accepted, for example `1.5h`.

`scripts/maintenance/setup.sh` accepts the same suffixes and `YYYY-MM-DD` for the
data start
date.

## Precedence

- DSN: `--dsn`, then `VSCAPE_TRACKER_DSN`, then `PG*` variables and
  `db.yaml`.
- Priority players: `--priority`, then
  `VSCAPE_TRACKER_PRIORITY_PLAYERS`, then `collector.yaml`.
- Base URL: `--base-url`, then `VSCAPE_TRACKER_BASE_URL`, then
  `web.yaml`.
- Config directory: `VSCAPE_TRACKER_CONFIG_DIR`, then `config/`.

Compose passes environment values into the containers. A command-line flag set
in `docker/compose.yaml` wins over the environment-derived default.

## Related documentation

- [Operations](operations.md)
- [Architecture](architecture.md)
- [Reports](reports.md)
