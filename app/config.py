"""Settings (env vars) and library config (config.yaml)."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    tmdb_api_key: str = ""
    config_path: Path = Path("/config/config.yaml")
    db_path: Path = Path("/data/deadair.db")
    watch_region: str = "US"
    episode_refresh_days: float = 7
    provider_check_hours: float = 24
    wikidata_links: bool = True  # look up show-page links on Wikidata
    update_check: bool = True    # ask GitHub daily whether a newer DeadAir exists
    # Creates the first admin at startup if there are no users yet. Without
    # these, the first visitor is asked to create the admin account.
    admin_user: str = ""
    admin_password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tmdb_api_key=os.environ.get("TMDB_API_KEY", "").strip(),
            config_path=Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
            db_path=Path(os.environ.get("DB_PATH", "/data/deadair.db")),
            watch_region=os.environ.get("WATCH_REGION", "US").upper(),
            episode_refresh_days=float(os.environ.get("EPISODE_REFRESH_DAYS", "7")),
            provider_check_hours=float(os.environ.get("PROVIDER_CHECK_HOURS", "24")),
            wikidata_links=_env_bool("WIKIDATA_LINKS", True),
            update_check=_env_bool("UPDATE_CHECK", True),
            admin_user=os.environ.get("ADMIN_USER", "").strip(),
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        )


@dataclass(frozen=True)
class ShowConfig:
    tmdb_id: int
    channels: tuple[str, ...]
    weight: float = 1.0
    name: str | None = None  # optional display-name override
    title: str | None = None  # TMDB title, informational (keeps the YAML readable)
    # Optional exact deep links, keyed by service name; beats the search link.
    links: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    # A TMDB episode group id (e.g. a show's DVD order) to number episodes by.
    # Only the S01E02 labels change; episodes are still tracked by TMDB's numbers.
    episode_order: str | None = None
    # Episodes never to pick, e.g. ones pulled from streaming: "S06E10" in
    # TMDB's numbering. Set with Ban after a Skip; applies to everyone.
    never_pick: tuple[str, ...] = ()


@dataclass
class AppConfig:
    services: list[str] = field(default_factory=list)
    include_free: bool = True
    include_rent_buy: bool = True
    cooldown_days: float = 14  # watched/skipped episodes sit out this long
    search_urls: dict[str, str] = field(default_factory=dict)
    # Optional per-channel settings, e.g. {"scifi": {"emoji": "🚀"}}. Channels
    # themselves come from show tags; this only decorates them.
    channel_meta: dict[str, dict[str, str]] = field(default_factory=dict)
    shows: list[ShowConfig] = field(default_factory=list)

    def channel_emoji(self, name: str) -> str | None:
        return self.channel_meta.get(name, {}).get("emoji") or None

    def channel_balance(self, name: str) -> str:
        """How the channel's shows share its picks (see BALANCE_MODES)."""
        return self.channel_meta.get(name, {}).get("balance") or DEFAULT_BALANCE

    @property
    def channels(self) -> dict[str, list[ShowConfig]]:
        """Channel name -> shows tagged with it, in order of first appearance."""
        out: dict[str, list[ShowConfig]] = {}
        for show in self.shows:
            for ch in show.channels:
                out.setdefault(ch, []).append(show)
        return out

    def find_show(self, tmdb_id: int) -> ShowConfig | None:
        return next((s for s in self.shows if s.tmdb_id == tmdb_id), None)


# How a channel's shows share the picks, by episode count n (times the show's weight):
#   episodes: n        - every episode equally likely; long shows dominate
#   sqrt:     sqrt(n)  - long shows still come up more, short ones aren't buried
#   shows:    1        - every show equally likely
# Set per channel (channels: {scifi: {balance: shows}}); sqrt is the default.
BALANCE_MODES = ("episodes", "sqrt", "shows")
DEFAULT_BALANCE = "sqrt"

_EPISODE_CODE = re.compile(r"^S(\d+)E(\d+)$", re.I)


def episode_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def parse_episode_code(code: str) -> tuple[int, int] | None:
    m = _EPISODE_CODE.match(str(code).strip())
    return (int(m[1]), int(m[2])) if m else None


