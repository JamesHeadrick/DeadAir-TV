"""Random episode selection."""

from __future__ import annotations

import datetime as dt
import random

from .config import ShowConfig
from .db import Database


def pick_episode(
    db: Database,
    shows: list[ShowConfig],
    rng: random.Random | None = None,
    today: str | None = None,
    exclude_episodes: list[str] = (),
) -> tuple[ShowConfig, dict] | None:
    """Pick one aired episode across ``shows``.

    Every episode starts with equal probability, so a show with 200 episodes
    comes up ~10x as often as one with 20. A show's ``weight`` multiplies the
    odds of each of its episodes (weight 2 = each episode twice as likely).

    ``exclude_episodes`` ("tmdb_id:season:episode" keys, e.g. ones already
    shown) are avoided. If that rules out everything, they're allowed again
    rather than coming up empty.
    """
    rng = rng or random.Random()
    today = today or dt.date.today().isoformat()
    ids = [s.tmdb_id for s in shows]

    counts = db.pickable_episode_counts(ids, today, exclude_episodes)
    if not any(counts.values()) and exclude_episodes:
        exclude_episodes = ()
        counts = db.pickable_episode_counts(ids, today)
    candidates = [(s, counts[s.tmdb_id]) for s in shows if counts.get(s.tmdb_id)]
    if not candidates:
        return None

    show, n = rng.choices(candidates, weights=[c * s.weight for s, c in candidates])[0]
    row = db.nth_pickable_episode(show.tmdb_id, rng.randrange(n), today, exclude_episodes)
    if row is None:  # table changed between queries (refresh in progress)
        return None
    return show, dict(row)
