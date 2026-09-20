# Monarch — Operations Reference

## Shared initial credentials

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


## Everything is wired for you on first boot

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
  download client (category `tv` / `movies` / `music` / `xxx`), enables
  hardlinks + extra-file import, and writes the shared `AllowedHosts` list
  (`init/arr-allowlist.txt`) so Prowlarr can reach it over the compose network
* **Prowlarr** - sets Forms authentication, adds **qBittorrent** as the
  download client, registers Radarr, Sonarr, Lidarr and Whisparr as
  **Apps** (full sync) - so indexers added in Prowlarr flow to all *arr apps -
  writes the same `AllowedHosts` list for itself (the *arrs call it back at
  `http://prowlarr:9696`), and adds a **FlareSolverr proxy** (tag an indexer
  `cloudflare` to route it through the proxy). **Adding the indexers themselves
  is a separate, explicit step** - `scripts/prowlarr-indexers.py`, below - and
  it is the only part of this wiring that talks to public trackers.
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


## Services & ports

| Service    | URL                   | Notes |
|------------|-----------------------|-------|
| Homarr (dashboard) | http://localhost:7575 | `app.monarch.innotel.us` |
| Jellyfin   | http://localhost:8097 | `https://media.magnate.innotel.us`; Authentik LDAP users; admin = your `.env` credentials |
| Jellyseerr | http://localhost:5055 | `req.monarch.innotel.us`; requests; connected to Jellyfin + Radarr/Sonarr |
| Prowlarr   | http://localhost:9696 | indexers; all *arr apps pre-registered |
| Radarr     | http://localhost:7878 | movies |
| Sonarr     | http://localhost:8989 | tv |
| Lidarr     | http://localhost:8686 | music |
| Whisparr   | http://localhost:6969 | xxx |
| Bazarr     | http://localhost:6767 | subtitles; connected to Sonarr/Radarr |
| qBittorrent | http://localhost:8080 | WebUI (enabled), login = your credentials, torrent port 6881 |
| SABnzbd    | http://localhost:8082 | Usenet (optional); host port = `SABNZBD_PORT` (compose default 8082) |
| Transmission | http://localhost:9091 | optional extra downloader |
| Deluge     | http://localhost:8112 | optional; default WebUI password is `deluge` on first login |
| autobrr    | http://localhost:7474 | optional; manual setup |
| Authentik  | http://localhost:9000 | `auth.monarch.innotel.us`; SSO + `paid_users` group = user management |
| Authentik LDAP | localhost:389 / 636 | LDAP outpost - Jellyfin logins authenticate against it |
| **Monarch AI** | http://localhost:8002 | `monarch-recs` - AI recommendations + smart search (internal API) |
| **Monarch Health** | http://localhost:8003 | `monarch-health` - media health analytics (internal API) |
| Nginx Proxy Manager | http://localhost:2081 | `admin.monarch.innotel.us`; reverse proxy + wildcard SSL (container `:81`) |
| Clipbucket | http://localhost:8098 | `https://tube.innotel.us`; migrated from `.72`, database and files imported |
| IPTV guide | http://localhost:3011 | `tv.monarch.innotel.us`; XMLTV guide (`/guide.xml`) for Jellyfin Live TV (host 3011 → container 3000) |
| TVHeadend / NextPVR / Dispatcharr | 9981 / 8866 / 9191 | optional legacy live-TV backends (Jellyfin Live TV uses a native M3U tuner, so these are not required) |


## Completed media migration from the legacy `.72` server

The Jellyfin media and user migration to the Monarch instance on `.46`
(`192.168.1.46`) is complete. The active service is published on host port
`8097` (container port `8096`) because `.46:8096` belongs to the dashboard.
The public endpoint is:

```
https://media.magnate.innotel.us
```

The new instance contains four canonical libraries and the migrated
Authentik-backed profiles. During the broader migration, the legacy
`media.innotel.us` endpoint is intentionally active again and routes to the
original Jellyfin on `.72:8096`; it must remain available until the remaining
`.72` services have been migrated. The new `.46` endpoint remains available
at `media.magnate.innotel.us`:

| Library | Path | Current items |
|---------|------|---------------|
| Movies | `/data/media/movies` | 4 |
| TV Shows | `/data/media/tv` | 4 |
| Music | `/data/media/music` | 94 |
| Other | `/data/media/xxx` | 1 |

The migrated profiles are `cola` and `dhunter`, plus the local `admin`
account. `cola` is a regular paid user; `dhunter` is also in
`jellyfin_admins`. Jellyfin authentication flows through the Authentik LDAP
outpost and the LDAP-Auth plugin. The source `.72` media files were copied
before cutover; the real source Jellyfin database was preserved while the
obsolete zero-byte `library.db` artifact was removed.

The legacy `.72` machine still hosts unrelated services (including
databases and TV/download services), as well as the legacy Jellyfin
currently published at `media.innotel.us`. The billing-api and Clipbucket
on .72 are retired (Magnate handles billing; Clipbucket is migrated to
.46). Do not remove that compose project or shared `/docker/appdata` until
each remaining service has been separately migrated or decommissioned.

### Legacy `.72` migration boundary

The old host is still an active `arr` Compose project. The following services
are duplicated on `.46` and should not be cut over or removed until their
individual configs and queues are compared:

- Jellyfin, Seerr, Sonarr, Radarr, Lidarr, Whisparr, Prowlarr, qBittorrent,
  Bazarr, and IPTV.

The following remain unique or have not yet been verified on `.46`:

- Dispatcharr, TVHeadend, NextPVR, jfa-go, Requestarr, Transmission, Deluge,
  SABnzbd, and autobrr.

Clipbucket has now been staged on `.46` at host port `8098` and its public
route has been cut over to `https://tube.innotel.us`. The imported snapshot
contains 80 database tables, 2 users, 0 video rows, and the complete
application/upload tree. The `.72` Clipbucket container remains running on
`:8088` as a rollback copy; its application files and database were not
deleted. The NPM database was backed up on `.71` before edge repair, and stale
generated NPM files were retained under
`/usr/src/proxy/backups/generated-pre-clipbucket-20260904164122`.

The legacy billing API on `.72` is **RETIRED** — do not migrate it. Magnate
subscribe.innotel.us is the single billing platform for the entire Innotel
ecosystem. All Stripe keys, webhook endpoints, and subscription logic live in
Magnate. The old `api.monarch.innotel.us` route has been removed. Do not
copy or reuse its live Stripe credentials in a new deployment.

| Legacy route | Current target | Status / migration action |
|--------------|----------------|---------------------------|
| `media.innotel.us` | `.72:8096` | Intentionally retained during migration |
| `media.magnate.innotel.us` | `.46:8097` | New migrated Jellyfin; verified |
| `dl.innotel.us`, `movies.innotel.us`, `mp3.innotel.us`, `xxx.innotel.us` | `.72` downloader / *arr ports | Active legacy routes; retain until queue/config migration |
| `tube.innotel.us` | `.46:8098` | Migrated Clipbucket; verified over public HTTPS. Rollback source remains on `.72:8088` |
| `index.innotel.us`, `req.innotel.us`, `tv.innotel.us`, `brr.innotel.us`, `accounts.innotel.us`, `portainer.innotel.us` | `.72` services | Active legacy routes; verify each during its service migration |
| `api.monarch.innotel.us` | retired | Billing-api decommissioned — Magnate handles all billing |
| `subscribe.monarch.innotel.us` | repurposed | Now the shared subscribe portal (public page on :3040); billing still via Magnate |

The migration rule is: copy/configure first, verify the replacement and its
public DNS/TLS/dependencies, then switch the route, and only later remove the
source service after an explicit retention decision.

### Remaining .72 service migration checklist

Enable one service at a time with `MONARCH_LEGACY=1` in `.env` (or
`docker compose --profile legacy up -d <service>`). Verify on .46, then
cut the DNS route and remove from .72.

| Service | .72 port | Verify on .46 | DNS cut | .72 removed |
|---------|----------|---------------|--------|-------------|
| Dispatcharr | 9191 | ☐ | ☐ | ☐ |
| TVHeadend | 9981/9982 | ☐ | ☐ | ☐ |
| NextPVR | 8866 | ☐ | ☐ | ☐ |
| Transmission | 9091 | ☐ | ☐ | ☐ |
| Deluge | 8112 | ☐ | ☐ | ☐ |
| autobrr | 7474 | ☐ | ☐ | ☐ |

After all services are migrated, retire `media.innotel.us` and remove
the .72 host from the compose project.


## Subdomains & Nginx Proxy Manager (automatic)

`scripts/npm-proxy-hosts.py` (invoked by `setup.sh`) configures Nginx Proxy
Manager entirely through its API. Two modes (`.env`):

- `NPM_MODE=local` (default) - the stack runs its own `nginx-proxy-manager`
  container (compose profile `npm`, admin UI on **:81**) and setup drives it
  at `http://localhost:81`.
