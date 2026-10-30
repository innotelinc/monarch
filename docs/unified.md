# Gizmo — the unified platform (`core`)

> **Status, 2026-10-30: rolled back.** The merge described below was deployed and
> then taken back at the operator's request: the *arr stack and its gateways run
> again, `core` is parked behind `profiles: ["retired"]`, and the hostnames answer
> as they did before the merge. The steps that were run, and the one NPM gotcha
> that cost a round trip, are in [Rollback](#rollback). What follows still
> describes the merge — keep it as the record of what `core` does, not as the
> state of the deployment.

**Gizmo** is the product name; `core` is the service that implements it, and the
repository keeps `monarch` as the infrastructure name (container `monarch-core`,
project `monarch`) so a rename never depends on a data migration.

Eight hostnames used to answer for one product family: `media`, `req`,
`movies`, `tv`, `index`, `xxx`, `mp3` and `dl`. Behind them ran nineteen
containers and twelve separate OIDC gateways — six of them doing work only
because the *arr applications have no identity of their own.

`core` is one service that does the work of the *arr family, the request
frontends, the dashboards and the live-TV backends: it owns the catalog, the
wanted queue, indexer search, the grab decision, the download watch, subtitle
state and the channel lineup, and it serves the only UI anyone signs in to.

## What replaced what

| Retired | Was doing | Now |
|---|---|---|
| Sonarr, Radarr, Lidarr, Whisparr | one service per media type: monitor, search, decide, import | `core`: one `wanted` table, one profile, one engine |
| Prowlarr | normalise and fan out indexer searches | `core/indexers.py`: Torznab/Newznab directly **and** Prowlarr's search API as one aggregated indexer, deduped by infohash |
| Jellyseerr, Requestrr | two request frontends over the same catalog | `core` Requests view; one queue, one dedupe key |
| Bazarr | subtitle fetch and sync | `core/subtitles.py`: hash + title search, written next to the video, misses recorded |
| Homarr, `monarch-landing` | two front doors onto the same stack | the `core` UI (this is the only one left) |
| Deluge, SABnzbd | two more download clients | Transmission only |
| TVHeadend, NextPVR, Dispatcharr, autobrr, iptv-epg | four ways to make a playlist playable | `core/livetv.py`: M3U tuner + XMLTV guide read natively |
| ClipBucket | video CMS beside the library | not ported; its media is an ordinary library folder |
| 12 × oauth2-proxy | one gateway per hostname | **one** gateway, one `_innotel_sso` cookie, one `X-Forwarded-Email` |

**Jellyfin stays.** It is not part of the duplication: it plays and transcodes,
and rewriting that is a transcode pipeline, not a merge. `core` decides what
should exist; Jellyfin shows it.

## What the merge saves

The old stack was roughly nineteen containers plus twelve gateways pinned to
one host. The scheduler is now a single thread inside one process instead of
five container entrypoints; the catalog is one SQLite file instead of six
application databases; search happens once per request instead of once per
*arr app; and there is one login instead of twelve.

The two expensive things left are the two that cannot be removed: Jellyfin
(transcoding) and Transmission (transfer). `deploy/monarch-core.compose.yml`
caps `core` at 512 MB and the gateway at 128 MB — deliberately the memory one
gateway used to take on its own.

## Subtitles and the Jellyfin handoff

The two jobs that used to need their own container are now two steps after an
import, in the same pass that links the file:

- **Subtitles.** A video is identified by the OpenSubtitles hash (size plus the
  first and last 64 KiB) *and* by its parsed title/year, so a hash miss still
  finds the release. The subtitle is written next to the file as
  `Name.en.srt` — never over one that exists — and the outcome is recorded, so a
  file with genuinely no subtitle is not re-queried every pass. Languages come
  from `CORE_SUBTITLE_LANGS`. Two providers ship: the OpenSubtitles.com API when
  `OPENSUBTITLES_API_KEY` is set, and the keyless OpenSubtitles.org XML-RPC
  service otherwise — so subtitles work before anyone creates an account, and
  improve (quota, maintained API) when a key is added.
- **Jellyfin.** `POST /Library/Media/Updated` names the folder that appeared and
  `POST /Library/Refresh` covers the rest. An unreachable Jellyfin is reported
  in Settings and never fails an import that already put the file on disk.

## Dedupe

Three levels, and they are why the merged platform is smaller than the sum of
the parts:

1. **Catalog** — `items.fingerprint` is unique (kind, title, year, season,
   episode, quality). Rescanning ten thousand files adds nothing; two copies of
   the same release at the same quality collapse into one row.
2. **Content** — `library.plan_dedupe` hashes the first megabyte plus the file
   size, so two differently named files that are the same bytes are reported as
   one group with the recoverable bytes counted. The Settings view shows the
   report; nothing is deleted by the service.
3. **Grabs** — a release is unique per wanted item, and a release already
   attempted is never attempted again; the same torrent from three indexers is
   one candidate with the best-seeded copy kept.

## Cutover

Order matters, and each step is reversible until the last one.

1. **Back up** the *arr appdata (`/docker/appdata/{sonarr,radarr,prowlarr,…}`)
   and the `monarch` repo checkout. Nothing below deletes data.
2. **Start `core` beside the old stack** on a spare port:
   `docker compose -f deploy/monarch-core.compose.yml up -d core`, then open
   `http://<host>:8090/` with `CORE_AUTH_DISABLED=1` for the first look.
3. **Migrate intent**: `python3 scripts/migrate-from-arr.py --dry-run …`, then
   without `--dry-run`. This imports Prowlarr's indexers and Sonarr/Radarr's
   monitored-and-missing items into `wanted`.
4. **Rescan the library** in the UI. The files stay where they are; only the
   catalog rows are written. Confirm the item counts match what Jellyfin sees.
5. **Audit duplicates** in Settings → *Audit duplicates*. Review before removing
   anything: the report is evidence, the removal is a separate decision.
6. **Let the scheduler run for a day** with the old stack still up. The two do
   not fight: `core` only searches for items in its own `wanted` queue.
7. **Collapse the edge** ✅ done: `media.innotel.us` answers for the single
   gateway and the other six hostnames (`movies`, `req`, `index`, `xxx`, `mp3`,
   `dl`) are 301s to it. `tv.innotel.us` is the exception: it points at Jellyfin,
   because playing is the one thing Gizmo hands off (see below).

   The hostnames were the easy half. Four **proxy** hosts still forwarded to
   ports nothing listened on — `xxx` (ClipBucket), `tube`, `requestrr.monarch`
   and `sabnzbd.monarch` — which is a 502 nobody sees until the name is typed.
   They are 301s now, and the `*.monarch` names for Sonarr, Radarr, Lidarr and
   Whisparr point straight at `media` instead of at each other's redirect.
   `req.monarch.innotel.us` (Jellyseerr) was the one name missed: its proxy host
   was marked deleted rather than converted, so NPM had no server block for it
   and the edge answered the TLS handshake with `unrecognized name` — which reads
   as "the whole edge is down" in a browser. It is a 301 like its siblings now.
   Two
   NPM details cost a round trip each: a new host is refused while the domain is
   still claimed (delete, then create — the old definition is saved to
   `/root/npm-retired-hosts.json` first), and a redirection host does **not**
   inherit the wildcard it needs — a certificate copied from another host made
   every one of the four fail TLS with `no alternative certificate subject
   name matches`. Each kept its original `certificate_id`.
8. **Stop the retired containers** ✅ done: twenty stopped — Sonarr, Radarr,
   Lidarr, Whisparr, Bazarr, Jellyseerr, Requestrr, Sabnzbd, Homarr, ClipBucket
   and the ten gateways that existed only to give them identity, plus the old
   `monarch-landing` nginx page. Eighteen containers are left: core and its
gateway, Jellyfin and its gateway, Transmission, Prowlarr, FlareSolverr, IPTV,
   Authentik LDAP, Watchtower, the three metric exporters and the health pinger.
   Appdata is untouched, so rollback is still `docker compose up -d <service>`.

### Playing is a link, not a page

Gizmo decides what should exist; Jellyfin plays it. So the UI keeps one door out:
`tv.innotel.us` forwards to `jellyfin-sso` (port 14010) with playback-friendly
timeouts and buffering off, and the sidebar's **Watch** link opens it in a new
tab. Jellyfin's own accounts still work there (TV and native clients cannot
complete a browser OIDC flow), which is why that hostname is not a 301.

