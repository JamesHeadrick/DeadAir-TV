"""Settings (env vars) and library config (config.yaml)."""

from __future__ import annotations

import os
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
    enable_adb: bool = False
    tv_ip: str = ""
    adb_port: int = 5555

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            tmdb_api_key=os.environ.get("TMDB_API_KEY", "").strip(),
            config_path=Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")),
            db_path=Path(os.environ.get("DB_PATH", "/data/deadair.db")),
            watch_region=os.environ.get("WATCH_REGION", "US").upper(),
            episode_refresh_days=float(os.environ.get("EPISODE_REFRESH_DAYS", "7")),
            provider_check_hours=float(os.environ.get("PROVIDER_CHECK_HOURS", "24")),
            enable_adb=_env_bool("ENABLE_ADB"),
            tv_ip=os.environ.get("TV_IP", "").strip(),
            adb_port=int(os.environ.get("ADB_PORT", "5555")),
        )


@dataclass(frozen=True)
class ShowConfig:
    tmdb_id: int
    channels: tuple[str, ...]
    weight: float = 1.0
    name: str | None = None  # optional display-name override
    # Optional exact deep links, keyed by service name; beats the search link.
    links: dict[str, str] = field(default_factory=dict, hash=False, compare=False)


@dataclass
class AppConfig:
    services: list[str] = field(default_factory=list)
    include_free: bool = True
    include_rent_buy: bool = True
    search_urls: dict[str, str] = field(default_factory=dict)
    shows: list[ShowConfig] = field(default_factory=list)

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


class ConfigError(ValueError):
    pass


def _str_map(value: object, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be a mapping")
    return {str(k): str(v) for k, v in value.items()}


def parse_config(data: object) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping with 'services:' and 'shows:'")

    services = data.get("services") or []
    if not isinstance(services, list):
        raise ConfigError("'services' must be a list of service names")

    raw_shows = data.get("shows")
    if not isinstance(raw_shows, list) or not raw_shows:
        raise ConfigError("'shows' must be a non-empty list")

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
        if not isinstance(channels, list) or not channels:
            raise ConfigError(f"{where}: needs at least one channel, e.g. channels: [sitcom]")
        shows.append(
            ShowConfig(
                tmdb_id=tmdb_id,
                channels=tuple(dict.fromkeys(str(c) for c in channels)),
                weight=weight,
                name=str(show["name"]) if show.get("name") else None,
                links=_str_map(show.get("links"), f"{where} links"),
            )
        )

    return AppConfig(
        services=[str(s) for s in services],
        include_free=bool(data.get("include_free", True)),
        include_rent_buy=bool(data.get("include_rent_buy", True)),
        search_urls=_str_map(data.get("search_urls"), "search_urls"),
        shows=shows,
    )


def load_config(path: Path) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))
