# Monarch — Mission Alignment

> **Mission:** the ecosystem's **MediaOps** platform — a self-hosted, Netflix-style
> streaming experience with full ownership of content, infrastructure and user
> data. Monarch **is not** the identity provider, the secrets platform, or the
> storage platform; it consumes those.

This page scores Monarch against that mission. Every line is checkable in this
repo — the commands are at the bottom. It exists so a gap is a **known,
prioritised** gap rather than an assumption.

## 1. Role boundaries — on track

| Layer | Owner | Monarch's side of the contract | Status |
|-------|-------|-------------------------------|--------|
| Identity | **Authentik** (Cerulean core) | LDAP outpost + Jellyfin LDAP-Auth for streaming logins; embedded-outpost `auth_request` gate (`fa`) for the ten hosts with no OIDC of their own (media apps, the Jellyseerr alias and the NPM admin UI); `paid_users` group = access, `jellyfin_admins` = admins; the sign-in host is never gated | **Done** — no app has a second, local login prompt left |
| Secrets | **Infisical** | `compose.infisical.yml` + `scripts/infisical-setup.sh` import the stack's secrets, and `infisical-setup.py --render/--check` makes `.env` **derived** from the store (rotating a secret in Infisical is the fix; `--check` exits 1 on drift) | **Partial** — the profile is opt-in and a host that has not provisioned the workspace still carries live values in `.env` |
| Storage | **ONYX** | Content stays on `/data` volumes; Monarch owns metadata only | **Not started** — roadmap, see §4 |
| Revenue | **Magnate** | Billing, plans, coupons, trials and subscriptions live in Magnate — `subscribe.innotel.us` is the **portal landing page and the subscribing page**. Monarch consumes the entitlement decision (`GET /api/entitlements?plan=&user=`) and the `paid_users` group | **Done** — `scripts/magnate-entitlements.py` maps the plan to Jellyfin playback policy (streams, quality cap, downloads) and `drift-check` re-verifies it; profiles stay advisory (see §4) |
| Trust | **Cerulean** | Wildcard certs via the NPM edge (DNS challenge); DNS records written through Cerulean's **Technitium HTTP API** (no TSIG/nsupdate) | **Done** — new subdomains get A records automatically; a name that already resolves (Monarch's CNAMEs) is left as is |
| Edge | **NPM Edge** | `scripts/npm-hosts.conf` + `npm-proxy-hosts.py` render every proxy host, then `check-proxy-ports.py` proves each row points at a port compose publishes | **Done** — 14/14 rows match live, the wildcard cert (`*.monarch.innotel.us`) is assigned to every host so HTTPS/SNI serves, the apex board is live, and the map is CI- and drift-checked |

## 2. Product capabilities — on track

| Mission capability | Delivered by | Status |
|---|---|---|
| Video streaming, collections, libraries | Jellyfin (pin: v12.0.0 digest) | **Done** |
| User profiles, watch history, Continue Watching | Native Jellyfin profiles, backed by Authentik identities | **Done** |
| Live TV | Native M3U tuner + XMLTV guide (`iptv` container, `/opt/epg/channels.xml`) | **Done** — no TVHeadend/NextPVR needed |
| AI recommendations + smart search | `monarch-recs` (`/api/recommendations`, `/api/trending`, `/api/search`, optional LLM blurbs) | **Done** — fully local TF‑IDF, no external AI required |
| Analytics | `monarch-health` (missing/duplicate/orphan detection, per-library stats, disk, most-played) | **Done** |
| Media discovery | Jellyfin metadata agents + Prowlarr indexers flowing to all `*arr` apps | **Done** |
| Subscription tiers / plans | Delegated to Magnate → `subscribe.innotel.us` (choose, pay, manage) → Authentik `paid_users` → Jellyfin policy per `scripts/magnate-tiers.json` | **Done** — tiers gate *access* and now *playback* (stream limit, quality cap, downloads); profiles advisory |
| Trailer/extra, multi-audio, subtitle support | Jellyfin + Bazarr | **Done** (no in-repo HLS/DASH plumbing — Jellyfin owns playback) |
| DVR, recording, time shift, live pause | — | **Not started** (§4) |
| Kids profiles / PIN / parental controls | Jellyfin's built-in parental ratings + profile PIN | **Available, not wired by init** (§4) |
| Adaptive streaming (HLS/DASH), 4K/HDR | Jellyfin transcoding (hardware accel is a documented opt-in) | **By design** — no custom player or packager in this repo |

## 3. Delivery surfaces — small, intentional difference

The product brief lists `app`, `api`, `admin`, `stream` and `tv`. Monarch runs
`@`/`app` (Homarr), `auth`, `tv`, `admin`, `req` and the ten gated hosts.

* `api` / `stream` are **not** separate hosts: there is no Monarch-owned API
  service (the internal APIs `monarch-recs` / `monarch-health` are deliberately
  not exposed), and the streaming surface is Jellyfin, published through the
  Magnate edge at `media.magnate.innotel.us`.
