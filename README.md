# Monarch — Media Platform <br />

A premium self-hosted Netflix-style media platform with **AI-powered
recommendations**, **live TV**, **user profiles**, **Authentik SSO**, **media
automation** and **enterprise-grade infrastructure**.

<br />

| | |
|---|---|
| **Streaming** | Jellyfin (movies, TV, music, live TV) with user profiles & watch history |
| **AI** | content-based recommendations, smart search, optional LLM blurbs (`monarch-recs`) |
| **Health** | library + disk analytics, missing/duplicate/orphan detection (`monarch-health`) |
| **Auth** | Authentik SSO — users & passwords live in Authentik; Jellyfin logins resolve via LDAP |
| **Media automation** | Sonarr / Radarr / Lidarr / Prowlarr / qBittorrent / Bazarr, wired automatically |
| **Live TV** | native M3U tuner + XMLTV guide (iptv-org), zero TVHeadend setup |
| **Proxy & SSL** | Nginx Proxy Manager auto-configured via API, wildcard Let's Encrypt cert via DNS challenge |
| **Delivery** | Docker Compose stack, one-command installer, offline bundle, live/install ISO |
| **Subscriptions** | Magnate is the source billing platform (Stripe checkout → Authentik `paid_users` group) |
| **Extras** | watch parties (SyncPlay), offline downloads, request portal (Jellyseerr) |

Objectives of this repo: rebrand the classic *arr stack as **Monarch**,
Authentik-first authentication, everything Dockerized, release artifacts on
every tagged release, first-party images published to GHCR, and automatic
reverse-proxy + wildcard-SSL provisioning for the `monarch.innotel.us`
subdomains. No third-party author attribution is included anywhere in this
project.

