# DeadAir-TV
Solves the "there's nothing to watch" once and for all

A small self-hosted "random episode channel" web app. You set up channels,
each a list of shows. Tap a channel and it picks a random episode from them,
with an **Open** button that deep-links into the streaming app. **Reroll**
picks again.

- Episode lists come from TMDB and are cached in SQLite. They refresh weekly.
- Once a day it checks TMDB watch providers (JustWatch data, US flatrate by
  default). Any show that's no longer on its configured service gets flagged in the UI.
- One mobile-first dark page, no auth. Meant for LAN/VPN use only.
- Optional: **Play on TV** launches the deep link on a Chromecast with Google TV over ADB.

## Quick start (Raspberry Pi 5 / any Docker host)

```sh
git clone https://github.com/JamesHeadrick/DeadAir-TV.git && cd DeadAir-TV
cp .env.example .env                        # set TMDB_API_KEY
cp channels.example.yaml channels.yaml      # add your channels
mkdir -p data && sudo chown 1000:1000 data  # container runs as uid 1000
docker compose up -d --build
```

Then open `http://<pi-ip>:8000`. The first TMDB sync starts right away and
takes a few seconds per show.

The image is built from `python:3.12-slim-bookworm`, which is multi-arch, so
building it on the Pi gives you an arm64 image with no extra steps.

### Nginx Proxy Manager

Add a Proxy Host that forwards to `http://<pi-ip>:8000`. If NPM is on the same
Docker network, you can use `http://deadair-tv:8000` and drop the `ports:`
mapping. No special settings are needed, and every URL in the app is relative.
Put an Access List on it if the proxy is reachable from outside your LAN/VPN.

## Configuration

### `channels.yaml`

```yaml
channels:
  Sitcoms:
    - tmdb_id: 1400          # from themoviedb.org/tv/1400-seinfeld
      service: Netflix       # TMDB/JustWatch provider name
      show_url: https://www.netflix.com/title/...
    - tmdb_id: 2316
      service: Peacock Premium
      show_url: https://www.peacocktv.com/...
      weight: 2              # optional
      name: The Office       # optional display-name override
```

- **Picking**: every aired episode (season 0 and unaired episodes excluded)
  has the same chance of being picked. A show with 200 episodes therefore
  comes up 10× as often as one with 20.
- **weight** (default `1`) multiplies the odds of each of that show's episodes.
  With `weight: 2`, each of its episodes is twice as likely as a normal one.
- **service** is matched loosely against TMDB provider names: case and
  punctuation are ignored, `Disney+` matches `Disney Plus`, and `Netflix`
  matches `Netflix Standard with Ads`. Add-on channels like "Max Amazon
  Channel" don't count. When a show is flagged, the card lists the provider
  names TMDB currently reports, so you can copy the exact one.

After editing `channels.yaml`, run `curl -X POST http://<host>:8000/api/refresh`
to reload it and force a full re-sync. Restarting the container also works.

### Environment (`.env`)

| Var | Default | |
|---|---|---|
| `TMDB_API_KEY` | – | v3 API key or v4 read access token |
| `WATCH_REGION` | `US` | provider region to check |
| `EPISODE_REFRESH_DAYS` | `7` | |
| `PROVIDER_CHECK_HOURS` | `24` | |
| `ENABLE_ADB` | `false` | phase 2, see below |
| `TV_IP` | – | Chromecast IP |
| `ADB_PORT` | `5555` | |

## Phase 2: Play on TV (optional)

1. On the Chromecast with Google TV, go to *Settings → System → About* and tap
   *Android TV OS build* 7 times to enable Developer options. Then turn on
   *Settings → System → Developer options → USB debugging*.
2. Give the TV a DHCP reservation, then set `ENABLE_ADB=true` and `TV_IP=...` in `.env`.
3. Press **Play on TV** once and accept the "Allow USB debugging?" prompt on
   the TV (tick *Always allow*). The key is stored in `./data/.android`, so
   this is a one-time step.

The button runs `adb connect <tv-ip>:5555` and then
`adb shell am start -a android.intent.action.VIEW -d <show_url>`. Only URLs
from `channels.yaml` are ever sent. Whether the link opens the right app
depends on the service's Android TV app handling that URL.

## Development

```sh
pip install -r requirements-dev.txt
pytest
CHANNELS_CONFIG=channels.yaml DB_PATH=data/dev.db TMDB_API_KEY=... uvicorn app.main:app --reload
```

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
Streaming availability data is provided by JustWatch via TMDB.