- `NPM_MODE=remote` - reuse an existing NPM server: the NPM container is
  **not** started, and setup drives the remote server's API
  (`NPM_BASE_URL`, e.g. `http://192.168.1.46:81` - the LAN address of the
  NPM host; do **not** point it at the public admin UI
  `https://proxy.innotel.us`, which is Authentik-gated and would 302 the
  API calls to the SSO sign-in). The remote server
  forwards to this host, so set `NPM_FORWARD_HOST` to the address it can
  reach this host at - a LAN IP (e.g. `192.168.1.46`), public IP, or
  hostname (instead of `container`). The ports in `npm-hosts.conf` are
  this host's published ports, so they work in both modes.

Either way the script creates (or reconciles) one proxy host per entry. `@`
is the **apex** — the base domain itself, which is the **main interface**
users log into (the Homarr dashboard). Everything else is a subdomain.

A row's port is this host's published port, and it may be written as
`${VAR:-default}`: use the **same variable and default the compose file
publishes from** and one `.env` value drives both files. `sabnzbd` does this
(`SABNZBD_PORT`); `scripts/check-proxy-ports.py` parses `docker-compose.yml`
and the map with the deployer's own loader and **fails when a row cannot
work** — it is a CI step and part of `monarch-drift-check`, and it is what
catches a wrong port even when the live NPM matches the map perfectly
(that is how `tv` forwarded to the Zeus portal's `:3001` while the IPTV guide
published `3011`). Rows naming a service outside `docker-compose.yml`
(`authentik-server` is the shared Cerulean stack) are reported as unverified,
not as failures.

**One row may name its own upstream.** `admin` does: `admin ${NPM_HOST_IP} 81`
forwards to the NPM admin UI *on the NPM host*, because the admin UI does not
run on this Docker host. A dotted hostname or an IP in the forward column wins
over `NPM_FORWARD_HOST` (compose service names never contain a dot, so the two
forms cannot be confused). This is not cosmetic: with the old
`nginx-proxy-manager 2081` row the request path was *SSO gate answers, then the
upstream fails*, because this stack's own optional `npm` profile is not running
and nothing listens on :2081 — `https://admin.monarch.innotel.us` was dead while
every other host worked. `NPM_HOST_IP` must be set in `.env`; the row resolves
to `192.168.1.46:81` here, the same upstream `admin.zeus.innotel.us` uses.

| Subdomain | Service | Port | WebSockets |
|-----------|---------|------|------------|
| `monarch.innotel.us` (apex, `@`) | Homarr dashboard — main login | 7575 | yes |
| `app.monarch.innotel.us` | Homarr dashboard | 7575 | yes |
| `auth.monarch.innotel.us` | Authentik (SSO + user portal) | 9000 | - |
| `media.magnate.innotel.us` | Jellyfin via Magnate edge | 8097 → container 8096 | yes |
| `tv.monarch.innotel.us` | IPTV/EPG guide | 3011 → container 3000 | - |
| `admin.monarch.innotel.us` | Nginx Proxy Manager admin (SSO-gated) | `NPM_HOST_IP`:81 | - |
| `req.monarch.innotel.us` | Jellyseerr request portal | 5055 | yes |
| `subscribe.monarch.innotel.us` | shared subscribe portal (public landing page) | 3040 | - |

(`api.monarch.innotel.us` was removed - **Magnate** at `subscribe.innotel.us`
is the source billing platform for all projects. `subscribe.monarch.innotel.us`
was later repointed at the shared subscribe portal - a public marketing page
served by nginx on :3040, one page per service picked by Host header - and is a
managed row in `npm-hosts.conf`, not a leftover.)

The mapping lives in `scripts/npm-hosts.conf` — add/remove lines freely; the
script reconciles the proxy hosts on every run (idempotent). For a local NPM
you can also forward to host-published ports with
`NPM_FORWARD_HOST=host.docker.internal`.

Deleting a line is deliberately **not** enough to delete a host: a typo in the
conf must never take a live service down. The host becomes **drift** —
`npm-proxy-hosts.py --check` fails and `drift-check` reports it — and is
removed explicitly:

```
python3 scripts/npm-proxy-hosts.py --prune --dry-run   # preview
python3 scripts/npm-proxy-hosts.py --prune             # delete
```

`--prune` only touches names inside `MONARCH_DOMAIN`, so the other products
sharing `proxy.innotel.us` can never be removed from here. It is how the
retired `subscribe.monarch.innotel.us` host above was cleaned up, and why
`--check` no longer lets a host linger just because the conf stopped naming it.

#### Wildcard SSL (automatic)

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
apex A record:

```
*.monarch.innotel.us   A   <this host's public IP>
monarch.innotel.us     A   <this host's public IP>
```

#### DNS records (Cerulean's Technitium)

**Cerulean owns the DNS plane and it is Technitium over HTTP** — "no SSH, no
TSIG, no nsupdate" (`cerulean` → docs/stack.md). When
`NPM_FORWARD_HOST` is an IP and `TECHNITIUM_URL` is set, `npm-proxy-hosts.py`
keeps records in sync itself:

* a subdomain with **no** record gets an `A` (TTL 300) — the case this exists
  for, a newly added line in `npm-hosts.conf`;
* a name that **already resolves** is left exactly as it is. Monarch's hosts are
  CNAMEs to the apex (`tv.monarch.innotel.us CNAME innotel.us`), which is where
  the A record lives, and Technitium refuses an `A` alongside a `CNAME` — so the
  script reports `already resolves (...) - left as is` rather than fighting it.

Auth: `TECHNITIUM_TOKEN` (a persistent API token) wins, else
`TECHNITIUM_USER`/`TECHNITIUM_PASSWORD` logs in per run. Point the URL at the
LAN address of the Technitium Cerulean runs (`http://192.168.1.46:5380` on this
host), never at a container name — the script runs on the host.

The legacy `DNS_TSIG_*` / `nsupdate` path still works for a host that runs its
own BIND, and prints a warning naming Technitium when it is used. The old
standalone BIND at `192.168.1.80` is decommissioned: it no longer answers on
:53, which is why the previous TSIG-based automation silently stopped writing
records.

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


## Authentication & user management (Authentik)

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

#### Authentik LDAP -> Jellyfin (login gate)

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

##### The Authentik LDAP outpost version (bumping `authentik-ldap`)

`authentik-ldap` is a **client** of the Cerulean Authentik, and goauthentik keeps
an outpost and its server on one version line: the outpost speaks the server's
API and report schema, so the two are bumped together. The compose comment says
"bump both together" — this is the procedure that sentence is about, and
`drift-check` now fails the run when the two have drifted apart. On 2026-09-19
the outpost sat at `2026.8.2` against a `2026.8.3` server and nothing noticed.

**Ask the server what version it is.** Its own API answers, so nothing has to be
read out of the Cerulean repo or guessed:

```bash
cd 3-media/monarch
set -a; source .env; set +a
curl -s -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
  "${AUTHENTIK_BASE_URL%/}/api/v3/admin/version/" \
| python3 -c 'import sys,json; d=json.load(sys.stdin); print("server:", d["version_current"], "outpost_outdated:", d["outpost_outdated"])'
```

`version_current` is the version to track. `outpost_outdated` is the server's
verdict across **every** outpost on the shared Authentik, not just this one — so
it corroborates a problem but is not the thing to test.

**Compare it with what actually runs here, then set both files to that version**
— the running stack *and* the `ips` platform manifest, which must not disagree:

```bash
docker inspect authentik-ldap --format '{{.Config.Image}}'
# image: ghcr.io/goauthentik/ldap:<version_current>   (in both files below)
#   3-media/monarch/docker-compose.yml
#   ips/groups/3-media.yml
```

**Pull and recreate the outpost.** The tag is resolved when the container is
created, so a `restart` would keep running the old image:

```bash
docker compose pull authentik-ldap
docker compose up -d --force-recreate --no-deps authentik-ldap
```

**Prove it** — the outpost re-registers with the server on start, so the flag
should flip, and the login path must still work end to end:

```bash
curl -s -H "Authorization: Bearer $AUTHENTIK_BOOTSTRAP_TOKEN" \
  "${AUTHENTIK_BASE_URL%/}/api/v3/admin/version/" \
| python3 -c 'import sys,json; print("outpost_outdated:", json.load(sys.stdin)["outpost_outdated"])'
python3 scripts/verify-ldap.py     # binds, then runs the paid_users search the plugin runs
scripts/drift-check.sh             # "ok: authentik LDAP outpost image tracks the server"
```

`drift-check` compares the container's tag against `version_current` and names
this procedure in the failure, so a bump that forgets either side surfaces on the
next run instead of at the next Jellyfin login.

##### When a Cerulean identity cannot sign in (HTTP 500 from the login form)