### Rollback

Step 8 is a profile, not a deletion: the twenty retired services sat behind
`profiles: ["retired"]` in `docker-compose.yml`, so a plain `docker compose up
d` never started them again. Their appdata and NPM hosts were untouched, which is
what made the rollback below a `compose up` and an edge change rather than a
restore from backup.

#### What that took, end to end

This is the path that was actually taken:

1. **Start the apps and their gateways**, without touching `core`:
   `docker compose --profile retired up -d sonarr sonarr-sso radarr radarr-sso
   lidarr lidarr-sso whisparr whisparr-sso bazarr bazarr-sso jellyseerr
   jellyseerr-sso requestrr requestrr-sso sabnzbd sabnzbd-sso homarr clipbucket
   clipbucket-sso landing`. Every app answered on its loopback port afterwards
   (Sonarr 8989, Radarr 7878, Lidarr 8686, Whisparr 6969, Bazarr 6767, Jellyseerr
   5055, Requestrr 4545, Sabnzbd 8082, Homarr 7575, ClipBucket 8098); the *arr
   DBs were intact and the apps came up without a migration step.
2. **Take the profile off the twenty services and put it on `core`/`core-sso`**,
   so `docker compose up -d` is the old stack again and Gizmo is what needs an
   explicit `--profile retired` to start.
