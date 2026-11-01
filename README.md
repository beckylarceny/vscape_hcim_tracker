# vscape_hcim_tracker

Builds a PostgreSQL database of hardcore ironman (HCIM) experience history and
writes a plain-text ranking report.

## How the history is derived

`GET /api/tracker/player/{username}?time={seconds}` returns how much xp a
player gained during the last `seconds` seconds. Therefore

```
xp_at(t) = xp_now - gained(now - t)
```

`xp_now` comes from `/api/highscores/player/{username}`. The program reconstructs
the whole year on a coarse grid at multiples of `--interval` (default 24 hours)
and stores one row per player, skill and checkpoint. On every run it also adds
the current checkpoint, aligned to `--collect-interval` (default 6 hours), and
re-checks the last `--retry-window` (default 7 days), so failed fetches are
retried on later runs. The API history caps out at roughly one year, so
`--lookback` should stay at or below `365d`.

All checkpoints are aligned to the Unix epoch (`floor(now / step) * step`), so
repeated runs inside the same step do not create duplicates and a crashed
backfill resumes where it stopped. The backfill grid is anchored to `--interval`,
not to `--collect-interval`.

Only accounts whose `/api/highscores/player/{username}` response still reports
`ironman == 2` (hardcore) are inserted into `players`. Accounts that are no
longer hardcore are skipped before anything is written.

Storage is sparse. A snapshot row is written only when a skill's xp differs from
the last stored value, so an inactive player costs 24 rows (one baseline per
skill) regardless of how long they are tracked. Every successfully fetched
checkpoint is recorded in `checkpoints`, independently of whether any skill
changed, so unchanged checkpoints are not fetched again. Between two stored
points a skill's xp is assumed constant, which is exactly how `xp_at(t)` is
derived and how the report evaluates gains.

## Requirements

- Python 3.10+
- PostgreSQL 13+
- `psycopg` 3: `pip install -r requirements.txt`

## Setup

```sh
createdb xp_tracker
export VSCAPE_HCIM_TRACKER_DSN="postgresql:///xp_tracker"
```

The schema is created automatically:

- `players(username, ironman, first_seen, last_seen)`
- `snapshots(username, skill, observed_at, xp)` - primary key
  `(username, skill, observed_at)`; only rows where xp changed
- `checkpoints(username, observed_at)` - primary key
  `(username, observed_at)`; every fetched checkpoint, changed or not
- `runs(id, started_at, finished_at, interval_seconds,
  collect_interval_seconds, lookback_seconds, player_count, request_count,
  failure_count, status)`

`skill` contains every name from `data.py`'s `SKILLS`, including `overall`.
The player list comes from the HCIM highscore pages, so players who have never
gained xp are included too.

## Usage

One collection run, full one-year backfill:

```sh
python -m src --dsn "$VSCAPE_HCIM_TRACKER_DSN"
```

Only record the recent retry window (no history reconstruction):

```sh
python -m src --no-backfill
```

Continuous mode, one new checkpoint every 6 hours (the first run also performs
the one-year backfill):

```sh
python -m src --loop
```

Rebuild the report from existing data without fetching:

```sh
python -m src --report-only
```

