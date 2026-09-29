"""Work out where *you* can watch a show, from TMDB watch-provider data.

Tiers, best first; only the best non-empty tier is shown:
  1. subscription - TMDB "flatrate" providers that match your `services`
  2. free         - TMDB "free" + "ads" providers (Tubi, Pluto TV, ...)
  3. rent_buy     - TMDB "rent" + "buy" providers
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, quote_plus

from .config import AppConfig, ShowConfig
from .tmdb import image_url
from .wikidata import SERVICE_ALIASES

TIER_LABELS = {
    "subscription": "On your services",
    "free": "Free with ads",
    "rent_buy": "Rent or buy",
}

# Search pages per service; {q} is the URL-encoded show name. Keys are matched
# with the same loose rules as service names. Override/extend via
# `search_urls:` in config.yaml.
DEFAULT_SEARCH_URLS = {
    "Netflix": "https://www.netflix.com/search?q={q}",
    "Hulu": "https://www.hulu.com/search?q={q}",
    "Disney Plus": "https://www.disneyplus.com/search?q={q}",
    "HBO Max": "https://play.hbomax.com/search?q={q}",
    "Max": "https://play.max.com/search?q={q}",
    "Peacock": "https://www.peacocktv.com/search?q={q}",
    "Paramount Plus": "https://www.paramountplus.com/search/?q={q}",
    "Apple TV": "https://tv.apple.com/search?term={q}",
    "Amazon Prime Video": "https://www.amazon.com/s?k={q}&i=instant-video",
    "Amazon Video": "https://www.amazon.com/s?k={q}&i=instant-video",
    "Tubi": "https://tubitv.com/search/{q_path}",
    "The Roku Channel": "https://therokuchannel.roku.com/search/{q_path}",
    "Pluto TV": "https://pluto.tv/search/details?query={q}",
    "YouTube": "https://www.youtube.com/results?search_query={q}",
    "Google Play Movies": "https://play.google.com/store/search?q={q}&c=movies",
    "Fandango At Home": "https://athome.fandango.com/content/browse/search?searchString={q}",
}


def _norm(name: str) -> str:
    name = name.lower().replace("+", "plus")
    return re.sub(r"[^a-z0-9]", "", name)


def names_match(configured: str, provider_name: str) -> bool:
    """Loose match of a service name you typed against a TMDB provider name.

    "Disney+" matches "Disney Plus"; "Netflix" matches "Netflix Standard with Ads".
    Add-on channels sold through another store ("HBO Max Amazon Channel") don't
    count as the service itself.
    """
    want, have = _norm(configured), _norm(provider_name)
    if not want or not have:
        return False
    if have == want or want.startswith(have):
        return True
    return have.startswith(want) and "channel" not in have[len(want):]


def _match_service(service: str, providers: list[dict]) -> tuple[dict | None, list[dict]]:
    """The provider entry to show for one of your services, plus every variant of it.

    An exact name match wins, so picking "Netflix" doesn't also surface
    "Netflix Standard with Ads". Loose matching only kicks in when there's no
    exact match (e.g. a hand-typed "Disney+"), and then yields one entry.
    """
    variants = [p for p in providers if names_match(service, p["provider_name"])]
    exact = [p for p in variants if _norm(p["provider_name"]) == _norm(service)]
    return (exact or variants or [None])[0], variants


# Add-on channels are watched inside the store's own app, so search there.
_CHANNEL_STORES = {
    "amazonchannel": "Amazon Video",
    "appletvchannel": "Apple TV",
    "rokupremiumchannel": "The Roku Channel",
}


def builtin_search_url(service: str) -> str | None:
    """The built-in search link template for a service name, if there is one."""
    found = _lookup(DEFAULT_SEARCH_URLS, service)
    if not found:
        for suffix, store in _CHANNEL_STORES.items():
            if _norm(service).endswith(suffix):
                return DEFAULT_SEARCH_URLS[store]
    return found


def _lookup(mapping: dict[str, str], provider_name: str) -> str | None:
    # Prefer an exact normalized match so "Max" doesn't claim "HBO Max" etc.
    for key, val in mapping.items():
        if _norm(key) == _norm(provider_name):
            return val
    for key, val in mapping.items():
        if names_match(key, provider_name):
            return val
    return None


# Watch options that open the show itself sort ahead of searches.
SOURCE_RANK = {"manual": 0, "auto": 0, "search": 1, "tmdb": 2}


@dataclass
class WatchOption:
    provider_id: int | None
    provider_name: str
    logo_url: str | None
    url: str
    # Where the link came from: "manual" (pasted in Settings), "auto" (Wikidata),
    # "search" (the service's search page) or "tmdb" (TMDB's where-to-watch page).
    source: str = "search"


@dataclass
class Access:
    checked: bool                      # False until the first provider check
    tier: str | None = None            # None = nowhere you can watch
    options: list[WatchOption] = field(default_factory=list)
    other_subscriptions: list[str] = field(default_factory=list)  # services you don't have

    @property
    def watchable(self) -> bool:
        # Unchecked shows stay pickable so a fresh install works immediately.
        return not self.checked or self.tier is not None

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "tier": self.tier,
            "tier_label": TIER_LABELS.get(self.tier or "", "Not on any of your services"),
            "options": [o.__dict__ for o in self.options],
            "other_subscriptions": self.other_subscriptions,
        }


def _dedupe(providers: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in providers:
        key = p.get("provider_id") or p.get("provider_name")
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def compute_access(
    cfg: AppConfig, show: ShowConfig, show_name: str, providers: dict | None,
    auto_links: dict[str, str] | None = None,
) -> Access:
    if providers is None:
        return Access(checked=False)

    flatrate = providers.get("flatrate", [])
    mine, covered = [], set()
    for service in cfg.services:  # keep your ordering: first listed wins
        match, variants = _match_service(service, flatrate)
        if match:
            mine.append(match)
        covered.update(p["provider_name"] for p in variants)
    mine = _dedupe(mine)
    # Other subscription services carrying the show, minus tiers of ones you have
    # (e.g. "Netflix Standard with Ads" when you have "Netflix").
    others = [p["provider_name"] for p in flatrate if p["provider_name"] not in covered]

    tiers = [("subscription", mine)]
    if cfg.include_free:
        tiers.append(("free", _dedupe(providers.get("free", []) + providers.get("ads", []))))
    if cfg.include_rent_buy:
        tiers.append(("rent_buy", _dedupe(providers.get("rent", []) + providers.get("buy", []))))

    for tier, found in tiers:
        if found:
            options = [_option(cfg, show, show_name, p, providers.get("link"), auto_links or {}) for p in found]
            # Services whose link opens the show come before ones that only
            # search; your service order decides within each group (stable sort).
            options.sort(key=lambda o: SOURCE_RANK[o.source])
            return Access(checked=True, tier=tier, options=options, other_subscriptions=others)
    return Access(checked=True, other_subscriptions=others)


def _auto_link(auto_links: dict[str, str], provider_name: str) -> str | None:
    for service, url in auto_links.items():
        names = (service, *SERVICE_ALIASES.get(service, ()))
        if any(names_match(n, provider_name) for n in names):
            return url
    return None


def _option(
    cfg: AppConfig, show: ShowConfig, show_name: str, p: dict, tmdb_link: str | None,
    auto_links: dict[str, str],
) -> WatchOption:
    """Link priority: pasted in Settings > found on Wikidata > search page > TMDB page."""
    name = p["provider_name"]
    source = "manual"
    url = _lookup(show.links, name)
    if not url:
        source, url = "auto", _auto_link(auto_links, name)
    if not url:
        template = _lookup(cfg.search_urls, name) or builtin_search_url(name)
        if template:
            source = "search"
            url = template.format(q=quote_plus(show_name), q_path=quote(show_name, safe=""))
    if not url:
        source = "tmdb"
        url = tmdb_link or f"https://www.themoviedb.org/tv/{show.tmdb_id}/watch"
    return WatchOption(
        provider_id=p.get("provider_id"),
        provider_name=name,
        logo_url=image_url(p.get("logo_path"), "w92"),
        url=url,
        source=source,
    )
