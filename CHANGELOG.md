# Changelog

Notable changes to Monarch, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); releases are tagged
`v1.<n>` (see [Earlier releases](#earlier-releases)).

Monarch had no changelog before v1.22, so everything older is summarised from its
release tag - `git log <tag>` still has the full detail. From v1.22 on, entries
are written by hand and describe the behaviour change, not the commits.

## [Unreleased]

### Added

- **Seerr's Owner can belong to the account people actually sign in as.**
  Seerr decides its Owner by row id (`user.id === 1` in
  `server/routes/user/index.ts`: only row 1 may grant admin, and only row 1 may
  modify row 1), so an install whose first account was the break-glass Jellyfin
  admin had an Owner nobody uses — and `main.localLogin: false` makes that
  account unreachable when the gateway is the only way in. `scripts/seerr-owner.py`
  hands it over by **swapping** the two accounts (identity, permissions, every
  foreign key onto `user.id`, and the live sessions), so the displaced admin keeps
  its row, its data and its admin bit. The account is named once in the invariants
  manifest (`jellyseerr.owner`, `MONARCH_SEERR_OWNER`) and `drift-check` now fails
  when Seerr is owned by somebody else.

### Fixed

- **Prowlarr's app tests pass, and its indexers now reach all four \*arrs.**
  Every app connection test answered `ProwlarrUrl: Prowlarr URL is invalid,
  <app> cannot connect to Prowlarr`, and Sonarr, Radarr, Lidarr and Whisparr
  each showed an empty indexer list - while the Apps page showed all four
  registered. The cause is a Host-header guard, not a URL: an \*arr answers HTTP
  400 to any name it was not told about, and the allowlist named only the edge
  domain, so Prowlarr calling `http://sonarr:8989` (and Sonarr calling back at
  `http://prowlarr:9696`) was refused *before authentication ran*. The list is
  now `init/arr-allowlist.txt`, read by BOTH `monarch-init` (which writes it into
  all five apps) and `scripts/arr-allowed-hosts.py`, which applies it from the
  host and **restarts what it changed** - the setting is only read at startup,
  which is why "applied" on its own had left the apps still refusing. Verified
  live: all four app tests return 200.
- **The two indexers Prowlarr had were both Cloudflare-blocked and neither
  carried the `cloudflare` tag**, so FlareSolverr was configured and unused and
  every search came back empty from an indexer the UI showed as enabled. Both
  are tagged now (Prowlarr's own test passes through the proxy), and the stack
  has 72 indexers instead of 2 - Sonarr 22, Radarr 12, Lidarr 13, Whisparr 6
  after a sync, the counts differing by category as they should.
- **The Homarr dashboard was a pile of appended tiles with dead links in it.**
  Twelve tiles 000'd (no DNS record at all): the retired services
  (`profiles: ["legacy"]`) still carried `*.monarch.local` links, Requestrr's
  console pointed at a private LAN address, and Onyx, Oasis, Rizzaura and
  Transmission were tiles for names that were never created in NPM. `scripts/
  seed-homarr-board.py` is now a design rather than an appender: named sections
  (`Now`, `Playback`, `Media activity`, `Libraries`, `Indexers & downloads`,
  `Business`, `Platform`, `Infrastructure`), every tile placed at a designed x/y,
  anything the design does not name removed - including the app row, so a dead
  entry cannot linger in the picker - and every board in the database updated,
  not just the first. The live board went from 44 scattered tiles to 40 in eight
  sections with no duplicates and no orphans.
- **Downloads landed in the library, and every \*arr warned about it.** Sonarr,
  Radarr, Lidarr and Whisparr each reported "Download client qBittorrent places
  downloads in the root folder /data/media/<type>", and `lidarr`'s category in
  qBittorrent did exactly that - an unfinished album was written straight into
  the music library for Jellyfin to scan. Two settings were wrong at once:
  Servarr spells the download client's category after the media type
  (`tvCategory`/`movieCategory`/`musicCategory`) and **has no plain `category`
  field**, so the client was created with `category` set, no field matched, the
  value was dropped, and every app kept whatever name it had been given by hand
  (`tv-sonarr`, `radarr`, `lidarr`, `tv-whisparr`). The categories those names
  referred to had been created by hand too - pointing at `/data/media/<type>` -
  while the four the manifest names (`tv`/`movies`/`music`/`xxx`, saving into
  `/data/torrents/<type>`) sat unused. `monarch-init` now reconciles both halves
  (correcting a path, creating a missing category, removing names the manifest
  does not carry, and PUTing the app's corrected client), the categories derive
  from `MONARCH_APPS` so the name an app is told and the name that exists cannot
  be two strings, and the manifest carries the name -> path map so
  `scripts/arr-download-categories.py` can apply and verify the same thing on a
  live host - `drift-check` compares the **paths**, which nothing did before.
- **qBittorrent asked for its own password after Cerulean.** Arriving through
  `qbittorrent-sso` worked and then presented the app's own login form, so the
  identity that opened the stack was asked to sign in again with a credential
  it does not have. The WebUI is published on loopback only and the gateway is
  its only route, so `monarch-init` now tells qBittorrent to trust the subnet the
  gateway calls from (`bypass_auth_subnet_whitelist`) - discovered from the
  interface that shares that subnet, not a literal - and Cerulean is the only
  credential. `drift-check` asserts the whitelist is still in place, because
  an emptied one is silent: the app simply starts asking again.
- **An \*arr holding no indexers while Prowlarr holds dozens: the missing step
  was the sync, and nothing checked it.** Filling Prowlarr's list does not put
  anything into an app - Prowlarr only offers an indexer to each app during an
  application sync - so Prowlarr can be full, all four app tests green, and
  Sonarr, Radarr, Lidarr and Whisparr each holding none of it. That is the state
  that reads as "Prowlarr is not registering its indexers". `scripts/arr-sync.py`
  now performs the sync and *counts what arrived*: an app has received when it
  holds an indexer whose base URL is Prowlarr's own proxy path
  (`<prowlarrUrl>/<id>/`). Fewer than Prowlarr has is the supported state -
  Prowlarr deliberately will not sync an indexer that returns no results in that
  app's categories - so only "none received", and a failing app test, are
  findings. `scripts/drift-check.sh` fails on both, which nothing did before.
- **The `cloudflare` tag is what makes FlareSolverr do anything, and 15 of the
  indexers already in Prowlarr did not have it.** They were present, enabled, and
  blocked - the state that makes a search look empty rather than unconfigured -
  while the stack's own FlareSolverr was running and configured as an indexer
  proxy. `scripts/prowlarr-indexers.py --repair` is now a first-class step: it
  tags only the indexers a second attempt through the proxy fixes, so repairing
  fifteen does not mean re-testing six hundred definitions, and it refuses to run
  at all when Prowlarr holds no indexer proxy, because then the tag would be
  written, the indexer would still be blocked, and the run would report a repair
  that changed nothing. Verified live: 17 indexers tagged, and every one of them
  answers Prowlarr's own test through the proxy.
- **`prowlarr-indexers.py` re-tested every indexer it already held, and skipped
  one it did not.** The dedup compared the definition's *display name*
  (`BTdirectory`) against Prowlarr's stored `definitionName`, which for a
  Cardigann definition is its id (`btdirectory`) - so all 74 held indexers were
  re-offered, Prowlarr answered each with `Should be unique`, and the report
  called present indexers failed candidates while a definition whose label
  collided with another slipped through as "already there". Uniqueness is on the
  definition id, and that is what is compared now.
- **`--privacy public,semiPrivate` never matched a `semiPrivate` definition.**
  The classes on the command line were compared to the definitions' lowercased
  `privacy` value without normalising case, so the invocation this script's own
  usage block and `docs/operations.md` both show ran the narrower set without
  saying so: 64 definitions were never attempted. Live, the same flag now
  reports `152 definition(s) marked public, semiprivate; 74 already in Prowlarr,
  78 to try` instead of `88 ... 14 to try`.
- **A Cerulean identity can sign in again - in Jellyfin, and therefore in Seerr
  and on the TV clients.** Three unrelated faults had stacked up, and all three
  read as "Jellyfin is broken" from a login form that answers HTTP 500 for a
  correct password exactly as it does for a wrong one:

  1. **Two copies of the LDAP plugin** were installed (`LDAP-Auth` v23 and
     `LDAP Authentication_24.0.0.0` v24). Jellyfin loads both, the plugin's own
     configuration type is cast across two load contexts, and every
     authentication threw `InvalidCastException`. The older folder is retired
     (`.superseded-2026-09-18`) and `drift-check` now counts them.
  2. **The outpost's token was refused** - `403 Forbidden (Token invalid/expired)`
     - so `authentik-ldap` never started its LDAP listener and Jellyfin's bind
     failed with `Connection refused`. Regenerated, and pinned in both places.
  3. **The store held placeholder text.** `cerulean/data/monarch` had
     `ak-ldap-outpost-2026    # outpost API token (monarch stack)` - quotes,
     comment and all - for both `AUTHENTIK_LDAP_TOKEN` and
     `AUTHENTIK_LDAP_BIND_TOKEN`, migrated there from a `.env` whose lines
     carried inline comments, so `.env`, Vault, the bind user's password and
     Jellyfin's `LDAP-Auth.xml` all agreed on a value no token had ever been
     minted from. Both secrets are regenerated and written to all four places,
     and Jellyfin's plugin config is rewritten from the same template
     `monarch-init` uses.

  `scripts/drift-check.sh` now runs `scripts/verify-ldap.py` (written for exactly
  this failure and never wired to anything) as part of its sign-in posture.
- **`verify-ldap.py` no longer blames the credential for a late reply.** The
  outpost logs `took-ms: 3316` for a bind against the Cerulean Authentik while
  the script waited 3 seconds, so a working bind token was reported as
  "has drifted from the bind user's password" and the printed fix sent an
  operator to rotate a secret that was fine. The wait is 15s now, a completely
  silent attempt is retried once, the wait ends when the awaited message arrives
  (not on an idle timer, so a fast outpost is read in milliseconds), and the two
  cases report differently: **no reply is `exit 1` (unreachable)**, a result code
  is `exit 2` (the credential). `drift-check` treats the first as a note and
  still fails on the second.

### Added

- **`scripts/prowlarr-indexers.py`** - adds every Prowlarr definition marked
  `public` that passes Prowlarr's own test (retrying a Cloudflare failure
  through FlareSolverr and tagging the indexer when that is what made it work),
  repairs the ones already in the list the same way, and adds nothing it has not
  just tested. `--check` tests the indexers already there; `--check --offline`
  is the cheap question the drift timer asks, because testing thirty trackers
  every six hours is a way to earn a ban. Definitions marked `private` are never
  attempted - they want an account that does not exist here.
- **`scripts/arr-allowed-hosts.py`** - applies `init/arr-allowlist.txt` to all
  five apps and restarts the ones it changed (`--no-restart` to batch that,
  `--check` for the drift timer, exit 2 on drift and 1 when the apps are not on
  this host). `monarch-init` reads the same file, so the container side and the
  host side cannot disagree about what a name is.

### Changed

- **`media.innotel.us`, `media.magnate.innotel.us`, `req.innotel.us` and
  `req.monarch.innotel.us` publish the app's own sign-in page instead of gating
  it.** Gating the page asked for a browser OIDC flow from clients that do not
  have one: a TV client opening the Jellyfin login page in a webview landed on
  Authentik instead of the form, and Quick Connect had no page to enter its code
  on. The API half was already passed through for that reason; the page now is
  too. **Cerulean Authentik is still the only credential store**, by the apps'
  own wiring - Jellyfin's login page offers its SSO button and native clients
  bind against the LDAP outpost, Seerr keeps no password of its own and signs in
  with the Jellyfin account. `scripts/verify-sso.py` asserts both directions:
  the gated names still redirect to the IdP, these four answer 200 with the app's
  own page and never redirect to it.
- **`scripts/jellyfin-oidc-sso.py`** checks the login page's SSO button end to
  end - the plugin's config (an enabled provider on the Cerulean issuer with this
  zone's client id) and the callback registered on the `monarch-media` provider -
  since both halves fail silently from the login page. Run by `drift-check`.
- **Jellyfin's plugins are pinned, and `monarch-init` installs them.**
  `init/jellyfin-plugins.json` records, per plugin, the release and the sha256 of
  both the zip and the assembly inside it, so "the plugin" means one build to a
  fresh install, a repair and the drift check alike. It covers the OIDC plugin
  (`Ezeqielle/jellyfin-plugin-oidc` v1.0.10 — the Cerulean Authentik button;
  Jellyfin's catalog does not carry it and its `meta.json` ships no `sourceUrl`,
  which is why it was hand-installed before, so a rebuilt host came back with a
  login form and no SSO) and the LDAP plugin (`jellyfin/jellyfin-plugin-ldapauth`
  v24 — the credential store behind that page, whose install path used to fetch
  "GitHub's latest release"). `scripts/jellyfin-plugin-pin.py
  --check|--status|--install` judges and repairs both, and **counts the folders
  carrying each assembly**: a second non-retired copy of an auth plugin is not
  cosmetic, it is the v23-beside-v24 case above. `init/init.py` also writes the
  OIDC plugin's config (provider, `MONARCH_SSO_*` client id/secret, and the
  `paid_users` / `jellyfin_admins` role mappings) and no longer falls back to an
  unpinned LDAP release.
- **Local Jellyfin accounts the deployment keeps are declared, not guessed.**
  Publishing Jellyfin's own sign-in page left native clients with a second
  password in Jellyfin's store, and that account is indistinguishable from a
  stray. `JELLYFIN_LOCAL_ACCOUNTS` (`.env`, comma-separated) names them beside
  `JELLYFIN_ADMIN_USER`: `jellyfin-login-methods.py` reports and disables every
  local account that is *not* declared, refuses to disable a declared one, and
  prints the count so the set cannot grow quietly. The names stay deployment
  state - they are not in this repo.
- **`MONARCH_SSO_REDIRECT_URIS` carries the two Jellyfin callback URIs**
  (`…/sso/OIDC/Callback/authentik`), because a provider refresh takes the list
  verbatim and a list without them drops the SSO button's callback.

## [v1.22] - 2026-09-13

### Added

- **Cerulean Authentik is the only login for the media apps.** The nine `*arr`
  apps, qBittorrent and SABnzbd sit behind the Authentik embedded outpost
  (`fa` in `scripts/npm-hosts.conf`, `nginx auth_request`); Bazarr moved from
  HTTP basic auth to the same gate. No app has a second local login left, which
  is what the mission's "no local authentication" boundary requires.
- **`subscribe.innotel.us` is the portal entry point.** The Homarr board's first
  tile, the landing page's primary call to action, `SUBSCRIBE_URL` in the env
  templates, and a CI step that fails if either entry point loses the link.
  Monarch hosts no pricing page and processes no payments - Magnate does.
- **`scripts/magnate-entitlements.py`** (+ `scripts/magnate-tiers.json`): maps
  Magnate's plan slugs (`basic`/`standard`/`premium`/`agents`/`magnate`) to
  Jellyfin policy - stream limit, bitrate ceiling, downloads - so tiers now
  change how a plan *plays*, not just whether it can sign in. `--check` runs in
  the drift check.
- **`scripts/jellyfin-admin-password.py`**: aligns Jellyfin's local admin
  password with `MONARCH_PASSWORD` through Jellyfin's own forgot-password flow,
  then mints/refreshes the durable admin API key. This closes the last
  credential that could silently drift from `.env`.
- **`scripts/check-proxy-ports.py`**: fails CI when a port in
  `npm-hosts.conf` does not match the compose publish (and vice versa), so a
  proxy host cannot point at a port nothing listens on.
- **`jellyfin-admin-password.py --check-apps`**, run by `drift-check`: reads the
  Jellyfin key that Jellyseerr and Homarr each hold and proves Jellyfin still
  accepts it. A rotation that stopped halfway leaves an app authenticating with
  a token Jellyfin has forgotten, and nothing else notices - the container is
  up, its own UI answers, and only its requests to Jellyfin fail.
- **`npm-proxy-hosts.py --prune`**: deletes the proxy hosts in this domain that
  `npm-hosts.conf` no longer lists. Scoped to `MONARCH_DOMAIN`, so the other
  products sharing the NPM are never touched.
- **DNS automation** now uses Cerulean's Technitium HTTP API
  (`TECHNITIUM_URL` + token) instead of the retired BIND/TSIG path, and leaves a
  name that already resolves alone (Monarch's hosts are CNAMEs to the apex).
- `infisical-setup.py --render` / `--check`: render the secret set into `.env`
  and verify it matches, so Infisical can be the source rather than a copy.

### Changed

- **Every Monarch proxy host now carries the wildcard certificate.** They were
  live with `certificate_id: 0`, so browsers failed the TLS handshake
  (`unrecognized name`) even though the hosts resolved. Existing certificate
  `*.monarch.innotel.us` is reused - no re-issuance.
- **Homarr is deployed.** The apex (`monarch.innotel.us`) and
  `app.monarch.innotel.us` forward to it, but no Homarr container existed on the
  host - the dashboard was dark while its proxy hosts were live.
- Edge ports can be written as `${VAR:-default}`, resolved from `.env`:
  `SABNZBD_PORT` now drives both the publish and the proxy host, so the two
  cannot drift apart.
- The SSO sign-in host can never be gated: a 401 there redirects to itself and
  would lock every gated host out. `npm-proxy-hosts.py` refuses it by
  construction and `--check` enforces it.
- `--check` treats a host that is live in this domain but missing from
  `npm-hosts.conf` as drift, not a footnote. That is how the retired host below
  stayed live unnoticed.

### Fixed

- `tv.monarch.innotel.us` forwarded to host port `3001` - the Zeus portal - so
  the IPTV guide's hostname served another product's UI. It now forwards to
  `3011`, which is where the guide is published (container `3000`).
- `admin.monarch.innotel.us` forwarded to container port `81` while the compose
  `npm` profile publishes `2081`, and was the last public admin surface without
  SSO. Now `2081` and gated (`NPM_FORWARD_AUTH_EXCLUDE=admin` is the way back in).
- Removed the retired `subscribe.monarch.innotel.us` proxy host, which forwarded
  to a port nothing listens on.
- The drift check was verifying three things wrongly and reporting a broken
  stack that was not broken: the `*arr` apps' auth method (init intentionally
  sets `external` so the Cerulean gate is the only login), the Jellyfin login
  header (`Authorization`, not `X-Emby-Authorization`) and the Bazarr auth
  invariant.
- `verify` step for the Jellyfin admin credential: a password change revokes
  every session token, so the exported token went stale silently and the checks
  that read it failed. The credential is now a durable API key.
- **The LDAP outpost was documented but never deployed.** `docker-compose.yml`
  carried the service comment, init provisioned provider/outpost/token into
  Authentik, and the Jellyfin plugin pointed at `authentik-ldap:3389` - but no
  container ever served it, so no LDAP login could ever succeed. The
  `authentik-ldap` service now exists (image version-matched to the Authentik
  server), and `monarch-init`'s LDAP chain completes end-to-end.
- **`monarch-init` no longer posts the admin password to Jellyseerr on every
  run.** The drift-check timer (`--heal`, every 6h) re-runs init, which logged
  into Seerr with `admin`/`MONARCH_PASSWORD` each cycle - write-only noise that
  turned into permanent 401s after any password rotation. Init now checks the
  public settings first and only logs in when initialization is actually
  needed.
- **Seerr's Jellyfin Sync 404'd every 5 minutes** on Jellyfin 12: `/Items/Latest`
  now requires `userId`, and Seerr's owner row was still bound to the
  pre-rotation admin user id (deleted with the ghost user). Rebound to the live
  admin; scans complete again. The stale ghost user row (duplicate `admin`,
  same dead id) was removed.
- **Whisparr answered 400 to every caller that was not `localhost`.** Its
  `AllowedHosts` allowlist named only the edge domain and `.46`, so in-network
  automation (monarch-init, Homarr) was locked out while drift-check blamed
  "unreachable". The allowlist now names every stack container that talks to
  it.
- `subscribe.monarch.innotel.us` is back in `npm-hosts.conf` - as the **shared
  subscribe portal** (public page on :3040, one page per service by Host
  header), so the 6-hourly drift heal no longer sees it as drift and re-runs
  init for nothing.

### Ops notes

- Run `python3 scripts/npm-proxy-hosts.py --check` to compare the live edge
  against `npm-hosts.conf`; `--prune` removes what the conf no longer lists.
- Run `python3 scripts/jellyfin-admin-password.py --check` after changing
  `MONARCH_PASSWORD`, and `--set` to re-align Jellyfin if it drifted.
- `monarch-init` now exports a **durable API key** instead of a session token, so
  the credential AI recommendations, health analytics and entitlements read is
  not revoked by the next password change. On an existing install:
  `python3 scripts/jellyfin-admin-password.py --set`.
- Rotated the Jellyfin API keys held by Jellyseerr and Homarr (their values had
  been printed while inspecting the credential chain, and `.env`'s
  `JELLYFIN_API_KEY` had been left empty): the new keys are in place and the old
  tokens answer 401. Homarr's copy is encrypted — see the rotation table in
  `docs/operations.md` before editing it by hand.
- `bash scripts/drift-check.sh` is the single pass/fail gate for the live stack.

## Earlier releases

Generated from the release tags. `git log <tag>` has the commits behind each.

| Version | Date | Headline |
| --- | --- | --- |
| `v1.21` | 2026-09-11 | Pin the Jellyfin image to the verified 12.0.0 build |
| `v1.20` | 2026-09-10 | Sync the attribution guard with the verifier; canonical CI workflow |
| `v1.17`-`v1.19` | 2026-09-08 | Multi-arch stack support for amd64 + arm64 |
| `v1.16` | 2026-09-08 | Landing page GitHub Pages link |
| `v1.15` | 2026-09-07 | Env template; conform the repo to the platform standard |
| `v1.14` | 2026-09-06 | Point the Homarr Signara tile at `app.signara.innotel.us` |
| `v1.13` | 2026-09-06 | Unified auth architecture - Cerulean Authentik |
| `v1.12` | 2026-09-04 | Remove the stale `subscribe.monarch` route from the operations table |
| `v1.11` | 2026-09-03 | Fix workflow badge URLs (`github.com`, not `github.io`) |
| `v1.10` | 2026-09-03 | Self-healing drift check driven by the monarch-init invariants manifest |
| `v1.9` | 2026-09-02 | Remote Nginx Proxy Manager, BIND/TSIG wildcard SSL, hardening |
| `v1.8` | 2026-09-02 | Point repo references at the new `innotelinc/monarch` location |
| `v1.7` | 2026-09-02 | Fix Authentik startup: Redis, healthcheck, directory permissions |
| `v1.5`-`v1.6` | 2026-09-01 | Drop the GHCR login now that Monarch images are public |
| `v1.4` | 2026-09-01 | Rebrand to Monarch Media Platform; AI recs, health analytics, one-command setup |
| `v1.3` | 2026-09-01 | Ship the subscription platform image in the offline bundle |
| `v1.0.0`-`v1.2` | 2026-08-30 | First release: build scripts executable, first-release versioning |
