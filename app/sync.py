"""Background refresh of episodes (weekly) and provider availability (daily)."""

from __future__ import annotations

import asyncio
import logging
import time

from .config import AppConfig, Settings
from .db import Database
from .tmdb import TMDBClient

log = logging.getLogger("deadair.sync")

LOOP_INTERVAL_S = 3600


class Syncer:
    def __init__(self, settings: Settings, db: Database, client: TMDBClient):
        self.settings = settings
        self.db = db
        self.client = client
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

            for tmdb_id in tmdb_ids:
                row = existing.get(tmdb_id)
                if force or self._stale(row and row["episodes_refreshed_at"], ep_age, now):
                    try:
                        show, episodes = await self.client.fetch_show_with_episodes(tmdb_id)
                        self.db.replace_episodes(show, episodes)
                        log.info("refreshed %s (%s): %d episodes", show["name"], tmdb_id, len(episodes))
                    except Exception as e:  # keep going; old cache stays usable
                        self.last_error = f"episodes for {tmdb_id}: {e}"
                        log.warning("episode refresh failed for %s: %s", tmdb_id, e)

                if force or self._stale(row and row["providers_checked_at"], prov_age, now):
                    try:
                        providers = await self.client.fetch_providers(
                            tmdb_id, self.settings.watch_region
                        )
                        self.db.set_providers(tmdb_id, providers)
                    except Exception as e:
                        self.last_error = f"providers for {tmdb_id}: {e}"
                        log.warning("provider check failed for %s: %s", tmdb_id, e)

    async def loop(self, get_config) -> None:
        while True:
            try:
                await self.run_once(get_config())
            except Exception:
                log.exception("sync loop iteration failed")
            await asyncio.sleep(LOOP_INTERVAL_S)
