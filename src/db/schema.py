"""PostgreSQL schema created by the database service."""

from __future__ import annotations

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
