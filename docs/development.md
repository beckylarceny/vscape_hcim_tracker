# Development

The project is plain Python with PostgreSQL. There is no build step and no
frontend toolchain. HTML and SVG charts are generated in Python. CSS is edited
in `src/web/report.css` and copied into each generated page.

## Local setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Requirements:

- Python 3.13 is used by the container image.
- PostgreSQL with the `psycopg` driver.
- `PyYAML` for configuration.

Set a database and run a service:

```sh
export VSCAPE_HCIM_TRACKER_DSN="postgresql://user:pass@localhost:5432/xp_tracker"
python -m src.db
python -m src.collector --no-backfill --limit 10 --log-level DEBUG
python -m src.web --report-interval 0 --serve-host 127.0.0.1
```

The web service defaults to `reports/` and port 8000. `python -m src` runs the
collector.

## Tests

```sh
python -m unittest discover -s tests -t .
```

Database tests are skipped unless a disposable database is reachable. They
truncate tables, so never point them at production data.

```sh
VSCAPE_HCIM_TRACKER_TEST_DSN="postgresql://user:pass@localhost:5432/xp_tracker_test" \
  python -m unittest tests.test_storage -v
```

`tests/test_storage.py` also uses `build_dsn("xp_tracker_test")` when the test DSN
is unset. It skips when that database cannot be reached.

## Module map

### Entry points

| Path | Responsibility |
| --- | --- |
| `src/__main__.py` | Entry point for `python -m src`. |
| `src/main.py` | Default entry point. Delegates to the collector. |
| `src/collector/__main__.py` | Entry point for `python -m src.collector`. |
| `src/web/__main__.py` | Entry point for `python -m src.web`. |
| `src/db/__main__.py` | Entry point for `python -m src.db`. |

### Common

Shared code used by every service.

| Path | Responsibility |
| --- | --- |
| `src/common/cli.py` | Shared argument parser. |
| `src/common/priority.py` | Priority player selection. |
| `src/common/storage.py` | SQL helpers and schema calls. |
| `src/common/utility.py` | Config loading, DSN building, durations, time slots. |

### Collector

| Path | Responsibility |
| --- | --- |
| `src/collector/api.py` | HTTP access, retries and JSON parsing. |
| `src/collector/cli.py` | Collector argument parser. |
| `src/collector/collection.py` | Discovery, current xp and backfill writes. |
| `src/collector/grid.py` | Backfill and retry timestamp grid. |
| `src/collector/main.py` | Collection loop, signal handling and service wiring. |
| `src/collector/priority.py` | Priority thread and priority tables. |
| `src/collector/rate.py` | Global request pacing. |
| `src/collector/run.py` | One collection run and run accounting. |

### Database

| Path | Responsibility |
| --- | --- |
| `src/db/main.py` | One-shot schema command. |
| `src/db/schema.py` | `CREATE TABLE` and index statements. |

### Web

| Path | Responsibility |
| --- | --- |
| `src/web/chart.py` | Inline SVG charts. |
| `src/web/cli.py` | Web argument parser. |
| `src/web/format.py` | Duration and count formatting. |
| `src/web/html.py` | Escaping, tabs, atomic writes and inline page style. |
| `src/web/main.py` | Report startup, server startup and service loop. |
| `src/web/player_report.py` | Player data, best windows and player pages. |
| `src/web/priority_report.py` | Priority pages and RSS feeds. |
| `src/web/report.css` | Page styles. Edited here and copied into generated pages. |
| `src/web/report.py` | General rankings, text report and HTML report. |
| `src/web/reporting.py` | Report orchestration, caching and incremental writes. |
| `src/web/server.py` | Static HTTP server. |
| `src/web/status_report.py` | Status page, text version and Docker access. |

## Data flow

```text
vidyaplace API
    |
    v
src.collector.api  ->  src.collector.collection  ->  PostgreSQL
                                                      |
                                                      v
                                            src.web.reporting
                                                      |
                              +-----------------------+------------------+
                              |                       |                  |
                        src.web.report      src.web.player_report  src.web.status_report
                              |                       |                  |
                              +-----------------------+------------------+
                                                      |
                                                 reports/
```

The collector is the only service that fetches xp. The web service is read-only
with respect to xp tables and writes only to the report directory.

## Design constraints

- Store sparse xp: insert a snapshot only when the value changes.
- Record every successful fetch as a checkpoint, including unchanged values.
- Derive historical xp from current xp minus gained xp.
- Clamp negative derived xp to zero.
- Keep one global request rate across workers and retries.
- Keep report generation independent of collection.
- Degrade the status page instead of failing it when Docker or SQL queries fail.
- Use UTC for stored timestamps and rendered dates.
- Write reports atomically.
- Keep the web service free of highscore API calls.

## Schema changes

Add statements to `src/db/schema.py`. Use idempotent forms:

```sql
CREATE TABLE IF NOT EXISTS ...
CREATE INDEX IF NOT EXISTS ...
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...
```

The services run `ensure_schema` at startup. Existing installations need the new
statement to be backward compatible or explicitly migrated.

## Adding a skill

1. Add the skill to `skills` in `config/defaults/collector.yaml` and
   `config/collector.yaml`.
2. Confirm the API returns `<skill>_xp` and `<skill>_diff`.
3. Restart the services.
4. Existing history for the new skill starts at the next collection.

`overall` is special: its level row shows total level, and it is used to select
priority players.

## Report changes

- Use `html.esc` for every value inserted into HTML, XML or JavaScript strings.
- Use `html.atomic_write` for output files.
- Keep text reports in sync with HTML pages. Several tests compare both.
- Preserve the single `generated at` metadata line on ranking pages.
- Player pages must remain deterministic for unchanged watermarks.

## Tests by area

| File | Area |
| --- | --- |
| `tests/test_main.py` | Grid, rate limiter, collection and priority logic. |
| `tests/test_storage.py` | Schema, SQL helpers, report writes and pruning. |
| `tests/test_report.py` | Ranking scans, HTML and text general report, cache. |
| `tests/test_player_report.py` | Player data, levels, records and charts. |
| `tests/test_priority_report.py` | Priority data, events, pages and feeds. |
| `tests/test_status_report.py` | Status data, Docker rows, coverage and text output. |
| `tests/test_web.py` | Root URL redirect. |

## Related documentation

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [Operations](operations.md)
- [Reports](reports.md)
