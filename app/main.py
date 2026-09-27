"""DeadAir TV: pick a channel, get a random episode."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adb
from .access import Access, compute_access
from .config import (
    AppConfig,
    ConfigError,
    Settings,
    ShowConfig,
    content_version,
    dump_config,
    parse_config,
    read_config_text,
    save_config,
)
from .db import Database
from .picker import pick_episode
from .sync import Syncer
from .tmdb import TMDBClient, image_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deadair")

STATIC_DIR = Path(__file__).parent / "static"


PROVIDER_LIST_TTL_S = 86400


class State:
    settings: Settings
    db: Database
    config: AppConfig
    config_text: str = ""
    config_error: str | None = None
    tmdb: TMDBClient | None = None
    syncer: Syncer | None = None
    provider_list: tuple[float, list[dict]] | None = None


state = State()
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def current_config() -> AppConfig:
    """The live config, re-read whenever config.yaml changes on disk.

    A broken hand edit keeps the last good config and surfaces the error.
    """
    text = read_config_text(state.settings.config_path)
    if text != state.config_text:
        try:
            state.config = parse_config(yaml.safe_load(text))
            state.config_error = None
            if state.syncer:
                _spawn(state.syncer.run_once(state.config))  # fetch newly added shows
        except (ConfigError, yaml.YAMLError) as e:
            state.config_error = str(e)
            log.warning("config.yaml invalid, keeping previous config: %s", e)
        state.config_text = text
    return state.config


class ShowIn(BaseModel):
    tmdb_id: int
    channels: list[str]
    weight: float = 1.0
    name: str | None = None
    title: str | None = None
    links: dict[str, str] = {}


class ConfigIn(BaseModel):
    version: str  # from GET /api/config; guards against overwriting newer edits
    services: list[str]
    include_free: bool = True
    include_rent_buy: bool = True
    search_urls: dict[str, str] = {}
    shows: list[ShowIn]


class PlayRequest(BaseModel):
    tmdb_id: int
    provider_id: int | None = None  # which watch option; default = the first


def create_app(settings: Settings | None = None, start_sync: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.settings = settings
        state.db = Database(settings.db_path)
        state.config = AppConfig()
        state.config_text = None  # force the first load
        state.syncer = None
        state.provider_list = None
        current_config()
        if state.config_error:
            raise RuntimeError(f"{settings.config_path}: {state.config_error}")
        task = None
        if settings.tmdb_api_key:
            state.tmdb = TMDBClient(settings.tmdb_api_key)
            state.syncer = Syncer(settings, state.db, state.tmdb)
            if start_sync:
                task = asyncio.create_task(state.syncer.loop(current_config))
        else:
            log.warning("TMDB_API_KEY not set; episode data will not be fetched")
        yield
        if task:
            task.cancel()
        if state.tmdb:
            await state.tmdb.aclose()
            state.tmdb = None

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
        info = _show_infos(current_config().shows)
        out = []
        for name, shows in current_config().channels.items():
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
            "services": current_config().services,
            "adb_enabled": state.settings.enable_adb and bool(state.settings.tv_ip),
            "sync_error": state.syncer.last_error if state.syncer else "TMDB_API_KEY not set",
            "config_error": state.config_error,
        }

    @app.get("/api/shows")
    async def shows():
        """Every configured show and where you can watch it."""
        info = _show_infos(current_config().shows)
        return {"shows": [_public(info[s.tmdb_id]) for s in current_config().shows]}

    @app.get("/api/pick")
    async def pick(channel: str):
        shows = current_config().channels.get(channel)
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
        show = current_config().find_show(req.tmdb_id)
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
        """Force a full TMDB re-sync in the background."""
        cfg = current_config()
        if state.syncer is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        _spawn(state.syncer.run_once(cfg, force=True))
        return {"ok": True}

    # --- settings -----------------------------------------------------------

    @app.get("/api/config")
    async def get_config():
        cfg = current_config()
        rows = state.db.get_shows([s.tmdb_id for s in cfg.shows])
        shows = []
        for s in cfg.shows:
            row = rows.get(s.tmdb_id)
            shows.append(
                {
                    "tmdb_id": s.tmdb_id,
                    "channels": list(s.channels),
                    "weight": s.weight,
                    "name": s.name,
                    "title": (row["name"] if row and row["name"] else None) or s.title,
                    "links": s.links,
                    "poster_url": image_url(row["poster_path"], "w185") if row else None,
                }
            )
        return {
            "version": content_version(state.config_text or ""),
            "config_error": state.config_error,
            "services": cfg.services,
            "include_free": cfg.include_free,
            "include_rent_buy": cfg.include_rent_buy,
            "search_urls": cfg.search_urls,
            "shows": shows,
        }

    @app.put("/api/config")
    async def put_config(body: ConfigIn):
        current_config()
        if body.version != content_version(state.config_text or ""):
            raise HTTPException(409, "config.yaml changed since you opened Settings - reload and try again")
        data = body.model_dump(exclude={"version"})
        try:
            cfg = parse_config(data)
        except ConfigError as e:
            raise HTTPException(400, str(e))
        text = dump_config(cfg)
        try:
            save_config(state.settings.config_path, text)
        except OSError as e:
            raise HTTPException(500, f"couldn't write {state.settings.config_path}: {e}")
        current_config()  # picks up the new file and starts syncing new shows
        return {"ok": True, "version": content_version(text)}

    @app.get("/api/tmdb/search")
    async def tmdb_search(q: str):
        if state.tmdb is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        q = q.strip()
        if not q:
            return {"results": []}
        try:
            return {"results": await state.tmdb.search_tv(q)}
        except httpx.HTTPError as e:
            raise HTTPException(502, f"TMDB search failed: {e}")

    @app.get("/api/tmdb/providers")
    async def tmdb_providers():
        """All TV providers in your region, for picking your services."""
        if state.tmdb is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        cached = state.provider_list
        if not cached or time.time() - cached[0] > PROVIDER_LIST_TTL_S:
            try:
                providers = await state.tmdb.list_tv_providers(state.settings.watch_region)
            except httpx.HTTPError as e:
                raise HTTPException(502, f"TMDB provider list failed: {e}")
            state.provider_list = cached = (time.time(), providers)
        return {"providers": cached[1]}

    return app


def _show_infos(shows: list[ShowConfig]) -> dict[int, dict]:
    rows = state.db.get_shows([s.tmdb_id for s in shows])
    out = {}
    for show in shows:
        row = rows.get(show.tmdb_id)
        name = show.name or (row["name"] if row and row["name"] else None) or show.title or f"TMDB #{show.tmdb_id}"
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
