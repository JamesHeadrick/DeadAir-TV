import asyncio
import json
import random
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.access import compute_access, names_match
from app.config import AppConfig, ConfigError, Settings, ShowConfig, parse_config
from app.db import Database
from app.picker import pick_episode
from app.sync import Syncer
from app.tmdb import TMDBClient


def _eps(tmdb_id, season, n, air_date="2000-01-01"):
    return [
        {
            "tmdb_id": tmdb_id, "season": season, "episode": i, "title": f"Ep {i}",
            "overview": "", "still_path": None, "air_date": air_date, "runtime": 22,
        }
        for i in range(1, n + 1)
    ]


def _show(tmdb_id, status="Ended"):
    return {"tmdb_id": tmdb_id, "name": f"Show {tmdb_id}", "status": status,
            "poster_path": None, "backdrop_path": None}


# --- config ------------------------------------------------------------------

def test_parse_config_tags_become_channels():
    cfg = parse_config({
        "services": ["Netflix"],
        "shows": [
            {"tmdb_id": "1", "channels": ["sitcom", "short"]},
            {"tmdb_id": 2, "channels": "scifi", "weight": 3, "links": {"Netflix": "https://n/2"}},
            {"tmdb_id": 3, "channels": ["sitcom"]},
        ],
    })
    assert list(cfg.channels) == ["sitcom", "short", "scifi"]
    assert [s.tmdb_id for s in cfg.channels["sitcom"]] == [1, 3]
    assert cfg.find_show(2).weight == 3
    assert cfg.find_show(2).links == {"Netflix": "https://n/2"}
    assert cfg.include_free and cfg.include_rent_buy


@pytest.mark.parametrize("bad", [
    {},
    {"shows": []},
    {"shows": [{"tmdb_id": 1}]},
    {"shows": [{"tmdb_id": 1, "channels": ["a"], "weight": 0}]},
    {"shows": [{"tmdb_id": 1, "channels": ["a"]}, {"tmdb_id": 1, "channels": ["b"]}]},
    {"services": "Netflix", "shows": [{"tmdb_id": 1, "channels": ["a"]}]},
])
def test_parse_config_rejects_bad_config(bad):
    with pytest.raises(ConfigError):
        parse_config(bad)


# --- where to watch ----------------------------------------------------------

@pytest.mark.parametrize("configured,provider,expected", [
    ("Netflix", "Netflix", True),
    ("Netflix", "Netflix Standard with Ads", True),
    ("Disney+", "Disney Plus", True),
    ("Paramount+", "Paramount Plus Essential", True),
    ("HBO Max", "HBO Max Amazon Channel", False),
    ("Hulu", "Peacock Premium", False),
])
def test_names_match(configured, provider, expected):
    assert names_match(configured, provider) is expected


def _prov(*names):
    return [{"provider_id": sum(map(ord, n)), "provider_name": n, "logo_path": "/l.png"} for n in names]


def _access(providers, **cfg_kw):
    cfg = AppConfig(services=cfg_kw.pop("services", ["Hulu", "Netflix"]), **cfg_kw)
    show = ShowConfig(7, ("x",))
    return compute_access(cfg, show, "Rick & Morty", providers)


def test_access_prefers_my_subscriptions_in_my_order():
    a = _access({"flatrate": _prov("Netflix", "Hulu", "Max"), "ads": _prov("Tubi TV"),
                 "rent": _prov("Apple TV")})
    assert a.tier == "subscription"
    assert [o.provider_name for o in a.options] == ["Hulu", "Netflix"]
    assert a.other_subscriptions == ["Max"]
    assert a.options[1].url == "https://www.netflix.com/search?q=Rick+%26+Morty"
    assert a.options[0].logo_url.endswith("/w92/l.png")


def test_access_falls_back_to_free_then_rent_buy():
    a = _access({"flatrate": _prov("Max"), "free": _prov("Pluto TV"), "ads": _prov("Tubi TV")})
    assert a.tier == "free"
    assert [o.provider_name for o in a.options] == ["Pluto TV", "Tubi TV"]
    assert a.options[1].url == "https://tubitv.com/search/Rick%20%26%20Morty"

    a = _access({"flatrate": _prov("Max"), "rent": _prov("Apple TV"), "buy": _prov("Amazon Video"),
                 "link": "https://tmdb/watch"})
    assert a.tier == "rent_buy" and a.watchable
    assert [o.provider_name for o in a.options] == ["Apple TV", "Amazon Video"]


