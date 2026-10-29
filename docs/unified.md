# Gizmo — the unified platform (`core`)

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
| 12 × oauth2-proxy | one gateway per hostname | **one** gateway, one `_innotel_sso` cookie, one `X-Auth-Request-Email` |

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

Step 8 is a profile, not a deletion: the twenty retired services sit behind
`profiles: ["retired"]` in `docker-compose.yml`, so a plain `docker compose up
d` never starts them again, and bringing one back is
`docker compose --profile retired up -d sonarr`. Their appdata and NPM hosts are
untouched; `monarch-init`'s `depends_on` no longer names them, because a
required service that depends on a profiled one makes the whole file refuse to
load.

Until step 8 nothing is destroyed, so rollback is: stop `core`, re-enable the
old proxy hosts. After step 8, restoring the appdata backup and re-enabling the
hosts returns the old stack exactly as it was — the *arr applications still hold
their own configuration; only their identity is replaced.

## Identity

One oauth2-proxy instance terminates the browser session for the whole
platform and sets `X-Auth-Request-Email`. `core` refuses every `/api/*` request
without it; there is no second user store and no local password form.
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

Jellyfin keeps playing and transcoding, so it keeps its own identity — but that
identity is Gizmo's. `assets/gizmo/` holds the wordmark and the TV splash in both
palettes at 1920×1080 and 3840×2160; `render.py` regenerates them from the SVG.
On the host the **4K dark** render is installed at
`/docker/appdata/jellyfin/data/data/gizmo-splash.png` and the branding config
points `SplashscreenLocation` at it with `SplashscreenEnabled`, with the server
name set to Gizmo so a TV client shows one product, not two. Verified by reading
it back from `GET /Branding/Splashscreen` (image/png, 3840×2160).

Jellyfin 12's shell paints the splash on `<div class="splashLogo">` as a
background image and has no second splash setting, so the light theme is served
by one `@media (prefers-color-scheme: light)` rule in the branding CSS that
replaces that background with the light render (the 1920×1080 one, embedded as
a data URI — a 4K one would double the branding config). The rule is fenced by
`/* gizmo-light-splash */` … `/* /gizmo-light-splash */` so re-running the
installer replaces it instead of stacking a second copy; the 4K light PNG sits
next to the dark one in `assets/gizmo/` and in the Jellyfin config for anyone
who wants it as the splash outright.

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
