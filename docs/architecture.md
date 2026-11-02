# Architecture notes

## Service split

Three compose services share one image:

- `db` - `postgres:17-alpine`, persistent volume `pgdata`.
- `collector` - `python -m src.collector --loop`, outbound HTTPS to the
  highscores API, writes snapshots and checkpoints.
- `web` - `python -m src.web`, reads PostgreSQL, renders static files, serves
  port 8000, and reads `/var/run/docker.sock` for the status page.

The collector and web service never call each other. PostgreSQL is the only
channel.

## Cost of the split

Measured with `resource.getrusage(RUSAGE_SELF).ru_maxrss` after importing each
entry point, host Python 3.14, no database connections:

| process | max RSS |
| --- | --- |
| collector only | 25 MiB |
| web only | 24 MiB |
| both modules in one process | 26 MiB |

The image is built once and shared by both services, so container storage is
not duplicated. The marginal cost of the second container is about 23-25 MiB of
anonymous memory plus a few MiB of container runtime overhead. Neither process
burns CPU while idle: one sleeps on `time.sleep`, the other on its report
timer.

## Merging collector and web

### Lost

- **Fault isolation.** A collector crash or OOM kill currently leaves the site
  online. A merged process takes both down. The same applies to an interpreter
  fault in psycopg or JSON parsing.
- **Startup isolation.** `web` exits when it cannot bind the port. Today the
  collector keeps running. Merged, a busy port stops collection too.
- **Restart granularity.** `docker compose restart web` leaves collection
  running today. Merged, every restart interrupts a backfill run.
  `scripts/update.sh` already recreates both, so updates do not change.
- **Privilege boundary.** `web` mounts `/var/run/docker.sock` read-only, which is
  host-root equivalent. Merging puts the code that fetches and parses remote
  JSON from `vidyaplace.org` in the same container as that socket. The
  collector does not need the socket today.
- **GIL contention.** Report rendering is pure Python. A full ranking rebuild
  measured about 3 s for 371 players and 400k snapshots. A full player-page rebuild
  measured about 0.06 s per player, about 21 s for all players. The collector is
  I/O-bound, so the effect is request latency, not throughput, but the bursts
  share one interpreter.
- **Scheduling and health.** One process has one uptime, one restart policy and
  one health state. The status page can no longer report them independently.

### Gained

- About 23-25 MiB RSS and one container.
- One compose service, one log stream, one CLI argument set.
- In-process signalling could replace the 60 s report poll. After the report
  data cache this saves about 0.3 s per pass, so the gain is small.

### Decision

Keep the services separate while the status page mounts the Docker socket.
The split costs about 25 MiB and buys fault and privilege isolation.

Merge only when both conditions hold:

1. The status page stops reading `/var/run/docker.sock` directly, or reads it
   through a narrow exporter.
2. A failure to bind the HTTP port is allowed to stop collection.

If memory is the constraint, remove the socket mount first and then merge.

## Refactor results

Measured on synthetic data: 371 players, 400,680 snapshot rows, 249,312
checkpoint rows, PostgreSQL 18, host Python 3.14.

| metric | before | after |
| --- | --- | --- |
| `build_report_data` cold | 8.5 s | 3.0 s |
| `write_report`, unchanged data | 5.5 s | 0.3 s |
| snapshot record scan | 3.4 s | 2.1 s |
| ranking queries per report | 192 | 5 |
| snapshot scans per report | 24 | 1 |
| `latest_xp_by_skill` queries per collection run | 371 | 1 |
| `ensure_schema` calls per minute | 1 | 0 (startup only) |

The report cache keys on exact row counts of `players`, `snapshots` and
`checkpoints`. Any inserted or deleted row invalidates it. Window boundaries
may drift by up to one hour while the counts are unchanged.

Remaining cost on a data change: player pages are rewritten for every player
whose checkpoint history moved. That is about 21 s for 371 players during a
backfill. Reducing it needs a page watermark based on snapshots instead of
checkpoints, which would stop shading newly fetched but unchanged stretches.
