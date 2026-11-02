# Architecture

The project has three runtime services plus PostgreSQL. The collector writes xp
history, the web service renders static reports, and the database service owns
the schema and storage.

## Services

| Service | Image | Role |
| --- | --- | --- |
| `db` | `postgres:17-alpine` | Stores players, xp snapshots, fetch checkpoints and run history. |
| `collector` | `vscape_hcim_tracker-app:latest` | Discovers hardcore ironman players and fills the checkpoint grid. |
| `web` | `vscape_hcim_tracker-app:latest` | Reads the database, writes reports and serves them over HTTP. |

The collector and web service share one image but run different commands:

```sh
python -m src.collector --loop --workers 2 --rate 4
python -m src.web --serve-port 8000 --serve-host 0.0.0.0
```

`python -m src` is equivalent to `python -m src.collector`.

## Collection cycle

One collector run does the following:

1. Discover hardcore ironman players.
2. Fetch current xp for every discovered player.
3. Build the checkpoint grid.
4. Walk the grid from newest to oldest.
5. Write snapshots only where xp changed.
6. Record every successful fetch as a checkpoint.
7. Record the run in `runs`.

### Player discovery

`src/collector/api.py::list_hcim_players` reads the overall highscores metadata,
then every page with `ironman=2` (hardcore). It keeps only rows whose `ironman`
field is still `2`.

Discovery uses the hardcoded `https://vidyaplace.org/api` base. The per-player
endpoints come from `config/collector.yaml`:

- `urls.player_highscores` for current xp.
- `urls.player_tracker` for xp gained over a time window.

The tracker endpoint returns gained xp for a relative age in seconds, not
historical xp. The collector reconstructs history as:

```text
xp_at(t) = current_xp - xp_gained_since(t)
```

### Checkpoint grid

`src/collector/grid.py::build_grid` builds the timestamps to fetch. The result
is sorted newest first and is the union of two grids.

Backfill grid:

```text
anchor = floor(now / interval) * interval
times  = anchor, anchor - interval, ... down to now - lookback
```

Recent retry grid:

```text
current = floor(now / collect_interval) * collect_interval
count   = max(1, retry_window / collect_interval)
times   = current, current - collect_interval, ... for count entries
```

Defaults:

- `interval`: 24 hours, the coarse backfill step.
- `collect_interval`: 6 hours, the live step.
- `retry_window`: 7 days, always rechecked.
- `lookback`: everything since 2025-01-26, clamped by `data_start_epoch`.

`--no-backfill` removes the coarse grid and leaves only the retry window.
`--lookback` shortens the backfill but cannot reach before the configured data
start.

### Fetching and storage

For each grid timestamp, newest first:

1. Skip the timestamp for players whose checkpoint already exists.
2. Fetch gained xp for the age `now - timestamp`.
3. Derive each skill's historical xp and clamp negative values to zero.
4. Compare with the newest stored xp for that player and skill.
5. Insert snapshot rows only for skills whose xp differs.
6. Insert a checkpoint row for the fetch.

Because the walk is newest first, the database fills backwards in time and the
newest coverage appears first. A skill with no xp change between two fetched
timestamps has no row at the older timestamp. Reports treat stored xp as
constant between rows.

The initial comparison value comes from the newest stored xp per player and
skill. During a run the collector keeps that value in memory. A missing xp value
is treated as the previous value.

All inserts use `ON CONFLICT DO NOTHING`. Repeated runs are safe.

### Failure handling

- HTTP 404, an empty body, `null`, or non-JSON text means "not found".
- 429, 500, 502, 503 and 504 are retried.
- Network errors and timeouts are retried.
- Other HTTP errors fail the request.
- Each failed player or checkpoint increments `failure_count`.
- A run is `ok` with no failures, `partial` with failures, and `error` if
  collection aborts.
- One failed player does not stop the run.

Retries happen up to five times. The backoff starts at 2 seconds, doubles, is
capped at 60 seconds, and adds up to one second of jitter. `Retry-After` is
honoured when present.

`src/collector/rate.py::RateLimiter` enforces one global request rate across
worker threads and retries. `--rate 0` disables pacing.

### Priority collection

When `--loop` is active, the collector starts a second daemon thread. Every 10
minutes it:

1. Prunes priority rows older than the retention window.
2. Selects the configured top players by `overall` xp plus `priority.players`.
3. Fetches current xp and the current 10-minute gains for each player.
4. Writes changed skills to `priority_snapshots`.
5. Writes a `priority_checkpoints` row for each fetch.

Priority collection uses separate tables so frequent updates do not inflate the
long-term history. Defaults are a 10-minute interval and 24-hour retention.

## Report cycle

The web service does the following at startup:

1. Creates the report directory.
2. Ensures the database schema.
3. Writes one report from existing data.
4. Starts the HTTP server.
5. Starts the report timer unless `--report-interval 0`.

The report timer is independent of collection. It rebuilds reports every 60
seconds by default.

### Ranking computation

`src/web/reporting.py::report_data` computes rankings from:

- Latest xp at or before the current time.
- Latest xp at or before each span boundary.
- Best sliding windows across the lookback.
- Coverage buckets from checkpoints.

Results are cached by report parameters and by row counts for `players`,
`snapshots` and `checkpoints`. The cache is reused for up to one hour while those
counts are unchanged. Collection writes change the counts, so new data triggers
a rebuild.

### Incremental pages

Player pages are rewritten only when that player's stored xp changes. A
watermark stores the snapshot row count and newest snapshot time for regular and
priority data. Checkpoints alone do not trigger a rewrite, because a fetch
without an xp change does not affect the page content.

Player pages are also rewritten when:

- The report directory is new.
- The file is missing.
- The priority player set changes.
- `force` is requested internally.

Priority pages are written on each report pass for tracked players and players
who still hold unpruned priority rows. Stale player and priority files are
deleted.

All report writes are atomic: write a temporary file in the same directory, then
replace the target.

## Database schema

Schema creation is idempotent and runs at collector, web and db startup.
`src/db/schema.py` owns the statements.

| Table | Key | Purpose |
| --- | --- | --- |
| `players` | `username` | Verified hardcore ironman accounts. |
| `snapshots` | `(username, skill, observed_at)` | Sparse xp history. Rows exist only when xp changed. |
| `checkpoints` | `(username, observed_at)` | Every fetched timestamp, including unchanged xp. |
| `priority_snapshots` | `(username, skill, observed_at)` | Sparse 10-minute xp history. |
| `priority_checkpoints` | `(username, observed_at)` | Every priority fetch. |
| `runs` | `id` | Collection run status, request counts and failures. |

Indexes:

- `snapshots_skill_user_time` supports latest-xp and ranking queries.
- `priority_snapshots_user_time` supports priority page queries.

`players` uses `last_seen = now()` on every verified upsert. Snapshot and
checkpoint rows cascade when a player is deleted.

## Timing and time zones

All stored timestamps are `timestamptz`. The reports render dates in UTC.
Durations are converted to seconds at startup. `next_slot` aligns live fetches to
the configured interval plus a grace period: 60 seconds for live collection and
5 seconds for priority collection.

## Related documentation

- [Operations](operations.md)
- [Configuration](configuration.md)
- [Reports](reports.md)
- [Development](development.md)
