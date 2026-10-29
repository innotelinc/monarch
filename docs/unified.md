# The unified platform (`core`)

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
| Prowlarr | normalise and fan out indexer searches | `core/indexers.py`: Torznab/Newznab directly, deduped by infohash |
| Jellyseerr, Requestrr | two request frontends over the same catalog | `core` Requests view; one queue, one dedupe key |
| Bazarr | subtitle fetch and sync | `core/subtitles.py`: OpenSubtitles hash + title search, written next to the video, misses recorded |
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
  from `CORE_SUBTITLE_LANGS`; the provider is `OPENSUBTITLES_API_KEY`.
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
7. **Collapse the edge**: point `media.innotel.us` at the single gateway, and
   make the other seven hostnames 301 to it. In Nginx Proxy Manager each becomes
   a redirection host — no certificate of its own beyond the wildcard.
8. **Stop the retired containers** and remove their NPM proxy hosts. Keep their
   appdata for a rollback window; `docker compose down` (without `-v`) is
   enough.

### Rollback

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

## Open items

- **Notification of failures.** Failed grabs and failed subtitle fetches are
  recorded and shown; nothing emails or pushes yet.
- **Sync drift.** Subtitles are written as found. Tools like `ffsubsync` fix a
  subtitle timed for a different release; that step is not wired in.
- **Usenet.** `core` speaks Torznab/Newznab for search but only Transmission for
  transfer. Sabnzbd is stopped with the rest of the old stack.
