import asyncio
import dataclasses
import json
import re
import random
import urllib.parse
import time
from collections import Counter

import httpx
import yaml
import pytest
from fastapi.testclient import TestClient

from app import main, updates
from app.access import compute_access, names_match
from app.config import (
    AppConfig, ConfigError, Settings, ShowConfig, dump_config, load_config, parse_config, save_config,
)
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
    [],
    {"shows": "nope"},
    {"shows": [{"tmdb_id": 1}]},
    {"shows": [{"tmdb_id": 1, "channels": ["  "]}]},
    {"shows": [{"tmdb_id": 1, "channels": ["a"], "weight": 0}]},
    {"shows": [{"tmdb_id": 1, "channels": ["a"]}, {"tmdb_id": 1, "channels": ["b"]}]},
    {"services": "Netflix", "shows": [{"tmdb_id": 1, "channels": ["a"]}]},
])
def test_parse_config_rejects_bad_config(bad):
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_empty_config_is_valid():
    assert parse_config(None).shows == []
    assert parse_config({}).channels == {}


def test_dump_config_round_trips(tmp_path):
    cfg = parse_config({
        "services": ["Netflix", " Hulu ", "Netflix"],
        "include_free": False,
        "search_urls": {"Foo": "https://foo/?q={q}"},
        "shows": [
            {"tmdb_id": 1, "title": "Seinfeld", "channels": ["sitcom", " short "]},
            {"tmdb_id": 2, "channels": ["scifi"], "weight": 2.5, "name": "X", "links": {"Hulu": "https://h"}},
        ],
    })
    assert cfg.services == ["Netflix", "Hulu"]
    text = dump_config(cfg)
    assert text.startswith("# DeadAir config")
    assert "weight" not in text.split("tmdb_id: 2")[0]  # default weight not written
    path = tmp_path / "config.yaml"
    save_config(path, text)
    again = load_config(path)
    assert dump_config(again) == text
    assert again.find_show(1).channels == ("sitcom", "short")
    assert again.find_show(2).links == {"Hulu": "https://h"}

    save_config(path, "shows: []\n")
    assert (tmp_path / "config.yaml.bak").read_text() == text
    assert load_config(tmp_path / "missing.yaml").shows == []


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


def test_access_exact_service_name_hides_other_tiers_of_it():
    # You picked "Netflix" (no ads) from TMDB's list; the ad tier shouldn't show up too.
    a = _access({"flatrate": _prov("Netflix", "Netflix Standard with Ads", "Hulu")}, services=["Netflix"])
    assert [o.provider_name for o in a.options] == ["Netflix"]
    assert a.other_subscriptions == ["Hulu"]  # the ad tier isn't "another service" either

    # Picking the ads plan itself matches exactly too.
    a = _access({"flatrate": _prov("Netflix", "Netflix Standard with Ads")}, services=["Netflix Standard with Ads"])
    assert [o.provider_name for o in a.options] == ["Netflix Standard with Ads"]

    # Hand-typed names with no exact match still work, as a single entry.
    a = _access({"flatrate": _prov("Disney Plus", "Disney Plus Basic with Ads")}, services=["Disney+"])
    assert [o.provider_name for o in a.options] == ["Disney Plus"]
    a = _access({"flatrate": _prov("Paramount Plus Essential", "Paramount Plus Premium")}, services=["Paramount+"])
    assert [o.provider_name for o in a.options] == ["Paramount Plus Essential"]
    assert a.other_subscriptions == []


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


