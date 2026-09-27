"""DeadAir TV: pick a channel, get a random episode."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adb
from .access import Access, compute_access
from .config import AppConfig, ConfigError, Settings, ShowConfig, load_config
from .db import Database
from .picker import pick_episode
from .sync import Syncer
from .tmdb import TMDBClient, image_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deadair")

STATIC_DIR = Path(__file__).parent / "static"


class State:
    settings: Settings
    db: Database
    config: AppConfig
    syncer: Syncer | None = None


state = State()
_background: set[asyncio.Task] = set()


class PlayRequest(BaseModel):
    tmdb_id: int
    provider_id: int | None = None  # which watch option; default = the first


def create_app(settings: Settings | None = None, start_sync: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.settings = settings
        state.db = Database(settings.db_path)
        state.config = load_config(settings.config_path)
        client = None
        task = None
        if settings.tmdb_api_key:
            client = TMDBClient(settings.tmdb_api_key)
            state.syncer = Syncer(settings, state.db, client)
            if start_sync:
                task = asyncio.create_task(state.syncer.loop(lambda: state.config))
        else:
            log.warning("TMDB_API_KEY not set; episode data will not be fetched")
        yield
        if task:
            task.cancel()
        if client:
            await client.aclose()

    app = FastAPI(title="DeadAir TV", lifespan=lifespan)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/channels")
    async def channels():
        info = _show_infos(state.config.shows)
        out = []
        for name, shows in state.config.channels.items():
            out.append(
                {
                    "name": name,
                    "shows": [info[s.tmdb_id]["show_name"] for s in shows],
                    "unwatchable": [
                        info[s.tmdb_id]["show_name"] for s in shows if not info[s.tmdb_id]["_watchable"]
                    ],
                }
            )
        return {
            "channels": out,
            "services": state.config.services,
            "adb_enabled": state.settings.enable_adb and bool(state.settings.tv_ip),
            "sync_error": state.syncer.last_error if state.syncer else "TMDB_API_KEY not set",
        }

    @app.get("/api/shows")
    async def shows():
        """Every configured show and where you can watch it."""
        info = _show_infos(state.config.shows)
        return {"shows": [_public(info[s.tmdb_id]) for s in state.config.shows]}

    @app.get("/api/pick")
    async def pick(channel: str):
        shows = state.config.channels.get(channel)
        if shows is None:
            raise HTTPException(404, f"unknown channel {channel!r}")
        info = _show_infos(shows)
        watchable = [s for s in shows if info[s.tmdb_id]["_watchable"]]
        if not watchable:
            raise HTTPException(404, "none of this channel's shows are on your services")
        result = pick_episode(state.db, watchable)
        if result is None:
            raise HTTPException(503, "no episodes cached yet for this channel - try again shortly")
        show, ep = result
        row = state.db.get_show(show.tmdb_id)
        return {
            "channel": channel,
            **_public(info[show.tmdb_id]),
            "season": ep["season"],
            "episode": ep["episode"],
            "code": f"S{ep['season']:02d}E{ep['episode']:02d}",
            "title": ep["title"],
            "overview": ep["overview"],
            "air_date": ep["air_date"],
            "runtime": ep["runtime"],
            "still_url": image_url(ep["still_path"]) or image_url(row["backdrop_path"] if row else None),
        }

    @app.post("/api/play")
    async def play(req: PlayRequest):
        s = state.settings
        if not (s.enable_adb and s.tv_ip):
            raise HTTPException(404, "Play on TV is disabled (set ENABLE_ADB=true and TV_IP)")
        # URLs are rebuilt server-side from config + TMDB data, never taken from the client.
        show = state.config.find_show(req.tmdb_id)
        if show is None:
            raise HTTPException(404, "unknown show")
        access = _show_infos([show])[show.tmdb_id]["_access"]
        options = access.options
        if req.provider_id is not None:
            options = [o for o in options if o.provider_id == req.provider_id]
        if not options:
            raise HTTPException(404, "no watch option for this show")
        try:
            out = await adb.play_on_tv(s.tv_ip, s.adb_port, options[0].url)
        except (adb.ADBError, FileNotFoundError) as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "output": out}

    @app.post("/api/refresh")
    async def refresh():
        """Reload config.yaml and force a full TMDB refresh in the background."""
        try:
            state.config = load_config(state.settings.config_path)
        except (ConfigError, OSError) as e:
            raise HTTPException(400, f"config error: {e}")
        if state.syncer is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        task = asyncio.create_task(state.syncer.run_once(state.config, force=True))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return {"ok": True}

    return app


def _show_infos(shows: list[ShowConfig]) -> dict[int, dict]:
    rows = state.db.get_shows([s.tmdb_id for s in shows])
    out = {}
    for show in shows:
        row = rows.get(show.tmdb_id)
        name = show.name or (row["name"] if row and row["name"] else f"TMDB #{show.tmdb_id}")
        providers = json.loads(row["providers_json"]) if row and row["providers_json"] else None
        access: Access = compute_access(state.config, show, name, providers)
        out[show.tmdb_id] = {
            "tmdb_id": show.tmdb_id,
            "show_name": name,
            "channels": list(show.channels),
            "poster_url": image_url(row["poster_path"], "w185") if row else None,
            "access": access.to_dict(),
            "_access": access,
            "_watchable": access.watchable,
        }
    return out


def _public(info: dict) -> dict:
    return {k: v for k, v in info.items() if not k.startswith("_")}


app = create_app()
