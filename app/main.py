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
from .config import ChannelConfig, ConfigError, Settings, ShowConfig, load_channels
from .db import Database
from .picker import pick_episode
from .sync import Syncer
from .tmdb import TMDBClient, image_url, service_available

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deadair")

STATIC_DIR = Path(__file__).parent / "static"


class State:
    settings: Settings
    db: Database
    channels: ChannelConfig
    syncer: Syncer | None = None


state = State()


class PlayRequest(BaseModel):
    channel: str
    tmdb_id: int

_background: set[asyncio.Task] = set()


def create_app(settings: Settings | None = None, start_sync: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.settings = settings
        state.db = Database(settings.db_path)
        state.channels = load_channels(settings.config_path)
        client = None
        task = None
        if settings.tmdb_api_key:
            client = TMDBClient(settings.tmdb_api_key)
            state.syncer = Syncer(settings, state.db, client)
            if start_sync:
                task = asyncio.create_task(state.syncer.loop(lambda: state.channels))
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
        cfg = state.channels
        rows = state.db.get_shows([s.tmdb_id for s in cfg.all_shows()])
        out = []
        for name, shows in cfg.channels.items():
            out.append(
                {
                    "name": name,
                    "shows": [_show_status(s, rows.get(s.tmdb_id)) for s in shows],
                }
            )
        return {
            "channels": out,
            "adb_enabled": state.settings.enable_adb and bool(state.settings.tv_ip),
            "sync_error": state.syncer.last_error if state.syncer else "TMDB_API_KEY not set",
        }

    @app.get("/api/pick")
    async def pick(channel: str):
        shows = state.channels.channels.get(channel)
        if shows is None:
            raise HTTPException(404, f"unknown channel {channel!r}")
        result = pick_episode(state.db, shows)
        if result is None:
            raise HTTPException(503, "no episodes cached yet for this channel - try again shortly")
        show, ep = result
        row = state.db.get_show(show.tmdb_id)
        status = _show_status(show, row)
        return {
            "channel": channel,
            **status,
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
        # Only launch URLs from our own config, never arbitrary client input.
        show = state.channels.find_show(req.channel, req.tmdb_id)
        if show is None:
            raise HTTPException(404, "show not found in channel")
        try:
            out = await adb.play_on_tv(s.tv_ip, s.adb_port, show.show_url)
        except (adb.ADBError, FileNotFoundError) as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "output": out}

    @app.post("/api/refresh")
    async def refresh():
        """Reload channels.yaml and force a full TMDB refresh in the background."""
        try:
            state.channels = load_channels(state.settings.config_path)
        except (ConfigError, OSError) as e:
            raise HTTPException(400, f"config error: {e}")
        if state.syncer is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        task = asyncio.create_task(state.syncer.run_once(state.channels, force=True))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return {"ok": True}

    return app


def _show_status(show: ShowConfig, row) -> dict:
    providers = json.loads(row["providers_json"]) if row and row["providers_json"] else None
    return {
        "tmdb_id": show.tmdb_id,
        "show_name": show.name or (row["name"] if row and row["name"] else f"TMDB #{show.tmdb_id}"),
        "service": show.service,
        "show_url": show.show_url,
        # None = not checked yet
        "available": None if providers is None else service_available(show.service, providers),
        "providers": [p["provider_name"] for p in providers] if providers else [],
    }


app = create_app()
