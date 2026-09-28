import asyncio
import json
import random
import time
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
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
    assert text.startswith("# DeadAir TV config")
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
        r = c.post("/api/auth/setup", json={"username": "admin", "password": "correct horse"})
        assert r.status_code == 200, r.text
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
    assert [c["name"] for c in channels] == ["sitcom", "scifi", "short"]
    assert channels[1]["shows"] == ["Show 2", "Brand New"]  # uncached show falls back to title
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
