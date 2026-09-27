"""Settings (env vars) and channel config (channels.yaml)."""

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
    config_path: Path = Path("/config/channels.yaml")
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
            config_path=Path(os.environ.get("CHANNELS_CONFIG", "/config/channels.yaml")),
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
    service: str
    show_url: str
    weight: float = 1.0
    name: str | None = None  # optional display-name override


@dataclass
class ChannelConfig:
    channels: dict[str, list[ShowConfig]] = field(default_factory=dict)

    def all_shows(self) -> list[ShowConfig]:
        return [s for shows in self.channels.values() for s in shows]

    def find_show(self, channel: str, tmdb_id: int) -> ShowConfig | None:
        for s in self.channels.get(channel, []):
            if s.tmdb_id == tmdb_id:
                return s
        return None


class ConfigError(ValueError):
    pass


def parse_channels(data: object) -> ChannelConfig:
    if not isinstance(data, dict) or not isinstance(data.get("channels"), dict):
        raise ConfigError("config must have a top-level 'channels:' mapping")

    channels: dict[str, list[ShowConfig]] = {}
    for ch_name, shows in data["channels"].items():
        ch_name = str(ch_name)
        if not isinstance(shows, list) or not shows:
            raise ConfigError(f"channel {ch_name!r} must be a non-empty list of shows")
        parsed = []
        for i, show in enumerate(shows):
            where = f"channel {ch_name!r}, show #{i + 1}"
            if not isinstance(show, dict):
                raise ConfigError(f"{where}: must be a mapping")
            missing = [k for k in ("tmdb_id", "service", "show_url") if not show.get(k)]
            if missing:
                raise ConfigError(f"{where}: missing {', '.join(missing)}")
            try:
                tmdb_id = int(show["tmdb_id"])
                weight = float(show.get("weight", 1.0))
            except (TypeError, ValueError) as e:
                raise ConfigError(f"{where}: {e}") from e
            if weight <= 0:
                raise ConfigError(f"{where}: weight must be > 0")
            parsed.append(
                ShowConfig(
                    tmdb_id=tmdb_id,
                    service=str(show["service"]),
                    show_url=str(show["show_url"]),
                    weight=weight,
                    name=str(show["name"]) if show.get("name") else None,
                )
            )
        channels[ch_name] = parsed
    return ChannelConfig(channels=channels)


def load_channels(path: Path) -> ChannelConfig:
    with open(path, encoding="utf-8") as f:
        return parse_channels(yaml.safe_load(f))
