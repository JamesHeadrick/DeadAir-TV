"""SQLite cache for TMDB show metadata, episodes and provider checks."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    tmdb_id               INTEGER PRIMARY KEY,
    name                  TEXT,
    status                TEXT,
    poster_path           TEXT,
    backdrop_path         TEXT,
    episodes_refreshed_at REAL,
    providers_checked_at  REAL,
    providers_json        TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    tmdb_id    INTEGER NOT NULL,
    season     INTEGER NOT NULL,
    episode    INTEGER NOT NULL,
    title      TEXT,
    overview   TEXT,
    still_path TEXT,
    air_date   TEXT,
    runtime    INTEGER,
    PRIMARY KEY (tmdb_id, season, episode)
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # --- shows -------------------------------------------------------------

    def get_show(self, tmdb_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM shows WHERE tmdb_id = ?", (tmdb_id,)).fetchone()

    def get_shows(self, tmdb_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not tmdb_ids:
            return {}
        marks = ",".join("?" * len(tmdb_ids))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM shows WHERE tmdb_id IN ({marks})", tmdb_ids)
            return {r["tmdb_id"]: r for r in rows}

    def replace_episodes(self, show: dict, episodes: list[dict]) -> None:
        """Atomically replace a show's metadata and full episode list."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shows (tmdb_id, name, status, poster_path, backdrop_path, episodes_refreshed_at)
                VALUES (:tmdb_id, :name, :status, :poster_path, :backdrop_path, :now)
                ON CONFLICT(tmdb_id) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status,
                    poster_path = excluded.poster_path,
                    backdrop_path = excluded.backdrop_path,
                    episodes_refreshed_at = excluded.episodes_refreshed_at
                """,
                {**show, "now": time.time()},
            )
            conn.execute("DELETE FROM episodes WHERE tmdb_id = ?", (show["tmdb_id"],))
            conn.executemany(
                """
                INSERT OR REPLACE INTO episodes
                    (tmdb_id, season, episode, title, overview, still_path, air_date, runtime)
                VALUES
                    (:tmdb_id, :season, :episode, :title, :overview, :still_path, :air_date, :runtime)
                """,
                episodes,
            )

    def set_providers(self, tmdb_id: int, providers: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO shows (tmdb_id, providers_json, providers_checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tmdb_id) DO UPDATE SET
                    providers_json = excluded.providers_json,
                    providers_checked_at = excluded.providers_checked_at
                """,
                (tmdb_id, json.dumps(providers), time.time()),
            )

    # --- episodes ----------------------------------------------------------

    def pickable_episode_counts(self, tmdb_ids: list[int], today: str) -> dict[int, int]:
        """Number of aired, non-special episodes per show."""
        if not tmdb_ids:
            return {}
        marks = ",".join("?" * len(tmdb_ids))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.tmdb_id, COUNT(*) AS n
                FROM episodes e JOIN shows s USING (tmdb_id)
                WHERE e.tmdb_id IN ({marks}) AND {_AIRED_SQL}
                GROUP BY e.tmdb_id
                """,
                [*tmdb_ids, today],
            )
            return {r["tmdb_id"]: r["n"] for r in rows}

    def nth_pickable_episode(self, tmdb_id: int, n: int, today: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT e.* FROM episodes e JOIN shows s USING (tmdb_id)
                WHERE e.tmdb_id = ? AND {_AIRED_SQL}
                ORDER BY e.season, e.episode
                LIMIT 1 OFFSET ?
                """,
                (tmdb_id, today, n),
            ).fetchone()


# An episode is pickable once it has aired. Undated episodes are only trusted
# for shows that have finished (older shows sometimes lack air dates).
_AIRED_SQL = """
    e.season > 0 AND (
        (e.air_date IS NOT NULL AND e.air_date != '' AND e.air_date <= ?)
        OR ((e.air_date IS NULL OR e.air_date = '') AND s.status IN ('Ended', 'Canceled'))
    )
"""