Instructions below are for Debian / Ubuntu, but Docker runs natively on any
Linux distro (and you can run the containers on Windows/Mac via
[Docker Desktop](https://docs.docker.com/desktop/)).

Besides the manual setup, this repo ships a **one-command setup script**, an
**installer**, an **offline bundle** and a **bootable live/install ISO**. See
[Installer & Live USB](#installer--live-usb) and
[Building a release](#building-a-release).

***************************

# Quick start (recommended)

```bash
git clone https://github.com/innotelinc/monarch
cd monarch
cp .env.sample .env    # edit MONARCH_USERNAME / MONARCH_PASSWORD etc. (below)
./setup.sh
```

`setup.sh` is idempotent and does five things:

1. Creates `.env` from `.env.sample` when missing (never overwrites).
2. Installs the stack via `scripts/install-monarch.sh` (Docker, `/data`
   layout, `monarch.service` systemd unit, `docker compose up -d`).
   `setup.sh` deploys in place from this checkout; set `MONARCH_TARGET`
   (e.g. `/opt/monarch`) to install elsewhere.
3. Seeds the **Homarr landing board** (`homarr/board.default.json`) so the
   apex `MONARCH_DOMAIN` shows the Monarch main interface linking to every
   subdomain. Boards you have already customized are left untouched.
4. Auto-configures **Nginx Proxy Manager** through its API
   (`scripts/npm-proxy-hosts.py`): creates the proxy hosts for the apex
   (`MONARCH_DOMAIN` itself) plus every subdomain, requests a
   **wildcard Let's Encrypt certificate** via a DNS challenge, and
   bootstraps the NPM admin account from `NPM_ADMIN_EMAIL` /
   `NPM_ADMIN_PASSWORD` when it has not been created yet.
5. Ensures the **Magnate Stripe webhook** endpoint (`subscribe.innotel.us`)
   exists from `STRIPE_SECRET_KEY` (`scripts/stripe-webhooks.sh`) when the
   signing secret is still a placeholder in `.env` — reruns skip it once
   configured.

Re-run `./setup.sh` any time you change `.env` or `scripts/npm-hosts.conf` —
the proxy hosts are reconciled in place and the certificate is reused.

**DNS prerequisite (one-time, outside the script):** users reach the stack
through the apex `https://<MONARCH_DOMAIN>` and its subdomains, so point both
at this host's public IP before/after running setup (a **wildcard** record
covers every `*.domain` subdomain; the **apex** record covers the main
interface). When the BIND/TSIG dynamic DNS options are configured in `.env`
(`DNS_TSIG_*`), the NPM script writes both A records itself instead:

```
*.monarch.innotel.us   A   <this host's public IP>
monarch.innotel.us     A   <this host's public IP>
```

For Cloudflare the API token in `NPM_DNS_CREDENTIALS` needs
**Zone:DNS:Edit** permission on the zone (needed for both the DNS A records
above and the wildcard certificate's DNS challenge). See [Subdomains &
Nginx Proxy Manager](#subdomains--nginx-proxy-manager-automatic) for the
full subdomain map.

**Migration note:** the old *arr stack used `ARR_USERNAME` / `ARR_PASSWORD`.
Monarch uses `MONARCH_USERNAME` / `MONARCH_PASSWORD` — if you have an
existing `.env`, regenerate it from `.env.sample` (or `sed -i 's/^ARR_/MONARCH_/' .env`).

***************************

# Shared initial credentials

Edit `.env` (it is gitignored, so your credentials never get committed) — the
two most important variables are:

```
MONARCH_USERNAME=admin        # your username
MONARCH_PASSWORD=monarch8     # your password
```

Those same credentials are applied automatically to **every service that
requires a login**: Jellyfin, Jellyseerr, Sonarr, Radarr, Lidarr, Whisparr,
Prowlarr, Bazarr, qBittorrent, Transmission, the Authentik bootstrap admin
(email `admin@innotel.us`) and the subscription platform's `/admin` panel.
Anything you change in `.env` later is picked up by the automation on the
next `docker compose up -d` (the init containers re-run and only touch
services that still have default/no credentials).

***************************

# Everything is wired for you on first boot

Two one-shot containers do the wiring so you do **not** have to click through
the manual setup:

| Container    | When            | What it does |
|--------------|-----------------|--------------|
| `monarch-seed` | before qBittorrent starts | writes qBittorrent's WebUI login (`MONARCH_USERNAME`/`MONARCH_PASSWORD`) into its config - no temporary-password dance |
| `monarch-init` | after the stack is up | wires the whole stack (below) |

`monarch-init` automatically:

* **Jellyfin** - completes the first-run wizard (creates the admin user with
  your credentials), adds Media libraries (`/data/media/movies`, `tv`,
  `music`, `xxx`), logs in and exports the admin token to
  `/docker/appdata/init/jellyfin-api-key.txt`
* **Jellyfin LDAP-Auth plugin** - installs the plugin and writes its config so
  Jellyfin logins authenticate against the Authentik LDAP outpost (see the
  dedicated section below)
* **Jellyfin Live TV** - adds the iptv-org M3U playlist as a native tuner and
  the iptv EPG container's guide as the XMLTV provider, and writes the EPG
  channel list to `/opt/epg/channels.xml` (see the Live TV section below)
* **Sonarr / Radarr / Lidarr / Whisparr** - sets Forms authentication with
  your credentials, adds the correct root folder, adds the **qBittorrent**
  download client (category `tv` / `movies` / `music` / `xxx`) and enables
  hardlinks + extra-file import
* **Prowlarr** - sets Forms authentication, adds **qBittorrent** as the
  download client, registers Radarr, Sonarr, Lidarr and Whisparr as
  **Apps** (full sync) - so indexers added in Prowlarr flow to all *arr apps -
  and adds a **FlareSolverr proxy** (tag an indexer `cloudflare` to route it
  through the proxy)
* **qBittorrent** - verifies the WebUI login and creates the `movies`, `tv`,
  `music` and `xxx` categories with their save paths under `/data/torrents`
* **Bazarr** - sets basic authentication with your credentials and connects
  Sonarr + Radarr so subtitle syncing works
* **Jellyseerr** - initializes the request manager against **Jellyfin**,
  connects Radarr + Sonarr, and enables **Jellyfin sign-in** (Settings >
  Users) so subscribers can log in with their Jellyfin accounts

Watch it work / check for problems:

```
sudo docker logs monarch-init
sudo cat /docker/appdata/init/status.json      # per-service result + any issues list
```

Everything is idempotent - `monarch-init` re-runs safely on every `up -d` and
only touches services that are still unconfigured.

***************************

# Services & ports

| Service    | URL                   | Notes |
|------------|-----------------------|-------|
| Homarr (dashboard) | http://localhost:7575 | `app.monarch.innotel.us` |
| Jellyfin   | http://localhost:8096 | `media.monarch.innotel.us`; admin = your `.env` credentials |
| Jellyseerr | http://localhost:5055 | `req.monarch.innotel.us`; requests; connected to Jellyfin + Radarr/Sonarr |
| Prowlarr   | http://localhost:9696 | indexers; all *arr apps pre-registered |
| Radarr     | http://localhost:7878 | movies |
| Sonarr     | http://localhost:8989 | tv |
| Lidarr     | http://localhost:8686 | music |
| Whisparr   | http://localhost:6969 | xxx |
| Bazarr     | http://localhost:6767 | subtitles; connected to Sonarr/Radarr |
| qBittorrent | http://localhost:8080 | WebUI (enabled), login = your credentials, torrent port 6881 |
| SABnzbd    | http://localhost:8081 | Usenet (optional) |
| Transmission | http://localhost:9091 | optional extra downloader |
| Deluge     | http://localhost:8112 | optional; default WebUI password is `deluge` on first login |
| autobrr    | http://localhost:7474 | optional; manual setup |
| Authentik  | http://localhost:9000 | `auth.monarch.innotel.us`; SSO + `paid_users` group = user management |
| Authentik LDAP | localhost:389 / 636 | LDAP outpost - Jellyfin logins authenticate against it |
| **Monarch AI** | http://localhost:8002 | `monarch-recs` - AI recommendations + smart search (internal API) |
| **Monarch Health** | http://localhost:8003 | `monarch-health` - media health analytics (internal API) |
| Nginx Proxy Manager | http://localhost:81 | `admin.monarch.innotel.us`; reverse proxy + wildcard SSL |
| Clipbucket | http://localhost:8088 | video platform |
| IPTV guide | http://localhost:3001 | `tv.monarch.innotel.us`; XMLTV guide (`/guide.xml`) for Jellyfin Live TV |
| TVHeadend / NextPVR / Dispatcharr | 9981 / 8866 / 9191 | optional legacy live-TV backends (Jellyfin Live TV uses a native M3U tuner, so these are not required) |

***************************

# Subdomains & Nginx Proxy Manager (automatic)

`scripts/npm-proxy-hosts.py` (invoked by `setup.sh`) configures Nginx Proxy
Manager entirely through its API. Two modes (`.env`):

- `NPM_MODE=local` (default) - the stack runs its own `nginx-proxy-manager`
  container (compose profile `npm`, admin UI on **:81**) and setup drives it
  at `http://localhost:81`.
- `NPM_MODE=remote` - reuse an existing NPM server: the NPM container is
  **not** started, and setup drives the remote server's API
  (`NPM_BASE_URL`, e.g. `https://proxy.innotel.us`). The remote server
  forwards to this host, so set `NPM_FORWARD_HOST` to the address it can
  reach this host at - a LAN IP (e.g. `192.168.1.46`), public IP, or
  hostname (instead of `container`). The ports in `npm-hosts.conf` are
  this host's published ports, so they work in both modes.

Either way the script creates (or reconciles) one proxy host per entry. `@`
is the **apex** — the base domain itself, which is the **main interface**
users log into (the Homarr dashboard). Everything else is a subdomain:

| Subdomain | Service | Port | WebSockets |
|-----------|---------|------|------------|
| `monarch.innotel.us` (apex, `@`) | Homarr dashboard — main login | 7575 | yes |
| `app.monarch.innotel.us` | Homarr dashboard | 7575 | yes |
| `auth.monarch.innotel.us` | Authentik (SSO + user portal) | 9000 | - |
| `media.monarch.innotel.us` | Jellyfin | 8096 | yes |
| `tv.monarch.innotel.us` | IPTV/EPG guide | 3001 | - |
| `admin.monarch.innotel.us` | Nginx Proxy Manager admin | 81 | - |
| `subscribe.monarch.innotel.us` | landing page + Stripe checkout | 3000 | - |
| `req.monarch.innotel.us` | Jellyseerr request portal | 5055 | yes |

(The old `subscribe.monarch.innotel.us` / `api.monarch.innotel.us` billing
hosts were removed — **Magnate** at `subscribe.innotel.us` is the source
billing platform for all projects.)

The mapping lives in `scripts/npm-hosts.conf` — add/remove lines freely; the
script reconciles the proxy hosts on every run (idempotent). For a local NPM
you can also forward to host-published ports with
`NPM_FORWARD_HOST=host.docker.internal`.

### Wildcard SSL (automatic)

The script requests one **Let's Encrypt wildcard certificate** for
`*.monarch.innotel.us` (+ the apex) using a **DNS challenge**, then attaches
it to every proxy host and forces HTTPS. Configuration in `.env`:

```
MONARCH_DOMAIN=monarch.innotel.us
SSL_EMAIL=admin@innotel.us
NPM_ADMIN_EMAIL=admin@innotel.us
NPM_ADMIN_PASSWORD=change-me          # set once in the NPM UI on first login
NPM_DNS_PROVIDER=cloudflare
NPM_DNS_CREDENTIALS={"auth_token":"your-cloudflare-api-token"}
```

One-time DNS prerequisite (outside the script): a wildcard A record plus the
apex A record (when BIND/TSIG dynamic DNS is configured — `DNS_TSIG_*` — the
script writes both itself):

```
*.monarch.innotel.us   A   <this host's public IP>
monarch.innotel.us     A   <this host's public IP>
```

For Cloudflare the API token needs **Zone:DNS:Edit** permission on the zone.
Other DNS providers are supported via `NPM_DNS_PROVIDER` (route53, godaddy,
vultr, ovh, hetzner, ...) — credentials always go in `NPM_DNS_CREDENTIALS`
as JSON (or set `CLOUDFLARE_API_TOKEN` for the Cloudflare convenience path).

First-time NPM admin (local mode) needs no manual step: current NPM images
boot **without** a default account, and the script bootstraps the admin from
`NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` automatically. If your NPM still has
the legacy `admin@example.com` / `changeme` default, the configured admin is
created alongside it. In remote mode make sure `NPM_ADMIN_EMAIL` /
`NPM_ADMIN_PASSWORD` are the existing server's real credentials. The script
prints clear guidance if the API login fails.

> The proxy hosts are created with `client_max_body_size 0;`, exploit
> blocking and HTTPS-forcing enabled; `monarch-recs` and `monarch-health`
> stay internal (not exposed by default).

***************************

# Authentication & user management (Authentik)

**User management is Authentik-first.** Authentik boots with a bootstrap
admin (no setup wizard to click through):

| What | Value |
|------|-------|
| Admin UI | `https://auth.monarch.innotel.us` (or `http://localhost:9000`) |
| Username | `akadmin` |
| Password | your `MONARCH_PASSWORD` from `.env` |
| Email | `admin@innotel.us` |

Bootstrap credentials are applied **only on first boot**. Changing
`MONARCH_PASSWORD` later does **not** reset the admin password — change it in
the admin UI instead (Directory -> Users -> `akadmin`).

**The `paid_users` group is the source of truth for who has access.**
Payments made through **Magnate** (the source billing platform, at
`subscribe.innotel.us`) are mirrored into `paid_users`, and subscribers are
given access in Authentik — there is no separate Jellyfin user store for
subscribers.

### Authentik LDAP -> Jellyfin (login gate)

Jellyfin logins authenticate against Authentik directly through the bundled
**LDAP outpost** and the Jellyfin **LDAP-Auth plugin**. Disabling a user in
Authentik blocks their Jellyfin login (the LDAP bind fails).

| Piece | Who sets it up | What it is |
|-------|----------------|------------|
| `authentik-ldap` container | `docker-compose.yml` | LDAP outpost, plain LDAP on 3389 / LDAPS on 6636 inside the network |
| LDAP provider + outpost + app | Magnate provisioning | base DN `dc=innotel,dc=us`; provider/outpost named `jellyfin-ldap` |
| Bind service account | Magnate provisioning | `authentik-ldap` service account + pinned token |
| Groups | Magnate provisioning | `paid_users` (subscribers) and `jellyfin_admins` (Jellyfin admins - auto-created) |
| Jellyfin LDAP-Auth plugin | `monarch-init` | installs the plugin and writes its config, then restarts Jellyfin |

The full access chain: **Magnate checkout -> subscriber added to `paid_users`
-> LDAP bind succeeds -> Jellyfin login works.** Subscription cancels -> user
set inactive -> LDAP bind fails -> Jellyfin login blocked.

**User profiles & watch history are native Jellyfin features.** Each Authentik
account maps to a Jellyfin profile (created automatically on first LDAP
login), with its own watch history, resume state, ratings and per-profile
Continue Watching rows. Profiles are managed in Authentik
(Directory -> Users); admins are granted via the `jellyfin_admins` group.

Test the outpost from the host:

```
ldapsearch -x -H ldap://localhost:389 -b dc=innotel,dc=us -D "cn=authentik-ldap,ou=users,dc=innotel,dc=us" -w ak-ldap-bind-2026 '(memberOf=cn=paid_users,ou=groups,dc=innotel,dc=us)' cn
```

### Subscription platform + billing

**Magnate** (`subscribe.innotel.us`) is the source billing platform for all
projects: visitors pick a plan and pay through Stripe Checkout, and Magnate
provisions the subscriber into Authentik (`paid_users`). Monarch's own
subscription/billing containers were removed in favor of it. See
`.env.sample` for `APP_URL`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`JELLYFIN_URL` / `JELLYFIN_API_KEY` and `ACCOUNT_PORTAL_URL` / `REQUEST_URL`.

Getting `JELLYFIN_API_KEY`: `monarch-init` exports the Jellyfin admin token on
first boot to `/docker/appdata/init/jellyfin-api-key.txt` — copy it into
`.env`. `monarch-recs` and `monarch-health` read the same file automatically.

***************************

# AI recommendations & smart search (monarch-recs)

`monarch-recs` (port 8002, internal API) is a content-based recommendation
engine over your Jellyfin library — **fully local, no external AI required**:

| Endpoint | What it does |
|----------|--------------|
| `GET /api/recommendations?user_id=<id>` | personalized picks from that profile's watch history (falls back to trending) |
| `GET /api/recommendations?item_id=<id>` | "more like this" for one title |
| `GET /api/trending` | most-played titles across all profiles |
| `GET /api/search?q=inception&genre=action&year=2010` | ranked smart search over name/genre/year/cast/overview with filters |
| `GET /api/describe?item_id=<id>` | LLM-generated blurb (when configured) |

How it works: each item is tokenized (name weighted, plus genres, tags,
cast, overview) into a sparse TF-IDF vector; recommendations are cosine
similarity in pure Python (no numpy/sklearn dependency). The index refreshes
every `REFRESH_INTERVAL` seconds (default 900) so new additions are picked
up. Auth: the admin token exported by `monarch-init`.

**Optional "AI" blurbs:** set `OPENAI_API_KEY` (and optionally
`OPENAI_BASE_URL` / `OPENAI_MODEL`) in `.env` to enable `/api/describe` with
any OpenAI-compatible endpoint — OpenAI, Ollama, vLLM, llama.cpp, etc.

```
curl "http://localhost:8002/api/recommendations?user_id=<uid>&limit=5"
curl "http://localhost:8002/api/search?q=night of the living dead"
```

***************************

# Media health analytics (monarch-health)

`monarch-health` (port 8003, internal API) periodically scans the Jellyfin
library + the `/data/media` volume and publishes a JSON report:

| Endpoint | What it does |
|----------|--------------|
| `GET /api/analytics` | latest report from the periodic scan |
| `POST /api/analytics/scan` | run a scan on demand |
| `GET /api/services` | reachability of Jellyfin, Authentik, recs, health |
| `GET /health` | service status + last scan time |

The report includes **per-library stats** (item counts by type),
**missing files** (items whose file vanished from disk), **duplicates**
(same filename within a library), **orphan files** (media on disk not
registered in Jellyfin), **disk usage** for the media volume, **recently
added** (30 days) and **most-played** titles. It is written to
`/docker/appdata/monarch-health/analytics.json` after every scan and served
through the API so dashboards can read a consistent snapshot.

```
curl http://localhost:8003/api/analytics | python3 -m json.tool
```

***************************

# Live-stack drift check (monarch-drift-check)

`scripts/drift-check.sh` probes the running stack and verifies it still
matches what `monarch-init` is supposed to maintain. It never writes
anything — it only reads API keys from `/docker/appdata` and issues checks
against the services:

| Checked service | Invariants verified |
|-----------------|---------------------|
| Sonarr / Radarr / Lidarr / Whisparr | API reachable, forms auth configured, expected media root folder, qBittorrent download client |
| Prowlarr | qBittorrent download client, Sonarr/Radarr/Lidarr/Whisparr apps registered |
| qBittorrent | WebUI login with the shared credentials, `movies`/`tv`/`music`/`xxx` categories |
| Jellyfin | admin login, media libraries (Movies / TV Shows / Music / Other) |
| Jellyseerr | initialized, Jellyfin sign-in enabled |
| Bazarr | API key readable, basic auth configured |
| Authentik (optional) | LDAP outpost provisioned (only when `AUTHENTIK_BASE_URL` is set) |

**Single source of truth:** what to check comes from
`/docker/appdata/init/invariants.json`, which `monarch-init` emits from the
same constants it configures with (`init/init.py` → `build_invariants()`).
The check therefore can never diverge from what init actually sets up — if
an app, root folder, category or library is added there, it is checked here
automatically. The fresh-install CI check builds that manifest from `init.py`
and validates it against `drift-check --check-manifest` on every PR, so the
lockstep is enforced before merge.

Each failure is printed as a `DRIFT-FAIL:` line and the script **exits
non-zero**, so it can be run from cron or a systemd timer to alert on drift:

```
# check once, human-readable
./scripts/drift-check.sh

# quiet (only DRIFT-FAIL lines on stderr) - for cron/timers
./scripts/drift-check.sh --quiet

# when drift is found, re-run monarch-init automatically, then re-verify
./scripts/drift-check.sh --quiet --heal
```

`install-monarch.sh` also installs a **systemd timer** that runs the check
quietly every 6 hours (with a randomized delay; `Persistent=true` catches up
after downtime), **auto-healing**: the service runs with `--heal`, so a
drifted stack repairs itself by re-running `monarch-init` and only alerts
(Telegram) if the re-verify still finds problems:

```
sudo systemctl status monarch-drift-check.timer
journalctl -u monarch-drift-check.service      # last run + any DRIFT-FAIL lines
```

### Telegram alerts (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` (create the bot
with @BotFather, get your chat id with @userinfobot) and the timer sends a
Telegram message listing every `DRIFT-FAIL` line when drift is found:

```
./scripts/drift-check.sh --test-telegram      # send a test message
```

Drift happens when a container is recreated without the seed (e.g. an app
reset its own config, or a volume was restored from a stale backup). Re-run
`monarch-init` to repair it (the timer's `--heal` mode does this
automatically):

```
sudo docker start monarch-init        # re-runs the one-shot init container
```

***************************

# Watch parties & offline downloads

Both are **native Jellyfin features**, enabled out of the box:

* **Watch parties (SyncPlay)** — any profile can start a SyncPlay session
  from the Jellyfin app (tap the SyncPlay icon and invite others); playback,
  pause and seek stay in sync across participants. SyncPlay is enabled by
  default; administer it in Jellyfin Dashboard -> Playback.
* **Offline downloads** — the Jellyfin mobile/desktop apps download titles to
  the device for offline viewing. Downloads are authenticated against
  Authentik (via the LDAP login chain), so revoked users lose access.

***************************

# Live TV (Jellyfin)

Live TV is wired automatically on first boot — **no TVHeadend or NextPVR
setup needed**. Jellyfin ingests a playlist directly as a native **M3U
tuner** and uses the XMLTV guide generated by the `iptv` container:

| Piece | Who sets it up | What it is |
|-------|----------------|------------|
| Channel source | `monarch-init` (Jellyfin API) | M3U tuner pointing at `LIVETV_M3U_URL` (default: the free iptv-org **US** playlist, ~700 channels) |
| Guide (EPG) | `monarch-init` + `iptv` container | channel list at `/opt/epg/channels.xml`; guide grabbed twice a day and served at `http://iptv:3000/guide.xml` |

**First boot:** `monarch-init` adds the tuner and guide provider
(idempotent). To get listings immediately:

```
sudo docker restart iptv        # triggers an EPG grab right away
```

**Custom providers:** set `LIVETV_M3U_URL` (and optionally
`LIVETV_GUIDE_URL`) in `.env` to your IPTV provider's playlist. Free
iptv-org streams are community-sourced — most play, but some channels may be
offline or geo-blocked.

***************************

# Restart services

```
sudo docker compose down
sudo docker compose up -d
```

***************************

# What's still manual (one-time, mostly external)

1. **Add indexers to Prowlarr** (`http://<host>:9696` -> Settings ->
   Indexers) — they flow automatically to Radarr/Sonarr/Lidarr/Whisparr.
   Legal/public-domain sources like **Archive.org** work great
   (see [Remaining config](#remaining-config)). Tag an indexer `cloudflare`
   to route it through the **FlareSolverr proxy** that `monarch-init`
   already registered.
2. **Stripe** — put your secret key (`STRIPE_SECRET_KEY`) in `.env`, then
   run `./scripts/stripe-webhooks.sh` once. It ensures the single Magnate
   webhook endpoint (`subscribe.innotel.us`, five events) exists and writes
   its signing secret into `.env` for you; `./setup.sh` does this
   automatically on first configure. The endpoint must be publicly reachable.

***************************

# Installer & Live USB

The repo ships a single **installer** that runs both as a live-USB→disk
installer and as an in-place installer on an already-running Linux box, plus
a **live/install ISO** you can boot and run the installer from.

## Installer

`scripts/install-monarch.sh` installs the whole platform. Two modes:

- **In-place** (already-installed Linux): installs Docker (if missing),
  creates the `/data` folder layout, creates `.env` from `.env.sample` when
  missing, loads any offline Docker image archives it finds, installs the
  `monarch.service` systemd unit, and starts the stack via
  `docker compose up -d`. Prefer `./setup.sh` which also configures Nginx
  Proxy Manager.
- **Live USB → disk** (booted from the Monarch ISO): partitions the selected
  disk, copies the live system, installs GRUB (BIOS + UEFI), creates a
  `monarch` login user, enables DHCP networking, and runs the in-place
  install inside the new system.

From a checked-out repo on any Debian/Ubuntu box:

```
./scripts/install-monarch.sh
```

By default it installs to `/opt/monarch` and creates a login user `monarch`
(password `monarch`) for disk installs. Override with `MONARCH_TARGET`,
`MONARCH_USER`, `MONARCH_PASSWORD`, `MONARCH_DISK`, `MONARCH_YES=1` (skip
the destructive-disk confirmation). The installer is idempotent and
non-destructive in in-place mode (it excludes `.env` from the payload copy,
and never overwrites an existing `.env`).

## Fresh-install check

`scripts/fresh-install-check.sh` smoke-checks the fresh-install path — the
pieces `./setup.sh` runs on a brand-new box — without touching a real
deployment:

1. Renders a throwaway `.env` from `.env.sample` (random credentials, never
your real `.env`).
2. Renders the Homarr landing board (`homarr/board.default.json`) and asserts
it is valid JSON with no leftover `__DOMAIN__` placeholders, that every tile
links to the apex or a subdomain declared in `npm-hosts.conf`, and that the
main-interface (apex) tile is present.
3. Dry-runs `scripts/npm-proxy-hosts.py` and asserts the apex host
(`MONARCH_DOMAIN` → Homarr) is planned alongside the subdomains.
4. Validates `docker-compose.yml` interpolation with the throwaway `.env`
(uses the docker CLI; no daemon needed).

```
./scripts/fresh-install-check.sh          # safe anywhere - no Docker needed
```

Add `--full` to also boot the real `homarr` container on a **disposable
host/VM** (Docker + sudo required), seed its board exactly like `setup.sh`
does, and verify it serves the board at `http://127.0.0.1:7575`:

```
./scripts/fresh-install-check.sh --full
```

The `--full` stage refuses to run while a live Monarch stack or existing
`/docker/appdata/homarr` state is detected, and shuts the test container down
when it finishes (keep it running with `MONARCH_CHECK_KEEP=1`). Override the
test domain with `MONARCH_CHECK_DOMAIN` (default `monarch-check.test`).

## Live / install USB

Build the ISO locally (Ubuntu 24.04 "noble" host recommended):

```
sudo apt install -y live-build xorriso mtools genisoimage grub-efi-amd64-bin grub-pc-bin isolinux syslinux-common
./scripts/build-live-usb.sh
```

Output: `dist/live-usb/monarch-live-amd64.iso` plus its `.sha256`. Write it
to a USB stick (replace `/dev/sdX` with the whole device):

```
sudo dd if=dist/live-usb/monarch-live-amd64.iso of=/dev/sdX bs=4M status=progress
```

Boot the target computer from the USB, wait for the Xfce desktop, and launch
**Install Monarch**. For a fully **offline** install, also copy the offline
bundle (`monarch-deployment.tar.gz`, `docker-images-part*.tar.gz`,
`SHA256SUMS`) to a FAT32 partition of the USB stick. The ISO is BIOS + UEFI
hybrid (Secure Boot must be disabled); the installed system boots straight
into the stack via `monarch.service`.

## Offline bundle

```
./scripts/build-offline-bundle.sh            # payload + image bundle + checksums
./scripts/build-offline-bundle.sh --deployment-only   # payload + checksums only
```

Output: `dist/offline-bundle/` — the docker image archives are split into
<2 GB parts for GitHub release uploads. To fetch a published release's
bundle:

```
./scripts/fetch-offline-bundle.sh          # -> ~/monarch-offline-bundle
```

***************************

# Building a release

Releases are cut by `.github/workflows/release.yml`. The workflow:

1. **Tagged release** (`git tag v1.2.0 && git push --tags`) — or manual
   "Run workflow" (choose a `minor`/`major` bump, or `lightweight` to skip
   the heavy builds for a quick test).
2. **build-images** publishes the first-party images to GHCR
   (`monarch-recs`, `monarch-health` — tagged with the release version
   **and** `latest`).
3. **release** cuts the GitHub release with the deployment payload + source
   bundle + checksums.
4. Two parallel CI jobs build & upload the **docker image bundle** and the
   **live ISO**.

Each release publishes:

- `monarch-live-amd64.iso` + `.sha256` (bootable live/install ISO)
- `docker-images-part*.tar.gz` (offline docker image bundle)
- `monarch-deployment.tar.gz` (source + compose + systemd installer payload)
- `monarch-source-bundle.tar.gz` + checksum
- `SHA256SUMS`
- GHCR images `ghcr.io/innotelinc/monarch-{recs,health}`

***************************

# Manual setup (without the installer)

```bash
# Docker
apt update && apt -y upgrade
apt -y install ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt update
apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# Folder structure (TRASH guide)
mkdir -p /data/{usenet/{incomplete,complete}/{tv,movies,music,xxx},media/{tv,movies,music,xxx},torrents/{tv,movies,music,xxx}}
chown -R 1000:1000 /data && chmod -R a=,a+rX,u+w,g+w /data

cd /opt
git clone https://github.com/innotelinc/monarch
cd monarch
cp .env.sample .env        # then edit MONARCH_USERNAME / MONARCH_PASSWORD etc.
sudo docker compose up -d
```

***************************

# Remaining config

Add some indexers to Prowlarr. These tools are powerful automation for
managing media, and there is a wealth of legal, copyright-free, and
open-source content you can use them for — e.g. in Radarr you can download
movies in the Public Domain or released under Creative Commons (Night of the
Living Dead (1968), His Girl Friday (1940), Charade (1963), The General
(1926), ...). The "Gold Standard" legal indexer is **Archive.org**, which
hosts thousands of public domain movies.

***************************

# Troubleshooting

### monarch-init / monarch-seed
`sudo docker logs monarch-init` shows what the automation did. Its per-service
result and any "MANUAL ACTIONS NEEDED" list is in
`/docker/appdata/init/status.json`. If a service was mid-startup during the
run, just re-run: `sudo docker start monarch-init`
(or `sudo docker compose up -d` — it is idempotent).

### qBittorrent WebUI login fails with the configured password
Grab the temporary password from `sudo docker logs qbittorrent` (search for
"A temporary password is provided for this session"), log in at
http://localhost:8080, set your password in **Tools > Options > Web UI**, then
re-run `sudo docker start monarch-init` to recreate the categories.

### DNS check
`sudo docker exec -it radarr cat /etc/resolv.conf` — the stack pins
Cloudflare DNS (1.1.1.1 / 1.0.0.1).

### Hardlinks check
Find the same file in `/data/torrents` and `/data/media` and compare inodes:
`ls -i /data/media/movies/<your video>` vs
`ls -i /data/torrents/movies/<your video>`. If they differ, check the
read/write permissions on source/destination (see Radarr/Sonarr logs).

### Files do not move from torrents to media folder
Check Activity -> Queue for "Downloaded - Unable to Import Automatically",
click Manual Import, confirm the correct movie, and import.

### FlareSolverr
The `flaresolverr` container is already in the stack and `monarch-init`
registers a **FlareSolverr proxy** in Prowlarr automatically (tagged
`cloudflare`). To use it, tag an indexer `cloudflare` in Prowlarr (Settings
> Indexers > edit indexer > Tags) — indexers without the tag are never
routed through the proxy. To change its settings manually: Prowlarr >
Settings > Indexers > Indexer Proxies > edit **FlareSolverr**.

### Jellyfin hardware acceleration
Add to the `jellyfin` service:

```yaml
    devices:
      - /dev/dri:/dev/dri
```

### SABnzbd Usenet client
The `sabnzbd` service is already in the stack on host port 8081 (so it does
not clash with qBittorrent on 8080). Use the TRASH-guide folder structure and
categories, and re-run `monarch-init` for downloader wiring.