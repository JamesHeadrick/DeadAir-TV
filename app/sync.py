"""Background refresh of episodes and show links (weekly) and provider availability,
per show and per season (daily)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .config import AppConfig, Settings
from .db import Database
from .tmdb import PROVIDER_TYPES, TMDBClient
from . import wikidata

log = logging.getLogger("deadair.sync")

LOOP_INTERVAL_S = 3600


class Syncer:
    def __init__(self, settings: Settings, db: Database, client: TMDBClient, wikidata_transport=None):
        self.settings = settings
        self.db = db
        self.client = client
        self.wikidata_transport = wikidata_transport  # for tests
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    def _stale(self, ts: float | None, max_age_s: float, now: float) -> bool:
        return ts is None or now - ts >= max_age_s

    async def run_once(self, cfg: AppConfig, force: bool = False) -> None:
        async with self._lock:
            now = time.time()
            ep_age = self.settings.episode_refresh_days * 86400
            prov_age = self.settings.provider_check_hours * 3600
            tmdb_ids = sorted({s.tmdb_id for s in cfg.shows})
            existing = self.db.get_shows(tmdb_ids)
            self.last_error = None

            orders = {s.tmdb_id: s.episode_order for s in cfg.shows}

            for tmdb_id in tmdb_ids:
                row = existing.get(tmdb_id)
                episodes_due = force or self._stale(row and row["episodes_refreshed_at"], ep_age, now)
                if episodes_due:
                    try:
                        show, episodes = await self.client.fetch_show_with_episodes(tmdb_id)
                        self.db.replace_episodes(show, episodes)
                        log.info("refreshed %s (%s): %d episodes", show["name"], tmdb_id, len(episodes))
                    except Exception as e:  # keep going; old cache stays usable
                        self.last_error = f"episodes for {tmdb_id}: {e}"
                        log.warning("episode refresh failed for %s: %s", tmdb_id, e)

                # An alternate episode order (e.g. DVD order): refetched with the
                # episodes, and right away when a different one is picked.
                order = orders.get(tmdb_id)
                stored = json.loads(row["episode_order_json"]) if row and row["episode_order_json"] else None
                stored_group = stored and stored.get("group")
                if order != stored_group or (order and episodes_due):
                    try:
                        mapping = await self.client.fetch_episode_order(order) if order else None
                        self.db.set_episode_order(tmdb_id, order, mapping)
                    except Exception as e:  # keep TMDB's numbering meanwhile
                        self.last_error = f"episode order for {tmdb_id}: {e}"
                        log.warning("episode order fetch failed for %s: %s", tmdb_id, e)

                never_checked_seasons = row is not None and row["season_providers_json"] is None
                if force or never_checked_seasons or self._stale(row and row["providers_checked_at"], prov_age, now):
                    try:
                        providers = await self.client.fetch_providers(
                            tmdb_id, self.settings.watch_region
                        )
                        self.db.set_providers(tmdb_id, providers)
                        await self._check_seasons(tmdb_id, providers)
                    except Exception as e:
                        self.last_error = f"providers for {tmdb_id}: {e}"
                        log.warning("provider check failed for %s: %s", tmdb_id, e)

            # Show-page links from Wikidata, weekly like episodes, in batches.
            if self.settings.wikidata_links:
                due = [t for t in tmdb_ids
                       if force or self._stale(existing.get(t) and existing[t]["links_checked_at"], ep_age, now)]
                if due:
                    try:
                        links = await wikidata.fetch_show_links(due, transport=self.wikidata_transport)
                        self.db.set_auto_links(links, due)
                        log.info("wikidata: show links for %d of %d shows", len(links), len(due))
                    except Exception as e:  # non-fatal: Open falls back to search
                        log.warning("wikidata lookup failed: %s", e)

    async def _check_seasons(self, tmdb_id: int, providers: dict) -> None:
        """Where each season streams, for services that only carry some seasons.

        Skipped when the show isn't available anywhere. A season TMDB has no
        data for is left out, so it falls back to the whole-show answer.
        """
        if not any(providers.get(k) for k in PROVIDER_TYPES):
            self.db.set_season_providers(tmdb_id, {})
            return
        by_season = {}
        for season in self.db.show_seasons([tmdb_id])[tmdb_id]:
            found = await self.client.fetch_providers(tmdb_id, self.settings.watch_region, season=season)
            if any(found.get(k) for k in PROVIDER_TYPES):
                by_season[season] = found
        self.db.set_season_providers(tmdb_id, by_season)

    async def loop(self, get_config) -> None:
        while True:
            try:
                await self.run_once(get_config())
            except Exception:
                log.exception("sync loop iteration failed")
            await asyncio.sleep(LOOP_INTERVAL_S)
