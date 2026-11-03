# Reports

The web service writes static text and HTML files into `reports/` and serves the
same directory over HTTP. Reports are rebuilt on a timer and are safe to read
while being replaced.

`defaults.site_title` in `config/web.yaml` sets the site name. The default is
`vidyascape tracker`.

HTML pages show the site title in a header above the page heading. The browser
title combines the page title and site title, for example
`general tracking - vidyascape tracker`. The plain-text reports start with
`VIDYASCAPE TRACKER REPORT` or `VIDYASCAPE TRACKER STATUS`.

The home page heading is `home`. The site header is plain text, not a link.

## Output files

| Path | Content |
| --- | --- |
| `reports/latest.txt` | Plain-text general report. |
| `reports/index.html` | Home page linking every report section. |
| `reports/general.html` | General rankings page. |
| `reports/status.txt` | Plain-text status page. |
| `reports/status.html` | Service, database, collection and coverage status. |
| `reports/players/<slug>.html` | One page per player. |
| `reports/players/<slug>.txt` | Plain-text player report. |
| `reports/priority/index.html` | Priority player index. |
| `reports/priority/<slug>.html` | One 24-hour priority page per player. |
| `reports/priority/<slug>.xml` | RSS feed for one priority player. |
| `reports/version` | Software version label written by `scripts/update.sh`. |

Pages are served without the `.html` suffix. `/` serves the home page,
`/general` the rankings, `/status` the status page, `/players/<slug>` a player
page and `/priority/<slug>` a priority page. `/priority/` serves the priority
index. The `.html` paths still work.

`player_slug` replaces characters outside `A-Z a-z 0-9 . _ -` with `_`. Names
that reduce to an empty slug use `player`.

## Home page

`index.html` links to general tracking, priority tracking and status. It shows
the generation time, tracked player count and version.

## General report

The general page has one tab per skill and a player finder. Each skill has:

- `Past`: the top players by xp gained over each span.
- `Best`: the best window of each span across the lookback.

Past gain is:

```text
latest xp at or before now - latest xp at or before now - span
```

Rows with no baseline or no positive gain are omitted. The table shows the
configured number of rows (`top_n`, default 5).

Best windows scan the sparse stored points. A skill's xp is treated as constant
between stored rows. A window needs one stored point at or before its start and
one at its end. Each player contributes at most one record per span. Zero-gain
windows are ignored. The `day` table shows one date; longer spans show start and
end dates.

Spans and `top_n` come from `config/web.yaml`. `latest.txt` contains the same
skill, past and best sections in text form, followed by the version line.

## Player pages

Each player page contains:

- Current xp and level per skill.
- Total level on the `overall` row.
- Past gains for day, week, month and year.
- Best windows for the same spans.
- Five charts per skill: the four spans plus an all-time chart.
- Snapshot counts and date ranges per fetch frequency.

Levels use the OSRS experience table and cap at 99. Total level sums every skill
except `overall`.

Charts show total xp as a line and xp/hour as a filled area. The all-time chart
is wider and uses more points. Series are thinned to a fixed number of points
while keeping their endpoints.

Missing-data shading marks stretches with fewer checkpoints than expected.
Edges use 36 hours or twice the live interval, whichever is larger. Interior
gaps are compared with the smallest nearby gap, so a missed live fetch is
flagged while a normal 24-hour backfill gap is not. Stretches that were fetched
and had no xp change stay unshaded.

Each player page links to a plain-text version at `players/<slug>.txt`. The
text file has the same current xp, past gains, best windows and snapshot
counts. Player pages and text files are rebuilt only when that player's stored
xp changes, when either file is missing, or when the priority set changes.
Checkpoint-only changes do not rewrite inactive players.

## Priority reports

Priority tracking covers:

- The top `priority.top` players by `overall` xp.
- Any players in `priority.players`.
- CLI or environment priority names.
- Players who still have unpruned priority rows.

Each priority page covers the last 24 hours. It shows current xp and one chart
per skill with total xp and xp/hour. The `overall` chart draws xp/hour as
variable-width bars, stacked and coloured by skill. Bars use translucent skill
colours. Hovering a bar shows the skill, xp gain, rate and UTC window. Other
skill charts use a filled xp/hour area. Priority snapshots are the base series.
Regular snapshots fill gaps in the priority series and are drawn in a different
colour, so periods without 10-minute collection are still visible.

The RSS feed contains changed xp grouped by priority fetch time, newest first.
Feed links use `base_url` or `VSCAPE_TRACKER_BASE_URL`. The link points to
the priority page and the feed itself.

Players who leave the priority set remain in the index until their high-frequency
rows are pruned.

## Status report

The status page has these sections:

| Section | Contents |
| --- | --- |
| `data coverage` | One bar segment per equal slice of the lookback window. |
| `service` | Process uptime, start time, Python and platform, report rebuild timer. Includes the `containers` table. |
| `database` | Connection, latency, server version, database uptime, name, host and size. Includes the `tables` row counts. |
| `backfill` | Backfill interval, retry window, lookback, last run and last run end. |
| `live` | Live interval, next live fetch and newest checkpoint. |
| `priority` | Priority interval, next priority fetch and newest priority checkpoint. |
| `players` | Verified player count and tracked priority players. |
| `snapshots` | Snapshot and checkpoint counts by fetch frequency, with first and last dates. |
| `recent runs` | The last five runs with status, requests, failures and players. |

`players` and `runs` are counted exactly. Snapshot and checkpoint counts use the
PostgreSQL planner estimate.

The coverage bar fades with the share of players that hold a checkpoint in each
slice. Its colour and pattern show fetch density: solid for hourly checkpoints,
diagonal stripes for 6-hour, finer diagonal stripes for daily and grey for sparse
data. The bar uses at most 300 slices, so a lookback longer than 300 days gives
slices wider than a day. Density compares each slice with the checkpoints a
cadence would leave in it, so two daily checkpoints in a 2-day slice stay daily
rather than sparse. Tooltips show the slice range, player count and density.

The container table comes from the Docker API through the read-only socket
mount. When the API is unavailable the page still renders and shows the error.

`status.txt` mirrors the HTML page in plain text.

## Rebuild and caching

The web service rebuilds reports every `report_interval_seconds` (default 60
seconds). `--report-interval 0` writes one report at startup and stops the
timer.

Ranking data is cached when:

- Report parameters are unchanged.
- Row counts for `players`, `snapshots` and `checkpoints` are unchanged.
- The cache entry is at most one hour old.

This avoids rescanning snapshot history every minute when no xp was written.
Player pages use per-player watermarks and are written only when their data
changes.

Old `report_*.txt`, `report_*.html` and temporary files are removed. Player and
priority pages that no longer have a corresponding player are deleted.

All writes use a temporary file and `os.replace`, so a reader never sees a
partially written report.

## Related documentation

- [Configuration](configuration.md)
- [Architecture](architecture.md)
- [Operations](operations.md)