def _episode_codes(value, where: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected a list like [S06E10]")
    out = []
    for code in value:
        parsed = parse_episode_code(code)
        if parsed is None:
            raise ConfigError(f"{where}: {code!r} isn't an episode code like S06E10")
        out.append(episode_code(*parsed))
    return tuple(sorted(set(out)))


class ConfigError(ValueError):
    pass


def _str_map(value: object, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be a mapping")
    return {str(k): str(v) for k, v in value.items()}


def parse_config(data: object) -> AppConfig:
    if data is None:  # empty file
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping with 'services:' and 'shows:'")

    services = data.get("services") or []
    if not isinstance(services, list):
        raise ConfigError("'services' must be a list of service names")

    raw_shows = data.get("shows") or []
    if not isinstance(raw_shows, list):
        raise ConfigError("'shows' must be a list")

    shows: list[ShowConfig] = []
    seen: set[int] = set()
    for i, show in enumerate(raw_shows):
        where = f"show #{i + 1}"
        if not isinstance(show, dict) or not show.get("tmdb_id"):
            raise ConfigError(f"{where}: needs a tmdb_id")
        try:
            tmdb_id = int(show["tmdb_id"])
            weight = float(show.get("weight", 1.0))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{where}: {e}") from e
        where = f"show {tmdb_id}"
        if tmdb_id in seen:
            raise ConfigError(f"{where}: listed twice (give it several channels instead)")
        seen.add(tmdb_id)
        if weight <= 0:
            raise ConfigError(f"{where}: weight must be > 0")
        channels = show.get("channels")
        if isinstance(channels, str):
            channels = [channels]
        if isinstance(channels, list):
            channels = [str(c).strip() for c in channels if str(c).strip()]
        if not isinstance(channels, list) or not channels:
            raise ConfigError(f"{where}: needs at least one channel, e.g. channels: [sitcom]")
        shows.append(
            ShowConfig(
                tmdb_id=tmdb_id,
                channels=tuple(dict.fromkeys(str(c) for c in channels)),
                weight=weight,
                name=str(show["name"]) if show.get("name") else None,
                title=str(show["title"]) if show.get("title") else None,
                links=_str_map(show.get("links"), f"{where} links"),
                episode_order=str(show["episode_order"]).strip() or None if show.get("episode_order") else None,
                never_pick=_episode_codes(show.get("never_pick"), f"{where} never_pick"),
            )
        )

    try:
        cooldown = float(data.get("cooldown_days", 14))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"cooldown_days: {e}") from e
    if cooldown < 0:
        raise ConfigError("cooldown_days can't be negative")

    channel_meta = _parse_channel_meta(data.get("channels"))

    return AppConfig(
        services=list(dict.fromkeys(str(s).strip() for s in services if str(s).strip())),
        include_free=bool(data.get("include_free", True)),
        include_rent_buy=bool(data.get("include_rent_buy", True)),
        cooldown_days=cooldown,
        search_urls=_str_map(data.get("search_urls"), "search_urls"),
        channel_meta=channel_meta,
        shows=shows,
    )


MAX_EMOJI_LEN = 16  # one emoji can be several code points (skin tones, ZWJ sequences, flags)


def _parse_channel_meta(raw: object) -> dict[str, dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'channels' must be a mapping like  scifi: {emoji: 🚀, balance: shows}")
    out: dict[str, dict[str, str]] = {}
    for name, meta in raw.items():
        where = f"channels.{name}"
        if isinstance(meta, list):
            raise ConfigError(
                f"{where}: channels no longer list shows - tag each show with "
                "`channels: [...]` instead, and use this section only for extras like emoji"
            )
        if meta is None:
            continue
        if not isinstance(meta, dict):
            raise ConfigError(f"{where}: must be a mapping, e.g. {{emoji: 🚀}}")
        entry: dict[str, str] = {}
        emoji = str(meta.get("emoji") or "").strip()
        if len(emoji) > MAX_EMOJI_LEN:
            raise ConfigError(f"{where}.emoji: use a single emoji")
        if emoji:
            entry["emoji"] = emoji
        balance = str(meta.get("balance") or DEFAULT_BALANCE).strip().lower()
        if balance not in BALANCE_MODES:
            raise ConfigError(f"{where}.balance: use one of {', '.join(BALANCE_MODES)}")
        if balance != DEFAULT_BALANCE:
            entry["balance"] = balance
        if entry:
            out[str(name).strip()] = entry
    return out


HEADER = "# DeadAir config. Edited by the web UI (Settings); hand edits are fine too.\n"


def dump_config(cfg: AppConfig) -> str:
    shows = []
    for s in cfg.shows:
        d: dict = {"tmdb_id": s.tmdb_id}
        if s.title:
            d["title"] = s.title
        d["channels"] = list(s.channels)
        if s.weight != 1:
            d["weight"] = s.weight
        if s.name:
            d["name"] = s.name
        if s.links:
            d["links"] = dict(s.links)
        if s.episode_order:
            d["episode_order"] = s.episode_order
        if s.never_pick:
            d["never_pick"] = list(s.never_pick)
        shows.append(d)
    data: dict = {
        "services": list(cfg.services),
        "include_free": cfg.include_free,
        "include_rent_buy": cfg.include_rent_buy,
        "cooldown_days": int(cfg.cooldown_days) if cfg.cooldown_days.is_integer() else cfg.cooldown_days,
    }
    if cfg.search_urls:
        data["search_urls"] = dict(cfg.search_urls)
    if cfg.channel_meta:
        data["channels"] = {name: dict(meta) for name, meta in sorted(cfg.channel_meta.items())}
    data["shows"] = shows
    return HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def content_version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def read_config_text(path: Path) -> str:
    """The raw file, or "" if it doesn't exist yet (fresh install)."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load_config(path: Path) -> AppConfig:
    return parse_config(yaml.safe_load(read_config_text(path)))


def save_config(path: Path, text: str) -> None:
    """Write atomically, keeping the previous file as <name>.bak."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = read_config_text(path)
    if old:
        path.with_name(path.name + ".bak").write_text(old, encoding="utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.replace(tmp, path)
        except OSError:
            # A single file bind-mounted into Docker can't be replaced by
            # rename (EBUSY); fall back to rewriting it in place.
            path.write_text(text, encoding="utf-8")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