Useful options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--dsn` | `$VSCAPE_HCIM_TRACKER_DSN` | PostgreSQL connection string |
| `--interval` | `24h` | backfill sampling step; accepts `s`, `m`, `h`, `d`, `w` |
| `--collect-interval` | `6h` | cadence of new checkpoints and `--loop` sleep |
| `--retry-window` | `7d` | recent span always re-checked, so failures are retried |
| `--lookback` | `365d` | how far back to reconstruct |
| `--workers` | `2` | concurrent player collectors |
| `--rate` | `4` | global HTTP requests per second, retries included, `0` for unlimited |
| `--no-backfill` | off | fetch only the retry window |
| `--report-only` | off | skip the API, rebuild the report once |
| `--collect-only` | off | fetch into the database only, write no reports |
| `--serve-only` | off | render and serve reports from the database, never fetch |
| `--report-interval` | `1m` | regenerate reports on this timer, `0` = off |
| `--report-dir` | `reports` | output directory for text and HTML |
| `--top` | `5` | entries per ranking |
| `--serve-port` | `0` | serve the report directory on this port, `0` = off |
| `--serve-host` | `127.0.0.1` | interface for `--serve-port` |
| `--loop` | off | run forever |
| `--limit` | `0` | process only the first N players (testing) |

## Cost of the initial backfill

At 24h resolution one year is 366 daily checkpoints, plus the current 6h
checkpoint and one highscores request per player: about 368 requests per player,
or 136,500 for 371 players. At the default 4 requests/second the first run
takes roughly 9.5 hours.

The backend answers 503 when several requests arrive at once, so the defaults
are deliberately conservative (`--workers 2`, `--rate 4`); 8 concurrent
connections produced 503 on about half of all requests during testing. Raise
`--workers` or `--rate` only if the server tolerates it.

Later runs only fetch checkpoints missing from the database. Normally that is
the current 6h checkpoint plus one highscores request per player, about 742
requests every 6 hours. The retry window costs no extra requests when the data
is already present.

## Reports

Every run writes two files:

- `reports/latest.txt` - plain text;
- `reports/latest.html` - static page.

Older `report_<timestamp>.txt`/`.html` files are deleted on every write, so
only the latest reports remain. The page links to `latest.txt` from a subheading
under the main title.

Both formats contain, for each skill and `overall`:

- current top 5 players by xp gained over the past day, week, month and year;
- best 5 xp gains over any window of exactly that length inside the lookback,
  with the window start and end date; each player appears at most once with
  their own best window.

Players with zero gain in a span are omitted from that table. The text
report follows the same order as the page: skill, then Past and Best, then
day/week/month/year within each.

The HTML page is a single dependency-free file: one section per skill, each
with a separate "Past" table and "Best" table for every span (day, week, month,
year). Best tables show a start date and an end date; the day table shows a
single date (the window end). Tables sit side by side on wide screens and stack
on narrow ones. The page is regenerated at startup and every
`--report-interval` (default 1 minute) by the report process; when a single
process both collects and serves, it is also rewritten after each run.

Above the tables, a coverage bar spans the lookback window: each segment covers
a run of consecutive days with the same coverage, and its brightness is the
fraction of tracked players with a checkpoint in those days. Use it to judge
backfill progress and to spot gaps caused by backend downtime. Hovering a
segment shows the date range, player count and percentage.

The metadata block shows the generation time in UTC, the player count, the
backfill and collection intervals in hours, and the lookback in years and
months, each on its own line.

Day, week, month and year are fixed durations: 24h, 7d, 30d and 365d.
Stored points are sparse: xp between two stored points is treated as constant.
A record window of a given span therefore uses the xp value of the last stored
point at or before the window start, and the window ends at a stored point where
xp changed. Every record has exactly the nominal span; no window tolerance is
needed.

## Serving

`--serve-port 8000` starts a small HTTP server over the report directory, with
`/` mapped to `latest.html`. `--serve-only` renders from PostgreSQL, serves the
files, and never touches the API:

```sh
python -m src --serve-only --serve-port 8000 --serve-host 0.0.0.0 \
  --report-interval 1m
```

The page also links to `latest.txt`. `--serve-host` defaults to `127.0.0.1`;
use `0.0.0.0` in a container. Without `--serve-port` the files are written but
nothing listens. A single process can also collect and serve:

```sh
python -m src --loop --serve-port 8000 --serve-host 0.0.0.0
```

## Docker

`compose.yaml` runs three containers:

- `db`: `postgres:17-alpine`, data in the named volume `pgdata`;
- `collector`: fetches API data into the database (`--loop --collect-only`);
  no ports and no report files;
- `web`: renders the reports from the database every minute and serves
  `latest.html` / `latest.txt` on port `8000`, with `./reports` bind-mounted.

Only `web` publishes a port.

```sh
cp .env.example .env      # then set a real POSTGRES_PASSWORD
docker compose up -d --build
docker compose logs -f collector web
```

Open `http://localhost:8000/` for the page, or read `reports/latest.txt` on the
host. The first collector run performs the one-year backfill, then appends a
checkpoint every 6 hours indefinitely. `restart: unless-stopped` brings the
containers back after a reboot or crash. Failed checkpoints are retried on the
next run.

```sh
docker compose exec db psql -U xp_tracker -d xp_tracker -c "SELECT count(*) FROM snapshots"
docker compose down        # stop, keep the database volume
docker compose down -v     # stop and delete all stored xp
```

Container tests use the same `db` service and the collector image:

```sh
docker compose exec db createdb -U xp_tracker xp_tracker_test
docker compose run --rm collector python -m unittest discover -s tests -v
```

## Tests

Unit tests need no database:

```sh
python -m unittest discover -s tests
```

Database tests run when `VSCAPE_HCIM_TRACKER_TEST_DSN` points at a disposable database;
they truncate its tables in every test. Without the variable they are skipped.

```sh
createdb xp_tracker_test
VSCAPE_HCIM_TRACKER_TEST_DSN=postgresql:///xp_tracker_test python -m unittest discover -s tests -v
```

## Notes

- `xp_at(t)` is exact only up to the tracking resolution; the server stores
  samples, and `xp_now` and the gain query are separate requests.
- Negative derived xp is clamped to zero. It only happens if the two API
  endpoints disagree because of write timing.
- Fixed `data.py` line 16: the `SKILL_TRACKER_URL` placeholder used the
  non-existent key `HCIM` and broke parsing.

## Example crontab

6-hourly collection, no backfill (the first run should be done once without
`--no-backfill`):

```cron
7 */6 * * * cd /home/a/Projects/vscape_hcim_tracker && VSCAPE_HCIM_TRACKER_DSN=postgresql:///xp_tracker python -m src --no-backfill >> tracker.log 2>&1
```

Cron runs the same command as `--loop` does; use one or the other, not both.