3. **Restore the edge.** `media.innotel.us` goes back to `jellyfin-sso` (14010),
   `tv.innotel.us` back to `sonarr-sso` (14002) — Jellyfin had taken that name in
   the merge — `xxx`/`tube` back to Whisparr/ClipBucket (14004/14011), the apex
   back to the landing page (14014), and `app`, `req`, `bazarr`, `sabnzbd`,
   `requestrr` back to proxies. `radarr/sonarr/lidarr/whisparr.monarch` are 301s
   to `movies`/`tv`/`mp3`/`xxx` again. `scripts/npm-hosts.conf` carries the rows.
4. **Stop Gizmo**: `docker compose stop core core-sso` (and the profile keeps it
   that way).

**The gotcha that cost a round trip:** five of the names served a 301 to `media`
while the NPM database said they were already proxy hosts to the gateways — a
host that was re-pointed during the merge kept its *generated* nginx config until
it was saved again. Re-saving each host (an idempotent `PUT` of the same values)
rewrote the config and the name proxied correctly. `tv.monarch` had the same
stale config and the same fix. If a name answers with the old rule after a change,
re-save the host before believing the API.

Verification after the revert: `npm-proxy-hosts.py --check` — 12 proxy hosts
match the conf; `check-proxy-ports.py` — every row forwards to a published port;
`verify-sso.py` — every gated name is Authentik-only, both published names serve
their app's own sign-in page, and the app ports are closed on the LAN.

## Identity

One oauth2-proxy instance terminates the browser session for the whole
platform and sets `X-Forwarded-Email` on the request it hands `core` (the
`pass-user-headers` header — `X-Auth-Request-*` is the nginx `auth_request`
name and never reaches a reverse-proxied upstream). `core` refuses every
`/api/*` request without it; there is no second user store and no local
password form.
`CORE_AUTH_DISABLED=1` exists for local development only — with it set, the
service trusts whatever the client sends and must never be published.

The gateway forwards to `core` only. Jellyfin keeps its own Cerulean Authentik
integration through the LDAP outpost, unchanged.

## Layout

```
assets/gizmo/          the product mark and the TV splash (SVG source + rendered PNG)
unified/
  app/parser.py        release-name parsing (the *arr value, written down)
  app/scoring.py       quality profiles, upgrade rules, the grab decision
  app/indexers.py      Torznab/Newznab client, cross-indexer dedupe
  app/downloader.py    Transmission RPC
  app/subtitles.py     OpenSubtitles search, naming, the fetch pass
  app/jellyfin.py      library refresh after an import
  app/library.py       scan, content dedupe, import paths
  app/livetv.py        M3U + XMLTV, deterministic channel numbering
  app/acquisition.py   the loop: search → decide → grab → import
  app/db.py            SQLite schema; dedupe enforced by unique indexes
  app/main.py          FastAPI API + the single-page UI
  static/              the UI (no build step)
scripts/migrate-from-arr.py   import indexers and the missing queue
deploy/monarch-core.compose.yml
```

## What the UI does

Library is three shapes, because a catalogue of 258 files is not a grid: films
are cards with their poster, a show is one poster with its episodes in
season/episode order, and music is artist → album → tracks. A poster comes from
`/api/art/<id>`, which proxies Jellyfin's index (one request for the grid, held
for ten minutes) and falls back to the initials the card used to show. Live TV
lists the whole dial with what is on now; the guide is streamed to a temporary
file and parsed with `iterparse`, because reading a 77 MB XMLTV whole took the
service past its memory limit and the page answered 502.

