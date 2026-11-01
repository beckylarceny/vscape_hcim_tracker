"""PostgreSQL schema and queries."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS players (
        username   text PRIMARY KEY,
        ironman    smallint NOT NULL,
        first_seen timestamptz NOT NULL DEFAULT now(),
        last_seen  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        username    text NOT NULL REFERENCES players (username) ON DELETE CASCADE,
        skill       text NOT NULL,
        observed_at timestamptz NOT NULL,
        xp          bigint NOT NULL CHECK (xp >= 0),
        PRIMARY KEY (username, skill, observed_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS snapshots_skill_user_time
        ON snapshots (skill, username, observed_at DESC) INCLUDE (xp)
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        username    text NOT NULL REFERENCES players (username) ON DELETE CASCADE,
        observed_at timestamptz NOT NULL,
        PRIMARY KEY (username, observed_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS priority_snapshots (
        username    text NOT NULL REFERENCES players (username) ON DELETE CASCADE,
        skill       text NOT NULL,
        observed_at timestamptz NOT NULL,
        xp          bigint NOT NULL CHECK (xp >= 0),
        PRIMARY KEY (username, skill, observed_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS priority_snapshots_user_time
        ON priority_snapshots (username, observed_at DESC) INCLUDE (skill, xp)
    """,
    """
    CREATE TABLE IF NOT EXISTS priority_checkpoints (
        username    text NOT NULL REFERENCES players (username) ON DELETE CASCADE,
        observed_at timestamptz NOT NULL,
        PRIMARY KEY (username, observed_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id               bigserial PRIMARY KEY,
        started_at       timestamptz NOT NULL,
        finished_at      timestamptz,
        interval_seconds integer NOT NULL,
        collect_interval_seconds integer NOT NULL DEFAULT 0,
        lookback_seconds integer NOT NULL,
        player_count     integer,
        candidate_count  integer NOT NULL DEFAULT 0,
        request_count    integer,
        failure_count    integer NOT NULL DEFAULT 0,
        status           text NOT NULL DEFAULT 'running'
    )
    """,
    """
    ALTER TABLE runs
        ADD COLUMN IF NOT EXISTS collect_interval_seconds integer NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS failure_count integer NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS candidate_count integer NOT NULL DEFAULT 0
    """,
)


def connect(dsn: str) -> "psycopg.Connection":
    try:
        import psycopg
    except ModuleNotFoundError as error:
        raise RuntimeError(
            'psycopg is required: pip install "psycopg[binary]"'
        ) from error

    return psycopg.connect(dsn)


def ensure_schema(conn: psycopg.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()


def as_datetime(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)


def upsert_players(conn: psycopg.Connection, players: Iterable[tuple[str, int]]) -> None:
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO players (username, ironman) VALUES (%s, %s)
            ON CONFLICT (username) DO UPDATE
                SET ironman = EXCLUDED.ironman, last_seen = now()
            """,
            list(players),
        )
    conn.commit()


def fetched_checkpoint_times(
    conn: psycopg.Connection, username: str, timestamps: Sequence[int]
) -> set[int]:
    """Timestamps already fetched for this player, successful or unchanged."""
    if not timestamps:
        return set()
    rows = conn.execute(
        """
        SELECT observed_at
        FROM checkpoints
        WHERE username = %s AND observed_at = ANY(%s)
        """,
        (
            username,
            [as_datetime(epoch) for epoch in timestamps],
        ),
    ).fetchall()
    return {int(row[0].timestamp()) for row in rows}


def latest_xp_by_skill(
    conn: psycopg.Connection, username: str
) -> dict[str, int]:
    """Most recent stored xp per skill for one player."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (skill) skill, xp
        FROM snapshots
        WHERE username = %s
        ORDER BY skill, observed_at DESC
        """,
        (username,),
    ).fetchall()
    return {skill: xp for skill, xp in rows}


def latest_priority_xp_by_skill(
    conn: psycopg.Connection, username: str
) -> dict[str, int]:
    """Most recent high-frequency xp per skill for one player."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (skill) skill, xp
        FROM priority_snapshots
        WHERE username = %s
        ORDER BY skill, observed_at DESC
        """,
        (username,),
    ).fetchall()
    return {skill: xp for skill, xp in rows}


def insert_checkpoints(
    conn: psycopg.Connection, username: str, timestamps: Sequence[int]
) -> None:
    if not timestamps:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO checkpoints (username, observed_at)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(username, as_datetime(epoch)) for epoch in timestamps],
        )
    conn.commit()


def insert_snapshots(conn: psycopg.Connection, rows: Sequence[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO snapshots (username, skill, observed_at, xp)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (username, skill, observed_at) DO NOTHING
            """,
            rows,
        )
    conn.commit()