There are three independent pieces in this chain and **all of them must match
Authentik**, or sign-in breaks in a way that looks like a Jellyfin bug — the form
answers `500` for a correct password exactly as it does for a wrong one:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `authentik-ldap` unhealthy; logs repeat `403 Forbidden (Token invalid/expired)` | The outpost's API token in the container no longer matches the key Authentik holds for the `jellyfin-ldap` outpost | Set the outpost token key to the value in `.env`, then recreate the container |
| Outpost healthy but bind fails with LDAP `49` (`invalidCredentials`) | `AUTHENTIK_LDAP_BIND_TOKEN` and the bind user's password in Authentik have drifted | `set_password` the bind user to the `.env` value, re-write Jellyfin's `LDAP-Auth.xml`, restart Jellyfin |
| Log shows `LDAP-Auth, Version=23…` **and** `…Version=24…` with `InvalidCastException: …PluginConfiguration cannot be cast to …PluginConfiguration` | Two plugin folders hold `LDAP-Auth.dll`. Jellyfin loads both, and the plugin's own config type is cast across two load contexts, so *every* authentication throws — 500 for the right password and the wrong one alike | Retire the older folder (`.superseded-<date>`) so one copy remains, then restart Jellyfin |

**An HTTP 500 also arrives through Seerr**, whose Jellyfin sign-in passes
Jellyfin's status straight through as its own: `[Jellyfin API]: Something went
wrong while authenticating with the Jellyfin server: Request failed with status
code 500`. Fixing Jellyfin fixes Seerr; `401` there means the path is healthy and
the password was simply wrong.

**The usual root cause is an inline comment — and it can be inside the stored
value.** `AUTHENTIK_LDAP_TOKEN=…` and `AUTHENTIK_LDAP_BIND_TOKEN=…` must each sit
on a line of their own. Docker Compose strips a trailing `# comment` from an
unquoted value, so the outpost container boots with a truncated token while the
value `monarch-init` pinned in Authentik keeps the rest of the line — the two can
never match. Comments belong on the line **above** the value.

On 2026-09-18 that failure had gone one layer deeper: the *store* held the
placeholder text itself (`ak-ldap-outpost-2026    # outpost API token (monarch
stack)`, quotes and all) for both keys, migrated there from a `.env` whose lines
carried comments, so `.env`, Vault, the bind user and the plugin config all
agreed on a value that no Authentik token had ever been minted from. Comment
stripping cannot catch that — the comment is *inside* the value. The check that
would have is `scripts/check-vault-refs.py` in the `ips` repo (it flags a stored
value that looks like a comment or a placeholder), and the repair is to
regenerate both secrets and write them to all four places that must agree:
Authentik, Vault, `.env`, and Jellyfin's `LDAP-Auth.xml`.

A rebuild does not fix a drifted token, because Compose reads `.env` fresh but
the outpost token in Authentik is whatever was last pinned. After changing
either value:

```bash
cd 3-media/monarch
# The outpost picks up AUTHENTIK_LDAP_TOKEN from .env:
docker compose up -d --force-recreate --no-deps authentik-ldap
# Jellyfin re-reads LDAP-Auth.xml (the plugin only reloads its config on restart):
docker restart jellyfin
```

Then prove the path the plugin uses — bind, then the `paid_users` search — with
the standard-library checker (it resolves the outpost's address from Docker, so
it runs from the host or from inside any container on the network):

```bash
python3 scripts/verify-ldap.py
#   PASS the Jellyfin LDAP login path works end to end.
#   FAIL bind: result code 49 (invalidCredentials).
```

`monarch-init` is the supported way to re-pin both: with clean `.env` values it
sets the outpost token key and the bind user's password from them and rewrites
Jellyfin's config.

#### Cerulean SSO for the media apps

The platform standard — Authentik as the only login, the two conforming
patterns, the required scope mappings, the one issuer mode and the shared session
store — is
[`ips/docs/sign-in-posture.md`](../../../ips/docs/sign-in-posture.md).
Every gateway in this stack is **pattern B**: one `oauth2-proxy` per app sharing
the stack's single `monarch-media` Authentik provider and one `_innotel_sso`
cookie, with the app bound to loopback so the gateway is the only door.
The stack's `SSO_SESSION_REDIS_HOST` must be the store's **routable** address
(`192.168.1.46`); this host does not run the store, so `172.17.0.1` here is this
host's own empty docker0 and every sign-in 500s on `/oauth2/callback`.

The media management apps (**Radarr, Sonarr, Lidarr, Whisparr, Bazarr,
Prowlarr, qBittorrent, Sabnzbd**), the `req.` Jellyseerr alias, **Jellyfin**
(`media.*` — its sign-in page, not its API), **Clipbucket** (`tube.innotel.us`),
the **IPTV guide** (`tv.<domain>`) and the **requestrr console**, plus the **NPM
admin UI** itself, do not speak OIDC - they only ship a local username/password
form. "Sign in with Authentik" for them is an **oauth2-proxy SSO gateway**: it
runs the browser through a real OIDC code flow against Cerulean Authentik and
only then proxies the app. There is no nginx `auth_request` and no outpost
anywhere in the path, and every gateway shares one `.innotel.us` session
cookie, so there is exactly **one** prompt across every host.

| Piece | Where | What it is |
|-------|-------|------------|
| SSO gateway | an `oauth2-proxy` sidecar deployed with each app | a real OIDC client registered in Authentik, sharing the `_innotel_sso` cookie |
| Proxy host | `scripts/npm-proxy-hosts.py` | forwards the host at the gateway's port instead of the app's |
| App login | `monarch-init` (`set_monarch_app_auth`) | `authenticationMethod=external` ("a reverse proxy authenticated this user"), so the app's own form is gone |

Because those apps trust the proxy for identity, the gateway has to be the *only*
path in. Each app's host port is therefore bound to `127.0.0.1`
(`radarr` `7878`, `sonarr` `8989`, `lidarr` `8686`, `whisparr` `6969`, `bazarr`
`6767`, `prowlarr` `9696`, `qbittorrent` `8080`, `sabnzbd` `8082`, `jellyseerr`
`5055`, `jellyfin` `8097`, `clipbucket` `8098`, `iptv` `3011`, `requestrr`
`4545`): the LAN address answers nothing, and the gateway reaches the app over the
compose network by container name. qBittorrent's peer port (`6881`) is the one
deliberate exception — it has to stay reachable.

The four most recent additions were the apps that had a public name and no gate
at all until 2026-09-16. Two of them are **published rather than gated**, and
that is a decision with a reason worth knowing before reading a log:

- **Jellyfin and Seerr serve their own sign-in pages** (`media.innotel.us`,
  `media.magnate.innotel.us`, `req.innotel.us`, `req.monarch.innotel.us`). Until
  2026-09-18 the *pages* were gated, with the Jellyfin API passed through for
  native clients — which turned out to be the wrong half: a TV client that opens
  `…/web/#/login` in a webview was sent to Authentik, a flow it cannot complete,
  and Quick Connect had no page to enter its code on. Their proxies now carry
  `OAUTH2_PROXY_SKIP_AUTH_ROUTES: "^/.*$"` and gate nothing, and the identity is
  enforced where the app already enforces it: Jellyfin's login page offers its
  **Cerulean Authentik** SSO button and native clients bind against the LDAP
  outpost, while Seerr has no password of its own and signs in with the Jellyfin
  account. The app ports stay loopback-only, so the proxy is still the only door
  on this host. `scripts/verify-sso.py` asserts both directions — the gated names
  still `302` to the IdP, these four answer `200` with the app's own page and
  never redirect to it. Re-opening `8097` on the LAN is not the fix if a client
  breaks; that skip line is.

  **The SSO button is two halves that fail silently** — a plugin config pointing
  at an issuer nobody signs in against (the button renders and dies inside
  Authentik), and a provider that never got the callback URI registered (it dies
  at the redirect with `invalid_request: redirect_uri does not match`).
  `python3 scripts/jellyfin-oidc-sso.py --check` reads both and reports the
  installed plugin's version; it is run by `scripts/drift-check.sh`. The plugin
  binary is **pinned** in `init/jellyfin-plugins.json` (release plus the sha256 of
  the zip *and* of the assembly inside it): `monarch-init` installs it, so a
  rebuilt host comes back with the button, and
  `python3 scripts/jellyfin-plugin-pin.py --check` judges the installed build
  against the pin. The callback URIs
  (`https://media[.magnate].innotel.us/sso/OIDC/Callback/authentik`) are part of
  `MONARCH_SSO_REDIRECT_URIS`, because a provider refresh takes that list verbatim.

`scripts/verify-sso.py` is the committed regression test for all of this: it
creates a throwaway Authentik identity, drives a real OIDC flow through every
gateway above, asserts the session opens the app, asserts an identity outside
`SSO_REQUIRED_GROUP` is refused, and checks that each app port answers on
loopback and refuses on the LAN. Exit codes: 0 pass, 1 fail, 2 cannot run.

##### The four recent names, as deployed (2026-09-17)

Each is now a gateway with an edge forward pointing *at the gateway*, verified by
driving the name: `media.innotel.us`, `media.magnate.innotel.us`, `tube.innotel.us`
and `tv.monarch.innotel.us` all answer `302` to
`auth.cerulean.innotel.us/application/o/authorize/` with `client_id=monarch-media`,
and the provider holds a redirect URI for every one of them plus
`requestrr.monarch.innotel.us` (16 in total). Before the change the first three
were `502` — the edge still forwarded to `.56:8097` / `.56:8098`, which are
loopback-only now.

**Two steps of that change are not this repo's to make, and both bit once:**

