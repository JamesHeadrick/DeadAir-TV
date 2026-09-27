# DeadAir-TV
Solves the "there's nothing to watch" once and for all

A small self-hosted "random episode channel" web app. You list the streaming
services you have and the shows you like, tagging each show with one or more
channels (`sitcom`, `scifi`, `short`...). Tap a channel and it picks a random
episode, then shows where *you* can watch it. **Reroll** picks again.

- Episode lists come from TMDB and are cached in SQLite. They refresh weekly.
- Once a day it asks TMDB (JustWatch data) where each show is streaming and
  compares that against your services. Best option first:
  1. **your subscriptions**
  2. **free / with ads** (Tubi, Pluto TV, ...)
  3. **rent / buy** (only when there's nothing else)

  Shows you can't watch anywhere are skipped when picking and flagged in the UI.
- An **All shows** page lists every show, where you can watch it, and which
  other services carry it.
- One mobile-first dark page, no auth. Meant for LAN/VPN use only.
- Optional: **Play on TV** launches the link on a Chromecast with Google TV over ADB.

## Quick start (Raspberry Pi 5 / any Docker host)

```sh
git clone https://github.com/JamesHeadrick/DeadAir-TV.git && cd DeadAir-TV
cp .env.example .env                        # set TMDB_API_KEY
mkdir -p config data
cp config.example.yaml config/config.yaml   # optional: or start empty and use Settings
sudo chown -R 1000:1000 config data         # container runs as uid 1000
docker compose up -d --build
```

Then open `http://<pi-ip>:8000` and tap **⚙** to pick your services and add
shows. Each new show's episodes are fetched right away, which takes a few
seconds per show.

The image is built from `python:3.12-slim-bookworm`, which is multi-arch, so
building it on the Pi gives you an arm64 image with no extra steps.

### Nginx Proxy Manager

Add a Proxy Host that forwards to `http://<pi-ip>:8000`. If NPM is on the same
Docker network, you can use `http://deadair-tv:8000` and drop the `ports:`
mapping. No special settings are needed, and every URL in the app is relative.
Put an Access List on it if the proxy is reachable from outside your LAN/VPN.

## Configuration

### Settings page (⚙)

Everything in `config.yaml` can be edited from the web UI:

- **Your services**: search TMDB's provider list for your region and tap to
  add, so the names always match what TMDB reports. The order they're listed
  in is the order watch options are shown in.
- **Where to watch**: turn the free/with-ads and rent/buy fallbacks on or off.
- **Shows**: search TMDB by name to add a show. For each show you can edit its
  channel tags (pick an existing channel or type a new one), its weight, a
  display name, and its deep links.
- **Search links**: add or override a service's search URL.

Saving rewrites `config/config.yaml` and keeps the previous version as
`config.yaml.bak`. Comments in the file don't survive a save from the UI. If
the file changed on disk since you opened Settings, the save is refused, so
you don't overwrite someone else's changes.

### `config.yaml`

The UI writes this file, but hand edits are fine too. They're picked up
automatically without a restart. If a hand edit breaks the YAML, the app
keeps using the last good version and shows the error.

```yaml
services: [Netflix, Hulu, Disney Plus]   # what you subscribe to

shows:
  - tmdb_id: 1400             # from themoviedb.org/tv/1400-seinfeld
    channels: [sitcom]
  - tmdb_id: 615              # Futurama
    channels: [sitcom, scifi, short]
    weight: 2                 # optional
    name: Futurama            # optional display-name override
    links:                    # optional exact deep links per service
      Hulu: https://www.hulu.com/series/...
```

- **Channels** are tags. Each tag becomes a channel button, and a show can
  have as many as you like.
- **services** are matched loosely against TMDB provider names: case and
  punctuation are ignored, `Disney+` matches `Disney Plus`, and `Netflix`
  matches `Netflix Standard with Ads`. Add-on channels such as "HBO Max Amazon
  Channel" don't count. The **All shows** page lists the exact names TMDB uses
  for services you don't have, so you can copy them.
- **Open links**: TMDB tells you *which* services carry a show, but it doesn't
  give a link to the show inside each service. So **Open** goes to that
  service's search page for the show. A `links:` entry replaces the search page
  with an exact deep link. If a service has no known search URL, Open falls
  back to TMDB's "Where to watch" page. You can add search URLs with
  `search_urls:` (see `config.example.yaml`).
- `include_free: false` / `include_rent_buy: false` drop those tiers.
- **Picking**: every aired episode (season 0 and unaired episodes excluded)
  has the same chance of being picked, among the shows you can watch. A show
  with 200 episodes therefore comes up 10× as often as one with 20.
- **weight** (default `1`) multiplies the odds of each of that show's episodes.

To force a full TMDB re-sync (e.g. after a show adds a new season), run
`curl -X POST http://<host>:8000/api/refresh`.

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
`adb shell am start -a android.intent.action.VIEW -d <url>`, using the first
watch option's link. The server builds the URL itself; the browser never
supplies it. Exact `links:` deep links work best, because many TV apps ignore
search URLs.

## Roadmap

- **v2: logins.** There's no auth today: anyone who can reach the app can
  change its settings. That's fine for LAN/VPN use, but per-user logins (and
  maybe per-user services and channels) would be the next step.

## Development

```sh
pip install -r requirements-dev.txt
pytest
CONFIG_PATH=config/config.yaml DB_PATH=data/dev.db TMDB_API_KEY=... uvicorn app.main:app --reload
```

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
Streaming availability data is provided by JustWatch via TMDB.
