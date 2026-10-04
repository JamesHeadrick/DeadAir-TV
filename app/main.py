"""DeadAir: pick a channel, get a random episode."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth
from .access import Access, builtin_search_url, compute_access
from .config import (
    AppConfig,
    ConfigError,
    Settings,
    ShowConfig,
    content_version,
    dump_config,
    episode_code,
    parse_config,
    parse_episode_code,
    read_config_text,
    save_config,
)
from .db import Database
from .picker import pick_episode
from .sync import Syncer
from .tmdb import TMDBClient, image_url
from .updates import UpdateChecker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("deadair")

STATIC_DIR = Path(__file__).parent / "static"
# Not in every system's MIME table (e.g. slim Docker images).
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _render_index() -> str:
    """index.html with a content hash on each static/ URL (static/app.js?v=3f9c...).

    A changed file gets a new URL, so no browser or proxy cache can pair a new
    page with old JS/CSS.
    """
    def versioned(m: re.Match) -> str:
        file = STATIC_DIR / m.group(2)
        if not file.is_file():
            return m.group(0)
        digest = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
        return f'{m.group(1)}static/{m.group(2)}?v={digest}"'

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return re.sub(r'((?:src|href)=")static/([\w.-]+)"', versioned, html)


PROVIDER_LIST_TTL_S = 86400
CHANNEL_POSTERS = 4  # show posters fanned out on each channel button
ALL_CHANNELS = "*"   # the "Surprise me" channel: every show


class State:
    settings: Settings
    db: Database
    config: AppConfig
    config_text: str = ""
    config_error: str | None = None
    tmdb: TMDBClient | None = None
    syncer: Syncer | None = None
    provider_list: tuple[float, list[dict]] | None = None
    updates: UpdateChecker | None = None


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
    episode_order: str | None = None
    never_pick: list[str] = []


class ConfigIn(BaseModel):
    version: str  # from GET /api/config; guards against overwriting newer edits
    services: list[str]
    include_free: bool = True
    include_rent_buy: bool = True
    cooldown_days: float = 14
    balance: str = "sqrt"
    search_urls: dict[str, str] = {}
    channels: dict[str, dict[str, str]] = {}  # per-channel extras, e.g. {"scifi": {"emoji": "🚀"}}
    shows: list[ShowIn]


class EpisodeRef(BaseModel):
    tmdb_id: int
    season: int
    episode: int


class HistoryIn(EpisodeRef):
    kind: Literal["watched", "skipped"]


class Credentials(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class NewUser(Credentials):
    is_admin: bool = False


class UserUpdate(BaseModel):
    password: str | None = None
    is_admin: bool | None = None


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_admin: bool


# /api routes reachable without logging in.
PUBLIC_API = {"/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/version"}
throttle = auth.LoginThrottle()


def current_user(request: Request) -> User:
    return request.state.user  # set by the auth middleware


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "admins only")
    return user


def _session_hash(request: Request) -> str | None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    return auth.token_hash(token) if token else None


def _start_session(request: Request, response: Response, user_id: int) -> None:
    token, token_hash = auth.new_session_token()
    ttl = auth.SESSION_DAYS * 86400
    state.db.create_session(token_hash, user_id, ttl)
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=int(ttl), httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )


def _user_json(u) -> dict:
    return {"id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"])}


def create_app(settings: Settings | None = None, start_sync: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.settings = settings
        state.db = Database(settings.db_path)
        if settings.admin_user and settings.admin_password and state.db.count_users() == 0:
            problem = auth.validate_new_credentials(settings.admin_user, settings.admin_password)
            if problem:
                raise RuntimeError(f"ADMIN_USER/ADMIN_PASSWORD: {problem}")
            state.db.create_user(settings.admin_user, auth.hash_password(settings.admin_password), True)
            log.info("created admin user %r from ADMIN_USER", settings.admin_user)
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

    app = FastAPI(title="DeadAir", lifespan=lifespan)

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        """Make browsers check for a new page/JS/CSS on every load.

        Without a Cache-Control header they cache static files for a guessed
        time, so after an update the new page could run with old JS. Unchanged
        files still come back as a cheap 304 via their ETag.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        """Every /api route needs a valid session unless listed in PUBLIC_API."""
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_API:
            token_hash = _session_hash(request)
            row = state.db.session_user(token_hash) if token_hash else None
            if row is None:
                return JSONResponse({"detail": "not logged in"}, status_code=401)
            request.state.user = User(row["id"], row["username"], bool(row["is_admin"]))
        return await call_next(request)

    # --- auth -----------------------------------------------------------------

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        token_hash = _session_hash(request)
        row = state.db.session_user(token_hash) if token_hash else None
        return {
            "user": _user_json(row) if row else None,
            "needs_setup": state.db.count_users() == 0,
        }

    @app.post("/api/auth/setup")
    async def auth_setup(body: Credentials, request: Request, response: Response):
        """Create the first (admin) account. Only works while there are no users."""
        if state.db.count_users() > 0:
            raise HTTPException(409, "already set up - log in instead")
        problem = auth.validate_new_credentials(body.username, body.password)
        if problem:
            raise HTTPException(400, problem)
        user_id = state.db.create_user(body.username, auth.hash_password(body.password), True)
        _start_session(request, response, user_id)
        return {"user": _user_json(state.db.get_user(user_id))}

    @app.post("/api/auth/login")
    async def login(body: Credentials, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if throttle.blocked(ip):
            raise HTTPException(429, "too many failed logins - try again in a few minutes")
        row = state.db.get_user_by_name(body.username.strip())
        if not auth.check_login(body.password, row["password_hash"] if row else None):
            throttle.failed(ip)
            await asyncio.sleep(1)
            raise HTTPException(401, "wrong username or password")
        throttle.succeeded(ip)
        _start_session(request, response, row["id"])
        return {"user": _user_json(row)}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        token_hash = _session_hash(request)
        if token_hash:
            state.db.delete_session(token_hash)
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/password")
    async def change_password(body: PasswordChange, request: Request, user: User = Depends(current_user)):
        row = state.db.get_user(user.id)
        if not auth.verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(400, "current password is wrong")
        problem = auth.validate_new_credentials(user.username, body.new_password)
        if problem:
            raise HTTPException(400, problem)
        state.db.update_user(user.id, password_hash=auth.hash_password(body.new_password))
        state.db.delete_user_sessions(user.id, except_hash=_session_hash(request))  # log out other devices
        return {"ok": True}

    # --- users (admin) -------------------------------------------------------

    @app.get("/api/users")
    async def list_users(_: User = Depends(admin_user)):
        return {"users": [_user_json(u) for u in state.db.list_users()]}

    @app.post("/api/users")
    async def create_user(body: NewUser, _: User = Depends(admin_user)):
        problem = auth.validate_new_credentials(body.username, body.password)
        if problem:
            raise HTTPException(400, problem)
        try:
            user_id = state.db.create_user(body.username, auth.hash_password(body.password), body.is_admin)
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"username {body.username!r} is taken")
        return {"user": _user_json(state.db.get_user(user_id))}

    @app.patch("/api/users/{user_id}")
    async def update_user(user_id: int, body: UserUpdate, me: User = Depends(admin_user)):
        target = state.db.get_user(user_id)
        if target is None:
            raise HTTPException(404, "no such user")
        if body.is_admin is False and target["is_admin"] and state.db.count_admins() == 1:
            raise HTTPException(400, "can't demote the last admin")
        password_hash = None
        if body.password is not None:
            problem = auth.validate_new_credentials(target["username"], body.password)
            if problem:
                raise HTTPException(400, problem)
            password_hash = auth.hash_password(body.password)
        state.db.update_user(user_id, password_hash=password_hash, is_admin=body.is_admin)
        if password_hash and user_id != me.id:
            state.db.delete_user_sessions(user_id)  # a reset password logs them out everywhere
        return {"user": _user_json(state.db.get_user(user_id))}

    @app.delete("/api/users/{user_id}")
    async def delete_user(user_id: int, me: User = Depends(admin_user)):
        if user_id == me.id:
            raise HTTPException(400, "you can't delete your own account")
        if state.db.get_user(user_id) is None:
            raise HTTPException(404, "no such user")
        state.db.delete_user(user_id)
        return {"ok": True}

    index_html = _render_index()

    @app.get("/", include_in_schema=False)
    async def index():
        return HTMLResponse(index_html)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/version")
    async def version():
        """Build info baked into the image by CI (see the Dockerfile's build args)."""
        return _build_info()

    @app.get("/api/updates")
    async def updates(_: User = Depends(admin_user)):
        """Whether a newer DeadAir is out (admins only; cached, GitHub is asked daily)."""
        if not state.settings.update_check:
            return {"enabled": False, "update": None, "checked_at": None}
        info = _build_info()
        if state.updates is None or (state.updates.version, state.updates.commit) != (info["version"], info["commit_full"]):
            state.updates = UpdateChecker(info["version"], info["commit_full"])
        return {"enabled": True, **await state.updates.get()}

    @app.get("/api/channels")
    async def channels():
        info = _show_infos(current_config().shows)
        out = []
        for name, shows in sorted(current_config().channels.items(), key=lambda kv: kv[0].casefold()):
            watchable = [info[s.tmdb_id] for s in shows if info[s.tmdb_id]["_watchable"]]
            out.append(
                {
                    "name": name,
                    "emoji": current_config().channel_emoji(name),
                    "shows": [info[s.tmdb_id]["show_name"] for s in shows],
                    "unwatchable": [
                        info[s.tmdb_id]["show_name"] for s in shows if not info[s.tmdb_id]["_watchable"]
                    ],
                    "posters": _random_posters(watchable),
                }
            )
        # "Surprise me": every show at once (shown when there's more than one channel).
        everything = [i for s in current_config().shows if (i := info[s.tmdb_id])["_watchable"]]
        return {
            "channels": out,
            "all_channels": {
                "name": ALL_CHANNELS,
                "shows": len(current_config().shows),
                "posters": _random_posters(everything),
            },
            "services": current_config().services,
            "sync_error": state.syncer.last_error if state.syncer else "TMDB_API_KEY not set",
            "config_error": state.config_error,
            "cooldown_days": current_config().cooldown_days,
        }

    @app.get("/api/shows")
    async def shows():
        """Every configured show and where you can watch it."""
        info = _show_infos(current_config().shows)
        return {"shows": [_public(info[s.tmdb_id]) for s in current_config().shows]}

    @app.get("/api/access")
    async def show_access(tmdb_id: int, season: int | None = None):
        """Where to watch one show (or one season of it), e.g. to refresh a
        pick after its links were edited."""
        show = current_config().find_show(tmdb_id)
        if show is None:
            raise HTTPException(404, "unknown show")
        info = _show_infos([show])[tmdb_id]
        return {"access": info["_season_access"].get(season, info["_access"]).to_dict()}

    @app.get("/api/pick")
    async def pick(
        channel: str,
        user: User = Depends(current_user),
        show: int | None = None,
        skip_show: list[int] = Query(default=[]),
        skip_ep: list[str] = Query(default=[]),
    ):
        """Random episode from a channel.

        show:      only pick from this show ("another episode").
        skip_show: shows to leave out ("different show").
        skip_ep:   "tmdb_id:season:episode" keys to avoid (already seen).
        """
        cfg = current_config()
        shows = list(cfg.shows) if channel == ALL_CHANNELS else cfg.channels.get(channel)
        if shows is None:
            raise HTTPException(404, f"unknown channel {channel!r}")
        info = _show_infos(shows)
        watchable = [s for s in shows if info[s.tmdb_id]["_watchable"]]
        if show is not None:  # one show only ("another episode", or rolling a show from All shows)
            if show not in info:
                raise HTTPException(404, "unknown show")
            if not info[show]["_watchable"]:
                raise HTTPException(404, f"{info[show]['show_name']} isn't on your services right now")
        if not watchable:
            raise HTTPException(404, "none of this channel's shows are on your services")
        candidates = [s for s in watchable if s.tmdb_id not in skip_show]
        if show is not None:
            candidates = [s for s in watchable if s.tmdb_id == show]
        if not candidates:
            raise HTTPException(404, "no other shows left in this channel")
        cooling = state.db.cooldown_keys(user.id, _cooldown_since())
        result = pick_episode(
            state.db, candidates, exclude_episodes=[*skip_ep[-500:], *cooling],
            skip_seasons=[k for s in candidates for k in info[s.tmdb_id]["_skip_seasons"]],
            banned=[k for s in candidates for k in _banned_keys(s)],
            balance=cfg.balance,
        )
        if result is None:
            raise HTTPException(503, "no episodes cached yet for this channel - try again shortly")
        picked, ep = result
        picked_info = info[picked.tmdb_id]
        # Where to watch this episode's season (a service may only have some seasons).
        season_access = picked_info["_season_access"].get(ep["season"], picked_info["_access"])
        row = state.db.get_show(picked.tmdb_id)
        return {
            "channel": channel,
            # Emoji for the picked channel and the show's other channels (only those that have one).
            "channel_emoji": {
                c: e for c in {channel, *picked.channels} if (e := cfg.channel_emoji(c))
            },
            # How many other shows "Different show" could still offer.
            "other_shows": sum(1 for s in watchable if s.tmdb_id not in skip_show and s is not picked),
            **_public(picked_info),
            "access": season_access.to_dict(),
            "season": ep["season"],
            "episode": ep["episode"],
            # In the show's chosen episode order, if any; season/episode stay TMDB's.
            "code": _episode_code(row, ep["season"], ep["episode"]),
            "title": ep["title"],
            "overview": ep["overview"],
            "air_date": ep["air_date"],
            "runtime": ep["runtime"],
            "still_url": image_url(ep["still_path"]) or image_url(row["backdrop_path"] if row else None),
            "history": _history(user.id, picked.tmdb_id, ep["season"], ep["episode"]),
        }

    # --- watched / skipped --------------------------------------------------

    @app.post("/api/history")
    async def add_history(body: HistoryIn, user: User = Depends(current_user)):
        """Mark an episode watched or skipped; either puts it on cooldown (for you)."""
        if current_config().find_show(body.tmdb_id) is None:
            raise HTTPException(404, "unknown show")
        entry_id = state.db.add_history(user.id, body.tmdb_id, body.season, body.episode, body.kind)
        # The id lets the app undo this mark (e.g. a Skip tapped by mistake).
        return {"id": entry_id, "history": _history(user.id, body.tmdb_id, body.season, body.episode)}

    @app.post("/api/ban")
    async def ban_episode(body: EpisodeRef, _: User = Depends(admin_user)):
        """Never pick this episode again, for anyone (e.g. it was pulled from streaming).

        Saved on the show in config.yaml as never_pick; unban it in the show's editor.
        """
        cfg = current_config()
        show = cfg.find_show(body.tmdb_id)
        if show is None:
            raise HTTPException(404, "unknown show")
        code = episode_code(body.season, body.episode)
        if code not in show.never_pick:
            banned = dataclasses.replace(show, never_pick=tuple(sorted({*show.never_pick, code})))
            new_cfg = dataclasses.replace(cfg, shows=[banned if s is show else s for s in cfg.shows])
            try:
                save_config(state.settings.config_path, dump_config(new_cfg))
            except OSError as e:
                raise HTTPException(500, f"couldn't write {state.settings.config_path}: {e}")
            current_config()
        return {"ok": True, "never_pick": list(current_config().find_show(body.tmdb_id).never_pick)}

    @app.post("/api/history/unwatch")
    async def unwatch(body: EpisodeRef, user: User = Depends(current_user)):
        """Undo a 'watched' mark made during the current cooldown window."""
        state.db.undo_watched(user.id, body.tmdb_id, body.season, body.episode, _cooldown_since())
        return {"history": _history(user.id, body.tmdb_id, body.season, body.episode)}

    @app.get("/api/history")
    async def recent_history(user: User = Depends(current_user), limit: int = Query(20, ge=1, le=100)):
        """Your latest watched/skipped marks, newest first."""
        cfg = current_config()
        rows = state.db.recent_history(user.id, limit)
        shows = state.db.get_shows(list({r["tmdb_id"] for r in rows}))
        since = _cooldown_since()
        out = []
        for r in rows:
            show, row = cfg.find_show(r["tmdb_id"]), shows.get(r["tmdb_id"])
            name = (show and show.name) or (row and row["name"]) or (show and show.title) or f"TMDB #{r['tmdb_id']}"
            out.append({
                "id": r["id"], "tmdb_id": r["tmdb_id"], "show_name": name,
                "code": _episode_code(row, r["season"], r["episode"]), "title": r["title"],
                "kind": r["kind"], "at": r["at"], "cooling_down": r["at"] >= since,
            })
        return {"history": out}

    @app.delete("/api/history/{entry_id}")
    async def undo_history(entry_id: int, user: User = Depends(current_user)):
        """Undo one of your marks (the episode can come up again)."""
        if not state.db.delete_history_entry(user.id, entry_id):
            raise HTTPException(404, "no such history entry")
        return {"ok": True}

    @app.delete("/api/history")
    async def clear_history(user: User = Depends(current_user)):
        """Clears only your own watched/skipped history."""
        return {"deleted": state.db.clear_history(user.id)}

    @app.post("/api/refresh")
    async def refresh(_: User = Depends(admin_user)):
        """Force a full TMDB re-sync in the background."""
        cfg = current_config()
        if state.syncer is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        _spawn(state.syncer.run_once(cfg, force=True))
        return {"ok": True}

    # --- settings -----------------------------------------------------------

    @app.get("/api/config")
    async def get_config(_: User = Depends(admin_user)):
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
                    "episode_order": s.episode_order,
                    "never_pick": list(s.never_pick),
                    # Labels for the editor's banned list, in the show's episode order.
                    "never_pick_info": {code: _episode_label(row, code) for code in s.never_pick},
                    "poster_url": image_url(row["poster_path"], "w185") if row else None,
                    "watch": _watch_fallbacks(cfg, s, row),
                }
            )
        return {
            "version": content_version(state.config_text or ""),
            "config_error": state.config_error,
            "services": cfg.services,
            "include_free": cfg.include_free,
            "include_rent_buy": cfg.include_rent_buy,
            "cooldown_days": cfg.cooldown_days,
            "balance": cfg.balance,
            "search_urls": cfg.search_urls,
            "channels": cfg.channel_meta,
            # What "Open" uses for each of your services when search_urls has no entry.
            "builtin_search_urls": {
                svc: url for svc in cfg.services if (url := builtin_search_url(svc))
            },
            "shows": shows,
        }

    @app.put("/api/config")
    async def put_config(body: ConfigIn, _: User = Depends(admin_user)):
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
    async def tmdb_search(q: str, _: User = Depends(admin_user)):
        if state.tmdb is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        q = q.strip()
        if not q:
            return {"results": []}
        try:
            return {"results": await state.tmdb.search_tv(q)}
        except httpx.HTTPError as e:
            raise HTTPException(502, f"TMDB search failed: {e}")

    @app.get("/api/tmdb/episode_groups")
    async def tmdb_episode_groups(tmdb_id: int, _: User = Depends(admin_user)):
        """A show's alternate episode orders, for its Episode order setting."""
        if state.tmdb is None:
            raise HTTPException(503, "TMDB_API_KEY not set")
        try:
            groups = await state.tmdb.list_episode_groups(tmdb_id)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"TMDB episode orders failed: {e}")
        # TMDB's own order, counted the same way as the groups (regular seasons).
        return {"groups": groups, "tmdb_count": state.db.count_episodes(tmdb_id)}

    @app.get("/api/tmdb/providers")
    async def tmdb_providers(_: User = Depends(admin_user)):
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
    ids = [s.tmdb_id for s in shows]
    rows = state.db.get_shows(ids)
    seasons_by_show = state.db.show_seasons(ids)
    out = {}
    for show in shows:
        row = rows.get(show.tmdb_id)
        name = show.name or (row["name"] if row and row["name"] else None) or show.title or f"TMDB #{show.tmdb_id}"
        providers = json.loads(row["providers_json"]) if row and row["providers_json"] else None
        auto_links = _auto_links(row)
        access: Access = compute_access(state.config, show, name, providers, auto_links)

        # Per season, when TMDB knows where seasons stream; seasons it has no
        # data for use the whole-show answer.
        by_season_json = json.loads(row["season_providers_json"]) if row and row["season_providers_json"] else {}
        season_access: dict[int, Access] = {}
        if providers is not None and by_season_json:
            for season in seasons_by_show[show.tmdb_id]:
                data = by_season_json.get(str(season))
                season_access[season] = (
                    compute_access(state.config, show, name, data, auto_links) if data else access
                )
        skipped = [s for s, a in season_access.items() if not a.watchable]
        watchable = len(skipped) < len(season_access) if season_access else access.watchable

        out[show.tmdb_id] = {
            "tmdb_id": show.tmdb_id,
            "show_name": name,
            "channels": list(show.channels),
            "poster_url": image_url(row["poster_path"], "w185") if row else None,
            "thumb_url": image_url(row["poster_path"], "w92") if row else None,
            "access": {**access.to_dict(), "by_season": _season_groups(season_access)},
            "_access": access,
            "_season_access": season_access,
            "_skip_seasons": [f"{show.tmdb_id}:{s}" for s in skipped],
            "_watchable": watchable,
            "watchable": watchable,  # for All shows' Roll button
        }
    return out


