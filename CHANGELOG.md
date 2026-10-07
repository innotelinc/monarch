# Changelog

Notable changes to Monarch, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); releases are tagged
`v1.<n>` (see [Earlier releases](#earlier-releases)).

Monarch had no changelog before v1.22, so everything older is summarised from its
release tag - `git log <tag>` still has the full detail. From v1.22 on, entries
are written by hand and describe the behaviour change, not the commits.

## [Unreleased]

### Added

- **The drift verdict is in the file an operator already reads.** `drift-check`
  records its verdict in `drift-last` and nothing else looked at it unless you knew
  to ask, so `monarch-init` now folds that file into the `status.json` it writes
  under `drift` - `sudo cat /docker/appdata/init/status.json`, the file that already
  says what init did, also says whether the live stack is drifted (`verdict`, `at`,
  `issues`, `heal`, `heal_streak`, the findings) - and its SUMMARY log gains a
  `drift` line. init only *reads* the check's file: "init says everything is
  configured" and "the live stack is healthy" are different claims, and only the
  check probes the services. Parsing a verdict is forgiving by design (an
  unrecognised line, or a host whose timer has not run yet, is not fatal), which
  `scripts/tests/test_init_status_drift.py` pins.

- **A gateway key that stopped being accepted is now drift, not a mystery.**
  Nothing watched OmniRoute's own idea of its credentials: the gateway on the
  proxy host kept answering `:20128`, refused the work with a 401, and the symptom
  landed somewhere else entirely as *every model in the chain failed*. Set
  `DRIFT_GATEWAY_URL` in `.env` and `drift-check` reports each connection the
  gateway refused — **the authentication classes only** (401 or 403, in either
  shape the gateway reports a status, or by `lastErrorType`). A 429, a 402, a 404
  or a 503 is a quota, a balance or an upstream, all of which are true of a
  free-tier gateway most of the day, and a check that fired on those would be
  ignored within a week. A connection somebody switched **off** is not reported
  either: switching one off is the documented fix for a provider that is out of
  credit, and a fix that raises the alarm is worse than no check at all.
  `DRIFT_GATEWAY_TOKEN` exists for a gateway that also gates `/api/*` and is not
  needed here — the endpoint answers the same fields unauthenticated, checked both
  ways — so no credential is put on this host for it. Unlike the Dead-container
  case, **nothing in the heal touches a refused key**: re-keying is a person's
  work, so the finding repeats until somebody does it or switches the connection
  off. The classification is `scripts/gateway-provider-keys.py` (exit `0` clean,
  `1` refused, `2` unreadable — the `verify-ldap.py` convention) and it is covered
  by `scripts/tests/test_gateway_provider_keys.py`.

- **ClipBucket's catalogue now follows the library by itself — additions and
  deletions.** `clipbucket-library.py` owns the import but it is a command
  somebody has to remember, and it only ever asks one direction: *is every item
  on disk in the catalogue?* A row whose source is **gone** is never examined, so
  `--check` reported `in sync` while tube.innotel.us still listed a film Jellyfin
  had deleted — and on 2026-09-28 two films (`Runner`, `The Mongoose`) had been
  sitting in `/data/media` invisible for days for want of someone typing the
  command. `scripts/clipbucket-sync.py` closes both halves, and
  `systemd/monarch-clipbucket-sync.{service,timer}` runs it every two minutes
  (installed and enabled by `install-monarch.sh` alongside the other oneshots).
  It **deletes** a vanished item — its catalogue rows (plus the
  `cb_video_image`/`cb_video_thumb` thumbnails the import writes, and its
  `cb_collection_items` series membership), its thumbnail directory, and the media
  copy it made. That last part is why deletion rather than hiding is the default:
  the import **hardlinks** the source into the file volume, so removing the row
  and leaving the file would keep a deleted film's bytes on disk for as long as
  the volume lived. `--hide` is the reversible variant (`active='no'`, the switch
  the app's own browse query reads) for a library that is only temporarily
  absent.

  The row deletions are exhaustive and run inside one transaction, which is a
  correction rather than a flourish: the first version deleted `cb_video`, its
  images and its thumbs, and looked right, but `cb_videos_categories` also has a
  RESTRICT foreign key onto `cb_video.videoid`, so MySQL aborted the statement
  *after* `cb_video_image` had already gone — the item ended up half-deleted, its
  row still listed and its images gone. Every table keyed to `cb_video` is now
  covered, and a constraint the list does not know about rolls the deletion back
  to an item still whole, which is a state the next run can judge.

  Three guards keep it from doing damage or doing work: it refuses outright when
  the library is entirely empty (an unmounted `/data/media` looks exactly like
  "every film was deleted", and deleting is the one repair a re-run cannot undo);
  the expensive half
  (a full library walk plus one ffprobe per item) runs **only** when a fingerprint
  of every file's path, size and mtime actually changed, and a source written
  within `CLIPBUCKET_SYNC_SETTLE` seconds (180) is held back from the fingerprint
  so a half-copied film is never imported while an `*arr` app is still writing it.
  First run on this host found and hid one such stale row
  (`ghost-in-the-cell-2026-24a89923`) that no existing check could see.

- **The renewal is scheduled, and the restart is part of it.**
  `systemd/monarch-jellyfin-tls.{service,timer}` runs `jellyfin-tls.py --renew
  --restart` weekly (Mondays 04:17, randomised) and is installed and enabled by
  `install-monarch.sh` alongside the drift-check and Live TV timers, so a
  certificate that went stale is repaired without anyone having to notice.
  Jellyfin reads `network.xml` and the certificate only at startup, which is why
  `--restart` exists at all and why the timer carries it. `--renew` is a no-op —
  it installs nothing and restarts nothing — when the edge holds the material the
  listener is already serving, and only a positive "the listener is serving this"
  skips: an unreachable listener leaves the question open and the install happens.
  A restart docker refuses exits 1 rather than reporting a successful install
  that changed nothing, because `--check` compares the listener against the
  `.pem` on disk and an un-restarted app is exactly that mismatch.

- **The certificate Jellyfin serves renews itself from the edge.** Cerulean
  renews `media.innotel.us` on its timer and pushes the result to NPM and
  nowhere else, so a renewal left the listener serving the material from the day
  it was installed: the drift check reported it, but the repair was a manual
  fetch, copy and `--apply`. `scripts/jellyfin-tls.py --renew` closes that. It
  reads the certificate covering the name back from the edge — an exact match
  before a covering wildcard, newest expiry first — using the `NPM_BASE_URL` /
  `NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` this stack already drives the remote
  NPM with, writes it beside the bundle as the `.pem` the check compares against,
  and installs it. It reads the repo's `.env` itself, and reports instead of
  installing nothing when the edge holds no certificate for the name.

- **ClipBucket's library check now asks the site, not just the filesystem.**
  `scripts/clipbucket-library.py --serve-check` fetches every catalogue item's
  watch page, takes the `<source>` URLs the page emits, and requires a file
  behind each one that the web server will actually return bytes from. The
  existing `--check` judges this repo's *model* of the app — the name
  `get_video_files()` builds from a row — and every check in that model can pass
  while the site plays nothing, which is exactly how the first import's
  `<name>-<hash>.mp4` rows looked complete and had no playable source at all.
  Byte-serving is the half `ls` cannot see: a file the container user cannot
  read is listed fine and answered 403. `drift-check` runs it after the
  catalogue check, and it needs no `ffmpeg` on the host, so a media host is
  judged on what it serves rather than refused for lacking an encoder.

### Changed

- **Jellyfin is pinned to the 12.2 build.** The image was pinned to 12.0.0 by
  digest; it now points at the verified 12.2 index digest
  (`sha256:048001357ab34f032f757c72aff22d7256f7ee96e63005ba451cc3265fc99797`),
  the build hotio's `release-12.2` tag resolves to (its own label reports
  `org.opencontainers.image.version=12.2`). The digest is the multi-arch index,
  so amd64 and arm64 hosts move together; set `JELLYFIN_IMAGE` to move off it
  deliberately instead of by surprise.

- **A heal that keeps failing stops trying.** Five attempts in a row (tunable:
  `DRIFT_HEAL_MAX_STREAK`) that do not clear the drift is not a plan, so the check
  now stands the heal down instead of reconciling the stack every tick forever:
  nothing is healed (`heal standing down - 5 attempt(s) in a row have not cleared
  this`), the alert says the repair it will not make again and names the one a
  person runs after looking (`sudo /opt/monarch/scripts/drift-check.sh
  --reset-streak`), and it repeats at most once per
  `DRIFT_STANDDOWN_ALERT_REPEAT_SEC` (default 86400s) so a stack already known to be
  in that state stops paging. The runs in between print why they stayed silent
  (`no alert - the heal stood down … ago and already said so`) on stderr, so a quiet
  timer is distinguishable from a timer that found nothing. `--reset-streak` keeps
  the heal clock - the rate limiter still needs it - and zeroes the count and the
  stand-down clock, which is the whole escape hatch. The verdict also gained
  `heal=` (`none`, `healed`, `recheck`, `suppressed`, `stood_down`), because "the
  stack is drifted" and "nothing was tried about it" are different answers.

- **The drift check keeps a scoreboard, and can be asked for it.** A timer's run
  was only readable in the journal, and its state was one number (when it last
  tried to heal), so both "is the stack drifted right now?" and "has anything been
  trying to fix this?" needed a shell on the host and a time window. Every
  completed run now records its verdict in `/docker/appdata/init/drift-last`
  (`verdict`, `at`, `host`, `manifest`, `issues`, `heal_streak` and one `finding=`
  line per finding) and `scripts/drift-check.sh --status` prints it, exiting like
  the check itself does (`0` clean, `1` drift, `2` nothing recorded yet).
  `/docker/appdata/init/drift-heal-last` now holds `<epoch> <count>` — the heal
  clock it always was, plus how many attempts in a row have **not** cleared the
  drift, incremented on an attempt and reset by a clean run — and the alert says
  it (`this has survived 3 heal attempt(s) in a row`, or `this is heal attempt 4
  in a row that has not cleared it` after a re-check). A file from before this
  reads as a streak of zero. The directory all of it lives in is
  `MONARCH_STATE_DIR` (default `/docker/appdata/init`, unset on a host) so the
  whole check can run against state that is not a host's own — which is what the
  new tests and the `drift-alerts` CI job do, and what "staged" now keys off: a
  run whose manifest is not the one under that directory neither alerts nor
  records a verdict. `scripts/tests/test_drift_check_alerts.py` covers both alert
  wordings, the streak and the recorded verdict offline (every probed port points
  at a closed one, so there is drift to find on any machine and no stack, boot or
  bot is involved); the CI job covers the same ground end to end.

- **qBittorrent is gone; Transmission is the downloader.** qBittorrent 5.2.x
  leaked memory (~10 GB/day, upstream qBittorrent/qBittorrent#24618) and
  OOM-restart-looped against the monarch cgroup until the container had to be
  replaced. The `qbittorrent` and `qbittorrent-sso` services are removed and
  `transmission` (behind `transmission-sso`) is the only torrent client. The
  per-app folders are unchanged: Servarr sends its category to Transmission as a
  subfolder of the download dir, which `monarch-seed` pins to `/data/torrents`,
  so `movies`/`tv`/`music`/`xxx` still land under `/data/torrents/<type>` and are
  the same path in every container. `monarch-init`'s `configure_qbittorrent` is
  now `configure_transmission` (RPC session, download dir, no local login),
  `scripts/arr-download-categories.py` reconciles each *arr's Transmission client
  instead of qBittorrent's category objects, and `scripts/drift-check.sh` asserts
  the download dir and the absent local login. The WebUI keeps **no login at all**
  (Transmission has no per-subnet bypass): it is published on loopback only and
  `transmission-sso` is the sole door, so Cerulean stays the only credential. The
  NPM host and the registered redirect URI move from `qbittorrent.<domain>` to
  `transmission.<domain>`; re-register the redirect in Authentik if the provider
  was provisioned before this change.

- **Seerr is on 3.5.0.** The `jellyseerr` container was recreated onto
  `ghcr.io/seerr-team/seerr:latest` at v3.5.0 (commit `e2f24cb`), up from
  v3.4.1 (`69f73a6`). 3.5.0 carries one breaking change — `GET
  /settings/{plex,jellyfin}/library` no longer accepts `sync`/`enable`, which
  moved to `POST …/library/sync` and `PUT …/library/{id}` — and nothing in this
  repo or `monarch-init` calls either, so no code changed with it. The Owner
  row-id rule `scripts/seerr-owner.py` depends on is unchanged in the image
  (`dist/routes/user/index.js` still refuses `id !== 1`), and `seerr-owner.py
  --check`, `seerr-login-methods.py --check`, `/api/v1/status`
  (`updateAvailable: false`) and `req.monarch.innotel.us` were all re-verified
  after the recreate.

### Fixed

- **`docker exec -i` no longer drains the caller's stdin.** This is the consumer
  the previous note was looking for: `clipbucket-install.py` invokes
  `docker exec -i` for every in-container read, and `-i` attaches the *host's*
  stdin and streams it into the container, so `read_app_file()` - the first thing
  the check does that touches ClipBucket - consumed whatever the caller had on
  stdin. Measured on monarch 2026-10-06 under a PATH shim that logs whichever
  child advances the script's own file offset: `docker exec -i clipbucket cat
  /srv/http/clipbucket/upload/includes/config.php`, and the caller's script ended
  there. A call with nothing to send now gets `stdin=DEVNULL`; the two that carry
  their own input (a file write, a mysql statement) still pass it. Pinned by
  tests in `test_clipbucket_install.py`.

- **The library check no longer eats its caller's stdin.** `ffmpeg` — and so
  `ffprobe` — reads its own stdin for interactive keys (`q` to quit), so a child
  that inherits the caller's stdin consumes it. That is invisible until the
  caller's stdin is something it needs, which is exactly the case for a host
  check started from a script on stdin (`ssh host 'bash -s' <<EOF`): the caller's
  script silently ends at that call, and it reads as a hang rather than as a
  short read. `scripts/clipbucket-library.py` now passes `-nostdin` and
  `stdin=DEVNULL` at the `ffprobe` spawn and `stdin=DEVNULL` for every other child
  (`run()` covers the docker, mysql and ffmpeg calls), so nothing in it reads a
  stream it does not own. Both spawn points are pinned by tests. (Measured on
  monarch 2026-10-06: the check run over ssh does lose those lines. This fixes the
  `ffprobe` instance; the same run still has another consumer somewhere in it,
  which is still open.)

- **The drift alert says which failure it is.** A repair that did not take and a
  run that only looked both arrived as the same nine words, `⚠️ Monarch drift check
  failed on <host>`, and they were told apart by remembering that the timer runs
  with `--heal`. The re-check the heal executes is now marked, so the surviving
  failure reads **AFTER A HEAL** and opens with *a heal reconciled the stack and
  re-ran monarch-init, and this re-check still finds drift — the repair did not
  take, so this one needs a person*, while a read-only run says *nothing was
  repaired* and names `--heal` as the repair. The send is replaceable too
  (`DRIFT_TELEGRAM_CMD`, the same seam as `MONARCH_LDAP_PROBE`), which is how the
  new `drift-alerts` CI job asserts both rules — and the staged-run rule beside
  them — without a request leaving the runner. That job needs no stack (absent
  services are findings, which is what the alert reacts to), so it runs with the
  rest of CI.

- **A staged drift run can no longer page.** `MONARCH_INVARIANTS=<path>` is how
  CI and an operator rehearse the check against a manifest other than this host's
  own, and its findings are the thing being looked at rather than an alarm — but
  the run still notified, so the runs that verified this check's own failure paths
  pushed a fabricated library name and a port nothing listens on to an operator's
  phone. A run whose manifest is not this host's now reports every `DRIFT-FAIL`
  line and exits non-zero exactly as before, and withholds only the Telegram
  alert (saying so in its output). Blanketing `TELEGRAM_*` in the shell is not the
  answer: `drift-check` re-sources `.env` itself, so the values come back. The
  guard is pinned in CI beside the other drift-check posture assertions, and
  `.github/workflows/ci.yml` gained a step that keeps both plugin config writers
  on `apply_plugin_config()` — a bare byte comparison reads as changed on every
  run, which is how `monarch-init` came to restart Jellyfin every run.

- **A heal no longer reports the Jellyfin it just restarted as drifted.**
  `drift-check --heal` reconciled the stack, re-ran `monarch-init`, and re-verified
  immediately — but init restarts Jellyfin through its own `POST /System/Restart`,
  which is asynchronous: the old process answers for a few seconds and the new one
  then boots for ~40s, its setup server listening first and replying `503` the
  whole time. The re-check was therefore the first reader of a Jellyfin that had
  not finished starting, and it reported two findings about a credential nothing
  was wrong with: `jellyfin: libraries missing: 'Movies' 'TV Shows' 'Music'
  'Other' (have: '')` and `jellyfin: an app's stored API key no longer
  authenticates`, both gone by the next tick. Two changes, at the two layers that
  were each telling half the truth. The heal **waits for the stack it restarted**
  before judging it — it polls one authenticated call (`GET /Users`, which only
  the wired server can answer, so the boot-time setup server cannot satisfy it)
  until it returns 200, up to `DRIFT_READY_TIMEOUT_SEC` (default 300s), and says
  so if that elapses (`DRIFT_READY_WAIT=0` skips it). And the probes **stop
  conflating "did not answer" with "the answer is wrong"**: the library check
  reads the status code and reports a non-200 as *the server is not serving, so
  its libraries could not be read* rather than as missing libraries, and
  `jellyfin-admin-password.py --check-apps` now fails only on a verdict —
  `401`/`403` is a rejected key, while `0` or a 5xx is reported as unverified,
  because a key cannot be rejected by a server that never read it (the module
  already treated an unreadable key that way; a server that would not answer was
  the case it missed). The `--check-apps` verdicts are pinned by
  `scripts/tests/test_jellyfin_admin_password.py`.

- **`monarch-init` no longer calls a restart it cannot see "done".** The wait
  that followed `POST /System/Restart` accepted any non-zero status as up, so it
  was satisfied by the *outgoing* process — the call answers while the server it
  is replacing is still serving — and init finished wiring a Jellyfin that went
  down moments later and spent ~40s booting. That is the half of the 2026-10-05
  00:04 heal the drift-check gate above was papering over. A restart is now two
  observations: `wait_for_jellyfin_restart()` waits for the old process to stop
  answering (bounded by a grace, so a `/System/Restart` that turns out to be a
  no-op does not hang) and then for a server that can answer an *authenticated*
  call (`GET /Users`) to start. The public `/System/Info/Public` is not enough to
  ask with — this build's setup host binds the port first and answers it — and a
  stale key answers `401` there, which is the wired server answering, so the wait
  never judges a credential it has not been asked about yet. Pinned by
  `scripts/tests/test_init_jellyfin_restart_wait.py`.

- **`monarch-init` restarted Jellyfin on every single run, for nothing.** It
  writes the LDAP-Auth plugin config and restarts Jellyfin only when a value it
  manages has moved — the plugin reads that config at startup, so a rotated bind
  token has to be noticed — but it decided that by comparing the file whole. That
  file is shared: the plugin owns `<LdapUsers>`, the Jellyfin accounts it has
  linked to LDAP identities, and rewrites the file in its own shape (including an
  `encoding="utf-8"` XML declaration) as soon as it has linked one. The comparison
  was therefore true on **every** run and init restarted Jellyfin — ~40s of
  downtime, and the 503 window this changelog's other Jellyfin entries are about —
  whether or not anything had changed. Measured on monarch 2026-10-06: every
  managed value byte-identical, and the run restarted Jellyfin anyway. It now
  compares the values it manages (`plugin_config_changed()`) rather than the bytes,
  and carries the plugin's `<LdapUsers>` across when it does rewrite — so those
  links are no longer dropped, and the file converges instead of alternating.
  Both configs are now rendered, compared, and *then* written: the write itself was
  unconditional, so a no-op run still handed the plugin a file in init's shape for
  it to rewrite. The same read-render-compare-apply shape covers the OIDC config
too, and its
  comparison is deep — the client secret lives at
  `/Providers/OidcProviderConfig/ClientSecret`, so a shallow map would have
  stopped noticing a rotation while still looking correct. `apply_plugin_config()`
  is what both call: it writes **only** when the managed values moved, so a no-op
  run no longer hands a plugin a file of init's shape to rewrite, or restarts
  Jellyfin for the write. Pinned by
  `scripts/tests/test_init_plugin_config_idempotence.py`.

- **A six-hourly run that lands inside a Jellyfin restart no longer pages.**
  Something other than the heal can restart Jellyfin — its own dashboard, a
  plugin install — and this build answers `503` from its setup host for the whole
  ~40s boot. The library probe now waits `DRIFT_JELLYFIN_GRACE_SEC` (default 60s,
  one boot; `0` judges the first read) for the server to come back before calling
  it drift, and a run that still cannot read the libraries says the server is not
  serving rather than that the libraries are gone.

- **ClipBucket can no longer start with a blank MariaDB password.** The password
  left the compose file on 2026-09-19 and became `CLIPBUCKET_DB_PASSWORD`, but an
  `.env` written before that day has no such variable — so the runtime was handed
  `MYSQL_PASSWORD=""`. `clipbucket/entrypoint.sh` uses that value only when there
  is no database yet, so it bit on a fresh volume (a new host, or a dropped one) by
  creating the `clipbucket` user with an empty password. That works, which is
  exactly why nothing reported it: `docker compose config` warned and no run
  failed. The compose now refuses the stack instead of guessing
  (`${CLIPBUCKET_DB_PASSWORD:?set … in .env}`, the shape NPM and Capstone already
  use), and `.env.example` plus `docs/operations.md` record what the value has to
  be on a host whose `clipbucket_db` volume already exists — the value it was
  initialized with, because a different one fails at the installer.

- **ClipBucket's install is a command, not a browser wizard.** The app ships a
  nine-step installer behind `upload/cb_install/`, and a deployment has no browser
  in it — so a host whose volume never got the wizard sat serving the installer,
  which looks the same as an installed site with no content. `scripts/
  clipbucket-install.py` replays the wizard's own steps (the 17 SQL files in
  `ajax.php`'s order, the version row, `includes/config.php` from its template,
  the admin account, the site settings, the lock), `--check` reports the state for
  `drift-check`, and `--apply` is idempotent. Two of those steps are load-bearing
  and were found by breaking on them: without the **version row** every
  *logged-in* page returns HTTP 500 from inside `User->get()` (anonymous pages are
  fine, which is why it reads like a session bug), and without removing
  `files/temp/install.me` the browser installer stays reachable over the finished
  site.

- **"ClipBucket is migrated" now says which half.** The migration record claimed
  the import brought "80 database tables, 2 users, 0 video rows". Measured on
  2026-09-22, the `clipbucket/` directory in the `monarch_clipbucket_db` volume
  holds one entry (`db.opt`) — no tables — while the files volume holds the freshly
  cloned application source, so the container serves its **installer**. The record
  now states the measured state and both recovery paths, and the route table beside
  it carries the `.56` targets and today's caller-visible statuses rather than the
  pre-move `.46` ones. Content is a separate question and this release does not
  answer it: `.72` — the documented rollback source — answers on no port as of
  2026-09-22, so the videos are either in a dump taken before that or not at all.

- **The LDAP outpost image tracks the Authentik server version, and drifting
  apart is now a drift finding.** `authentik-ldap` is a *client* of the Cerulean
  Authentik and has to sit on the server's version line; the compose comment has
  said "bump both together" since the outpost was added, but a comment is not a
  check. On 2026-09-19 the deployment ran `ldap:2026.8.2` against a `2026.8.3`
  server — the server reported `outpost_outdated: true` and nothing noticed. The
  image is now `2026.8.3` in both files that carry it (`docker-compose.yml` and
  the `ips` platform manifest), and `drift-check` compares the running
  container's tag against the server's advertised `version_current`, failing
  with the repair named. The server's own `outpost_outdated` flag is used only
  as corroboration, never as the trigger: this is the estate's *shared*
  Authentik, so that flag goes true when any outpost anywhere lags.
  `docs/operations.md` gains the bump procedure.

- **A dead LDAP outpost is a drift finding, not a note.** `drift-check` treated
  "nothing answered" as a note so a slow bind would not be mistaken for a drifted
  credential — measured cost on 2026-09-18: the outpost ran 45 minutes with its
  API token rejected (`403 Forbidden (Token invalid/expired)`, `/ldap
  healthcheck` failing 541 times, port 3389 never opened), every Cerulean identity
  got HTTP 500 from Jellyfin's login form, and the run still reported *all
  live-stack invariants OK*. An outpost that is up, past its start period
  (`DRIFT_LDAP_GRACE_SEC`, 90s) and still not serving now fails the run and names
  the repair — `docker compose up -d --force-recreate authentik-ldap`, because
  init pins the token but a process already running against the old one keeps
  failing. `authentik-ldap` is also probed for restarts and stale images now.

### Added

- **Seerr's Owner can belong to the account people actually sign in as.**
  Seerr decides its Owner by row id (`user.id === 1` in
  `server/routes/user/index.ts`: only row 1 may grant admin, and only row 1 may
  modify row 1), so an install whose first account was the break-glass Jellyfin
  admin had an Owner nobody uses — and `main.localLogin: false` makes that
  account unreachable when the gateway is the only way in. `scripts/seerr-owner.py`
  hands it over by **swapping** the two accounts (identity, permissions, every
  foreign key onto `user.id`, and the live sessions), so the displaced admin keeps
  its row, its data and its admin bit.  The account is named once in the invariants manifest (`jellyseerr.owner`,
  `MONARCH_SEERR_OWNER`) and `drift-check` now fails
  when Seerr is owned by somebody else.

- **ClipBucket's catalogue is the media library, and `tube.innotel.us` now has
  something in it.** A finished install is an *empty* site — measured
  2026-09-22, `cb_video` held 0 rows against a `files/videos/` holding only the
  app's own `example.mp4` — so the site answered correctly and showed nothing,
  which is the same thing a broken import looks like. `scripts/clipbucket-library.py`
  imports `/data/media` (what Jellyfin serves: movies and TV) into ClipBucket's
  catalogue and is idempotent, read-only against the library, and reportable:

  ```bash
  python3 scripts/clipbucket-library.py --check   # 0 in sync, 1 behind, 2 cannot tell
  python3 scripts/clipbucket-library.py --apply
  ```

  Three things have to be true for a video to be visible and each fails
  invisibly on its own, so one tool owns all three: a `cb_video` row in the
  state the browse query requires (filed under **Movies** or **TV Shows**, the
  show name as a tag), a playable file under the name the app itself *builds*
  (`files/videos/imported/<file_name>-<quality>.mp4` with `video_files` set —
  found by breaking on it: a bare `<file_name>.mp4` is read by
  `update_video_files()` as a resolution, so the player then requests a second
  file that was never written), and the five `num_thumbs` thumbnails at the five
  `VideoThumbs` resolutions with their `cb_video_image`/`cb_video_thumb` rows,
  since a row without them renders a broken card. An item's identity is its
  **path**, so a re-run converges rather than duplicating and a title edit in the
  admin area is not undone.

  Nothing is re-encoded, ever. Measured across the library (2 H.264/AAC MP4, 8
  H.264 in MKV, 5 HEVC): an already-web-playable MP4 is **hardlinked** (the
  media root and the docker volume share a filesystem, so it costs nothing), an
  MKV is remuxed with `-c:v copy` and audio to stereo AAC, and HEVC is remuxed
  the same way — playable where the client decodes HEVC and not in Firefox. Both
  `--check` and `--apply` name how many items carry HEVC, so it is a recorded
  number rather than a surprise. The copy's video codec is checked against the
  source's, which is what makes "a stream copy" an invariant: a copy an earlier
  version transcoded reads as drift and is rebuilt in seconds.
  `drift-check` runs the check (only once the install is finished, since every
  finding after that is a consequence of it), and the library IS the list: what
  should not be on the site belongs out of `/data/media`, not on a second
  exclusion list that would drift from it.

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