Search scores every candidate and the number is the decision's reason; **Grab**
sends that release to Transmission by hand and goes through the same dedupe as an
automatic grab. Settings carries the subtitle state (found / missing / failed),
a *Fetch missing subtitles* button, the Jellyfin status and a manual refresh, the
indexer list and the duplicate audit.

## Why Prowlarr is still running

Most trackers in this estate have no Torznab endpoint of their own — Prowlarr
is the only thing that speaks to them. Deleting it would either break search or
require a scraper per site, so `core` keeps it in the smallest role it can:
**one** indexer row of kind `prowlarr`, whose results core parses, ranks,
dedupes and filters (usenet answers are dropped, because Transmission is the
only client). The other four *arr services are gone; Prowlarr is a search proxy
with a database, not an application anyone signs in to. Retiring it later is a
matter of adding a native indexer row per tracker and deleting the one row.

## The Jellyfin splash

Jellyfin keeps playing and transcoding, but the login screen is the one page
every identity sees before it has a session, so it wears **Monarch's** mark, not
a second product's — and the TV clients (Android TV, Roku, Fire TV) ask the same
endpoint on launch, so one artwork covers the login card and the living room.
`assets/monarch-splash.svg` is the design;
`scripts/jellyfin-splash.py` renders and installs it as
`/docker/appdata/jellyfin/data/data/monarch-splash.png` (3840×2160) and points
`SplashscreenLocation` at that path with `SplashscreenEnabled`. The API cannot
set the location — `BrandingOptionsDto` omits it — so the file is the only way,
and `GET /Branding/Splashscreen` is the read-back that proves which image a
client is actually handed.

The 2026-10 design is *Celestial Cinema*: a seeded star field over obsidian,
film-strip perforations down both edges, a twelve-blade aperture iris closed on
a golden play mark inside a 72-tick bezel and 48-ray crown, two orbit paths with
satellites, and mirrored equaliser wings either side of the wordmark. It is
4K because a TV upscales 1080p visibly on a 4K panel, and it is drawn from one
primitive list with two backends, so the committed SVG and the installed PNG
cannot drift apart.

Each design ships twice, at 4K and at 1920×1080 (`monarch-splash-1080.png`), for
a client that caps the size of a splash image; `--apply` installs the 4K one
unless `--scale 1` says otherwise, and the check reads the name `branding.xml`
carries, so either install is judged as the choice it was.

The cream variant is committed and installable, but it is not what the media host
runs, and that is a measured choice rather than a preference: a client paints its
own white boot spinner over the centre of the splash, and the light render's
centre measures L 0.78 — 1.26:1 against white, i.e. invisible — where the dark
render's is L 0.02 (14.2:1). A dark login card and the splash's own dark ink both
read on cream (16.7:1), so the variant is fine behind the form; it is the boot
chrome the clients draw that it disappears.

There is no second splash setting, so the light theme is not a `@media` rule in
the branding CSS: the whole backdrop is one stylesheet
(`scripts/jellyfin-background.py`), and switching palettes is
`--apply --variant light`, which installs `monarch-splash-light.png` as the
splash outright. The Gizmo renders under `assets/gizmo/` are the previous
identity; `render.py` still reproduces them, but nothing installs them, and the
styling that used to swap in their light render (a fenced
`/* gizmo-light-splash */` block appended to the branding CSS) is gone — the
stylesheet is rebuilt from `assets/monarch-background.svg`, so re-applying the
backdrop cannot bring it back.

The image is also digest-pinned and labelled
`com.centurylinklabs.watchtower.enable=false`: an upgrade there is a database
migration, and Watchtower's idea of "newer" was the 11 → 12 jump that crash-
looped on its own migration. Moving Jellyfin is `JELLYFIN_IMAGE=… docker
compose up -d jellyfin`, deliberately.

## Open items

- **Notification of failures.** Failed grabs and failed subtitle fetches are
  recorded and shown; nothing emails or pushes yet.
- **Sync drift.** Subtitles are written as found. Tools like `ffsubsync` fix a
  subtitle timed for a different release; that step is not wired in.
- **Usenet.** `core` searches Newznab indexers but only transfers through
  Transmission, so usenet results are filtered out of Prowlarr answers rather
  than offered and then failing. Sabnzbd is stopped with the rest of the old
  stack; a second download client is the work this needs.
- **Prowlarr.** Still running, in the reduced role described above.