- **The `media.*` and `tube.*` forwards live in the edge** (Magnate owns the
  media names, the archive side the tube one). They are NPM proxy hosts, not
  `npm-hosts.conf` rows, so `scripts/npm-proxy-hosts.py` cannot manage them — and
  its `--prune` deliberately refuses to touch anything outside
  `MONARCH_DOMAIN`. Changing them is an NPM API change (or the admin UI) on the
  edge host; `tv.*` and `requestrr.*` *are* this zone's rows and are handled by
  the script.
- **A proxy host's TLS is part of its state, and the script used to drop it.**
  `--hosts-only` does no certificate work, and the update body carried
  `certificate_id: 0` — which NPM writes as a value rather than reading as "leave
  it alone". One run therefore stripped the wildcard certificate (id 48) and
  `ssl_forced` from all sixteen hosts in this domain, including the dashboard and
  the auth host. It is fixed in two places: the script now carries an existing
  host's certificate over when a run resolves none, and `--check` flags
  `serves no TLS certificate (certificate_id=0)` as drift (suppressed by
  `--skip-ssl`, which is what a genuinely cert-less zone checks with). If it ever
  happens again, the pre-change values are in the 02:00 NPM backup
  (`backups/npm-backup-<date>-020000.tar.gz`, table `proxy_host`).

#### The apps' own sign-in methods

The gateway proves a Cerulean session for the *name*. It says nothing about the
credential form behind it, and two of these apps keep a store of their own — a
way in that no gateway covers, and the reason a user disabled in Authentik could
still sign in:

| App | What it keeps | Check | Repair |
|-----|---------------|-------|--------|
| Jellyseerr | `main.localLogin` — "Enable Local Sign-In": email and password, in Seerr's own store | `python3 scripts/seerr-login-methods.py --check` | `--apply` |
| Jellyfin | accounts in Jellyfin's own database rather than the LDAP outpost | `python3 scripts/jellyfin-login-methods.py --check` | `--apply`, or declare it (`JELLYFIN_LOCAL_ACCOUNTS`) |

Neither is configuration in this repo, which is why the two scripts exist rather
than `.env` keys: Seerr re-enables local sign-in on a settings import, and
Jellyfin's first-run wizard (or an administrator in its UI) creates a local
account. `scripts/drift-check.sh` runs both as checks and fails on drift.

What each one deliberately leaves alone:

- **Seerr's Jellyfin sign-in stays on.** Turning it off too would leave a gateway
  that authenticates nobody *into Seerr*: the proxy proves a session for the name,
  but Seerr still needs its own, and the Jellyfin account is how a user gets one
  without a second password. The password is what is removed, not the sign-in.
- **Jellyfin's break-glass `admin`.** It is one of the local accounts Monarch
  wants (`jellyfin-admin-password.py` keeps its password in step with
  `MONARCH_PASSWORD` and mints the durable API key the services use), so the check
  passes when the declared accounts are the only ones left and `--apply` never
  touches it. Everything else local is *disabled* rather than deleted: the login
  is refused, the watch history and the account id survive, and the account can be
  handed back to the LDAP provider by enabling it again. Because it signs in
  against Jellyfin's own store it never passes through the provider, so the OIDC
  role mapping never reaches it — `monarch-init` grants it the `jellyfin_admins`
  rights itself (`EnableContentDeletion`, `EnableLiveTvManagement`), and the
  latter is what puts a delete action on a recording. Without it the Recordings
  library only grows and the only way to clear it is a shell on the host.
- **Any account the deployment declares in `JELLYFIN_LOCAL_ACCOUNTS`** (`.env`,
  comma-separated, beside `JELLYFIN_ADMIN_USER`). Appointing `media.*` to serve
  its own sign-in page cost it the gate, and a TV or mobile client that can
  complete neither the browser flow nor an LDAP bind signs in against Jellyfin's
  own store — a real second password, and one that looks identical to a stray.
  Declaring it is the difference between "we keep this deliberately" and "nobody
  noticed": declared accounts are never reported and never disabled, the count is
  printed on every run, and dropping a name makes it a stray again. The names live
  in the operator's `.env`, not here, because which person keeps a local login is
  deployment state that changes — the repo would be describing last month's
  deployment, and `drift-check` runs the same script either way.

#### Seerr's Owner is a row id, not a permission

Seerr has exactly one Owner, and the server decides it by row: in
`server/routes/user/index.ts` (image `ghcr.io/seerr-team/seerr`, v3.4.1)
`canMakePermissionsChange()` refuses to let anybody but `user.id === 1` grant
admin, and `PUT /:id` refuses to let anybody but row 1 modify row 1. So the
**badge, and the right to hand out admin, belong to whichever account completed
setup first** — usually the break-glass Jellyfin admin, which nobody signs in as,
and which `main.localLogin: false` can make unreachable outright. No endpoint
moves it; `permissions` is a bitfield and `Owner` is derived from the row.

`scripts/seerr-owner.py` **swaps the two accounts** rather than renumbering
either: every `user` column except `id`, every foreign key onto `user.id`
(discovered from the schema, so a table a future release adds follows too) and
the live sessions all change sides. Nothing is deleted, so the displaced account
keeps its id, its data and its admin bit.

```
python3 scripts/seerr-owner.py --check      # who owns Seerr (the default)
python3 scripts/seerr-owner.py --apply      # hand it to the manifest's account
```

The account is named once, in the invariants manifest (`jellyseerr.owner`, from
`MONARCH_SEERR_OWNER`, default `dhunter`), which is what `monarch-init` writes,
what the script reads and what `drift-check` judges. Swap it back by running
`--apply --account <other>`. Sign in again afterwards: sessions are re-pointed to
follow their identity, which is a deliberate change of who they are.

Setup (the gateway deploys with the app on this host; this repo's script
reconciles the proxy hosts and supports `--check`/`--dry-run`):

```
python3 scripts/npm-proxy-hosts.py          # proxy hosts -> the gateways
```

Nothing here writes a forward-auth gate, and nothing can: the snippet
machinery and the `NPM_FORWARD_AUTH*` switches were removed. The way back in
when Authentik is unreachable is the LAN admin port with `BREAKGLASS_LOGIN=1`
on the NPM host - not a gate exemption.

The **sign-in host is never fronted by a gateway**: a signed-out browser is
sent to `auth.$MONARCH_DOMAIN`, so putting it behind one would loop onto
itself and make every other host unreachable.

#### Subscription platform + billing

