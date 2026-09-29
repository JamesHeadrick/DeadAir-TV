"""Show-page links from Wikidata.

Wikidata (CC0) records many shows' IDs on streaming services, and each of
those ID properties carries its own link template ("formatter URL", P1630),
e.g. https://www.netflix.com/title/$1. So instead of hardcoding property
IDs, we find the properties by their link's domain and build URLs from
Wikidata's own templates. Shows are matched by their TMDB TV ID, whose
property is found the same way (formatter URL on themoviedb.org/tv/).
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("deadair.wikidata")

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "DeadAir/1.0 (+https://github.com/JamesHeadrick/DeadAir-TV)"
BATCH = 50  # shows per query

# Link domain -> the TMDB provider name it belongs to. Links are matched to
# watch providers with the same loose rules as service names.
SERVICE_DOMAINS = {
    "netflix.com": "Netflix",
    "hulu.com": "Hulu",
    "disneyplus.com": "Disney Plus",
    "hbomax.com": "HBO Max",
    "max.com": "HBO Max",
    "peacocktv.com": "Peacock",
    "paramountplus.com": "Paramount Plus",
    "primevideo.com": "Amazon Prime Video",
    "tv.apple.com": "Apple TV",
}

# Other names TMDB uses for the same service.
SERVICE_ALIASES = {"HBO Max": ("Max",)}

_DOMAIN_RE = "|".join(re.escape(d) for d in SERVICE_DOMAINS)

QUERY = """
SELECT ?tmdb ?fmt ?id WHERE {
  VALUES ?tmdb { %(ids)s }
  ?tmdbProp wikibase:propertyType wikibase:ExternalId ;
            wdt:P1630 ?tmdbFmt ;
            wikibase:directClaim ?tmdbClaim .
  FILTER(CONTAINS(STR(?tmdbFmt), "themoviedb.org/tv/"))
  ?item ?tmdbClaim ?tmdb .
  ?prop wikibase:propertyType wikibase:ExternalId ;
        wdt:P1630 ?fmt ;
        wikibase:directClaim ?claim .
  FILTER(REGEX(STR(?fmt), "^https?://([a-z0-9-]+\\\\.)*(%(domains)s)/", "i"))
  ?item ?claim ?id .
}
"""


def service_for_url(url: str) -> str | None:
    host = re.sub(r"^https?://", "", url.lower()).split("/", 1)[0]
    for domain, service in SERVICE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return service
    return None


def parse_bindings(bindings: list[dict]) -> dict[int, dict[str, str]]:
    """SPARQL rows -> {tmdb_id: {service: show_url}}. First link per service wins."""
    out: dict[int, dict[str, str]] = {}
    for row in bindings:
        try:
            tmdb_id = int(row["tmdb"]["value"])
            fmt = row["fmt"]["value"]
            ident = row["id"]["value"]
        except (KeyError, ValueError):
            continue
        if "$1" not in fmt:
            continue
        url = fmt.replace("$1", ident)
        service = service_for_url(url)
        if service:
            out.setdefault(tmdb_id, {}).setdefault(service, url)
    return out


async def fetch_show_links(
    tmdb_ids: list[int], transport: httpx.AsyncBaseTransport | None = None
) -> dict[int, dict[str, str]]:
    """Look up show-page links for the given TMDB TV IDs. Shows Wikidata
    doesn't know (or has no streaming IDs for) are simply absent."""
    result: dict[int, dict[str, str]] = {}
    async with httpx.AsyncClient(
        timeout=60, transport=transport,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    ) as client:
        for i in range(0, len(tmdb_ids), BATCH):
            chunk = tmdb_ids[i : i + BATCH]
            query = QUERY % {
                "ids": " ".join(f'"{int(t)}"' for t in chunk),
                "domains": _DOMAIN_RE.replace("\\", "\\\\"),
            }
            resp = await client.post(SPARQL_URL, data={"query": query, "format": "json"})
            resp.raise_for_status()
            result.update(parse_bindings(resp.json()["results"]["bindings"]))
    return result