def _season_groups(season_access: dict[int, Access]) -> list[dict]:
    """Runs of seasons you'd watch in the same place, e.g. S1-5 on Netflix, S6-8
    nowhere. Empty when every season is the same (nothing worth showing)."""
    groups: list[dict] = []
    for season in sorted(season_access):
        a = season_access[season]
        where = {"tier": a.tier, "tier_label": a.to_dict()["tier_label"],
                 "providers": [o.provider_name for o in a.options]}
        if groups and groups[-1]["where"] == where:
            groups[-1]["seasons"].append(season)
        else:
            groups.append({"seasons": [season], "where": where})
    if len(groups) < 2:
        return []
    return [{"first": g["seasons"][0], "last": g["seasons"][-1], **g["where"]} for g in groups]


def _cooldown_since() -> float:
    return time.time() - current_config().cooldown_days * 86400


def _history(user_id: int, tmdb_id: int, season: int, episode: int) -> dict | None:
    """Your most recent watched/skipped mark, and whether it's still cooling down."""
    row = state.db.last_history(user_id, tmdb_id, season, episode)
    if row is None:
        return None
    return {"kind": row["kind"], "at": row["at"], "cooling_down": row["at"] >= _cooldown_since()}


def _watch_fallbacks(cfg: AppConfig, show: ShowConfig, row) -> list[dict]:
    """Where the show is watchable for you, and what Open would use there
    without a pasted link (for the Settings > Open links editor)."""
    providers = json.loads(row["providers_json"]) if row and row["providers_json"] else None
    name = show.name or (row["name"] if row and row["name"] else None) or show.title or f"TMDB #{show.tmdb_id}"
    bare = dataclasses.replace(show, links={})
    access = compute_access(cfg, bare, name, providers, _auto_links(row))
    return [
        {"provider_name": o.provider_name, "logo_url": o.logo_url, "fallback_url": o.url, "fallback_source": o.source}
        for o in access.options
    ]