**Magnate** (`subscribe.innotel.us`) is the **portal landing page and the
subscribing page** for the ecosystem: visitors land there, pick a plan and pay
through Stripe Checkout, and Magnate provisions the subscriber into Authentik
(`paid_users`) — which is the group Monarch's SSO gate and Jellyfin LDAP login
check. Monarch hosts no payment path, no pricing page and no plan copy; every
"Subscribe" link in the stack (the Homarr board's **Subscribe** tile and the
landing page) points at `SUBSCRIBE_URL` (`https://subscribe.innotel.us`), and CI
fails if either entry point drops it. `req.innotel.us` is the
subscriber-facing request portal (Jellyseerr) — it fronts the app through the
`jellyseerr-sso` gateway like `req.monarch.innotel.us`, so it needs an Authentik
session too; its callback is registered on the `monarch-media` provider. It is a
**manual** proxy host (outside `MONARCH_DOMAIN`), so no run of
`npm-proxy-hosts.py` maintains it. `ACCOUNT_PORTAL_URL` is the
**Authentik self-service** page (password reset) — a different thing from the
subscribe page. See `.env.sample` for `SUBSCRIBE_URL` / `APP_URL`,
`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `JELLYFIN_URL` /
`JELLYFIN_API_KEY` and `ACCOUNT_PORTAL_URL` / `REQUEST_URL`.

#### Entitlements — Magnate's decision, Monarch's policy

`paid_users` gates *access*; it does not constrain how a tier plays. That gap is
closed by **`scripts/magnate-entitlements.py`** with
**`scripts/magnate-tiers.json`** as the tier → playback table:

| Piece | What it is |
|-------|------------|
| Decision | Magnate `GET /api/entitlements?plan=<slug>&user=<username\|email>` → `{entitled, reason, plan, slug, status, expires_at}`; `Authorization: Bearer $ENTITLEMENTS_API_TOKEN` when Magnate sets one |
| Policy | `magnate-tiers.json`: `streams`, `quality_kbps` (0 = uncapped), `profiles` (advisory), `downloads` per plan slug (`basic` / `standard` / `premium` / `agents` / `magnate`, the slugs Magnate seeds) |
| Applied to | Jellyfin `MaxActiveSessions` (when the build has the field), `RemoteClientBitrateLimit`, `EnableContentDownloading` |

```
python3 scripts/magnate-entitlements.py --check   # report only (drift-check runs this)
python3 scripts/magnate-entitlements.py           # converge the policies
python3 scripts/magnate-entitlements.py --disable-unentitled   # opt-in lockout
```

Two deliberate rules: an **unentitled user is reported, never silently
disabled** (the local Jellyfin admin is not a Magnate subscriber — locking them
out would be self-inflicted), and `exempt_users` in the tier file is skipped
entirely. `profiles` is recorded rather than enforced, because Jellyfin has no
per-user profile limit — it is what Magnate/Authentik are asked to allow.
Jellyfin auth for this script is the `MediaBrowser Token=` header (the pinned
v12 image answers 401 to `?api_key=` and `X-Emby-Token`), read from
`JELLYFIN_API_KEY` or `/docker/appdata/init/jellyfin-api-key.txt`.

`JELLYFIN_API_KEY` should be a **durable API key**, not a session token: a
password change revokes every session token the admin holds, which is exactly
how this credential went stale once, and an API key survives it. `monarch-init`
exports a session token on first boot (enough to get the stack up), and
`scripts/jellyfin-admin-password.py --set` replaces it with a durable API key
called `monarch-admin` and writes both places it is read from:
`/docker/appdata/init/jellyfin-api-key.txt` and `JELLYFIN_API_KEY` in `.env`.
`monarch-recs` and `monarch-health` read the same file automatically.

#### The Jellyfin admin password

`MONARCH_PASSWORD` sets the Jellyfin `admin` password on a fresh install, and
Jellyfin refuses to change an existing one without the current password — so an
operator changing it by hand desynchronises the shared credential, and no
`monarch-init` re-run can put it back.
**`scripts/jellyfin-admin-password.py`** aligns it through Jellyfin's own
forgot-password flow, which needs no old password:

```
python3 scripts/jellyfin-admin-password.py --check   # drift only, changes nothing
python3 scripts/jellyfin-admin-password.py --set     # align, then refresh the API key
```

Two traps in that flow are worth knowing before running it by hand: the PIN is
written to a `passwordreset*.json` file **inside the container** (the API only
returns the path, so the call looks like it did nothing), and redeeming the PIN
makes **the PIN itself** the account password — log in with the PIN, not with an
empty password. `--set` handles both, then sets `MONARCH_PASSWORD` through
`POST /Users/{id}/Password`, which is why the account ends up matching `.env`
instead of drifting again. Subscribers never use this account: they sign in
through the Authentik LDAP outpost.

#### Homarr's encryption key (and the Jellyfin key it holds)

Homarr encrypts every integration secret it stores — the Jellyfin API key
included — with `SECRET_ENCRYPTION_KEY` from `.env` (see the rotation table
below for the exact scheme). The key and `/docker/appdata/homarr/appdata/db`
are **one artifact**: the ciphertext only means anything to the key that made
it. Two consequences worth stating plainly, because neither failure looks like
a key problem from the outside:

- **Back them up and restore them together.** A restored Homarr DB without its
  key is a dashboard that boots and answers, with integrations that silently
  fail.
- **Never rotate it on a host whose DB already exists.** Changing the key
  orphans every stored secret at once; the container stays healthy and the
  tiles just stop working. If `.env` and the running container ever disagree,
  put `.env` back to the **container's** value — that is the one the ciphertext
  was made with — do not generate a new one.
- **Restart `homarr` after replacing `db.sqlite`.** SQLite is opened by
  inode, not by path: copy a DB in under a *running* container and the process
  keeps the old file, which the copy just unlinked. Restore a database this way
  — a migration, a backup — and Homarr carries on against the deleted inode,
  which after a restart-free swap looks like a brand-new install: it answers
  every route with a redirect to `/init`, the "Welcome to Homarr" wizard. On an
  SSO-only deployment that wizard is a dead end (there is no username/password
  form to finish it with), so the instance simply cannot be entered. Measured
  after this stack's move to `.56`: the on-disk DB held the user, the board and
  its 44 items, while `lsof` showed the server holding
  `/appdata/db/db.sqlite (deleted)`. `docker restart homarr` reopens the file
  on disk and the login page comes back with its **Login with Cerulean** button.
  When a restore is the reason, stop `homarr` first and copy the file in while it
  is down — the ordering hazard is the whole trap.

`monarch-drift-check` asserts `.env` and the running container carry the same
key, and `jellyfin-admin-password.py --check-apps` then proves the *decrypted*
key still authenticates against Jellyfin. Together those two cover both halves:
the key Homarr is handed, and the secret it can actually read with it.

#### Rotating a Jellyfin API key

Mint one with `POST /Auth/Keys?app=<name>` (204) and read it back from
`GET /Auth/Keys` — the list lags the create by a moment on this build. Delete one
with `DELETE /Auth/Keys/<token>`. The order that never breaks a consumer is
**mint → update the consumer → verify → delete the old key**: deleting first
leaves the app locked out for however long the rest takes.

| Consumer | Where its copy lives | How to update it |
|---|---|---|
| `monarch-admin` | `/docker/appdata/init/jellyfin-api-key.txt` + `JELLYFIN_API_KEY` in `.env` | `scripts/jellyfin-admin-password.py --set` |
| Jellyseerr | `settings.json` → `jellyfin.apiKey` (plaintext) | edit it and restart the container |
| Homarr | its own DB (`integrationSecret`, integration kind `jellyfin`) | **encrypted**: AES-256-CBC, key = `hex(SECRET_ENCRYPTION_KEY)`, 16-byte random IV, stored `hex(ciphertext).hex(iv)`. Set it in the Homarr UI, or re-encrypt with that scheme and restart |

`monarch-admin` is the key Monarch's own services use (AI recommendations, health
analytics, entitlements). The other two belong to the apps themselves and only
need rotating if their value is exposed. There is no endpoint that "tests" a key:
call any admin endpoint with it and expect 200 — which is what
`scripts/jellyfin-admin-password.py --check-apps` does for both app copies, and
`drift-check` runs it, so a rotation that stopped halfway cannot sit unnoticed.


## AI recommendations & smart search (monarch-recs)

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


## Media health analytics (monarch-health)

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


## Live-stack drift check (monarch-drift-check)

`scripts/drift-check.sh` probes the running stack and verifies it still
matches what `monarch-init` is supposed to maintain. It never writes
anything — it only reads API keys from `/docker/appdata` and issues checks
against the services:

| Checked service | Invariants verified |
|-----------------|---------------------|
| Sonarr / Radarr / Lidarr / Whisparr | API reachable, `authenticationMethod=external` (the Cerulean SSO gate is the only login), expected media root folder, qBittorrent download client |
| Prowlarr | qBittorrent download client, Sonarr/Radarr/Lidarr/Whisparr apps registered |
| qBittorrent | WebUI login with the shared credentials, `movies`/`tv`/`music`/`xxx` categories |
| Jellyfin | admin API access — the shared credentials when they still match, otherwise the durable admin API key (`/docker/appdata/init/jellyfin-api-key.txt`; when the local admin password has diverged the check says so and names the repair, `scripts/jellyfin-admin-password.py --set`) — plus media libraries (Movies / TV Shows / Music / Other) |
| Jellyfin API keys held by the apps | Jellyseerr's copy in `settings.json` and Homarr's encrypted copy in its database still authenticate — the state a half-finished rotation leaves behind, which nothing else catches since the container and its own UI stay up (`jellyfin-admin-password.py --check-apps`) |
| Jellyseerr | initialized, Jellyfin sign-in enabled, owned by the account the manifest names (`seerr-owner.py --check`) |
| Bazarr | API key readable, no local login (the Cerulean SSO gate is the login) |
| Authentik (optional) | LDAP outpost provisioned (only when `AUTHENTIK_BASE_URL` is set) |
| Cerulean Vault | `.env` holds materialized values with no unresolved `vault://` reference — the drift-check greps for leftovers (read-only; `scripts/vault-migrate.py --dry-run` shows which plaintext values are not in the store yet) |
| Magnate (when `MAGNATE_URL` is set) | every managed user's Jellyfin policy matches its Magnate tier (`scripts/magnate-entitlements.py --check`, read-only; skipped when no Jellyfin API key) |
| Nginx Proxy Manager (static) | `scripts/check-proxy-ports.py` — every `npm-hosts.conf` row forwards to a port `docker-compose.yml` publishes (or the container port); needs no credentials, runs in both NPM modes |
| Nginx Proxy Manager (live) | live proxy hosts match `scripts/npm-hosts.conf` — subdomain, forward host/port, websocket support and the SSO gate — and no host in `MONARCH_DOMAIN` is live that the conf no longer lists (`npm-proxy-hosts.py --check`; remove a retired host with `--prune`); skipped when the NPM container isn't running and `NPM_MODE!=remote` / no `NPM_ADMIN_*` credentials |
| Infra (host) | `/data` + `/docker/appdata` disk usage below 90%, probed containers not crash-looping (restart count), no stale images (recreate needed) |

> **Jellyfin on the pinned build.** It reads the MediaBrowser header from
> `Authorization`; the `X-Emby-*` spellings answer HTTP 400
> (`Value cannot be null. (Parameter 'request.App')`) or 401 regardless of the
> credentials — which is why `init/init.py`, `scripts/drift-check.sh` and
> `scripts/magnate-entitlements.py` all send `Authorization: MediaBrowser …`.
> Its local `admin` password is set by the first-run wizard and init cannot
> re-sync it for an existing user (the password endpoints need the *current*
> password), so it can diverge from `MONARCH_PASSWORD` after a change; for
> host-side automation the exported admin token
> (`/docker/appdata/init/jellyfin-api-key.txt`) is the credential to trust, and
> subscribers sign in through the Authentik LDAP outpost rather than as `admin`.

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
## check once, human-readable
./scripts/drift-check.sh

