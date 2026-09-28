# DeadAir-TV
Solves the "there's nothing to watch" once and for all

A small self-hosted "random episode channel" web app. You list the streaming
services you have and the shows you like, tagging each show with one or more
channels (`sitcom`, `scifi`, `short`...). Tap a channel and it picks a random
episode, then shows where *you* can watch it. Not feeling it?

- **Different show**: another show from the same channel. Skipped shows stay
  skipped until you leave the channel.
- **Another episode**: same show, different episode.
- **Reroll**: anything from the channel.

None of them repeat an episode you've already been shown during that channel visit.

To keep an episode out of rotation for a while, mark it:

- **Mark watched**: puts the episode on cooldown (14 days by default). Tap
  it again to undo. Nothing counts as watched until you tap this, so rolling
  an episode and never getting to it doesn't cost you anything.
- **Skip**: "not this one." It goes on the same cooldown, and the app rerolls
  straight away.

When an episode comes back up after its cooldown, the card mentions it
(e.g. "You watched this 3 weeks ago"). You can change the cooldown or clear
the history in Settings.

- Episode lists come from TMDB and are cached in SQLite. They refresh weekly.
- Once a day it asks TMDB (JustWatch data) where each show is streaming and
  compares that against your services. Best option first:
  1. **your subscriptions**
  2. **free / with ads** (Tubi, Pluto TV, ...)
  3. **rent / buy** (only when there's nothing else)

  Shows you can't watch anywhere are skipped when picking and flagged in the UI.
- An **All shows** page lists every show, where you can watch it, and which
  other services carry it.
- Username/password logins. **Admins** manage settings and users.
  **Viewers** can pick episodes and keep their own watched/skipped history.
- One mobile-first dark page.
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

Then open `http://<pi-ip>:8000`. On the first visit you create the admin
account; alternatively, set `ADMIN_USER`/`ADMIN_PASSWORD` in `.env` to create
it at startup. Then tap **⚙** to pick your services and add shows. Each new show's episodes are fetched right away, which takes a few
seconds per show.

The image is built from `python:3.12-slim-bookworm`, which is multi-arch, so
building it on the Pi gives you an arm64 image with no extra steps.

### Nginx Proxy Manager

Add a Proxy Host that forwards to `http://<pi-ip>:8000`. If NPM is on the same
Docker network, you can use `http://deadair-tv:8000` and drop the `ports:`
mapping. No special settings are needed, and every URL in the app is relative.
Serve it over HTTPS (NPM + Let's Encrypt, or your VPN's TLS) if you can. The
login cookie is marked HTTPS-only automatically when the request comes in over
HTTPS. Over plain `http://` on a LAN it still works, but the cookie is sent
unencrypted.

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
- **Watched & skipped**: set the cooldown length and clear the history.
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
- **services** are matched against TMDB provider names, ignoring case and
  punctuation. An exact match wins. If you picked "Netflix", its other plans
  (like "Netflix Standard with Ads") aren't shown as extra options. If there's
  no exact match, a looser match is tried: a hand-typed `Disney+` finds
  `Disney Plus`, and `Paramount+` finds `Paramount Plus Essential`. Add-on
  channels sold through another store, such as "HBO Max Amazon Channel", never
  count. Picking services in Settings uses TMDB's exact names, so you don't
  have to worry about any of this.
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

## Accounts

- **First run**: the first visitor creates the admin account, unless
  `ADMIN_USER`/`ADMIN_PASSWORD` already created one. If the app is reachable
  by others before you've done this, set the env vars instead.
- **Settings → Users** (admins): add users as admin or viewer, switch roles,
  reset passwords, and delete users. You can't delete yourself or demote the
  last admin.
- **Settings → Account** (everyone): change your password (this logs out your
  other devices) or log out.
- Each person's **✓ Watched / Skip** history and cooldowns are their own.
  History recorded before logins existed goes to the first admin.
- Sessions last 30 days. Passwords are hashed with scrypt. After 10 failed
  logins from one IP within 15 minutes, logins from that IP are blocked for a
  while.
- **Locked out?** Reset a password (or create a new admin) from the command line:

  ```sh
  docker compose exec deadair-tv python -m app.manage list-users
  docker compose exec deadair-tv python -m app.manage set-password james --admin
  ```

## Roadmap

- **Per-user services/channels**: right now shows, channels and services are
  shared by everyone; only watch history is per person.

## Development

```sh
pip install -r requirements-dev.txt
pytest
CONFIG_PATH=config/config.yaml DB_PATH=data/dev.db TMDB_API_KEY=... uvicorn app.main:app --reload
```

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
Streaming availability data is provided by JustWatch via TMDB. Icons are
from [game-icons.net](https://game-icons.net/), licensed under CC BY 3.0.

In the app, all credits live on the **Credits** page, which is linked from
the footer on every screen, including the login screen. The footer itself
keeps only the JustWatch credit next to the "Credits" link. **Adding an
icon?** Add a line for it to the icon list in the `credits-view` section of
`app/static/index.html`.