def _auto_links(row) -> dict[str, str]:
    """Show-page links found on Wikidata for this show (service -> URL)."""
    try:
        return json.loads(row["links_json"] or "{}") if row else {}
    except (KeyError, IndexError, ValueError):
        return {}


def _random_posters(infos: list[dict]) -> list[str]:
    """Up to CHANNEL_POSTERS poster thumbnails, a different random few each time."""
    urls = [i["thumb_url"] for i in infos if i["thumb_url"]]
    return random.sample(urls, min(CHANNEL_POSTERS, len(urls)))


def _banned_keys(show: ShowConfig) -> list[str]:
    """A show's never_pick codes as episode keys ("tmdb_id:season:episode")."""
    keys = []
    for code in show.never_pick:
        season, episode = parse_episode_code(code)  # validated when the config was parsed
        keys.append(f"{show.tmdb_id}:{season}:{episode}")
    return keys


def _episode_label(row, code: str) -> dict:
    """Display code and title for a stored S06E10 (TMDB numbering)."""
    season, episode = parse_episode_code(code) or (0, 0)
    title = None
    if row is not None:
        ep = state.db.get_episode(row["tmdb_id"], season, episode)
        title = ep["title"] if ep else None
    return {"code": _episode_code(row, season, episode), "title": title}


def _episode_code(row, season: int, episode: int) -> str:
    """"S01E02", using the show's chosen episode order when it covers this episode."""
    if row and row["episode_order_json"]:
        mapped = json.loads(row["episode_order_json"]).get("map", {}).get(f"{season}:{episode}")
        if mapped:
            season, episode = mapped
    return f"S{season:02d}E{episode:02d}"


def _build_info() -> dict:
    commit = os.environ.get("GIT_COMMIT", "").strip()
    return {
        "version": os.environ.get("APP_VERSION", "").strip() or "dev",
        "commit": commit[:7] or None,
        "commit_full": commit or None,
        "built": os.environ.get("BUILD_DATE", "").strip() or None,
    }


def _public(info: dict) -> dict:
    return {k: v for k, v in info.items() if not k.startswith("_")}


app = create_app()