## quiet (only DRIFT-FAIL lines on stderr) - for cron/timers
./scripts/drift-check.sh --quiet

## when drift is found, re-run monarch-init automatically, then re-verify
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

**Heal rate limit:** a heal attempt is recorded in
`/docker/appdata/init/drift-heal-last`; if drift is still present and the
last attempt was less than `DRIFT_HEAL_MIN_INTERVAL` (default 3600s) ago,
the check skips healing and escalates straight to an alert instead of
looping `monarch-init` on every tick.

Infra thresholds are tunable via `DRIFT_DISK_MAX_PCT` (default 90),
`DRIFT_MAX_RESTARTS` (default 10) and `DRIFT_HEAL_MIN_INTERVAL` in `.env`.

The **full-stack CI workflow** (`.github/workflows/full-stack-drift.yml`)
boots the real stack (jellyfin, *arrs, prowlarr, qBittorrent, bazarr,
jellyseerr, Nginx Proxy Manager) + `monarch-init` on a disposable runner,
provisions the NPM proxy hosts for the test domain, and runs the actual
drift check against it — so the proxy-host verification is exercised for
real on every run. It runs on PRs touching `init/`, `docker-compose.yml`,
the drift check, or the fresh-install check; nightly; and on demand
(`workflow_dispatch`):

```
./scripts/fresh-install-check.sh --full-stack     # boot + init + real drift check
```

#### Telegram alerts (optional)

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


## Watch parties & offline downloads

Both are **native Jellyfin features**, enabled out of the box:

* **Watch parties (SyncPlay)** — any profile can start a SyncPlay session
  from the Jellyfin app (tap the SyncPlay icon and invite others); playback,
  pause and seek stay in sync across participants. SyncPlay is enabled by
  default; administer it in Jellyfin Dashboard -> Playback.
* **Offline downloads** — the Jellyfin mobile/desktop apps download titles to
  the device for offline viewing. Downloads are authenticated against
  Authentik (via the LDAP login chain), so revoked users lose access.


## Live TV (Jellyfin)

Live TV is wired automatically on first boot — **no TVHeadend or NextPVR
setup needed**. Jellyfin ingests a playlist directly as a native **M3U
tuner** and uses the XMLTV guide generated by the `iptv` container:

| Piece | Who sets it up | What it is |
|-------|----------------|------------|
| Channel source | `monarch-init` (Jellyfin API) | M3U tuner pointing at `LIVETV_M3U_URL` (default: the free iptv-org **US** playlist, ~700 channels) |
| Guide (EPG) | `monarch-init` + `iptv` container | channel list at `/opt/epg/channels.xml` (assembled from `LIVETV_EPG_SITES`); guide grabbed twice a day and served at `http://iptv:3000/guide.xml`. The grabber holds the whole run in memory, so peak use is days x channels; `DAYS=2`, `NODE_OPTIONS=--max-old-space-size=3072` and `mem_limit: 4g` in `docker-compose.yml` are what keep that run inside the container's own memory instead of the host's |