def test_access_nowhere_and_toggles():
    rent_only = {"flatrate": _prov("Max"), "rent": _prov("Apple TV")}
    a = _access(rent_only, include_rent_buy=False)
    assert a.tier is None and not a.watchable and a.other_subscriptions == ["Max"]
    assert not _access({"ads": _prov("Tubi TV")}, include_free=False).watchable
    assert _access(None).watchable  # not checked yet -> still pickable


def test_access_link_override_and_fallback():
    cfg = AppConfig(services=["Netflix", "Obscure+"])
    show = ShowConfig(7, ("x",), links={"netflix": "https://www.netflix.com/title/123"})
    a = compute_access(cfg, show, "X", {"flatrate": _prov("Netflix", "Obscure Plus"), "link": "https://tmdb/w"})
    assert [o.url for o in a.options] == ["https://www.netflix.com/title/123", "https://tmdb/w"]


# --- picking -----------------------------------------------------------------

def test_pick_is_uniform_across_episodes_and_respects_weights(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_episodes(_show(1), _eps(1, 1, 30))
    db.replace_episodes(_show(2), _eps(2, 1, 10))
    rng = random.Random(0)

    shows = [ShowConfig(1, ("c",)), ShowConfig(2, ("c",))]
    c = Counter(pick_episode(db, shows, rng)[0].tmdb_id for _ in range(4000))
    assert 0.70 < c[1] / 4000 < 0.80  # 30 of 40 episodes

    shows = [ShowConfig(1, ("c",)), ShowConfig(2, ("c",), weight=3)]
    c = Counter(pick_episode(db, shows, rng)[0].tmdb_id for _ in range(4000))
    assert 0.45 < c[1] / 4000 < 0.55  # 30 vs 10*3


def test_pick_skips_specials_and_unaired(tmp_path):
    db = Database(tmp_path / "t.db")
    eps = _eps(1, 0, 5) + _eps(1, 1, 1) + _eps(1, 2, 5, air_date="2999-01-01")
    eps += [dict(e, season=3, air_date=None) for e in _eps(1, 3, 5)]
    db.replace_episodes(_show(1, status="Returning Series"), eps)
    rng = random.Random(1)
    for _ in range(50):
        _, ep = pick_episode(db, [ShowConfig(1, ("c",))], rng, today="2026-01-01")
        assert (ep["season"], ep["episode"]) == (1, 1)


def test_pick_includes_undated_episodes_of_ended_shows(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_episodes(_show(1, status="Ended"), [dict(e, air_date=None) for e in _eps(1, 1, 3)])
    assert pick_episode(db, [ShowConfig(1, ("c",))]) is not None


def test_pick_with_empty_cache_returns_none(tmp_path):
    assert pick_episode(Database(tmp_path / "t.db"), [ShowConfig(1, ("c",))]) is None


# --- TMDB sync ---------------------------------------------------------------

def _fake_tmdb(requests):
    def handler(request: httpx.Request):
        requests.append(request)
        path = request.url.path
        if path == "/3/tv/99/watch/providers":
            return httpx.Response(200, json={"results": {
                "US": {"link": "https://tmdb/watch/99",
                       "flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]},
            }})
        if path == "/3/tv/99":
            body = {"name": "Test Show", "status": "Ended", "seasons": [
                {"season_number": n} for n in range(0, 23)
            ]}
            for part in (request.url.params.get("append_to_response") or "").split(","):
                if part:
                    n = int(part.split("/")[1])
                    body[part] = {"episodes": [
                        {"season_number": n, "episode_number": 1, "name": f"S{n}",
                         "air_date": "2001-01-01"}
                    ]}
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_sync_fetches_all_seasons_except_zero_and_providers(tmp_path):
    requests = []
    db = Database(tmp_path / "t.db")
    client = TMDBClient("abc123", transport=_fake_tmdb(requests))
    cfg = parse_config({"shows": [{"tmdb_id": 99, "channels": ["a"]}]})
    syncer = Syncer(Settings(), db, client)

    asyncio.run(syncer.run_once(cfg))
    assert syncer.last_error is None
    assert all(r.url.params["api_key"] == "abc123" for r in requests)
    # 1 info call + 2 chunks of seasons (1-20, 21-22) + providers
    assert len(requests) == 4
    counts = db.pickable_episode_counts([99], "2026-01-01")
    assert counts == {99: 22}
    row = db.get_show(99)
    assert row["name"] == "Test Show"
    providers = json.loads(row["providers_json"])
    assert providers["flatrate"][0]["provider_name"] == "Hulu"
    assert providers["link"] == "https://tmdb/watch/99"
    assert providers["rent"] == []

    # Second run within the refresh windows does nothing.
    requests.clear()
    asyncio.run(syncer.run_once(cfg))
    assert requests == []


def test_bearer_token_auth():
    requests = []
    client = TMDBClient("eyJhbGciOi.token", transport=_fake_tmdb(requests))
    asyncio.run(client.fetch_providers(99, "US"))
    assert requests[0].headers["authorization"] == "Bearer eyJhbGciOi.token"
    assert "api_key" not in requests[0].url.params


# --- HTTP API ----------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "services: [Netflix]\n"
        "shows:\n"
        "  - tmdb_id: 1\n"
        "    channels: [sitcom, short]\n"
        "    links: {Netflix: 'https://netflix.example/1?a=1&b=2'}\n"
        "  - tmdb_id: 2\n"
        "    channels: [sitcom]\n"
    )
    settings = Settings(config_path=cfg, db_path=tmp_path / "t.db", enable_adb=True, tv_ip="10.0.0.5")
    app = main.create_app(settings, start_sync=False)
    with TestClient(app) as c:
        db = main.state.db
        db.replace_episodes(_show(1), _eps(1, 1, 3))
        db.replace_episodes(_show(2), _eps(2, 1, 3))
        db.set_providers(1, {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]})
        db.set_providers(2, {"flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]})
        yield c


def test_api_channels_and_shows(client):
    data = client.get("/api/channels").json()
    assert [c["name"] for c in data["channels"]] == ["sitcom", "short"]
    assert data["channels"][0]["shows"] == ["Show 1", "Show 2"]
    assert data["channels"][0]["unwatchable"] == ["Show 2"]
    assert data["services"] == ["Netflix"]
    assert data["adb_enabled"] is True

    shows = {s["tmdb_id"]: s for s in client.get("/api/shows").json()["shows"]}
    assert shows[1]["access"]["tier"] == "subscription"
    assert shows[2]["access"]["tier"] is None
    assert shows[2]["access"]["other_subscriptions"] == ["Hulu"]
    assert shows[1]["channels"] == ["sitcom", "short"]
    assert client.get("/").status_code == 200


def test_api_pick_skips_unwatchable_shows(client):
    for _ in range(20):
        ep = client.get("/api/pick", params={"channel": "sitcom"}).json()
        assert ep["tmdb_id"] == 1
    assert ep["code"].startswith("S01E0")
    assert ep["access"]["options"][0]["url"] == "https://netflix.example/1?a=1&b=2"
    assert client.get("/api/pick", params={"channel": "nope"}).status_code == 404


def test_api_play_uses_server_side_url(client, monkeypatch):
    calls = []

    async def fake_run(*args, timeout=15):
        calls.append(args)
        return "connected to 10.0.0.5:5555" if args[1] == "connect" else "Starting: Intent"

    monkeypatch.setattr(main.adb, "_run", fake_run)
    r = client.post("/api/play", json={"tmdb_id": 1})
    assert r.status_code == 200, r.text
    assert calls[0] == ("adb", "connect", "10.0.0.5:5555")
    assert calls[1][-1] == "'https://netflix.example/1?a=1&b=2'"

    assert client.post("/api/play", json={"tmdb_id": 2}).status_code == 404  # nowhere to watch
    assert client.post("/api/play", json={"tmdb_id": 3}).status_code == 404  # unknown show