* `admin` is the NPM admin UI — now SSO-gated like the media apps.
* The **portal** is not under `monarch.*` at all: `subscribe.innotel.us`
  (Magnate) is the ecosystem landing page *and* the subscribing page, because
  billing is Magnate's and Monarch must never take a payment. Monarch's own
  `subscribe.`/`api.` hosts were retired in its favour; every "Subscribe" link
  in this stack (the Homarr tile and the landing page) points at `SUBSCRIBE_URL`,
  and CI fails if either drops it.

## 4. Known gaps, in priority order

1. **ONYX as the authoritative store.** Media lives on local `/data`; ONYX is
   referenced only as the roadmap target. "Done" means the libraries mount the
   ONYX-backed store, the path contracts in `monarch-init` are unchanged, and
   `docs/operations.md` records the migration the way the `.72` → `.46` move is
   recorded.
2. **Infisical-first secrets.** `.env` is still the source of truth on a host
   that has not provisioned the workspace. "Done" means
   `scripts/infisical-setup.sh` is the documented path for every secret
   (including `NPM_*`, `AUTHENTIK_*`, `AUTH_OIDC_*`) and `.env` only carries the
   bootstrap keys.
3. **Entitlement depth.** The plan → policy mapping ships, but two edges remain:
   the **stream limit** is applied only where the Jellyfin build exposes
   `MaxActiveSessions`, and **profile limits** stay advisory because Jellyfin has
   no per-user profile cap. "Done" means a build-pinned field or a proxy-level
   session limit, and profile counts enforced by Magnate/Authentik.
4. **Family features.** Jellyfin supports parental ratings and profile PINs, but
   `monarch-init` never sets them, so they are manual. "Done" means init creates
   a `kids` group policy and the profiles the operator declares.
5. **DVR / recording.** Only meaningful with a tuner that can record; the stack
   ships M3U-only Live TV. "Done" means a documented tuner (HDHomeRun/Dispatcharr)
   profile plus recording paths under `/data`, and Jellyfin DVR wired by init.
6. **Role vocabulary.** The brief's roles (User, Creator, Moderator,
   Administrator, Organization Owner, Platform Admin) map today onto two
   Authentik groups. "Done" means the group set exists in the shared Authentik
   provisioning and the gate's `AUTHENTIK_FORWARD_GROUP` can name them.
7. **Observability.** No Prometheus/Grafana/Loki in this repo; `monarch-health`,
   `drift-check` (6-hourly, self-healing) and the Telegram alerts are the
   monitoring surface. Acceptable for the current scale — worth revisiting when
   the stream count justifies it.

## 5. Values

The mission's non-negotiables, checked one by one:

| Value | Where it is enforced |
|---|---|
| **Self-hosted, complete ownership** | Every service is a container in this repo; the only external dependency is Stripe Checkout (Magnate) and optional metadata/CDN providers you choose. Live/install ISO + offline bundle exist for air-gapped installs. |
| **Own your user data** | Profiles, watch history, playback state and analytics live in `/data`/`/docker/appdata`; nothing is sent to a third-party recommendation service (`monarch-recs` is local TF‑IDF). |
| **Identity is Authentik's, not Monarch's** | No second user store: `paid_users` gates access, LDAP gates streaming login, and no app keeps a competing password form. |
| **Secrets are Infisical's** | `.env` is derived (`--render`) and verified (`--check`); nothing writes secrets back. |
| **Revenue is Magnate's** | `subscribe.innotel.us` is the landing and subscribing page; Monarch never touches a card, a price, or an invoice. |
| **Storage is ONYX's** | Content paths are designed to move behind ONYX without changing `monarch-init` contracts (gap 1). |
| **Every service wired on first boot** | `monarch-seed` + `monarch-init` are idempotent single sources of truth; `drift-check` repairs drift automatically. |
| **Everything is verifiable** | `check-proxy-ports.py`, `npm-proxy-hosts.py --check`, `magnate-entitlements.py --check`, `infisical-setup.py --check`, `drift-check.sh` — each claim on this page has a command. |

## 6. Deliberately out of scope here

The brief also describes Kubernetes manifests, PostgreSQL/Redis/Meilisearch and
an OpenAPI surface. This repo is a **compose** platform on a single host, and
Jellyfin already owns the database, search and streaming concerns:

* **Kubernetes / autoscaling / StatefulSets** — the platform stack's convention
  is Docker Compose per product; HA would be an ecosystem-wide decision.
* **PostgreSQL / Redis / Meilisearch** — no Monarch-owned data store: state lives
  in Jellyfin/`*arr` appdata and `monarch-health` writes JSON. Adding a database
  would create the split-brain the role boundaries exist to prevent.
* **OpenAPI** — the only HTTP surfaces are the two internal Python APIs and the
  third-party app APIs; nothing external consumes a Monarch-owned schema yet.

## 7. Verifying this page

```bash
python3 scripts/check-proxy-ports.py             # edge map ↔ compose publishes
python3 scripts/npm-proxy-hosts.py --check       # live NPM ↔ edge map (needs creds)
python3 scripts/magnate-entitlements.py --check  # Magnate plan → Jellyfin policy
python3 scripts/infisical-setup.py --check       # .env derived from the store?
bash scripts/drift-check.sh                     # the running stack, read-only
git grep -n "subscribe.innotel.us"              # the portal entry points (values table)
git grep -niE "onyx"                            # today: docs/landing only (gap 1)
```