**How much of Live TV has a guide.** The guide can only carry the channels whose
site is in `LIVETV_EPG_SITES` (`init/init.py`), and that list is a measurement,
not a guess: every site in the [iptv-org EPG
repo](https://github.com/iptv-org/epg/tree/master/sites) was matched against the
dial's 1,470 stream ids, and the default set is the ones that cover it —
`xumo.tv` (128 of the dial), `tvtv.us` (163, the cable networks), `tvpassport.com`
(91) and `tvguide.com` (63) as second sources for the same listings, then the
smaller FAST guides. Together they name **339 of the 1,470 streams**; the rest are
community-sourced FAST feeds no EPG site publishes, and they still play — they
just have no "what's on" next to them.

To widen or replace the guide:

```
LIVETV_EPG_SITES=xumo.tv,tvtv.us,my-paid-guide.com     # in .env
sudo rm /opt/epg/channels.xml                          # the list is assembled once
sudo docker compose run --rm init                      # or let monarch-init re-run
sudo docker restart iptv                               # grab now
```

`channels.xml` is only written when it is absent, so an existing file is never
overwritten — delete it to re-assemble, or edit it by hand if you know what your
grabber needs.

**First boot:** `monarch-init` adds the tuner and guide provider
(idempotent). To get listings immediately:

```
sudo docker restart iptv        # triggers an EPG grab right away
```

**Custom providers:** set `LIVETV_M3U_URL` (and optionally
`LIVETV_GUIDE_URL`) in `.env` to your IPTV provider's playlist. Free
iptv-org streams are community-sourced — most play, but some channels may be
offline or geo-blocked.

### The Comcast dial

Out of the box the tuner is the raw iptv-org playlist: thousands of rows in
playlist order, no channel numbers, and no News/Sports/Movies split. A cable box
is the shape people already know, so this deployment runs its own dial on top of
the same streams:

| Piece | What it is |
|-------|------------|
| `data/comcast-springfield-channels.tsv` | the dial itself, exactly as the operator supplied it: one `number<TAB>name` per line, nothing inferred. This is the authoritative source for a channel number |
| `scripts/livetv_lineup.py` | matches the streams against it, numbers them, and writes the playlists and the renumbered guide into `/opt/epg` |
| `/opt/epg/comcast-springfield.m3u` | the dial — Jellyfin's tuner points here |
| `/opt/epg/comcast-springfield.xml` | the guide, renumbered from the `iptv` container's `guide.xml`; programmes are the guide's own, with each channel's number as its display name and `<lcn>` |
| `/opt/epg/<category>.m3u`, `/opt/epg/network-<name>.m3u` | News/Weather/Sports/Movies/Kids/Music/Documentary/Lifestyle/Local/Entertainment/General, plus one per network that appears more than once (the "several ABC stations, one subcategory" rule) |

The `iptv` container serves `/opt/epg` at `http://iptv:3000/<name>`, which is how
Jellyfin reaches all of it with nothing else running.

```bash
cd /usr/src/projects/complete/3-media/monarch
python3 scripts/livetv_lineup.py --plan              # the taxonomy + what lines up
python3 scripts/livetv_lineup.py --guide /opt/epg/guide.xml --out /opt/epg
python3 scripts/refresh-livetv-guide.py             # make Jellyfin re-read it, and wait
python3 scripts/verify-livetv-lineup.py             # the dial and the guide, as Jellyfin holds them
```

`refresh-livetv-guide.py` is the step that is easy to skip: writing `/opt/epg`
changes nothing on its own, because Jellyfin keeps its own copy of the playlist
and the guide until the *Refresh Guide* task runs. It starts the task and waits for
it to finish, so `verify-livetv-lineup.py` immediately afterwards reads the new
dial rather than the old one and reports a fault that is not there.

Re-run the generator whenever the lineup changes — it is idempotent, and Jellyfin
picks the files up on its next guide refresh (**Dashboard → Scheduled Tasks →
Refresh Guide**, or the API). On the deployment host a timer does it for you:
`monarch-livetv-dial.timer` runs the generator at 00:30 and 12:30, half an hour
behind the `iptv` container's own 00:00/12:00 guide grab, so the dial is always
built from the current guide rather than from yesterday's.

```bash
systemctl list-timers monarch-livetv-dial.timer
systemctl start monarch-livetv-dial.service      # rebuild now
journalctl -u monarch-livetv-dial -n 20          # what it did
```

Three things worth knowing before editing the dial:

* **The dataset is the operator's list, not a reconstruction.** Every number in
  `comcast-springfield-channels.tsv` is Comcast's for the Springfield franchise,
  so there is no "approximately right" row in it. That is why it is stored as
  pasted: transcribing it into another format is how a number gets quietly
  changed. If a channel moves, edit the TSV and re-run the generator.
* **A name is matched on its identifying words, not on a regex per channel.**
  Feed words (`HD`, `SD`, `East`, `DT2`, a parenthetical) are dropped, so `CNN`,
  `CNN HD` and `CNN HD East` are one channel. Among the dial rows that match, the
  *feed* then decides, because the dial lists a network's SD and HD positions as
  separate rows: an HD stream takes the HD row (`CNN HD` is 842, `Fox News
  Channel (720p)` is 841) and a name that says nothing stays on the classic
  position (`CNN` is 42). An HD row that takes a stream leaves the classic
  position beside it empty, and the lineup says both numbers are one channel, so
  the same feed is listed on the SD row as well (a *simulcast*: `Jewelry TV 2
  (720p)` is on 1032 *and* 8) rather than the low end of the dial going dark. The
  lowest number only breaks what specificity and feed leave tied. A shorthand
  that cannot collide (`DIAL_ALIASES`: HGTV for "Home &
  Garden Television", MSNBC for "MS NOW", Nat Geo for "National Geographic")
  binds to the dial's own legacy name. A lone generic word — "Cable News Network"
  reduces to just `news` — may never carry a match, so "CBS News 24/7" is *not*
  handed CNN's number.
* **Most of the dial has no stream, and gets none.** The list has 1,378 channels;
  the playlist has ~1,500 streams, and only the ones that name a channel in the
  dial take its number. Everything else — the Xumo FAST services, the Spanish
  feeds, the sports-team overflow — takes a number in its category's band
  instead of borrowing somebody's. The generator prints both counts, and lists
  dial entries left empty, so the gap is visible rather than implied. About 200
  of the empty rows are under 800, and almost all of them are channels no free
  playlist carries at all — the Springfield locals (WGBY 2, WSHM 3, WGGB 4, WWLP
  5), Local Access and the cable networks — so they are empty because there is
  no stream to put on them, not because a number went missing.

Sub-feeds of one network share its number with a decimal (`25`, `25.1`, `25.2`),
exactly as a cable dial does.

To add a category or a network playlist as a second tuner in Jellyfin (they are
served at `http://iptv:3000/news.m3u`, `.../network-pbs.m3u`, …), add an M3U
tuner per file and point the same XMLTV provider at
`http://iptv:3000/comcast-springfield.xml`; the channels duplicate the dial's,
which is why the dial is the tuner this deployment uses by default.


## Restart services

```
sudo docker compose down
sudo docker compose up -d
```


## What's still manual (one-time, mostly external)

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
3. **Jellyfin's plugins are pinned, not installed by hand.**
   `init/jellyfin-plugins.json` records, per plugin, the release plus the sha256
   of both the zip *and* the assembly inside it, and `monarch-init` installs from
   that file. Two plugins are in it, because both are load-bearing and neither
   says which build it is:

   - **`oidc`** — `OIDC RBAC`, assembly `Jellyfin.Plugin.OIDC.dll`,
     `Ezeqielle/jellyfin-plugin-oidc` v1.0.10. It is the **Cerulean Authentik**
     button on Jellyfin's own login page; Jellyfin's catalog does not carry it and
     its `meta.json` ships no `sourceUrl`, which is why it used to be installed by
     hand. Before the pin, the only record of what was installed was a zip in
     `/tmp`, and a rebuild came back with a login form and no SSO.
   - **`ldap`** — `LDAP Authentication`, assembly `LDAP-Auth.dll`,
     `jellyfin/jellyfin-plugin-ldapauth` v24. It is the credential store behind
     that page. Its `meta.json` reports an empty version list, and its install
     path used to ask GitHub for "the latest release" — which is how v23 ended up
     installed beside v24. Jellyfin loads every folder carrying an assembly, so
     the two copies cast the plugin's config type across two load contexts and
     **every authentication returned HTTP 500** for a right password and a wrong
     one alike. The pin is one build in one folder; a second non-retired copy is
     reported by name (`--check`), and folders retired by renaming
     (`LDAP-Auth.superseded-<date>`) are not counted, because Jellyfin reports
     those as `Superseded` rather than loading them.

   `python3 scripts/jellyfin-plugin-pin.py --check` (run by `drift-check`) judges
   both installed builds against the pins; `--status` prints both sides,
   `--plugin <name>` narrows it to one, and `--install` fetches, verifies both
   hashes and extracts into `/docker/appdata/jellyfin/data/plugins/<plugin_dir>/`,
   then tells you to restart Jellyfin (nothing here restarts your media server).

   `scripts/jellyfin-oidc-sso.py --check` covers the other two halves of the
   button — the plugin's config and the callback registered on the `monarch-media`
   provider. The provider's callback URIs are in `MONARCH_SSO_REDIRECT_URIS`; the
   ClientId/Authority/ClientSecret are the same `MONARCH_SSO_*` values the
   gateways use, written into
   `/docker/appdata/jellyfin/data/plugins/configurations/Jellyfin.Plugin.OIDC.xml`
   by `monarch-init` (including the `paid_users` / `jellyfin_admins` role
   mappings), and the name the plugin builds its `redirect_uri` from is
   `MONARCH_SSO_SERVER_BASE_URL`.


## Remaining config

Add some indexers to Prowlarr. These tools are powerful automation for
managing media, and there is a wealth of legal, copyright-free, and
open-source content you can use them for — e.g. in Radarr you can download
movies in the Public Domain or released under Creative Commons (Night of the
Living Dead (1968), His Girl Friday (1940), Charade (1963), The General
(1926), ...). The "Gold Standard" legal indexer is **Archive.org**, which
hosts thousands of public domain movies.


## Troubleshooting

#### monarch-init / monarch-seed
`sudo docker logs monarch-init` shows what the automation did. Its per-service
result and any "MANUAL ACTIONS NEEDED" list is in
`/docker/appdata/init/status.json`. If a service was mid-startup during the
run, just re-run: `sudo docker start monarch-init`
(or `sudo docker compose up -d` — it is idempotent).

#### qBittorrent WebUI login fails with the configured password
Grab the temporary password from `sudo docker logs qbittorrent` (search for
"A temporary password is provided for this session"), log in at
http://localhost:8080, set your password in **Tools > Options > Web UI**, then
re-run `sudo docker start monarch-init` to recreate the categories.

#### An *arr has no indexers, and Prowlarr says "Prowlarr URL is invalid, Sonarr cannot connect to Prowlarr"
That message is about the **Host header**, not the URL. Every *arr answers HTTP
400 to a Host name it was not told about (its DNS-rebinding guard, whose default
names only localhost), so Prowlarr calling `http://sonarr:8989` — or Sonarr
calling Prowlarr back at `http://prowlarr:9696` for a search — is refused before
authentication runs. The *arr then never receives an indexer, which is why the
app list can look perfectly registered while every indexer list is empty.

The allowlist is `init/arr-allowlist.txt`, read by both `monarch-init` (which
writes it into all five apps) and `scripts/arr-allowed-hosts.py` (which applies
it from the host). One detail decides whether the fix works:

```bash
python3 scripts/arr-allowed-hosts.py --check   # exit 2 = drift, 1 = apps not on this host
python3 scripts/arr-allowed-hosts.py           # apply, and RESTART what changed
```

**A running *arr has already read the old list**, so an "applied" API call on its
own leaves it refusing the new name. That is what `--no-restart` is for (apply
now, restart in a window). `scripts/drift-check.sh` runs the `--check`, and
Prowlarr's **Settings → Apps → Test** must answer 200 for all four.

`--check` sends each app one request carrying the name its peers use and treats
the 400 as the finding, rather than comparing `allowedHosts` — the setting is not
a reliable proxy for it in either direction (Lidarr 2.x answers a service-name
Host fine and never persists the field, so a comparison reports permanent drift
for an app that works).

#### Prowlarr has indexers but every search comes back empty
An indexer that is present and blocked looks exactly like an indexer that is not
there, except that it makes searches look *configured*. Most public trackers sit
behind Cloudflare, and Prowlarr only routes an indexer through FlareSolverr when
the indexer carries the `cloudflare` tag.

```bash
python3 scripts/prowlarr-indexers.py --check             # test every indexer already added
python3 scripts/prowlarr-indexers.py --repair            # tag the blocked ones for the proxy
python3 scripts/prowlarr-indexers.py                     # add the public ones that answer
python3 scripts/prowlarr-indexers.py --privacy public,semiPrivate   # try the closed-signup ones too
```

`--repair` is the cheap half and the one to run first: it tags **only** the
indexers already in the list that a second attempt through the proxy fixes, so
fixing fifteen blocked ones does not mean re-testing six hundred definitions. It
refuses to run when Prowlarr holds no indexer proxy, because the tag would then
be written, the indexer would still be blocked, and the run would report a repair
that changed nothing.

It adds nothing it has not just tested, retries a Cloudflare failure through the
proxy (tagging the indexer when that works), and repairs the ones already in the
list the same way. Definitions marked `private` are never attempted: they want an
account. `--check --offline` (what the drift timer runs) only asks whether an
enabled indexer exists, because testing thirty trackers every six hours is a way
to earn a ban — the deep test is the operator's command above.

#### Prowlarr is full but an *arr still has no indexers
Filling Prowlarr's list does not put anything into the apps. Prowlarr only offers
an indexer to each app during an **application sync**, so a deployment whose apps
were registered — or whose indexers were added — after the last sync shows
Prowlarr with seventy indexers, all four app tests green, and every *arr holding
none. That is "Prowlarr is not registering its indexers", and the missing step is
the sync.

```bash
python3 scripts/arr-sync.py --check   # exit 2 = an app received nothing, 1 = not reachable
python3 scripts/arr-sync.py           # sync all four, wait, then report what arrived
```

An app "has received" when it holds an indexer whose base URL is Prowlarr's own
proxy path (`<prowlarrUrl>/<id>/`), which is what a synced indexer looks like —
not a count of everything Prowlarr holds. **Fewer than Prowlarr has is the
supported state**: Prowlarr deliberately will not sync an indexer that returns no
results in that app's categories (its FAQ: "Prowlarr will not sync X Indexer to
App"), so an app holding a subset of Prowlarr's list is correct while holding none
is not. Only the zero is a finding, and `scripts/drift-check.sh` fails on it.

#### An *arr warns "Download client qBittorrent places downloads in the root folder /data/media/<type>"
A download's destination is two settings, and each one fails on its own without
looking like the other:

* **the category name each *arr sends.** Servarr spells it after the media type —
  `tvCategory` (Sonarr, Whisparr), `movieCategory` (Radarr), `musicCategory`
  (Lidarr). There is no plain `category` field, so a client created with
  `category` set is created with no category at all and the value is dropped
  silently. A download with a category qBittorrent does not know is saved to the
  **default** save path, not to a per-category one;
* **the save path qBittorrent maps that name to.** A category pointing at
  `/data/media/<type>` puts an unfinished album straight into the music library
  for Jellyfin to scan — and that is the warning above.

The manifest is one source of truth for both: each *arr's category comes from
`MONARCH_APPS`, and `qbt.category_paths` maps those names into the downloads tree
(`/data/torrents/<type>`, hardlink-friendly and outside every library root).
`monarch-init` reconciles both — it corrects a drifted path, creates a missing
category, removes one the manifest does not name, and PUTs the *arr's corrected
download client — but only while it runs, so a deployment that was configured by
hand before that survives until one of these does the same on a live host:

```bash
python3 scripts/arr-download-categories.py --check   # exit 2 = drift, 1 = not reachable
python3 scripts/arr-download-categories.py           # correct it (restarts nothing)
```

Run it after changing a category in the WebUI by hand, and after a host was
brought up from an older checkout. `scripts/drift-check.sh` runs the `--check`,
compares the category **paths** (not just the names - a right name at a wrong path
is exactly the case above), and rejects a category the manifest does not carry.

#### qBittorrent still asks for a password after signing in through Cerulean
qBittorrent keeps a WebUI password of its own (`monarch-seed` writes
`MONARCH_USERNAME`/`MONARCH_PASSWORD` into it, and the drift check logs in with
it), so arriving through `qbittorrent-sso` used to meet a **second** login: the
one the app itself shows. The WebUI is published on loopback only and
`qbittorrent-sso` is the sole route to it, so `monarch-init` tells the app to
trust the subnet the gateway calls from (`bypass_auth_subnet_whitelist`) and it
stops asking — Cerulean is then the only credential either way.

The subnet is **discovered, not configured**: Docker allocates it
(`172.18.0.0/16` on this host) and init reads it off the interface that contains
`qbittorrent`'s address, so a host that builds the stack with a different pool is
not left with a whitelist that matches nothing. If `drift-check` reports
`the WebUI does not trust the SSO gateway's subnet`, re-run `monarch-init`
(`configure_qbittorrent` sets it) — an emptied whitelist is otherwise silent, and
the app simply starts asking again.

#### The dashboard (Homarr) shows a dead link, a duplicate, or the wrong layout
Homarr v1 keeps boards in SQLite (`/docker/appdata/homarr/appdata/db/db.sqlite`),
and `scripts/seed-homarr-board.py` **is** the design: the `DESIGN` list in it
names the sections and the order of the tiles inside them, and re-running the
script is how a change reaches the board.

```bash
python3 scripts/seed-homarr-board.py            # apply the design to every board
python3 scripts/seed-homarr-board.py --help     # (the module docstring is the reference)
```

It is safe to run against a live Homarr (sqlite is WAL and the writes are one
transaction), and it converges rather than appends: sections the design does not
name are removed, tiles are re-placed at the designed x/y, and an app row the
design no longer carries is deleted **with its tile**. That last part is the
"dead link" repair — a tile pointing at a name with no DNS record 000s however
healthy its container is, so `DEAD` in the script lists the services that can
only fail (the retired `profiles: ["legacy"]` ones, and platforms whose public
names were never created in NPM).

Add a host to the board only after `getent hosts <name>` answers **on the
deployment host** — that is the difference between a tile and a dead link, and it
is how the `*.monarch.local` ones got in. Every board in the database is updated,
not just the first: a second board is what an operator gets after a teammate
saves their own, and a half-updated dashboard is worse than an unwritten one.

The board is a **directory, not a status page**: a tile is a name that resolves
and is routed, and the service behind it being down is a finding about that
service. As of 2026-09-18 two tiles answer **502** for that reason — `AthenIQ
Learn` and `AthenIQ Studio` point at `192.168.1.46:18080`, the Tutor (Open edX)
caddy AthenIQ ran there, and no host runs that stack today (no tutor containers
anywhere, the tutor data volumes are gone from `.46`; the repo declares
tutor-lms/cms/meilisearch/mysql/mongo/redis in `ips/groups/extras/1-primary.yml`
but not the caddy). They are kept deliberately: the links are right, and the day
the LMS is restored the tiles are already there. Verified with
`curl -o /dev/null -w '%{http_code}' -k <url>` from the deployment host, which is
how every tile was checked before the redesign.

#### DNS check
`sudo docker exec -it radarr cat /etc/resolv.conf` — the stack pins
Cloudflare DNS (1.1.1.1 / 1.0.0.1).

#### The Jellyfin sign-in form answers 500 for every password
See [When a Cerulean identity cannot sign in](#when-a-cerulean-identity-cannot-sign-in-http-500-from-the-login-form)
— three pieces have to agree with Authentik (the outpost's API token, the bind
credential, and one copy of the LDAP plugin), and `python3 scripts/verify-ldap.py`
tells them apart (`exit 1` unreachable, `2` bind refused, `3` search found
nobody). `scripts/drift-check.sh` runs it, so the failure is reported before a
subscriber finds it.

The plugin *build* is `scripts/jellyfin-plugin-pin.py`'s job now, including the
two-copies case: it counts the folders carrying each assembly, so v23 installed
beside v24 is reported by name rather than by symptom.

**A bind that gets no reply is not a wrong credential.** The outpost logs
`took-ms: 3316` for a bind against the Cerulean Authentik, and `verify-ldap.py`
used to wait 3 seconds — so a reply that was merely late was reported as "the bind
token has drifted", and the fix it printed sent an operator to rotate a working
secret. The wait is now well past the observed latency (15s, `--timeout`), a
silent attempt is retried once (`--attempts`), and the two cases come back
differently: *no reply* is `exit 1` (unreachable — the outpost is slow or
restarting), while a *result code* is `exit 2`.

`drift-check` fails on a result code, and on **no reply from an outpost that has
been up past its own start period** (`DRIFT_LDAP_GRACE_SEC`, 90s). That second
half is new: *no reply* was a note unconditionally until 2026-09-18, and the cost
was measured — the outpost ran 45 minutes with its API token rejected (container
log `403 Forbidden (Token invalid/expired)`, `/ldap healthcheck` failing 541
times, port 3389 never opened), every Cerulean identity got HTTP 500 from
Jellyfin's login form, and the drift run still said *all live-stack invariants
OK*. A container that is starting, or was restarted seconds ago, is still a note;
one that is up, past the grace window, and not serving is a finding, and the
message names the repair:

```bash
docker compose up -d --force-recreate authentik-ldap
```

Recreating is what fixes it: `monarch-init` pins the outpost's token to
`AUTHENTIK_LDAP_TOKEN`, but a process that started against the previous one keeps
failing on its own (the retry backoff grows to minutes and it never re-reads the
environment). `MONARCH_LDAP_PROBE` overrides the probe command, which is how the
note and fail paths are tested without breaking a working outpost.

#### Hardlinks check
Find the same file in `/data/torrents` and `/data/media` and compare inodes:
`ls -i /data/media/movies/<your video>` vs
`ls -i /data/torrents/movies/<your video>`. If they differ, check the
read/write permissions on source/destination (see Radarr/Sonarr logs).

#### Files do not move from torrents to media folder
Check Activity -> Queue for "Downloaded - Unable to Import Automatically",
click Manual Import, confirm the correct movie, and import.

#### FlareSolverr
The `flaresolverr` container is already in the stack and `monarch-init`
registers a **FlareSolverr proxy** in Prowlarr automatically (tagged
`cloudflare`). To use it, tag an indexer `cloudflare` in Prowlarr (Settings
> Indexers > edit indexer > Tags) — indexers without the tag are never
routed through the proxy. To change its settings manually: Prowlarr >
Settings > Indexers > Indexer Proxies > edit **FlareSolverr**.

#### Jellyfin hardware acceleration
Add to the `jellyfin` service:

```yaml
    devices:
      - /dev/dri:/dev/dri
```

#### SABnzbd Usenet client
The `sabnzbd` service is already in the stack on host port 8082 (so it does
not clash with qBittorrent on 8080), published from `SABNZBD_PORT` — the same
variable `scripts/npm-hosts.conf` forwards to, so moving the port moves both.
Use the TRASH-guide folder structure and



### Nightly disk cleanup

`scripts/docker-cleanup.sh` (mirrored from ips, canonical there) runs nightly at
04:17 via `/etc/cron.d/docker-cleanup`: build cache (2 GB kept), dangling and
unreferenced images, containers exited for more than a day, and container logs
over 50 MB (trimmed to 10 MB). Volumes are never touched. Run it manually with
`DRY_RUN=1 scripts/docker-cleanup.sh` to preview.