def insert_priority_snapshots(
    conn: psycopg.Connection, rows: Sequence[tuple]
) -> None:
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO priority_snapshots (username, skill, observed_at, xp)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (username, skill, observed_at) DO NOTHING
            """,
            rows,
        )
    conn.commit()


def insert_priority_checkpoints(
    conn: psycopg.Connection, username: str, timestamps: Sequence[int]
) -> None:
    if not timestamps:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO priority_checkpoints (username, observed_at)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(username, as_datetime(epoch)) for epoch in timestamps],
        )
    conn.commit()


def prune_priority_data(conn: psycopg.Connection, cutoff_epoch: int) -> None:
    """Drop high-frequency rows older than the retention cutoff."""
    cutoff = as_datetime(cutoff_epoch)
    conn.execute(
        "DELETE FROM priority_snapshots WHERE observed_at < %s", (cutoff,)
    )
    conn.execute(
        "DELETE FROM priority_checkpoints WHERE observed_at < %s", (cutoff,)
    )
    conn.commit()


def xp_at_or_before(
    conn: psycopg.Connection, skill: str, cutoff_epoch: int
) -> dict[str, int]:
    """Latest xp per player at or before `cutoff_epoch`."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (username) username, xp
        FROM snapshots
        WHERE skill = %s AND observed_at <= %s
        ORDER BY username, observed_at DESC
        """,
        (skill, as_datetime(cutoff_epoch)),
    ).fetchall()
    return {username: xp for username, xp in rows}


def coverage_buckets(
    conn: psycopg.Connection,
    start_epoch: int,
    end_epoch: int,
    buckets: int,
) -> list[tuple[int, int]]:
    """Players with a checkpoint in each equal slice of [start, end]."""
    if buckets <= 0 or end_epoch <= start_epoch:
        return []
    rows = conn.execute(
        """
        SELECT width_bucket(extract(epoch FROM observed_at), %s, %s, %s) AS b,
               count(DISTINCT username)
        FROM checkpoints
        WHERE observed_at >= %s AND observed_at <= %s
        GROUP BY b
        """,
        (
            start_epoch,
            end_epoch + 1,
            buckets,
            as_datetime(start_epoch),
            as_datetime(end_epoch),
        ),
    ).fetchall()
    counts = {int(bucket): int(count) for bucket, count in rows}
    width = (end_epoch + 1 - start_epoch) / buckets
    return [
        (int(start_epoch + (index - 1) * width), counts.get(index, 0))
        for index in range(1, buckets + 1)
    ]


def start_run(
    conn: psycopg.Connection,
    interval: int,
    collect_interval: int,
    lookback: int,
    candidate_count: int,
) -> int:
    row = conn.execute(
        """
        INSERT INTO runs (
            started_at, interval_seconds, collect_interval_seconds,
            lookback_seconds, player_count, candidate_count
        )
        VALUES (now(), %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            interval, collect_interval, lookback,
            candidate_count, candidate_count,
        ),
    ).fetchone()
    conn.commit()
    return int(row[0])


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    request_count: int,
    failure_count: int,
    status: str,
    player_count: int | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_at = now(),
            request_count = %s,
            failure_count = %s,
            status = %s,
            player_count = COALESCE(%s, player_count)
        WHERE id = %s
        """,
        (request_count, failure_count, status, player_count, run_id),
    )
    conn.commit()
