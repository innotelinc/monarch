# Changelog

Notable changes to Monarch, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); releases are tagged
`v1.<n>` (see [Earlier releases](#earlier-releases)).

Monarch had no changelog before v1.22, so everything older is summarised from its
release tag - `git log <tag>` still has the full detail. From v1.22 on, entries
are written by hand and describe the behaviour change, not the commits.

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
