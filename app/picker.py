"""Random episode selection."""

from __future__ import annotations

import datetime as dt
import math
import random

from .config import ShowConfig
from .db import Database


def pick_episode(
    db: Database,
    shows: list[ShowConfig],
    rng: random.Random | None = None,
    today: str | None = None,
    exclude_episodes: list[str] = (),
    skip_seasons: list[str] = (),
    banned: list[str] = (),
    balance: str = "sqrt",
) -> tuple[ShowConfig, dict] | None:
    """Pick one aired episode across ``shows``: a show, then one of its episodes.

    ``balance`` sets each show's share by its number of pickable episodes n:
    "episodes" (n, so a 200-episode show comes up 10x as often as a 20-episode
    one), "sqrt" (sqrt(n), about 3x) or "shows" (equal). A show's ``weight``
    multiplies its share. Episodes within a show are equally likely.

    ``exclude_episodes`` ("tmdb_id:season:episode" keys, e.g. ones already
    shown) are avoided. If that rules out everything, they're allowed again
    rather than coming up empty. ``skip_seasons`` ("tmdb_id:season", seasons
    with nowhere to watch them) and ``banned`` episode keys are never picked.
    """
    rng = rng or random.Random()
    today = today or dt.date.today().isoformat()
    ids = [s.tmdb_id for s in shows]

    banned = list(banned)
    exclude = [*exclude_episodes, *banned]
    counts = db.pickable_episode_counts(ids, today, exclude, skip_seasons)
    if not any(counts.values()) and exclude_episodes:
        exclude = banned  # allow seen/cooling episodes again, but never banned ones
        counts = db.pickable_episode_counts(ids, today, exclude, skip_seasons)
    candidates = [(s, counts[s.tmdb_id]) for s in shows if counts.get(s.tmdb_id)]
    if not candidates:
        return None

    show, n = rng.choices(candidates, weights=[_share(c, balance) * s.weight for s, c in candidates])[0]
    row = db.nth_pickable_episode(show.tmdb_id, rng.randrange(n), today, exclude, skip_seasons)
    if row is None:  # table changed between queries (refresh in progress)
        return None
    return show, dict(row)


def _share(n: int, balance: str) -> float:
    """A show's relative share of picks for n pickable episodes."""
    if balance == "shows":
        return 1.0
    if balance == "episodes":
        return float(n)
    return math.sqrt(n)
