import asyncio
import json
import random
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import ConfigError, Settings, ShowConfig, parse_channels
from app.db import Database
from app.picker import pick_episode
from app.sync import Syncer
from app.tmdb import TMDBClient, service_available


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

def test_parse_channels_defaults_and_order():
    cfg = parse_channels({"channels": {
        "B": [{"tmdb_id": "1", "service": "Netflix", "show_url": "https://x"}],
        "A": [{"tmdb_id": 2, "service": "Hulu", "show_url": "https://y", "weight": 3}],
    }})
    assert list(cfg.channels) == ["B", "A"]
    assert cfg.channels["B"][0] == ShowConfig(1, "Netflix", "https://x", 1.0)
    assert cfg.channels["A"][0].weight == 3


@pytest.mark.parametrize("bad", [
    {},
    {"channels": {"A": []}},
    {"channels": {"A": [{"tmdb_id": 1, "service": "Netflix"}]}},
    {"channels": {"A": [{"tmdb_id": 1, "service": "N", "show_url": "u", "weight": 0}]}},
])
def test_parse_channels_rejects_bad_config(bad):
    with pytest.raises(ConfigError):
        parse_channels(bad)


# --- provider matching -------------------------------------------------------

@pytest.mark.parametrize("service,providers,expected", [
    ("Netflix", ["Netflix"], True),
    ("Netflix", ["Netflix Standard with Ads"], True),
    ("Disney+", ["Disney Plus"], True),
    ("Paramount+", ["Paramount Plus Essential"], True),
    ("Max", ["Max Amazon Channel"], False),
    ("Hulu", ["Netflix", "Peacock Premium"], False),
    ("Hulu", [], False),
])
def test_service_available(service, providers, expected):
    assert service_available(service, [{"provider_name": p} for p in providers]) is expected


# --- picking -----------------------------------------------------------------

def test_pick_is_uniform_across_episodes_and_respects_weights(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_episodes(_show(1), _eps(1, 1, 30))
    db.replace_episodes(_show(2), _eps(2, 1, 10))
    rng = random.Random(0)

    shows = [ShowConfig(1, "N", "u"), ShowConfig(2, "N", "u")]
    c = Counter(pick_episode(db, shows, rng)[0].tmdb_id for _ in range(4000))
    assert 0.70 < c[1] / 4000 < 0.80  # 30 of 40 episodes

    shows = [ShowConfig(1, "N", "u"), ShowConfig(2, "N", "u", weight=3)]
    c = Counter(pick_episode(db, shows, rng)[0].tmdb_id for _ in range(4000))
    assert 0.45 < c[1] / 4000 < 0.55  # 30 vs 10*3


def test_pick_skips_specials_and_unaired(tmp_path):
    db = Database(tmp_path / "t.db")
    eps = _eps(1, 0, 5) + _eps(1, 1, 1) + _eps(1, 2, 5, air_date="2999-01-01")
    eps += [dict(e, season=3, air_date=None) for e in _eps(1, 3, 5)]
    db.replace_episodes(_show(1, status="Returning Series"), eps)
    rng = random.Random(1)
    for _ in range(50):
        _, ep = pick_episode(db, [ShowConfig(1, "N", "u")], rng, today="2026-01-01")
        assert (ep["season"], ep["episode"]) == (1, 1)


def test_pick_includes_undated_episodes_of_ended_shows(tmp_path):
    db = Database(tmp_path / "t.db")
    db.replace_episodes(_show(1, status="Ended"), [dict(e, air_date=None) for e in _eps(1, 1, 3)])
    assert pick_episode(db, [ShowConfig(1, "N", "u")]) is not None


def test_pick_with_empty_cache_returns_none(tmp_path):
    assert pick_episode(Database(tmp_path / "t.db"), [ShowConfig(1, "N", "u")]) is None


# --- TMDB sync ---------------------------------------------------------------

def _fake_tmdb(requests):
    def handler(request: httpx.Request):
        requests.append(request)
        path = request.url.path
        if path == "/3/tv/99/watch/providers":
            return httpx.Response(200, json={"results": {
                "US": {"flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]},
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
    cfg = parse_channels({"channels": {"A": [
        {"tmdb_id": 99, "service": "Netflix", "show_url": "u"},
    ]}})
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
    assert json.loads(row["providers_json"])[0]["provider_name"] == "Hulu"

    # Second run within the refresh windows does nothing.
    requests.clear()
    asyncio.run(syncer.run_once(cfg))
    assert requests == []


def test_bearer_token_auth():
    requests = []
    client = TMDBClient("eyJhbGciOi.token", transport=_fake_tmdb(requests))
    asyncio.run(client.fetch_flatrate_providers(99, "US"))
    assert requests[0].headers["authorization"] == "Bearer eyJhbGciOi.token"
    assert "api_key" not in requests[0].url.params


# --- HTTP API ----------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "channels.yaml"
    cfg.write_text(
        "channels:\n"
        "  Sitcoms:\n"
        "    - tmdb_id: 1\n"
        "      service: Netflix\n"
        "      show_url: https://netflix.example/1?a=1&b=2\n"
    )
    settings = Settings(config_path=cfg, db_path=tmp_path / "t.db", enable_adb=True, tv_ip="10.0.0.5")
    app = main.create_app(settings, start_sync=False)
    with TestClient(app) as c:
        main.state.db.replace_episodes(_show(1), _eps(1, 1, 3))
        main.state.db.set_providers(1, [{"provider_id": 15, "provider_name": "Hulu"}])
        yield c


def test_api_channels_and_pick(client):
    data = client.get("/api/channels").json()
    assert data["channels"][0]["name"] == "Sitcoms"
    assert data["channels"][0]["shows"][0]["available"] is False
    assert data["adb_enabled"] is True

    ep = client.get("/api/pick", params={"channel": "Sitcoms"}).json()
    assert ep["show_name"] == "Show 1"
    assert ep["code"].startswith("S01E0")
    assert ep["providers"] == ["Hulu"]
    assert ep["show_url"] == "https://netflix.example/1?a=1&b=2"

    assert client.get("/api/pick", params={"channel": "Nope"}).status_code == 404
    assert client.get("/").status_code == 200


def test_api_play_uses_configured_url(client, monkeypatch):
    calls = []

    async def fake_run(*args, timeout=15):
        calls.append(args)
        return "connected to 10.0.0.5:5555" if args[1] == "connect" else "Starting: Intent"

    monkeypatch.setattr(main.adb, "_run", fake_run)
    r = client.post("/api/play", json={"channel": "Sitcoms", "tmdb_id": 1})
    assert r.status_code == 200, r.text
    assert calls[0] == ("adb", "connect", "10.0.0.5:5555")
    assert calls[1][-1] == "'https://netflix.example/1?a=1&b=2'"

    assert client.post("/api/play", json={"channel": "Sitcoms", "tmdb_id": 2}).status_code == 404
