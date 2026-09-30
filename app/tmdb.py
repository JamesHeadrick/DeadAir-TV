"""Minimal async TMDB client."""

from __future__ import annotations

import httpx

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

PROVIDER_TYPES = ("flatrate", "free", "ads", "rent", "buy")
EPISODE_GROUP_TYPES = {
    1: "Original air date", 2: "Absolute", 3: "DVD", 4: "Digital",
    5: "Story arc", 6: "Production", 7: "TV",
}

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

    async def search_tv(self, query: str) -> list[dict]:
        data = await self._get("/search/tv", query=query, include_adult="false")
        return [
            {
                "tmdb_id": r["id"],
                "title": r.get("name") or r.get("original_name") or str(r["id"]),
                "year": (r.get("first_air_date") or "")[:4] or None,
                "overview": r.get("overview") or "",
                "poster_url": image_url(r.get("poster_path"), "w185"),
            }
            for r in data.get("results", [])
        ]

    async def list_tv_providers(self, region: str) -> list[dict]:
        """Every streaming provider TMDB knows about in a region, most popular first."""
        data = await self._get("/watch/providers/tv", watch_region=region)
        results = sorted(
            data.get("results", []),
            key=lambda p: (p.get("display_priorities") or {}).get(region, p.get("display_priority", 999)),
        )
        return [
            {
                "provider_id": p.get("provider_id"),
                "provider_name": p.get("provider_name", ""),
                "logo_url": image_url(p.get("logo_path"), "w92"),
            }
            for p in results
        ]

    async def list_episode_groups(self, tmdb_id: int) -> list[dict]:
        """A show's alternate episode orders (DVD, Digital, Story arc, ...)."""
        data = await self._get(f"/tv/{tmdb_id}/episode_groups")
        return [
            {
                "id": g["id"],
                "name": g.get("name") or "",
                "type": EPISODE_GROUP_TYPES.get(g.get("type"), "Other"),
                "episode_count": g.get("episode_count") or 0,
            }
            for g in data.get("results", [])
            if g.get("id")
        ]

    async def fetch_episode_order(self, group_id: str) -> dict[str, list[int]]:
        """Map "season:episode" (TMDB's numbers) to [season, episode] in an episode group.

        Groups become seasons in their listed order (a group named like
        "Specials" is season 0); episodes are numbered by their position.
        """
        data = await self._get(f"/tv/episode_group/{group_id}")
        out: dict[str, list[int]] = {}
        season = 0
        for group in sorted(data.get("groups", []), key=lambda g: g.get("order", 0)):
            special = "special" in (group.get("name") or "").lower()
            if not special:
                season += 1
            episodes = sorted(group.get("episodes", []), key=lambda e: e.get("order", 0))
            for i, ep in enumerate(episodes, start=1):
                key = f"{ep.get('season_number')}:{ep.get('episode_number')}"
                out.setdefault(key, [0 if special else season, i])
        return out

    async def fetch_providers(self, tmdb_id: int, region: str, season: int | None = None) -> dict:
        """Watch providers for one region, grouped by type, for the whole show
        or (with ``season``) just that season.

        Returns {"link": <TMDB watch page>, "flatrate": [...], "free": [...],
        "ads": [...], "rent": [...], "buy": [...]}.
        """
        path = f"/tv/{tmdb_id}" + (f"/season/{season}" if season is not None else "")
        data = await self._get(f"{path}/watch/providers")
        region_data = (data.get("results") or {}).get(region) or {}
        out: dict = {"link": region_data.get("link")}
        for kind in PROVIDER_TYPES:
            out[kind] = [
                {
                    "provider_id": p.get("provider_id"),
                    "provider_name": p.get("provider_name", ""),
                    "logo_path": p.get("logo_path"),
                }
                for p in sorted(region_data.get(kind, []), key=lambda p: p.get("display_priority", 999))
            ]
        return out
