"""Minimal async TMDB client."""

from __future__ import annotations

import re

import httpx

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

# TMDB allows up to 20 sub-requests per call via append_to_response.
_APPEND_LIMIT = 20


def image_url(path: str | None, size: str = "w780") -> str | None:
    return f"{IMAGE_BASE}/{size}{path}" if path else None


class TMDBClient:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Accept": "application/json"}
        params = {}
        # v4 "API Read Access Token" is a JWT; the classic v3 key is a short hex string.
        if api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            params["api_key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers=headers,
            params=params,
            timeout=20,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params) -> dict:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_show_with_episodes(self, tmdb_id: int) -> tuple[dict, list[dict]]:
        """Return (show metadata, episodes) for every season except season 0."""
        info = await self._get(f"/tv/{tmdb_id}")
        season_numbers = [
            s["season_number"]
            for s in info.get("seasons", [])
            if s.get("season_number", 0) > 0
        ]

        episodes: list[dict] = []
        for i in range(0, len(season_numbers), _APPEND_LIMIT):
            chunk = season_numbers[i : i + _APPEND_LIMIT]
            data = await self._get(
                f"/tv/{tmdb_id}",
                append_to_response=",".join(f"season/{n}" for n in chunk),
            )
            for n in chunk:
                for ep in (data.get(f"season/{n}") or {}).get("episodes", []):
                    episodes.append(
                        {
                            "tmdb_id": tmdb_id,
                            "season": ep.get("season_number", n),
                            "episode": ep["episode_number"],
                            "title": ep.get("name") or "",
                            "overview": ep.get("overview") or "",
                            "still_path": ep.get("still_path"),
                            "air_date": ep.get("air_date") or None,
                            "runtime": ep.get("runtime"),
                        }
                    )

        show = {
            "tmdb_id": tmdb_id,
            "name": info.get("name") or info.get("original_name") or str(tmdb_id),
            "status": info.get("status"),
            "poster_path": info.get("poster_path"),
            "backdrop_path": info.get("backdrop_path"),
        }
        return show, episodes

    async def fetch_flatrate_providers(self, tmdb_id: int, region: str) -> list[dict]:
        data = await self._get(f"/tv/{tmdb_id}/watch/providers")
        region_data = (data.get("results") or {}).get(region) or {}
        return [
            {"provider_id": p.get("provider_id"), "provider_name": p.get("provider_name", "")}
            for p in region_data.get("flatrate", [])
        ]


def _norm(name: str) -> str:
    name = name.lower().replace("+", "plus")
    return re.sub(r"[^a-z0-9]", "", name)


def service_available(service: str, providers: list[dict]) -> bool:
    """Loose match of a configured service name against TMDB provider names.

    "Disney+" matches "Disney Plus"; "Netflix" matches "Netflix Standard with Ads".
    Add-on channels sold through another store ("Max Amazon Channel") don't
    count as the service itself.
    """
    want = _norm(service)
    if not want:
        return False
    for p in providers:
        have = _norm(p.get("provider_name", ""))
        if not have:
            continue
        if have == want or want.startswith(have):
            return True
        if have.startswith(want) and "channel" not in have[len(want) :]:
            return True
    return False
