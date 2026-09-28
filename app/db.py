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

-- Episodes you marked watched or skipped; they sit out a cooldown.
CREATE TABLE IF NOT EXISTS episode_history (
    id      INTEGER PRIMARY KEY,
    tmdb_id INTEGER NOT NULL,
    season  INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('watched', 'skipped')),
    at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS episode_history_at ON episode_history (at);
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

    def set_providers(self, tmdb_id: int, providers: dict) -> None:
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

    def pickable_episode_counts(
        self, tmdb_ids: list[int], today: str, exclude: list[str] = ()
    ) -> dict[int, int]:
        """Number of aired, non-special episodes per show.

        ``exclude`` holds episode keys ("tmdb_id:season:episode") to leave out.
        """
        if not tmdb_ids:
            return {}
        marks = ",".join("?" * len(tmdb_ids))
        skip_sql, skip_args = _exclude_sql(exclude)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT e.tmdb_id, COUNT(*) AS n
                FROM episodes e JOIN shows s USING (tmdb_id)
                WHERE e.tmdb_id IN ({marks}) AND {_AIRED_SQL} {skip_sql}
                GROUP BY e.tmdb_id
                """,
                [*tmdb_ids, today, *skip_args],
            )
            return {r["tmdb_id"]: r["n"] for r in rows}

    def nth_pickable_episode(
        self, tmdb_id: int, n: int, today: str, exclude: list[str] = ()
    ) -> sqlite3.Row | None:
        skip_sql, skip_args = _exclude_sql(exclude)
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT e.* FROM episodes e JOIN shows s USING (tmdb_id)
                WHERE e.tmdb_id = ? AND {_AIRED_SQL} {skip_sql}
                ORDER BY e.season, e.episode
                LIMIT 1 OFFSET ?
                """,
                (tmdb_id, today, *skip_args, n),
            ).fetchone()


    # --- history -----------------------------------------------------------

    def add_history(self, tmdb_id: int, season: int, episode: int, kind: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO episode_history (tmdb_id, season, episode, kind, at) VALUES (?, ?, ?, ?, ?)",
                (tmdb_id, season, episode, kind, time.time()),
            )
            return cur.lastrowid

    def undo_watched(self, tmdb_id: int, season: int, episode: int, since: float) -> None:
        """Remove recent 'watched' marks for an episode (the button toggles)."""
        with self.connect() as conn:
            conn.execute(
                """DELETE FROM episode_history WHERE tmdb_id = ? AND season = ? AND episode = ?
                   AND kind = 'watched' AND at >= ?""",
                (tmdb_id, season, episode, since),
            )

    def cooldown_keys(self, since: float) -> list[str]:
        """Episode keys marked watched/skipped at or after ``since``."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tmdb_id, season, episode FROM episode_history WHERE at >= ?", (since,)
            )
            return [episode_key(r["tmdb_id"], r["season"], r["episode"]) for r in rows]

    def last_history(self, tmdb_id: int, season: int, episode: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT kind, at FROM episode_history WHERE tmdb_id = ? AND season = ? AND episode = ?
                   ORDER BY at DESC LIMIT 1""",
                (tmdb_id, season, episode),
            ).fetchone()

    def clear_history(self) -> int:
        with self.connect() as conn:
            return conn.execute("DELETE FROM episode_history").rowcount


def episode_key(tmdb_id: int, season: int, episode: int) -> str:
    return f"{tmdb_id}:{season}:{episode}"


def _exclude_sql(keys) -> tuple[str, list]:
    keys = list(keys)
    if not keys:
        return "", []
    marks = ",".join("?" * len(keys))
    return f"AND (e.tmdb_id || ':' || e.season || ':' || e.episode) NOT IN ({marks})", keys


# An episode is pickable once it has aired. Undated episodes are only trusted
# for shows that have finished (older shows sometimes lack air dates).
_AIRED_SQL = """
    e.season > 0 AND (
        (e.air_date IS NOT NULL AND e.air_date != '' AND e.air_date <= ?)
        OR ((e.air_date IS NULL OR e.air_date = '') AND s.status IN ('Ended', 'Canceled'))
    )
"""