def test_pick_avoids_excluded_episodes_until_none_left(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_episodes(_show(1), _eps(1, 1, 3))
    shows = [ShowConfig(1, ("c",))]
    rng = random.Random(3)
    for _ in range(30):
        _, ep = pick_episode(db, shows, rng, exclude_episodes=["1:1:1", "1:1:3"])
        assert ep["episode"] == 2
    # Everything already seen -> exclusions are dropped instead of failing.
    assert pick_episode(db, shows, rng, exclude_episodes=["1:1:1", "1:1:2", "1:1:3"]) is not None


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
        if m := re.fullmatch(r"/3/tv/99/season/(\d+)/watch/providers", path):
            # Hulu has seasons 1-5; TMDB knows nothing about the rest.
            if int(m.group(1)) > 5:
                return httpx.Response(200, json={"results": {}})
            return httpx.Response(200, json={"results": {"US": {
                "flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]}}})
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
    wd_requests = []

    def wikidata(request):
        wd_requests.append(request)
        return httpx.Response(200, json={"results": {"bindings": [
            {"tmdb": {"value": "99"}, "fmt": {"value": "https://www.hulu.com/series/$1"}, "id": {"value": "test-show-abc"}},
        ]}})

    syncer = Syncer(Settings(), db, client, wikidata_transport=httpx.MockTransport(wikidata))

    asyncio.run(syncer.run_once(cfg))
    assert syncer.last_error is None
    assert all(r.url.params["api_key"] == "abc123" for r in requests)
    # 1 info call + 2 chunks of seasons (1-20, 21-22) + providers + providers for each of 22 seasons
    assert len(requests) == 4 + 22
    by_season = json.loads(db.get_show(99)["season_providers_json"])
    assert sorted(by_season, key=int) == ["1", "2", "3", "4", "5"]  # no-data seasons left out
    counts = db.pickable_episode_counts([99], "2026-01-01")
    assert counts == {99: 22}
    row = db.get_show(99)
    assert row["name"] == "Test Show"
    providers = json.loads(row["providers_json"])
    assert providers["flatrate"][0]["provider_name"] == "Hulu"
    assert providers["link"] == "https://tmdb/watch/99"
    assert providers["rent"] == []

    assert json.loads(db.get_show(99)["links_json"]) == {"Hulu": "https://www.hulu.com/series/test-show-abc"}
    assert len(wd_requests) == 1

    # Second run within the refresh windows does nothing.
    requests.clear()
    asyncio.run(syncer.run_once(cfg))
    assert requests == [] and len(wd_requests) == 1

    # A show synced before per-season checks existed gets them on the next run.
    with db.connect() as conn:
        conn.execute("UPDATE shows SET season_providers_json = NULL")
    asyncio.run(syncer.run_once(cfg))
    assert len(requests) == 1 + 22 and db.get_show(99)["season_providers_json"] is not None


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
    settings = Settings(config_path=cfg, db_path=tmp_path / "t.db")
    app = main.create_app(settings, start_sync=False)
    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={"username": "admin", "password": "correct horse"})
        assert r.status_code == 200, r.text
        db = main.state.db
        db.replace_episodes(_show(1), _eps(1, 1, 3))
        db.replace_episodes(_show(2), _eps(2, 1, 3))
        db.set_providers(1, {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]})
        db.set_providers(2, {"flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]})
        yield c


def test_seasons_a_service_lacks_are_not_picked(client):
    db = main.state.db
    db.replace_episodes(_show(1), _eps(1, 1, 3) + _eps(1, 2, 3))
    netflix = {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]}
    hulu = {"flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]}
    db.set_season_providers(1, {1: netflix, 2: hulu})  # you only have Netflix

    for _ in range(15):
        ep = client.get("/api/pick", params={"channel": "short"}).json()
        assert ep["season"] == 1 and ep["access"]["options"][0]["provider_name"] == "Netflix"

    shows = {s["tmdb_id"]: s for s in client.get("/api/shows").json()["shows"]}
    assert shows[1]["access"]["tier"] == "subscription"  # the show as a whole
    assert [(g["first"], g["last"], g["tier"], g["providers"]) for g in shows[1]["access"]["by_season"]] == [
        (1, 1, "subscription", ["Netflix"]), (2, 2, None, [])]

    # A season TMDB has no data for uses the whole-show answer; nothing to call out.
    db.set_season_providers(1, {1: netflix})
    assert {client.get("/api/pick", params={"channel": "short"}).json()["season"] for _ in range(30)} == {1, 2}
    assert client.get("/api/shows").json()["shows"][0]["access"]["by_season"] == []

    # Every season elsewhere: the show counts as nowhere to watch.
    db.set_season_providers(1, {1: hulu, 2: hulu})
    assert client.get("/api/pick", params={"channel": "short"}).status_code == 404
    assert client.get("/api/channels").json()["channels"][0]["unwatchable"] == ["Show 1"]


def test_api_channels_and_shows(client):
    data = client.get("/api/channels").json()
    assert [c["name"] for c in data["channels"]] == ["short", "sitcom"]  # alphabetical
    sitcom = data["channels"][1]
    assert sitcom["shows"] == ["Show 1", "Show 2"]
    assert sitcom["unwatchable"] == ["Show 2"]
    assert sitcom["posters"] == []  # the test shows have no poster art
    assert data["services"] == ["Netflix"]

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


def test_api_pick_other_show_and_same_show(client):
    db = main.state.db
    db.set_providers(2, {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]})
    db.replace_episodes(_show(3), _eps(3, 1, 3))
    db.set_providers(3, {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]})
    main.state.settings.config_path.write_text(
        "services: [Netflix]\nshows:\n"
        "  - {tmdb_id: 1, channels: [sitcom]}\n"
        "  - {tmdb_id: 2, channels: [sitcom]}\n"
        "  - {tmdb_id: 3, channels: [sitcom]}\n"
    )

    def get(**params):
        r = client.get("/api/pick", params={"channel": "sitcom", **params})
        return r.status_code, r.json()

    # Different show: skipped shows never come back.
    for _ in range(20):
        _, ep = get(skip_show=[1])
        assert ep["tmdb_id"] in (2, 3)
    _, ep = get(skip_show=[1, 2])
    assert ep["tmdb_id"] == 3 and ep["other_shows"] == 0
    assert get(skip_show=[1, 2, 3])[0] == 404

    # Another episode of the same show, avoiding ones already seen.
    for _ in range(10):
        _, ep = get(show=2, skip_ep=["2:1:1", "2:1:2"])
        assert (ep["tmdb_id"], ep["episode"]) == (2, 3)
    assert ep["other_shows"] == 2


def test_api_config_get_put_and_conflict(client):
    cfg = client.get("/api/config").json()
    assert cfg["services"] == ["Netflix"]
    assert cfg["shows"][0]["title"] == "Show 1"  # name from the TMDB cache
    assert cfg["shows"][0]["links"] == {"Netflix": "https://netflix.example/1?a=1&b=2"}

    body = {
        "version": cfg["version"],
        "services": ["Hulu", "Netflix"],
        "include_free": True,
        "include_rent_buy": False,
        "shows": [
            {"tmdb_id": 1, "channels": ["sitcom"], "title": "Show 1"},
            {"tmdb_id": 2, "channels": ["scifi", "short"], "weight": 2},
            {"tmdb_id": 3, "channels": ["scifi"], "title": "Brand New"},
        ],
    }
    r = client.put("/api/config", json=body)
    assert r.status_code == 200, r.text
    new_version = r.json()["version"]

    # Written to disk and live immediately.
    on_disk = load_config(main.state.settings.config_path)
    assert on_disk.services == ["Hulu", "Netflix"] and not on_disk.include_rent_buy
    channels = client.get("/api/channels").json()["channels"]
    assert [c["name"] for c in channels] == ["scifi", "short", "sitcom"]
    assert channels[0]["shows"] == ["Show 2", "Brand New"]  # uncached show falls back to title
    assert client.get("/api/config").json()["version"] == new_version

    # Stale version -> 409; invalid config -> 400, file untouched.
    assert client.put("/api/config", json=body).status_code == 409
    body["version"] = new_version
    body["shows"][0]["channels"] = []
    assert client.put("/api/config", json=body).status_code == 400
    assert load_config(main.state.settings.config_path).find_show(3) is not None


def test_hand_edits_are_picked_up_and_bad_edits_reported(client):
    path = main.state.settings.config_path
    path.write_text("services: [Hulu]\nshows:\n  - tmdb_id: 2\n    channels: [late night]\n")
    data = client.get("/api/channels").json()
    assert [c["name"] for c in data["channels"]] == ["late night"]
    assert data["channels"][0]["unwatchable"] == []  # Show 2 is on Hulu

    path.write_text("shows: [oops")
    data = client.get("/api/channels").json()
    assert [c["name"] for c in data["channels"]] == ["late night"]  # last good config kept
    assert data["config_error"]


def test_api_tmdb_search_and_providers(client):
    def handler(request: httpx.Request):
        if request.url.path == "/3/search/tv":
            assert request.url.params["query"] == "seinfeld"
            return httpx.Response(200, json={"results": [
                {"id": 1400, "name": "Seinfeld", "first_air_date": "1989-07-05", "poster_path": "/p.jpg"},
            ]})
        if request.url.path == "/3/watch/providers/tv":
            return httpx.Response(200, json={"results": [
                {"provider_id": 15, "provider_name": "Hulu", "display_priorities": {"US": 5}},
                {"provider_id": 8, "provider_name": "Netflix", "display_priorities": {"US": 1}},
            ]})
        return httpx.Response(404)

    assert client.get("/api/tmdb/search", params={"q": "x"}).status_code == 503
    main.state.tmdb = TMDBClient("k", transport=httpx.MockTransport(handler))
    res = client.get("/api/tmdb/search", params={"q": " seinfeld "}).json()["results"]
    assert res == [{"tmdb_id": 1400, "title": "Seinfeld", "year": "1989", "overview": "",
                    "poster_url": "https://image.tmdb.org/t/p/w185/p.jpg"}]
    names = [p["provider_name"] for p in client.get("/api/tmdb/providers").json()["providers"]]
    assert names == ["Netflix", "Hulu"]


def test_watched_and_skipped_episodes_cool_down(client, monkeypatch):
    # Show 1 has 3 episodes and is the only watchable one in "short".
    def pick():
        return client.get("/api/pick", params={"channel": "short"}).json()

    def mark(ep, kind):
        r = client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": ep, "kind": kind})
        assert r.status_code == 200, r.text
        return r.json()["history"]

    h = mark(1, "watched")
    assert h["kind"] == "watched" and h["cooling_down"]
    mark(2, "skipped")
    for _ in range(10):
        ep = pick()
        assert ep["episode"] == 3 and ep["history"] is None

    # Undoing "watched" puts episode 1 back in rotation.
    r = client.post("/api/history/unwatch", json={"tmdb_id": 1, "season": 1, "episode": 1})
    assert r.json()["history"] is None
    assert {pick()["episode"] for _ in range(30)} == {1, 3}

    # After the cooldown, everything is back and the card mentions the old skip.
    now = time.time()
    monkeypatch.setattr(main.time, "time", lambda: now + 15 * 86400)
    seen = {}
    for _ in range(40):
        ep = pick()
        seen[ep["episode"]] = ep["history"]
    assert set(seen) == {1, 2, 3}
    assert seen[2]["kind"] == "skipped" and not seen[2]["cooling_down"]

    assert client.post("/api/history", json={"tmdb_id": 99, "season": 1, "episode": 1,
                                             "kind": "watched"}).status_code == 404
    assert client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": 1,
                                             "kind": "loved"}).status_code == 422
    assert client.delete("/api/history").json()["deleted"] == 1  # the undone "watched" is already gone


def test_everything_on_cooldown_still_picks(client):
    for ep in (1, 2, 3):
        client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": ep, "kind": "watched"})
    assert client.get("/api/pick", params={"channel": "short"}).status_code == 200


def test_cooldown_days_in_config(client):
    cfg = client.get("/api/config").json()
    assert cfg["cooldown_days"] == 14
    body = {k: cfg[k] for k in ("version", "services", "include_free", "include_rent_buy", "search_urls")}
    body["cooldown_days"] = 3
    body["shows"] = [{k: s[k] for k in ("tmdb_id", "channels", "weight", "name", "title", "links")}
                     for s in cfg["shows"]]
    assert client.put("/api/config", json=body).status_code == 200
    assert "cooldown_days: 3\n" in main.state.settings.config_path.read_text()
    body["version"] = client.get("/api/config").json()["version"]
    body["cooldown_days"] = -1
    assert client.put("/api/config", json=body).status_code == 400


# --- auth --------------------------------------------------------------------

from app import auth  # noqa: E402


def test_password_hashing():
    h = auth.hash_password("hunter22!")
    assert h.startswith("scrypt$") and "hunter22" not in h
    assert auth.verify_password("hunter22!", h)
    assert not auth.verify_password("hunter23!", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same") != auth.hash_password("same")  # salted


def test_api_requires_login(client):
    client.post("/api/auth/logout")
    assert client.get("/api/channels").status_code == 401
    assert client.get("/api/pick", params={"channel": "sitcom"}).status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"user": None, "needs_setup": False}
    # Setup only works on an empty install.
    assert client.post("/api/auth/setup", json={"username": "x", "password": "12345678"}).status_code == 409


def test_login_logout_and_throttle(client, monkeypatch):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": "ADMIN", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["user"]["is_admin"]  # usernames are case-insensitive
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert client.get("/api/channels").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/channels").status_code == 401

    monkeypatch.setattr(main, "throttle", auth.LoginThrottle(max_failures=2))
    bad = {"username": "admin", "password": "nope-nope"}
    assert client.post("/api/auth/login", json=bad).status_code == 401
    assert client.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 401
    good = {"username": "admin", "password": "correct horse"}
    assert client.post("/api/auth/login", json=good).status_code == 429


def _login_as(client, username, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def test_viewer_role_and_per_user_history(client):
    r = client.post("/api/users", json={"username": "kid", "password": "kidpass123"})
    assert r.status_code == 200 and r.json()["user"]["is_admin"] is False
    assert client.post("/api/users", json={"username": "Kid", "password": "kidpass123"}).status_code == 409
    assert client.post("/api/users", json={"username": "x", "password": "short"}).status_code == 400

    # Admin marks episodes 1 and 2 of show 1 (the only show in "short").
    for ep in (1, 2):
        client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": ep, "kind": "watched"})
    assert {client.get("/api/pick", params={"channel": "short"}).json()["episode"] for _ in range(10)} == {3}

    _login_as(client, "kid", "kidpass123")
    # The viewer's cooldowns are their own.
    assert {client.get("/api/pick", params={"channel": "short"}).json()["episode"] for _ in range(40)} == {1, 2, 3}
    assert client.delete("/api/history").json()["deleted"] == 0
    # ...and they can't touch settings or users.
    for method, url in [("get", "/api/config"), ("put", "/api/config"), ("get", "/api/users"),
                        ("get", "/api/tmdb/search?q=x"), ("get", "/api/tmdb/providers"), ("post", "/api/refresh")]:
        assert getattr(client, method)(url).status_code in (403, 422), url
    assert client.request("PUT", "/api/config", json={"version": "x", "services": [], "shows": []}).status_code == 403
    assert client.get("/api/auth/status").json()["user"]["username"] == "kid"


def test_user_admin_guards_and_password_change(client, tmp_path):
    me = client.get("/api/auth/status").json()["user"]
    assert client.delete(f"/api/users/{me['id']}").status_code == 400
    assert client.patch(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 400  # last admin

    kid = client.post("/api/users", json={"username": "kid", "password": "kidpass123"}).json()["user"]
    assert client.patch(f"/api/users/{kid['id']}", json={"is_admin": True}).json()["user"]["is_admin"]
    assert client.patch(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 200  # another admin exists
    assert client.get("/api/users").status_code == 403  # demoted admins lose admin access immediately

    # Password change: wrong current password rejected; other sessions are logged out.
    other = TestClient(client.app)
    _login_as(other, "kid", "kidpass123")
    _login_as(client, "kid", "kidpass123")
    assert client.post("/api/auth/password", json={"current_password": "nope", "new_password": "newpass123"}).status_code == 400
    assert client.post("/api/auth/password", json={"current_password": "kidpass123", "new_password": "newpass123"}).status_code == 200
    assert client.get("/api/channels").status_code == 200
    assert other.get("/api/channels").status_code == 401
    _login_as(client, "kid", "newpass123")

    assert client.delete(f"/api/users/{me['id']}").status_code == 200
    assert [u["username"] for u in client.get("/api/users").json()["users"]] == ["kid"]


def test_admin_from_env_and_legacy_history_migration(tmp_path):
    import sqlite3 as sq
    dbp = tmp_path / "t.db"
    conn = sq.connect(dbp)
    conn.executescript(
        "CREATE TABLE episode_history (id INTEGER PRIMARY KEY, tmdb_id INTEGER NOT NULL, season INTEGER NOT NULL,"
        " episode INTEGER NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL);"
        f"INSERT INTO episode_history VALUES (1, 1, 1, 1, 'watched', {time.time()});"
    )
    conn.commit()
    conn.close()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    settings = Settings(config_path=cfg, db_path=dbp, admin_user="boss", admin_password="bosspass1")
    with TestClient(main.create_app(settings, start_sync=False)) as c:
        assert c.get("/api/auth/status").json()["needs_setup"] is False
        _login_as(c, "boss", "bosspass1")
        users = main.state.db.list_users()
        assert [(u["username"], u["is_admin"]) for u in users] == [("boss", 1)]
        assert main.state.db.cooldown_keys(users[0]["id"], 0) == ["1:1:1"]


def test_manage_set_password(tmp_path, monkeypatch):
    from app import manage
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    answers = iter(["newpass123", "newpass123", "otherpass1", "otherpass1"])
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt="": next(answers))
    assert manage.main(["set-password", "rescue", "--admin"]) == 0
    db = Database(tmp_path / "t.db")
    u = db.get_user_by_name("rescue")
    assert u["is_admin"] and auth.verify_password("newpass123", u["password_hash"])
    assert manage.main(["set-password", "rescue"]) == 0  # existing user: password only
    u = db.get_user_by_name("rescue")
    assert u["is_admin"] and auth.verify_password("otherpass1", u["password_hash"])


def test_builtin_search_urls_include_store_channels(client):
    from app.access import builtin_search_url
    assert builtin_search_url("Netflix") == "https://www.netflix.com/search?q={q}"
    assert builtin_search_url("BritBox Amazon Channel") == builtin_search_url("Amazon Video")
    assert builtin_search_url("Starz Apple TV Channel") == builtin_search_url("Apple TV")
    assert builtin_search_url("Obscure Streamer") is None

    # A show only on an Amazon Channel you have opens an Amazon search, not TMDB's page.
    cfg = AppConfig(services=["BritBox Amazon Channel"])
    a = compute_access(cfg, ShowConfig(7, ("x",)), "Taskmaster",
                       {"flatrate": _prov("BritBox Amazon Channel"), "link": "https://tmdb/w"})
    assert a.options[0].url == "https://www.amazon.com/s?k=Taskmaster&i=instant-video"

    main.state.settings.config_path.write_text("services: [Netflix, Obscure Streamer]\nshows: []\n")
    assert client.get("/api/config").json()["builtin_search_urls"] == {
        "Netflix": "https://www.netflix.com/search?q={q}",
    }


def test_channels_sorted_case_insensitively_with_posters(client):
    db = main.state.db
    db.replace_episodes({**_show(1), "poster_path": "/one.jpg"}, _eps(1, 1, 3))
    main.state.settings.config_path.write_text(
        "services: [Netflix]\nshows:\n"
        "  - {tmdb_id: 1, channels: [comedy, Animation]}\n"
        "  - {tmdb_id: 2, channels: [comedy]}\n"  # on Hulu only: not watchable, so no poster
    )
    chans = client.get("/api/channels").json()["channels"]
    assert [c["name"] for c in chans] == ["Animation", "comedy"]
    assert chans[1]["posters"] == ["https://image.tmdb.org/t/p/w92/one.jpg"]


def test_channel_emoji_config():
    cfg = parse_config({
        "channels": {"scifi": {"emoji": "🚀"}, "sitcom": {"emoji": ""}, "drama": None,
                     "family": {"emoji": "👨‍👩‍👧‍👦"}},  # multi-codepoint emoji is fine
        "shows": [{"tmdb_id": 1, "channels": ["scifi", "sitcom"]}],
    })
    assert cfg.channel_meta == {"scifi": {"emoji": "🚀"}, "family": {"emoji": "👨‍👩‍👧‍👦"}}
    assert cfg.channel_emoji("scifi") == "🚀" and cfg.channel_emoji("sitcom") is None
    text = dump_config(cfg)
    assert "channels:\n  family:" in text and "scifi:\n    emoji: 🚀" in text
    assert parse_config(yaml.safe_load(text)).channel_meta == cfg.channel_meta

    with pytest.raises(ConfigError, match="no longer list shows"):  # the original config format
        parse_config({"channels": {"sitcom": [{"tmdb_id": 1}]}, "shows": []})
    with pytest.raises(ConfigError, match="single emoji"):
        parse_config({"channels": {"x": {"emoji": "🚀" * 20}}, "shows": []})


def test_channel_emoji_api(client):
    cfg = client.get("/api/config").json()
    body = {k: cfg[k] for k in ("version", "services", "include_free", "include_rent_buy",
                                "cooldown_days", "search_urls")}
    body["channels"] = {"sitcom": {"emoji": "😂"}}
    body["shows"] = [{k: s[k] for k in ("tmdb_id", "channels", "weight", "name", "title", "links")}
                     for s in cfg["shows"]]
    assert client.put("/api/config", json=body).status_code == 200
    assert client.get("/api/config").json()["channels"] == {"sitcom": {"emoji": "😂"}}

    chans = {c["name"]: c for c in client.get("/api/channels").json()["channels"]}
    assert chans["sitcom"]["emoji"] == "😂" and chans["short"]["emoji"] is None
    ep = client.get("/api/pick", params={"channel": "short"}).json()  # show 1 is on short + sitcom
    assert ep["channel_emoji"] == {"sitcom": "😂"}



# --- show links from Wikidata ------------------------------------------------

from app import wikidata  # noqa: E402


def test_wikidata_parse_and_service_mapping():
    rows = [
        {"tmdb": {"value": "1400"}, "fmt": {"value": "https://www.netflix.com/title/$1"}, "id": {"value": "70153373"}},
        {"tmdb": {"value": "1400"}, "fmt": {"value": "https://www.netflix.com/watch/$1"}, "id": {"value": "999"}},  # 2nd Netflix: first wins
        {"tmdb": {"value": "1400"}, "fmt": {"value": "https://play.max.com/show/$1"}, "id": {"value": "abc"}},
        {"tmdb": {"value": "615"}, "fmt": {"value": "https://www.hulu.com/series/$1"}, "id": {"value": "futurama-xyz"}},
        {"tmdb": {"value": "615"}, "fmt": {"value": "https://example.com/$1"}, "id": {"value": "x"}},  # unknown service
        {"tmdb": {"value": "oops"}, "fmt": {"value": "https://www.hulu.com/series/$1"}, "id": {"value": "x"}},
    ]
    assert wikidata.parse_bindings(rows) == {
        1400: {"Netflix": "https://www.netflix.com/title/70153373", "HBO Max": "https://play.max.com/show/abc"},
        615: {"Hulu": "https://www.hulu.com/series/futurama-xyz"},
    }
    assert wikidata.service_for_url("https://www.hbomax.com/series/x") == "HBO Max"
    assert wikidata.service_for_url("https://notmax.com/x") is None


def test_wikidata_fetch_batches_and_escapes():
    seen = []

    def handler(request: httpx.Request):
        body = urllib.parse.parse_qs(request.content.decode())
        seen.append(body["query"][0])
        assert request.headers["user-agent"].startswith("DeadAir/")
        return httpx.Response(200, json={"results": {"bindings": []}})

    ids = list(range(1, wikidata.BATCH + 6))
    assert asyncio.run(wikidata.fetch_show_links(ids, transport=httpx.MockTransport(handler))) == {}
    assert len(seen) == 2  # batched
    assert '"1" "2"' in seen[0] and f'"{wikidata.BATCH + 5}"' in seen[1]
    # Dots in the domain regex reach SPARQL as \\. (a regex-escaped dot inside a string literal).
    assert "netflix\\\\.com" in seen[0] and "themoviedb.org/tv/" in seen[0]


def test_link_priority_manual_then_auto_then_search():
    cfg = AppConfig(services=["Hulu", "Max", "Netflix"])
    providers = {"flatrate": _prov("Hulu", "Max", "Netflix")}
    auto = {"Hulu": "https://www.hulu.com/series/auto", "HBO Max": "https://play.max.com/show/auto"}
    show = ShowConfig(7, ("x",), links={"Hulu": "https://www.hulu.com/series/mine"})
    opts = {o.provider_name: o for o in compute_access(cfg, show, "Some Show", providers, auto).options}
    assert (opts["Hulu"].url, opts["Hulu"].source) == ("https://www.hulu.com/series/mine", "manual")
    assert (opts["Max"].url, opts["Max"].source) == ("https://play.max.com/show/auto", "auto")  # HBO Max link covers "Max"
    assert opts["Netflix"].source == "search" and "search?q=Some+Show" in opts["Netflix"].url


def test_access_endpoint_reflects_edited_links(client):
    first = client.get("/api/access", params={"tmdb_id": 2}).json()["access"]
    assert first["tier"] is None  # show 2 is only on Hulu, which you don't have
    assert client.get("/api/access", params={"tmdb_id": 1, "season": 1}).json()["access"]["options"][0]["source"] == "manual"
    assert client.get("/api/access", params={"tmdb_id": 999}).status_code == 404
    client.post("/api/auth/logout")
    assert client.get("/api/access", params={"tmdb_id": 1}).status_code == 401


# --- episode order (e.g. Firefly's DVD order) -------------------------------

# Firefly aired out of order: TMDB's S1E1 is "The Train Job", but in DVD order
# the pilot "Serenity" (TMDB's S1E11) comes first.
FIREFLY_DVD = {"id": "dvd1", "name": "DVD Order", "groups": [
    {"name": "Specials", "order": 0, "episodes": [{"season_number": 0, "episode_number": 1, "order": 0}]},
    {"name": "Season 1", "order": 1, "episodes": [
        {"season_number": 1, "episode_number": 1, "order": 1},   # The Train Job
        {"season_number": 1, "episode_number": 11, "order": 0},  # Serenity
        {"season_number": 1, "episode_number": 2, "order": 2},
    ]},
]}


def _episode_group_tmdb(calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/3/tv/1/episode_groups":
            return httpx.Response(200, json={"results": [
                {"id": "dvd1", "name": "DVD Order", "type": 3, "episode_count": 14},
                {"id": "x", "name": "Story", "type": 5, "episode_count": 14},
            ]})
        if request.url.path == "/3/tv/episode_group/dvd1":
            return httpx.Response(200, json=FIREFLY_DVD)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_fetch_episode_order_maps_tmdb_numbers():
    client = TMDBClient("k", transport=_episode_group_tmdb())
    mapping = asyncio.run(client.fetch_episode_order("dvd1"))
    assert mapping == {"1:11": [1, 1], "1:1": [1, 2], "1:2": [1, 3], "0:1": [0, 1]}
    groups = asyncio.run(client.list_episode_groups(1))
    assert [(g["id"], g["type"]) for g in groups] == [("dvd1", "DVD"), ("x", "Story arc")]


def test_episode_order_relabels_picks_but_not_history(client, tmp_path):
    db = main.state.db
    cfg = parse_config({"shows": [{"tmdb_id": 1, "channels": ["short"], "episode_order": "dvd1"}]})
    syncer = Syncer(Settings(wikidata_links=False), db, TMDBClient("k", transport=_episode_group_tmdb()))
    with db.connect() as conn:  # episodes and providers are fresh; only the order is new
        conn.execute("UPDATE shows SET episodes_refreshed_at = ?, providers_checked_at = ?, "
                     "season_providers_json = '{}'", (time.time(), time.time()))
    asyncio.run(syncer.run_once(cfg))
    assert syncer.last_error is None
    assert json.loads(db.get_show(1)["episode_order_json"])["map"]["1:1"] == [1, 2]

    seen = {}
    for _ in range(40):
        ep = client.get("/api/pick", params={"channel": "short"}).json()
        seen[(ep["season"], ep["episode"])] = ep["code"]
    # TMDB's 1x1 is labelled as DVD episode 2; numbers the order doesn't cover keep TMDB's.
    assert seen == {(1, 1): "S01E02", (1, 2): "S01E03", (1, 3): "S01E03"}
    # History still uses TMDB's numbers.
    client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": 1, "kind": "watched"})
    assert {client.get("/api/pick", params={"channel": "short"}).json()["episode"] for _ in range(20)} == {2, 3}

    # Back to TMDB's order: the next sync drops the mapping.
    asyncio.run(syncer.run_once(parse_config({"shows": [{"tmdb_id": 1, "channels": ["short"]}]})))
    assert db.get_show(1)["episode_order_json"] is None


def test_episode_order_in_config_and_api(client):
    text = dump_config(parse_config({"shows": [{"tmdb_id": 5, "channels": ["a"], "episode_order": "dvd1"}]}))
    assert "episode_order: dvd1" in text
    assert parse_config(yaml.safe_load(text)).shows[0].episode_order == "dvd1"

    cfg = client.get("/api/config").json()
    assert cfg["shows"][0]["episode_order"] is None
    cfg["shows"][0]["episode_order"] = "dvd1"
    assert client.put("/api/config", json=cfg).status_code == 200
    assert client.get("/api/config").json()["shows"][0]["episode_order"] == "dvd1"

    main.state.tmdb = TMDBClient("k", transport=_episode_group_tmdb())
    groups = client.get("/api/tmdb/episode_groups", params={"tmdb_id": 1}).json()["groups"]
    assert groups[0] == {"id": "dvd1", "name": "DVD Order", "type": "DVD", "episode_count": 14}


def test_ban_an_episode_for_everyone(client):
    # Skip returns an id, so a mis-tap can be undone.
    res = client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": 2, "kind": "skipped"}).json()
    assert client.delete(f"/api/history/{res['id']}").json() == {"ok": True}

    # Ban S01E02 of show 1: never picked again, even once everything else is on cooldown.
    assert client.post("/api/ban", json={"tmdb_id": 1, "season": 1, "episode": 2}).json() == {
        "ok": True, "never_pick": ["S01E02"]}
    for ep in (1, 3):
        client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": ep, "kind": "watched"})
    assert {client.get("/api/pick", params={"channel": "short"}).json()["episode"] for _ in range(20)} == {1, 3}

    # Saved on the show in config.yaml; the editor gets labels for it.
    assert "never_pick:\n  - S01E02" in main.state.config_text
    cfg = client.get("/api/config").json()
    show = cfg["shows"][0]
    assert show["never_pick"] == ["S01E02"]
    assert show["never_pick_info"] == {"S01E02": {"code": "S01E02", "title": "Ep 2"}}

    # Bans apply to everyone, but only admins can ban.
    client.post("/api/users", json={"username": "kid", "password": "kidpass123"})
    kid = TestClient(client.app)
    _login_as(kid, "kid", "kidpass123")
    assert {kid.get("/api/pick", params={"channel": "short"}).json()["episode"] for _ in range(20)} == {1, 3}
    assert kid.post("/api/ban", json={"tmdb_id": 1, "season": 1, "episode": 1}).status_code == 403

    # Unban from the editor (a normal config save).
    show["never_pick"] = []
    assert client.put("/api/config", json=cfg).status_code == 200
    assert 2 in {kid.get("/api/pick", params={"channel": "short"}).json()["episode"] for _ in range(40)}


def test_never_pick_config_validation():
    cfg = parse_config({"shows": [{"tmdb_id": 1, "channels": ["a"], "never_pick": ["s6e10", "S06E10", "S01E02"]}]})
    assert cfg.shows[0].never_pick == ("S01E02", "S06E10")  # normalised, de-duplicated, sorted
    with pytest.raises(ConfigError, match="isn't an episode code"):
        parse_config({"shows": [{"tmdb_id": 1, "channels": ["a"], "never_pick": ["episode 10"]}]})


def test_roll_one_show_from_all_shows(client):
    # "Let's watch a random Show 1": every channel, that show only.
    picks = [client.get("/api/pick", params={"channel": "*", "show": 1}).json() for _ in range(10)]
    assert {p["tmdb_id"] for p in picks} == {1}
    shows = {s["tmdb_id"]: s for s in client.get("/api/shows").json()["shows"]}
    assert shows[1]["watchable"] is True and shows[2]["watchable"] is False  # show 2 is only on Hulu
    r = client.get("/api/pick", params={"channel": "*", "show": 2})
    assert r.status_code == 404 and r.json()["detail"] == "Show 2 isn't on your services right now"
    assert client.get("/api/pick", params={"channel": "*", "show": 999}).json()["detail"] == "unknown show"


def test_surprise_me_rolls_from_every_show(client):
    db = main.state.db
    db.set_providers(2, {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]})  # both watchable now
    data = client.get("/api/channels").json()
    assert data["all_channels"]["name"] == "*" and data["all_channels"]["shows"] == 2
    picked = {client.get("/api/pick", params={"channel": "*"}).json()["tmdb_id"] for _ in range(30)}
    assert picked == {1, 2}  # show 2 isn't in "short"; "*" covers every channel


def test_channel_posters_are_a_random_few(monkeypatch):
    infos = [{"thumb_url": f"p{i}"} for i in range(6)] + [{"thumb_url": None}]
    seen = {tuple(main._random_posters(infos)) for _ in range(30)}
    assert all(len(p) == main.CHANNEL_POSTERS and set(p) <= {f"p{i}" for i in range(6)} for p in seen)
    assert len(seen) > 1  # not always the same four
    assert main._random_posters(infos[:2]) in (["p0", "p1"], ["p1", "p0"])


def test_recent_history_and_undo(client):
    client.post("/api/history", json={"tmdb_id": 1, "season": 1, "episode": 1, "kind": "watched"})
    client.post("/api/history", json={"tmdb_id": 2, "season": 1, "episode": 3, "kind": "skipped"})
    hist = client.get("/api/history").json()["history"]
    assert [(h["show_name"], h["code"], h["kind"]) for h in hist] == [
        ("Show 2", "S01E03", "skipped"), ("Show 1", "S01E01", "watched")]  # newest first
    assert hist[1]["title"] == "Ep 1" and hist[1]["cooling_down"] is True

    # Someone else can't undo your marks.
    client.post("/api/users", json={"username": "kid", "password": "kidpass123"})
    other = TestClient(client.app)
    _login_as(other, "kid", "kidpass123")
    assert other.get("/api/history").json()["history"] == []
    assert other.delete(f"/api/history/{hist[1]['id']}").status_code == 404

    assert client.delete(f"/api/history/{hist[1]['id']}").json() == {"ok": True}
    assert [h["show_name"] for h in client.get("/api/history").json()["history"]] == ["Show 2"]
    assert client.delete(f"/api/history/{hist[1]['id']}").status_code == 404


def test_openable_services_come_before_searches():
    # Prime is higher in your list, but only Hulu has a link to the show itself.
    cfg = AppConfig(services=["Amazon Prime Video", "Netflix", "Hulu", "Fubo"])
    providers = {"flatrate": _prov("Amazon Prime Video", "Netflix", "Hulu", "Fubo")}
    show = ShowConfig(3452, ("x",), links={"Netflix": "https://www.netflix.com/title/1"})
    auto = {"Hulu": "https://www.hulu.com/series/frasier"}
    opts = compute_access(cfg, show, "Frasier", providers, auto).options
    assert [(o.provider_name, o.source) for o in opts] == [
        ("Netflix", "manual"), ("Hulu", "auto"),              # openable, in your order
        ("Amazon Prime Video", "search"), ("Fubo", "tmdb"),  # then searches, then TMDB's page
    ]


def test_config_api_reports_open_link_fallbacks(client):
    db = main.state.db
    db.set_auto_links({1: {"Netflix": "https://www.netflix.com/title/111"}}, [1, 2])
    db.set_providers(2, {"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]})
    shows = {s["tmdb_id"]: s for s in client.get("/api/config").json()["shows"]}
    # Show 1 has a pasted Netflix link; the fallback shown is what Open would use without it.
    assert shows[1]["watch"] == [{"provider_name": "Netflix", "logo_url": None,
                                  "fallback_url": "https://www.netflix.com/title/111", "fallback_source": "auto"}]
    assert shows[2]["watch"][0]["fallback_source"] == "search"
    ep = client.get("/api/pick", params={"channel": "short"}).json()
    assert ep["access"]["options"][0]["source"] == "manual"  # the pasted link still wins on picks


def test_version_is_public(client, monkeypatch):
    client.post("/api/auth/logout")
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("BUILD_DATE", raising=False)
    assert client.get("/api/version").json() == {
        "version": "dev", "commit": None, "commit_full": None, "built": None,
    }
    monkeypatch.setenv("APP_VERSION", "pr-3")
    monkeypatch.setenv("GIT_COMMIT", "1a3607a0123456789abcdef0123456789abcdef0")
    monkeypatch.setenv("BUILD_DATE", "2026-09-29T12:00:00Z")
    res = client.get("/api/version")
    assert res.json()["version"] == "pr-3"
    # The CI smoke test greps for this exact compact form.
    assert '"commit":"1a3607a"' in res.text


def test_icons_and_manifest_are_served(client):
    page = client.get("/").text
    for path in ("static/icon.svg", "static/apple-touch-icon.png", "static/manifest.webmanifest"):
        assert re.search(rf'href="{re.escape(path)}\?v=[0-9a-f]{{10}}"', page), path
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    # Installable as an app: its own window, and the 192/512px icons browsers require.
    assert manifest.json()["display"] == "standalone"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in manifest.json()["icons"]}
    for icon in manifest.json()["icons"]:
        assert client.get(f"/static/{icon['src']}").status_code == 200, icon["src"]
    # Every <use href="#…"> points at an icon defined in the page's sprite.
    assert set(re.findall(r'<use href="#([\w-]+)"', page)) <= set(re.findall(r'<symbol id="([\w-]+)"', page))


def test_ui_files_are_revalidated(client):
    """Browsers must not run a stale app.js against a new index.html."""
    for path in ("/", "/static/app.js", "/static/style.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    # The page links each file by a hash of its contents, so an update gets new URLs.
    page = client.get("/").text
    assert re.search(r'src="static/app\.js\?v=[0-9a-f]{10}"', page)
    assert re.search(r'href="static/style\.css\?v=[0-9a-f]{10}"', page)
    etag = client.get("/static/app.js").headers["etag"]
    assert client.get("/static/app.js", headers={"If-None-Match": etag}).status_code == 304


# --- update check ---------------------------------------------------------------

def _github(latest_tag="v1.3.0", ahead_by=0, calls=None, fail=False):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if fail:
            return httpx.Response(503)
        if request.url.path.endswith("/releases/latest"):
            if latest_tag is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={
                "tag_name": latest_tag, "html_url": f"https://github.com/x/releases/tag/{latest_tag}"})
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={"ahead_by": ahead_by, "status": "ahead" if ahead_by else "identical"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_update_check_for_releases():
    def check(version, **kw):
        return asyncio.run(updates.check(version, "abc1234", _github(**kw)))
    newer = check("v1.2.0")
    assert newer["available"] and newer["latest"] == "v1.3.0" and newer["url"].endswith("/v1.3.0")
    assert check("v1.3.0")["available"] is False
    assert check("1.10.0", latest_tag="v1.9.0")["available"] is False  # numeric, not string, order
    assert check("v1.2.0", latest_tag=None) == {"available": False, "latest": None, "url": None, "behind": None}


def test_update_check_for_main_builds():
    calls = []
    res = asyncio.run(updates.check("main", "abc1234def", _github(ahead_by=3, calls=calls)))
    assert res["available"] and res["behind"] == 3 and res["url"].endswith("/compare/abc1234...main")
    assert calls == ["/repos/JamesHeadrick/DeadAir-TV/compare/abc1234def...main"]
    assert asyncio.run(updates.check("main", "abc", _github(ahead_by=0)))["available"] is False
    # Test builds aren't checked at all.
    for version, commit in (("pr-3", "abc"), ("dev", None), ("main", None), ("v1.2.0-rc1", "abc")):
        assert asyncio.run(updates.check(version, commit, _github(fail=True))) is None


def test_update_checker_caches_and_survives_failures(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(updates.time, "time", lambda: now[0])
    calls = []
    checker = updates.UpdateChecker("v1.0.0", "abc", _github(calls=calls))
    assert asyncio.run(checker.get())["update"]["available"]
    asyncio.run(checker.get())
    assert len(calls) == 1  # cached

    checker.transport = _github(calls=calls, fail=True)
    now[0] += updates.CHECK_EVERY_S
    assert asyncio.run(checker.get())["update"]["latest"] == "v1.3.0"  # GitHub down: keep the last answer
    now[0] += 60
    asyncio.run(checker.get())
    assert len(calls) == 2  # and don't retry for a while
    now[0] += updates.RETRY_AFTER_S
    asyncio.run(checker.get())
    assert len(calls) == 3


def test_updates_endpoint(client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "main")
    monkeypatch.setenv("GIT_COMMIT", "abc1234def")
    monkeypatch.setattr(main.state, "updates", updates.UpdateChecker("main", "abc1234def", _github(ahead_by=2)))
    res = client.get("/api/updates").json()
    assert res["enabled"] and res["update"]["behind"] == 2

    monkeypatch.setattr(main.state, "settings", dataclasses.replace(main.state.settings, update_check=False))
    assert client.get("/api/updates").json() == {"enabled": False, "update": None, "checked_at": None}

    client.post("/api/users", json={"username": "kid", "password": "kidpass123"})
    _login_as(client, "kid", "kidpass123")
    assert client.get("/api/updates").status_code == 403  # admins only
