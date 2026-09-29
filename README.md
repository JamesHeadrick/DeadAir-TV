# DeadAir
Solves the "there's nothing to watch" once and for all

(The repo and Docker container are named `DeadAir-TV` / `deadair-tv`; the app itself is just DeadAir.)

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
  This is checked per season too, when TMDB knows where each season streams:
  if Netflix only has seasons 1–5, an episode from season 6 shows your next
  option, or isn't picked if there's nowhere to watch it. All shows lists
  these per season (e.g. *S1–5: Netflix*, *S6–8: not on your services*).
- An **All shows** page lists every show, where you can watch it, and which
  other services carry it.
- Username/password logins. **Admins** manage settings and users.
  **Viewers** can pick episodes and keep their own watched/skipped history.
- One mobile-first dark page.
- Optional: **Play on TV** launches the link on a Chromecast with Google TV over ADB.

## Quick start (Raspberry Pi / any Docker host)

You need Docker and a free [TMDB API key](https://www.themoviedb.org/settings/api)
of your own. The prebuilt image runs on arm64 (Raspberry Pi 4/5) and amd64.

Make a folder with this `docker-compose.yml` in it, fill in your key, and run
`docker compose up -d`:

```yaml
services:
  deadair-tv:
    image: ghcr.io/jamesheadrick/deadair-tv:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config:/config   # your settings (config.yaml)
      - ./data:/data       # database: episodes cache, users, watch history
    environment:
      TMDB_API_KEY: "your-tmdb-api-key"
      # Optional: create the admin account on first start. Without these, the
      # first person to open the page creates it. They're ignored once any user
      # exists, so you can delete them after the first start.
      ADMIN_USER: "admin"
      ADMIN_PASSWORD: "pick-a-good-password"
```

Then open `http://<host>:8000`, log in, and tap **⚙** to pick your services
and add shows. Each new show's episodes are fetched right away, which takes a
few seconds per show. The `config` and `data` folders are created
automatically.

Other optional settings are listed under [Environment](#environment) below.
They can go in the same `environment:` block. The repo's own
`docker-compose.yml` does the same, but also reads an optional `.env` file.

**Updating:** `docker compose pull && docker compose up -d`. Your settings,
users and history live in `config/` and `data/`, outside the container.
`:latest` follows the main branch. To only get releases, set
`DEADAIR_IMAGE=ghcr.io/jamesheadrick/deadair-tv:1` in `.env` (every 1.x
release), or pin an exact one like `:1.0.0`. The running version is shown in
the footer, and admins get a notice there when a newer one is out.

### Testing a pull request

Every pull request from this repo publishes its own image, tagged `pr-<number>`,
once its checks pass. To try one before merging, point `.env` at it:

```sh
echo 'DEADAIR_IMAGE=ghcr.io/jamesheadrick/deadair-tv:pr-3' >> .env
docker compose pull && docker compose up -d
```

New pushes to the PR update the same tag, so re-run the second line to get
them. To go back, remove the `DEADAIR_IMAGE` line and run it again. The
database only ever gains columns, so switching back to `latest` is safe.

To build any branch yourself without cloning (slower on a Pi, around a few
minutes):

```sh
docker build -t deadair-tv:local https://github.com/JamesHeadrick/DeadAir-TV.git#my-branch
DEADAIR_IMAGE=deadair-tv:local docker compose up -d
```

Local builds show version `dev` in the footer unless you pass the build
info, e.g. `--build-arg APP_VERSION=my-branch --build-arg GIT_COMMIT=<sha>`.

### Building from source

```sh
git clone https://github.com/JamesHeadrick/DeadAir-TV.git && cd DeadAir-TV
cp .env.example .env                        # set TMDB_API_KEY
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Use `git checkout <branch>` first to build a branch.

The base image (`python:3.12-slim-bookworm`) is multi-arch, so building on a
Pi gives you an arm64 image with no extra steps.

### Nginx Proxy Manager

Add a Proxy Host that forwards to `http://<pi-ip>:8000`. If NPM is on the same
Docker network, you can use `http://deadair-tv:8000` and drop the `ports:`
mapping. No special settings are needed, and every URL in the app is relative.
Serve it over HTTPS (NPM + Let's Encrypt, or your VPN's TLS) if you can. The
login cookie is marked HTTPS-only automatically when the request comes in over
HTTPS. Over plain `http://` on a LAN it still works, but the cookie is sent
unencrypted.

## Configuration

### All shows and Settings (⚙)

Everything in `config.yaml` can be edited from the web UI. For admins, **All
shows** is where shows are managed:

- **Add a show**: search TMDB by name at the top of the page.
- **Edit**: on each show, change its channel tags (pick an existing channel or
  type a new one), its weight, a display name, and its Open links, or remove
  it. Each show also lists where you can watch it.

Viewers see the same list without the editing controls. Settings has the rest:

- **Your services**: search TMDB's provider list for your region and tap to
  add, so the names always match what TMDB reports. The order they're listed
  in is the order watch options are shown in, except that services with a
  link that opens the show come before ones that can only search for it.
- **Where to watch**: turn the free/with-ads and rent/buy fallbacks on or off.
- **Channels**: every channel in use, with an optional emoji (pick from
  suggestions or type your own), plus **Rename** and **Delete**, which apply
  to every show tagged with that channel. Renaming onto an existing channel
  merges the two.
- **Watched & skipped**: set the cooldown length and clear the history.
- **Search links**: add or override a service's search URL.

Both pages share one **Save** bar, and unsaved edits carry over when you
switch between them. Saving rewrites `config/config.yaml` and keeps the
previous version as `config.yaml.bak`. Comments in the file don't survive a
save from the UI. If the file changed on disk since you started editing, the
save is refused, so you don't overwrite someone else's changes.

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
  have as many as you like. The channel list is alphabetical. An optional
  top-level `channels:` section adds extras per channel, currently just an
  emoji:
  ```yaml
  channels:
    scifi: {emoji: 🚀}
  ```
- **services** are matched against TMDB provider names, ignoring case and
  punctuation. An exact match wins. If you picked "Netflix", its other plans
  (like "Netflix Standard with Ads") aren't shown as extra options. If there's
  no exact match, a looser match is tried: a hand-typed `Disney+` finds
  `Disney Plus`, and `Paramount+` finds `Paramount Plus Essential`. Add-on
  channels sold through another store, such as "HBO Max Amazon Channel", never
  count. Picking services in Settings uses TMDB's exact names, so you don't
  have to worry about any of this.
- **Open links**: TMDB tells you *which* services carry a show, but not where
  the show lives inside each service. **Open** uses the best link it has:
  1. **Your link:** pasted in Settings → the show → **Open links**, or `links:`
     in `config.yaml`. In the service's app, tap **Share → Copy link** on the
     show to get it.
  2. **Found automatically:** the show's page on Netflix, Hulu, HBO Max,
     Disney+, Peacock, Paramount+, Prime Video or Apple TV, looked up on
     [Wikidata](https://www.wikidata.org) by the show's TMDB ID during the
     weekly sync. Coverage is good for well-known shows. Turn it off with
     `WIKIDATA_LINKS=false`.
  3. **Search:** the service's search page for the show (you can add or
     override these with `search_urls:`). Some apps, like Hulu and HBO Max,
     ignore search links and open their home screen.
  4. **TMDB's "Where to watch" page**, for services with no search link.

  Links go to the show's page, not a specific episode: services don't publish
  episode IDs. In Settings, shows that still open a search are marked, so you
  can see which ones are worth pasting a link for.
- `include_free: false` / `include_rent_buy: false` drop those tiers.
- **Picking**: every aired episode (season 0 and unaired episodes excluded)
  has the same chance of being picked, among the shows you can watch. A show
  with 200 episodes therefore comes up 10× as often as one with 20.
- **weight** (default `1`) multiplies the odds of each of that show's episodes.

To force a full TMDB re-sync (e.g. after a show adds a new season), run
`curl -X POST http://<host>:8000/api/refresh`.

### Environment

Set these under `environment:` in your compose file, or in a `.env` file
next to it.

| Var | Default | |
|---|---|---|
| `TMDB_API_KEY` | – | v3 API key or v4 read access token |
| `WATCH_REGION` | `US` | provider region to check |
| `EPISODE_REFRESH_DAYS` | `7` | |
| `PROVIDER_CHECK_HOURS` | `24` | how often to check where shows (and each of their seasons) stream |
| `ENABLE_ADB` | `false` | phase 2, see below |
| `TV_IP` | – | Chromecast IP |
| `ADB_PORT` | `5555` | |
| `ADMIN_USER` / `ADMIN_PASSWORD` | – | create the first admin at startup (only when no users exist) |
| `WIKIDATA_LINKS` | `true` | look up show-page links on Wikidata (see Open links) |
| `UPDATE_CHECK` | `true` | check GitHub daily for a newer version (see Update notice) |
| `DEADAIR_IMAGE` | `ghcr.io/jamesheadrick/deadair-tv:latest` | image to run (read by docker-compose.yml, not the app) |
| `PUID` / `PGID` | `1000` | user/group the app runs as; the `config` and `data` folders are handed to it on start |

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

### Releases

GitHub Actions (`.github/workflows/docker.yml`) runs the tests and then
builds the image for arm64 and amd64:

- **Pull requests:** publishes `:pr-<number>` for branches in this repo
  (see Testing a pull request). Pull requests from forks are built but not
  published.
- **Push to `main`:** publishes `ghcr.io/jamesheadrick/deadair-tv:latest`.
- **Tag `vX.Y.Z`:** publishes `X.Y.Z`, `X.Y` and `X`, then creates a GitHub
  Release with notes generated from the merged pull requests. For example:

  ```sh
  git tag v1.0.0 && git push origin v1.0.0
  ```

### Update notice

The footer shows the running version (e.g. `DeadAir v1.2.0 · a1b2c3d`). Once a
day the server asks GitHub whether there's something newer, and admins see a
link in the footer when there is:

- **Release images** (`v1.2.0`, or tags like `:1`): *Update available: v1.3.0*,
  linked to the release notes.
- **`latest`** (built from main): *3 new commits on main*, linked to the list
  of changes.
- **Pull request and local builds** aren't checked.

It's one anonymous request to GitHub's public API, which for main builds
includes the commit being compared. Set `UPDATE_CHECK=false` to turn it off.

The package is linked to this repository, so it has the same visibility
as the repo: this repo is public, so the image is public and anyone can
pull it without logging in. Package settings live under your GitHub
profile → **Packages** → `deadair-tv`.

## Attribution

This product uses the TMDB API but is not endorsed or certified by TMDB.
Streaming availability data is provided by JustWatch via TMDB. Icons are
from [game-icons.net](https://game-icons.net/), licensed under CC BY 3.0.

In the app, all credits live on the **Credits** page, which is linked from
the footer on every screen, including the login screen. The footer itself
keeps only the JustWatch credit next to the "Credits" link. **Adding an
icon?** Add it as a `<symbol>` in the icon sprite at the top of `<body>` in
`app/static/index.html`, use it with `<svg><use href="#id"/></svg>`, and add a
line for it (with the icon) to the list in the `credits-view` section.

The DeadAir TV icon (`app/static/icon.svg`) was made for this project by Claude
(Anthropic), and is credited on the Credits page too. The
home-screen PNGs are rendered from `tools/icon-full.svg` with
`python tools/make_icons.py`.
