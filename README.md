<div align="center">

# 🦋 Monarch — Media Platform

**Your own Netflix-grade streaming platform — AI recommendations, live TV, and the full *arr automation stack, self-hosted.**

Monarch rebrands the classic *arr stack as one premium media platform: **Jellyfin**
streaming with user profiles, **AI-powered recommendations and smart search**, library
**health analytics**, **Cerulean Cerulean Authentik-first** authentication with LDAP login gating, the whole
**Sonarr/Radarr/Lidarr/Prowlarr** automation family wired automatically, **live TV** with a
native M3U tuner and XMLTV guide, and one-command installs — including a bootable
**live/install ISO** and an **offline bundle** for air-gapped deployment.

[![Release](https://github.com/innotelinc/monarch/actions/workflows/release.yml/badge.svg)](https://github.com/innotelinc/monarch/actions/workflows/release.yml)
[![Fresh install check](https://github.com/innotelinc/monarch/actions/workflows/fresh-install.yml/badge.svg)](https://github.com/innotelinc/monarch/actions/workflows/fresh-install.yml)
[![Latest release](https://img.shields.io/github/v/release/innotelinc/monarch?color=8b5cf6)](https://innotelinc.github.io/monarch/releases)

*One script, and your library gets a brain — with wings.*

</div>

> **About Monarch** — a premium, self-hosted media platform that turns a plain server into a
> streaming service with AI recommendations, live TV, per-user profiles, and fully automated
> media acquisition. Jellyfin is the streaming core; Cerulean Authentik owns the users; the *arr stack
> finds, downloads, and subtitles the content. **Landing page:** [innotelinc.github.io/monarch](https://innotelinc.github.io/monarch)

**Non-negotiables:** 100% self-hosted · Docker Compose stack · Cerulean Authentik-first
authentication (no separate user stores) · every service wired automatically on first boot ·
release artifacts on every tagged release.

---

## ✨ Features

| | | |
|---|---|---|
| 📺 **Streaming** | Jellyfin (movies, TV, music, live TV) with user profiles, watch history & SyncPlay watch parties | 
| 🤖 **AI recommendations** | `monarch-recs`: content-based picks + smart search over your library — fully local, no external AI required | 
| 🩺 **Library health** | `monarch-health`: missing/duplicate/orphan detection, per-library stats, disk usage, recently-added & most-played | 
| 🔐 **Cerulean Authentik SSO** | Users & passwords live in Cerulean Authentik; Jellyfin logins resolve via the LDAP outpost — disable a user and their login dies instantly | 
| 🎯 **Media automation** | Sonarr / Radarr / Lidarr / Whisparr / Prowlarr / qBittorrent / Bazarr, all configured and cross-wired by `monarch-init` | 
| 📡 **Live TV** | Native M3U tuner + XMLTV guide (iptv-org) — zero TVHeadend setup; bring your own playlist via `LIVETV_M3U_URL` | 
| 🌐 **Proxy & SSL** | Nginx Proxy Manager auto-configured via API with a wildcard Let's Encrypt cert (DNS challenge) | 
| 🛡️ **Self-healing** | `monarch-drift-check` verifies the running stack hourly (systemd timer) and auto-repairs drift by re-running init | 
| 💾 **Delivery** | One-command installer, offline bundle, bootable live/install ISO — install with or without internet | 
| 💳 **Subscriptions** | Magnate is the source billing platform: Stripe checkout → Cerulean Authentik `paid_users` group → access granted | 

## 🚀 Quick start (recommended)

```bash
git clone https://innotelinc.github.io/monarch
cd monarch
cp .env.sample .env    # edit MONARCH_USERNAME / MONARCH_PASSWORD etc.
./setup.sh
```

`setup.sh` is idempotent and does five things:

1. Creates `.env` from `.env.sample` when missing (never overwrites).
2. Installs the stack via `scripts/install-monarch.sh` (Docker, `/data` layout,
   `monarch.service` systemd unit, `docker compose up -d`). Set `MONARCH_TARGET`
   (e.g. `/opt/monarch`) to install elsewhere.
3. Seeds the **Homarr landing board** (via `scripts/seed-homarr-board.py`) so the apex
   domain shows the Monarch main interface with the full platform tile set — the media
   stack plus **Cerulean**, **Capstone**, Magnate, AthenIQ, Zeus, Signara, Onyx,
   Rizzaura, Atlas and Oasis (customized boards are left untouched).
4. Auto-configures **Nginx Proxy Manager** via its API — proxy hosts for the apex and
   every subdomain, plus a **wildcard Let's Encrypt certificate** via DNS challenge.
5. Ensures the **Magnate Stripe webhook** endpoint exists when `STRIPE_SECRET_KEY` is set.

Re-run `./setup.sh` any time you change `.env` or `scripts/npm-hosts.conf` — proxy hosts are
reconciled in place and the certificate is reused.

**DNS prerequisite (one-time):** point a wildcard + apex record at this host's public IP
(`*.monarch.innotel.us` and `monarch.innotel.us`), or set the `DNS_TSIG_*` options so the
NPM script writes both A records itself. The old `ARR_USERNAME`/`ARR_PASSWORD` variables are
`MONARCH_USERNAME`/`MONARCH_PASSWORD` now — regenerate an existing `.env` from `.env.sample`.

Everything after boot is **wired for you**: one-shot `monarch-seed` and `monarch-init`
containers complete the Jellyfin first-run wizard, install the LDAP-Auth plugin, add media
libraries, configure every *arr app with the qBittorrent client, register Prowlarr apps,
connect Bazarr, and initialize Jellyseerr — all idempotent, all with your shared credentials.

```bash
sudo docker logs monarch-init                       # what the automation did
sudo cat /docker/appdata/init/status.json           # per-service result + issues
```

## 🗺️ What you get

Main entry points (full table in [docs/operations.md](docs/operations.md)):

| Subdomain | Service | What it is |
|---|---|---|
| `monarch.<domain>` (apex) | Homarr dashboard | Main login / landing board |
| `media.<domain>` | Jellyfin | Streaming — movies, TV, music, live TV |
| `auth.<domain>` | Authentik | SSO, user management, `paid_users` access group |
| `req.<domain>` | Jellyseerr | Request portal (connected to Radarr/Sonarr) |
| `tv.<domain>` | IPTV guide | XMLTV EPG for Jellyfin Live TV |
| `admin.<domain>` | NPM admin | Reverse proxy + wildcard SSL |
| localhost:8002 / 8003 | monarch-recs / monarch-health | AI recommendations + health analytics (internal APIs) |

Credentials are shared: `MONARCH_USERNAME` / `MONARCH_PASSWORD` from `.env` are applied to
every service that requires a login (Jellyfin, Jellyseerr, all *arr apps, qBittorrent, the
Authentik bootstrap admin…). Subscriber access flows through **Magnate** billing:
checkout → `paid_users` group → LDAP bind succeeds → Jellyfin login works; cancel → user
inactive → login blocked.

## 📚 Documentation

| Document | Covers |
|---|---|
| [docs/operations.md](docs/operations.md) | Shared credentials, first-boot wiring, services & ports, subdomains/NPM, Authentik & LDAP, AI recommendations, health analytics, drift check, live TV, troubleshooting |
| [docs/deployment.md](docs/deployment.md) | Installer & live USB, fresh-install check, offline bundle, building a release, manual setup |

## 📦 Releases & offline install

Every `v*` tag triggers the [release workflow](.github/workflows/release.yml): first-party
images (`monarch-recs`, `monarch-health`) publish to GHCR, and the GitHub Release attaches
the deployment payload, source bundle, checksums, the split **docker image bundle**, and a
bootable **live/install ISO** (`monarch-live-amd64.iso`, BIOS + UEFI).

```bash
sudo dd if=dist/live-usb/monarch-live-amd64.iso of=/dev/sdX bs=4M status=progress   # write the ISO
./scripts/fetch-offline-bundle.sh                                                    # or grab a release's offline bundle
```

For a fully **offline** install, copy the offline bundle to a FAT32 partition of the USB
stick — the installer stages it automatically. Full walkthroughs in
[docs/deployment.md](docs/deployment.md).

## 🧱 Repo layout

```
docker-compose.yml          # the whole stack
init/                       # monarch-init + monarch-seed (first-boot wiring, single source of truth)
scripts/                    # setup.sh, install-monarch.sh, npm-proxy-hosts.py, drift-check.sh,
                            # fresh-install-check.sh, stripe-webhooks.sh, seed-homarr-board.py,
                            # ISO/offline builders
homarr/                     # Homarr board seed, legacy v0 format (board.default.json);
                            # v1 boards are seeded into sqlite by scripts/seed-homarr-board.py
.github/workflows/          # release, fresh-install check, full-stack drift CI
docs/                       # operations + deployment references
```

## 🔒 Security

`.env` is gitignored — credentials never get committed. The stack pins Cloudflare DNS for
its containers, and `monarch-drift-check` watches disk usage, crash-looping containers, and
stale images. No third-party author attribution is included anywhere in this project.

---

*Monarch — Media Platform. Self-hosted streaming with wings.*

## 🏛️ Platform stack

Monarch is the ecosystem's **MediaOps** platform — streaming, media libraries, recommendations, and live TV in the
[**Innotel Platform Stack**](https://github.com/innotelinc/innotel-platform-stack) — the
canonical single-responsibility architecture where Authentik owns identity, Infisical owns
secrets, Cerulean owns trust, ONYX owns storage, Magnate owns revenue, NPM Edge owns the edge, and every other
platform is a business function that consumes them. See
[docs/stack.md](docs/stack.md) for this platform's owns/consumes boundaries and its
Infisical secret setup.
