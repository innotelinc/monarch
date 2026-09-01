# ARR stack NEW VERSION <br />
Below are instructions for Debian / Ubuntu operating system, but docker can be natively run on any linux distro <br />
and if you have Windows or Mac - you can use for tools like [Docker Desktop](https://docs.docker.com/desktop/) to run docker containers. <br />

Besides the manual setup below, this repo ships an **installer**, an **offline bundle**, and a **bootable live/install ISO** (the same packaging approach as the [Capstone](https://github.com/innotelinc/capstone) project). See the [Installer & Live USB](#installer--live-usb) section further down, and for building releases see the [Building a release](#building-a-release) section. <br />

#Install Docker <br />
apt update && apt -y upgrade <br />
apt -y remove apparmor <br />
apt -y install ca-certificates curl gnupg build-essential perl curl wget xsel <br />
mkdir -m 0755 -p /etc/apt/keyrings <br />
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg <br />

echo \ <br />
  "deb [arch="$(dpkg --print-architecture)" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \ <br />
  "$(. /etc/os-release && echo "$VERSION_CODENAME")" stable" | \ <br />
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null <br />

apt update <br />

apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose-plugin docker-ce-rootless-extras docker-buildx-plugin <br />

systemctl start docker <br />
systemctl enable docker <br />

#Install GoLang <br />
cd /usr/src <br />
wget https://go.dev/dl/go1.26.0.linux-amd64.tar.gz <br />
tar -C /usr/local -xvf go1.26.0.linux-amd64.tar.gz <br />

tee -a ~/.profile<<EOF <br />
export PATH=$PATH:/usr/local/go/bin <br />
EOF <br />

source ~/.profile <br />

go version <br />

cd /usr/src <br />
git clone https://github.com/docker/compose.git <br />
cd compose <br />
make <br />
mv ./bin/build/docker-compose /usr/local/bin/ <br />
chmod +x /usr/local/bin/docker-compose <br />
sudo ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose <br />

#Portainer <br />
docker volume create portainer_data <br />

docker run -d -p 8000:8000 -p 9443:9443 --name portainer --restart=always -v /var/run/docker.sock:/var/run/docker.sock -v portainer_data:/data portainer/portainer-ce:latest <br />

#Accesible http://localhost:9443 <br />

```

To test if docker compose has been installed, run :
`docker-compose`

You should get a lot of command arguments including 'version' one, so run again:
`docker-compose version`

That will show all works as expected. <br />
Create folder structure as per this [TRASH GUIDE](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/) now:

```
cd /opt <br />
sudo mkdir -p /data/{usenet/{incomplete,complete}/{tv,movies,music,xxx},media/{tv,movies,music,xxx},torrents/{tv,movies,music,xxx}} <br />
sudo apt install tree <br />
tree /data <br />
sudo chown -R 1000:1000 /data <br />
sudo chmod -R a=,a+rX,u+w,g+w /data <br />
ls -ln /data <br />

git clone https://github.com/innotelinc/arr.git <br />
cd arr <br />

cp .env.sample .env      # then edit ARR_USERNAME / ARR_PASSWORD (see below)

Note that hostnames are not needed here as we have dedicated network for our containers <br />

***************************

# First run: <br />

You should be able to run all services now with simple `sudo docker-compose up -d`

***************************

# Shared initial credentials

Edit `.env` (it is gitignored, so your credentials never get committed) - the two
most important variables are:

```
ARR_USERNAME=admin        # your username
ARR_PASSWORD=arrarr8      # your password
```

Those same credentials are applied automatically to **every service that
requires a login**: Jellyfin, Jellyseerr, Sonarr, Radarr, Lidarr, Whisparr,
Prowlarr, Bazarr, qBittorrent, Transmission, the Authentik bootstrap admin
(email `admin@innotel.us`) and the subscription platform's `/admin` panel.
Anything you change in `.env` later is picked up by the automation on the next
`docker-compose up -d` (the init containers re-run and only touch services
that still have default/no credentials).

***************************

# Everything is wired for you on first boot

Two one-shot containers do the wiring so you do **not** have to click through
the old manual setup:

| Container    | When            | What it does |
|--------------|-----------------|--------------|
| `arr-seed`   | before qBittorrent starts | writes qBittorrent's WebUI login (`ARR_USERNAME`/`ARR_PASSWORD`) into its config - no temporary-password dance |
| `arr-init`   | after the stack is up | wires the whole stack (below) |

`arr-init` automatically:

* **Jellyfin** - completes the first-run wizard (creates the admin user with
  your credentials), adds Media libraries (`/data/media/movies`, `tv`,
  `music`, `xxx`), logs in and exports the admin token to
  `/docker/appdata/init/jellyfin-api-key.txt`
* **Jellyfin LDAP-Auth plugin** - installs the plugin and writes its config so
  Jellyfin logins authenticate against the Authentik LDAP outpost (see the
  dedicated section below)
* **Sonarr / Radarr / Lidarr / Whisparr** - sets Forms authentication with
  your credentials, adds the correct root folder, adds the **qBittorrent**
  download client (category `tv` / `movies` / `music` / `xxx`) and enables
  hardlinks + extra-file import
* **Prowlarr** - sets Forms authentication, adds **qBittorrent** as the
  download client, and registers Radarr, Sonarr, Lidarr and Whisparr as
  **Apps** (full sync) - so indexers added in Prowlarr flow to all *arr apps
* **qBittorrent** - verifies the WebUI login and creates the `movies`, `tv`,
  `music` and `xxx` categories with their save paths under `/data/torrents`
  (default save path `/data/torrents`, no temp dir)
* **Bazarr** - sets basic authentication with your credentials and connects
  Sonarr + Radarr so subtitle syncing works
* **Jellyseerr** - initializes the request manager against **Jellyfin** and
  connects Radarr + Sonarr

Watch it work / check for problems:

```
sudo docker logs arr-init
sudo cat /docker/appdata/init/status.json      # per-service result + any issues list
```

Everything is idempotent - `arr-init` re-runs safely on every `up -d` and only
touches services that are still unconfigured.

***************************

# Services & ports

| Service    | URL                   | Notes |
|------------|-----------------------|-------|
| Homarr (dashboard) | http://localhost:7575 | |
| Jellyfin   | http://localhost:8096 | admin = your `.env` credentials |
| Jellyseerr | http://localhost:5055 | requests; connected to Jellyfin + Radarr/Sonarr |
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
| Subscription platform | http://localhost:3000 | signup + Stripe Checkout landing page |
| Authentik  | http://localhost:9000 | SSO + `paid_users` group = user management |
| Authentik LDAP | localhost:389 / 636 | LDAP outpost - Jellyfin logins authenticate against it |
| Billing API | http://localhost:8001 | Stripe webhooks -> Authentik paid users + Postgres |
| Clipbucket | http://localhost:8088 | video platform |
| Dispatcharr/TVHeadend/NextPVR/IPTV | 9191 / 9981 / 8866 / 3001 | live TV stack |

***************************

# What's still manual (one-time, mostly external)

The automation covers app-to-app wiring. These still need you:

1. **Add indexers to Prowlarr** (`http://<host>:9696` -> Settings -> Indexers).
   They flow automatically to Radarr/Sonarr/Lidarr/Whisparr because they are
   already registered as Apps. Legal/public-domain sources like
   **Archive.org** work great (see [Remaining config](#remaining-config)).
2. **FlareSolverr proxy** - Prowlarr -> Settings -> Indexers -> Indexer Proxies:
   host `http://flaresolverr:8191`, tag it e.g. `cloudflare` (optional).
3. **Jellyseerr** - after first login, check Settings -> Users/Plex: enable
   **Jellyfin** sign-in if you want subscribers to log in with their Jellyfin
   accounts (the local `ARR_USERNAME` login from the wizard always works).
4. **Subscription platform + Authentik** - see the dedicated section below.

***************************

# Subscription Landing Page + User Management (Authentik)

The stack's public signup page is [jellyfin-subscription-platform](https://github.com/innotelinc/jellyfin-subscription-platform)
(`http://<host>:3000`): visitors pick a plan and pay through Stripe Checkout.
**User management is Authentik-first**: payments made through the landing page
are mirrored into the `paid_users` group by **billing-api**, and subscribers
are given access in Authentik (the platform's own checkout/admin plumbing
still runs - you manage the Jellyfin side from its `/admin` panel).

GitHub Container Registry image (the repo is private, so authenticate first):

```
echo YOUR_GH_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

Configure it via `.env` (all variables are documented in `.env.sample`):

| Variable | What it is |
|----------|------------|
| `APP_URL` | public URL of the site, e.g. `https://subscribe.innotel.us` (Stripe redirects use it) |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe keys (webhook #1 - the platform) |
| `BILLING_WEBHOOK_SECRET` | signing secret of webhook #2 (billing-api); optional, falls back to `STRIPE_WEBHOOK_SECRET` |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | Jellyfin server + key (platform's own provisioning) |
| `JFA_GO_URL` | account portal link shown to users - now Authentik's self-service user settings, e.g. `http://localhost:9000/if/user/` (jfa-go was removed) |
| `REQUEST_URL` | Jellyseerr request portal shown to Premium users, e.g. `https://req.innotel.us` |
| `SESSION_SECRET` | long random string (`openssl rand -hex 32`) |

**Getting `JELLYFIN_API_KEY`:** `arr-init` creates the Jellyfin admin and
exports its token (it is a valid API credential) on first boot to
`/docker/appdata/init/jellyfin-api-key.txt`. Copy it into `.env`:

```
sudo cp /docker/appdata/init/jellyfin-api-key.txt /tmp/jfkey.txt
echo "JELLYFIN_API_KEY=$(cat /tmp/jfkey.txt)" | sudo tee -a /opt/arr/.env
sudo docker compose up -d
```

Alternatively create a normal key in Jellyfin Dashboard -> Advanced -> API Keys
and use that.

**Stripe webhooks:** Stripe dashboard -> Developers -> Webhooks -> add **two**
endpoints, both with the same six events:

```
1. URL:     https://YOUR-DOMAIN/api/webhook      -> platform (checkout UX + admin)
2. URL:     http://<host>:8001/api/webhook       -> billing-api (Authentik paid_users)

Events:  checkout.session.completed
         customer.subscription.updated
         customer.subscription.deleted
         invoice.payment_succeeded
         invoice.payment_failed
```

Copy the `whsec_...` signing secret of endpoint 1 into `STRIPE_WEBHOOK_SECRET`
and endpoint 2's secret into `BILLING_WEBHOOK_SECRET` (or reuse one for both).
When a checkout completes, billing-api creates (or finds) the subscriber in
Authentik, adds them to the `paid_users` group and records the subscription
in Postgres; cancels (`customer.subscription.deleted`) disable their access.

### Authentik setup (one-time)

Authentik boots with a **bootstrap admin** - the `AUTHENTIK_BOOTSTRAP_*` env
vars on the `authentik-worker` container create it for you, so there is no
setup wizard to click through:

| What | Value |
|------|-------|
| Admin UI | `http://localhost:9000` |
| Username | `akadmin` |
| Password | your `ARR_PASSWORD` from `.env` |
| Email | `admin@innotel.us` |

Bootstrap credentials are applied **only on first boot** (when the Authentik
database is empty). Changing `ARR_PASSWORD` in `.env` later does **not** reset
the admin password - change it in the admin UI instead (Directory -> Users ->
`akadmin`).

**The `paid_users` group is the source of truth for who has access.** billing-api
talks to Authentik's API with the shared `AUTHENTIK_BOOTSTRAP_TOKEN` from
`docker-compose.yml` and auto-creates the group on its first successful Stripe
webhook, so there is nothing to set up manually:

- **Checkout completes** (`checkout.session.completed` / invoice paid) ->
  billing-api finds or creates the subscriber in Authentik (username/email
  from the checkout) and adds them to `paid_users`
- **Subscription cancels** (`customer.subscription.deleted`) -> billing-api
  sets the user `is_active = false`, so their access is off

To verify or grant access manually: admin UI -> Directory -> Groups ->
`paid_users`. Anyone in that group counts as a subscriber - add or remove
users there to grant/revoke access without touching Stripe.

Subscribers manage their own password in Authentik's self-service settings
(`http://localhost:9000/if/user/` - the "account portal" link the landing
page shows). The platform's own `/admin` panel is separate and signs in with
`ADMIN_PASSWORD` (defaults to `ARR_PASSWORD`).

### Authentik LDAP -> Jellyfin (login gate)

**Jellyfin logins now authenticate against Authentik directly.** The stack
ships an Authentik **LDAP outpost** (`authentik-ldap` container) and wires the
Jellyfin **LDAP-Auth plugin** to it, so there is no separate Jellyfin user
store for subscribers: users and passwords live in Authentik, and **disabling
a user in Authentik blocks their Jellyfin login** (the LDAP bind fails).

Everything except adding users is automated:

| Piece | Who sets it up | What it is |
|-------|----------------|------------|
| `authentik-ldap` container | `docker-compose.yml` | LDAP outpost (`ghcr.io/goauthentik/ldap`), plain LDAP on 3389 / LDAPS on 6636 inside the network, also mapped to host `389`/`636` for testing |
| LDAP provider + outpost + app | billing-api on startup (`ensure_ldap_setup`) | base DN `dc=innotel,dc=us`; provider/outpost named `jellyfin-ldap`; the outpost's API token is pinned to `ak-ldap-outpost-2026` (must match the container's `AUTHENTIK_TOKEN`) |
| Bind service account | billing-api on startup | `authentik-ldap` service account + token pinned to `ak-ldap-bind-2026`; a role grants it "Search full LDAP directory" on the provider |
| Groups | billing-api on startup | `paid_users` (subscribers - created on first Stripe webhook) and `jellyfin_admins` (Jellyfin admins - auto-created) |
| Jellyfin LDAP-Auth plugin | `arr-init` | installs the plugin (catalog, falls back to the GitHub release) and writes its config to `plugins/LDAP-Auth/LDAP-Auth.xml`, then restarts Jellyfin |

**What the Jellyfin plugin is configured with** (values in the config file
arr-init writes):

| Setting | Value |
|---------|-------|
| LDAP Server / Port | `authentik-ldap` / `3389` (plain LDAP on the docker network) |
| Bind User | `cn=authentik-ldap,ou=users,dc=innotel,dc=us` (password = `ak-ldap-bind-2026`) |
| Base DN | `dc=innotel,dc=us` |
| Search Filter | `(memberOf=cn=paid_users,ou=groups,dc=innotel,dc=us)` |
| Admin Filter | `(memberOf=cn=jellyfin_admins,ou=groups,dc=innotel,dc=us)` |
| Username attribute | `cn` (users log in with their Authentik username, not email) |
| Create users | enabled (first successful login auto-creates the Jellyfin account with all libraries) |

**So the full access chain is:** Stripe checkout -> billing-api adds the user
to `paid_users` (and they are active) -> the LDAP search finds them and the
bind succeeds -> Jellyfin login works. Subscription cancels -> billing-api
sets `is_active = false` -> the LDAP bind fails -> Jellyfin login is blocked,
even though the user is still in the group. Granting access manually is just
Directory -> Groups -> `paid_users` -> add the user.

**Jellyfin admins are managed the same way.** Add a user to Directory ->
Groups -> `jellyfin_admins` (auto-created at provisioning) and on their next
Jellyfin login the LDAP plugin grants them admin; remove them and the next
login revokes it. Because the admin filter is the source of truth, admin
grants made manually inside Jellyfin (Dashboard -> Users) are overridden on
the user's next LDAP login - use the group instead. The local `ARR_USERNAME`
admin account is unaffected.

Notes:

* The existing Jellyfin admin (`ARR_USERNAME`) is a local account and keeps
  working - the LDAP plugin is an *additional* authentication provider.
* LDAP is plaintext **inside the private docker network only** (host ports
  `389`/`636` are for `ldapsearch` testing). To use LDAPS end-to-end, assign
a certificate to the provider (Applications -> Providers -> `jellyfin-ldap`
-> Protocol settings) and change the plugin config to port `6636` with
`UseSsl` enabled.
* Test the outpost from the host: `ldapsearch -x -H ldap://localhost:389 -b dc=innotel,dc=us -D "cn=authentik-ldap,ou=users,dc=innotel,dc=us" -w ak-ldap-bind-2026 '(memberOf=cn=paid_users,ou=groups,dc=innotel,dc=us)' cn`
* All the token values above are internal defaults hardcoded in
  `docker-compose.yml` (same pattern as `AUTHENTIK_BOOTSTRAP_TOKEN`) - keep
them in sync if you change any of them.

***************************

## Restart services: <br />
It might be a good idea to restart all services and see if they come up as expected: <br />

```
sudo docker-compose down
sudo docker-compose up -d
```
 <br />
If the first line that says : <br />
`WARN[0000] No services to build`  - this message is actually expected here.  <br />

**************************

## Remaining config: <br />
That should be it, you just need to add some indexers to Prowlarr. <br />
You can add more indexers - just google for something like 'what are the best legal indexers for Prowlarr' or something similar. <br />

It is a common misconception that the "Arr" stack is only for pirated content.  <br />
In reality, these are powerful automation tools for managing media, and there is a wealth of legal, copyright-free, and open-source content you can use them for. <br />
In Radarr, you can download movies that have entered the Public Domain or are released under Creative Commons licenses. <br />
Public Domain Classics: These are "Golden Age" movies where the copyright was not renewed like: <br />
Night of the Living Dead (1968), His Girl Friday (1940), Charade (1963), and The General (1926). <br />
Configure Prowlarr with The "Gold Standard" Indexer for legal media like The Internet Archive (Archive.org). <br />
They host thousands of public domain movies. <br />

## For live TV use this m3u: https://iptv-org.github.io/iptv/index.m3u <br />

**************************

**************************

# Installer & Live USB <br /> <br />

This repo mirrors the packaging approach of the [Capstone](https://github.com/innotelinc/capstone) project: a single **installer** that runs both as a live-USB→disk installer and as an in-place installer on an already-running Linux box, plus a **live/install ISO** you can boot and run the installer from. <br />

## Installer <br />

`scripts/install-arr.sh` installs the whole ARR Media Stack. It has two modes: <br />
- **In-place** (already-installed Linux): installs Docker (if missing), creates the `/data` folder layout, creates `.env` from `.env.sample` when missing, loads any offline Docker image archives it finds, installs the `arr.service` systemd unit, and starts the stack via `docker compose up -d`. <br />
- **Live USB → disk** (booted from the ARR ISO): partitions the selected disk, copies the live system, installs GRUB (BIOS + UEFI), creates an `arr` login user, enables DHCP networking, and runs the in-place install inside the new system. <br />

From a checked-out repo on any Debian/Ubuntu box: <br />
`./scripts/install-arr.sh` <br />

By default it installs to `/opt/arr` and creates a login user `arr` (password `arr`) for disk installs. Override with `ARR_TARGET`, `ARR_USER`, `ARR_PASSWORD`, `ARR_DISK`, `ARR_YES=1` (skip the destructive-disk confirmation). <br />

The installer is idempotent and non-destructive in in-place mode (it excludes `.env` from the payload copy, and never overwrites an existing `.env`). <br />

## Live / install USB <br />

Build the ISO locally (Ubuntu 24.04 "noble" host recommended): <br />
```
sudo apt install -y live-build xorriso mtools genisoimage grub-efi-amd64-bin grub-pc-bin isolinux syslinux-common
./scripts/build-live-usb.sh
```
Output: `dist/live-usb/arr-media-live-amd64.iso` plus its `.sha256`. <br />

Write it to a USB stick (replace `/dev/sdX` with the whole device, not a partition): <br />
```
sudo dd if=dist/live-usb/arr-media-live-amd64.iso of=/dev/sdX bs=4M status=progress
```

Boot the target computer from the USB, wait for the Xfce desktop, and launch **Install ARR Media**. For a fully **offline** install, also copy the offline bundle (`arr-deployment.tar.gz`, `docker-images-part*.tar.gz`, `SHA256SUMS`) to a FAT32 partition of the USB stick — the installer detects and stages it automatically. <br />

The ISO is BIOS + UEFI hybrid (Secure Boot must be disabled). The **installed system** boots straight into the stack: the `arr.service` systemd unit starts Docker and runs `docker compose up -d` on first boot, and ethernet comes up with DHCP on every interface. Login user is `arr` (password `arr` — change it after first login, or pre-set `ARR_USER`/`ARR_PASSWORD`). <br />

## Offline bundle <br />

The deployment payload + docker image archives are built by: <br />
```
./scripts/build-offline-bundle.sh            # payload + image bundle + checksums
./scripts/build-offline-bundle.sh --deployment-only   # payload + checksums only
```
Output: `dist/offline-bundle/`. The docker image archives are split into <2 GB parts for GitHub release uploads. <br />

Note: the `jellyfin-subscription-platform` image is published from a **private**
repo, so it is kept commented out of `scripts/offline-images.txt` - uncomment it
when building a private bundle and make sure the build host can `docker pull`
it (GHCR token with `read:packages`). <br />

To fetch and unpack a published release's offline bundle: <br />
```
./scripts/fetch-offline-bundle.sh          # -> ~/arr-offline-bundle
```
This verifies `SHA256SUMS`, unpacks the deployment payload, and reassembles the per-image archives into `dist/docker-images/`. Point `install-arr.sh` at the result (`ARR_ASSET_DIR=~/arr-offline-bundle`) and it loads images locally instead of pulling from the network. <br />

**************************

# Building a release <br /> <br />

Releases are cut automatically by `.github/workflows/release.yml`. The workflow: <br />
1. Computes the next version (minor/major bump from the last release). <br />
2. Builds the source bundle and the deployment payload. <br />
3. Cuts a GitHub release with those artifacts. <br />
4. Two parallel CI jobs build & upload the **docker image bundle** and the **live ISO**. <br />

To trigger it: **Actions → Release ARR Media Stack → Run workflow** (optionally pick a `major` bump or `lightweight` to skip the heavy ISO/image builds for a quick test release). Each release publishes: <br />
- `arr-media-live-amd64.iso` + `.sha256` (bootable live/install ISO) <br />
- `docker-images-part*.tar.gz` (offline docker image bundle) <br />
- `arr-deployment.tar.gz` (source + compose + systemd installer payload) <br />
- `arr-source-bundle.tar.gz` + checksum <br />
- `SHA256SUMS` <br />

**************************

# Troubleshooting: <br />
### arr-init / arr-seed:
`sudo docker logs arr-init` shows what the automation did. Its per-service
result and any "MANUAL ACTIONS NEEDED" list is in `/docker/appdata/init/status.json`.
If a service was mid-startup during the run, just re-run: `sudo docker start arr-init`
(or `sudo docker-compose up -d` - `arr-init` is idempotent).

### qBittorrent WebUI login fails with the configured password:
The pre-seeded PBKDF2 hash matches stock qBittorrent, but if you run into it:
grab the temporary password from `sudo docker logs qbittorrent` (search for
"A temporary password is provided for this session"), log in at
http://localhost:8080, set your password in **Tools > Options > Web UI**, then
re-run `sudo docker start arr-init` to recreate the categories.

### DNS check:
Test if your containers use CloudFlare DNS (configured in docker-compose.yml file): <br />
`sudo docker exec -it radarr cat /etc/resolv.conf` <br />

### Hardlinks check:<br />
Check if the hardlinks work as expected: <br />
Go to `/data` folder on your host and run `tree` and `du -sch *` commands to see the folder structure. <br />
Find the same file in torrents and media that you have just downloaded and run commands: <br />
`ls -i /data/media/movies/<your video>` and check its inode id (in first column, like 3881112) <br />
Then run again the same command but for the torrent folder: <br />
`ls -i /data/torrents/movies/<your video>` and see if the inode id is the same as above. <br />
If they are - your hardlinks work as expected. <br />
If they don't - first go to logs to see what is the problem (for Radarr/Sonarr go to System - Log Files) <br />
If you have issue where the file is copied rather than hardlinked, then the most probable cause <br />
is the read/write permission on either source or destination, but that can all be found in those logs so start there. <br />


### Files do not move from torrents to media folder: <br />
If the video does not move automatically from torrents to media, then check the Activity - Queue. <br />
You might have a flag saying: 'Downloaded - Unable to Import Automatically' <br />
Click the Manual Import (icon that looks like human head on the far right of the item row) <br />
Confirm the Movie: In the popup, ensure the correct movie is selected in the dropdown. If it is correct, click 'Import' <br />


### FlareSolverr: <br />
You might want to add FlareSolverr if you find Prowlarr is failing to index some sites due to "Cloudflare" blocks: <br />
The `flaresolverr` container is already in the stack. In Prowlarr: <br />
- Open your Prowlarr Web UI (http://localhost:9696) <br />
- Go to Settings > Indexers. <br />
- Click the + (Add) button under Indexer Proxies and select FlareSolverr. <br />
- Fill in the details: <br />
- Name: FlareSolverr <br />
- Host: http://flaresolverr:8191 (Note: Using the service name flaresolverr works because they are on the same Docker network). <br />
- Tags: Give it a tag like cloudflare (this is important). <br />
- Save the proxy <br /> <br />


### Jellyfin hardware acceleration: <br />
For Jellyfin hardware acceleration you might want to add bottom 2 lines:  <br />

```
jellyfin:
    <<: *common-keys
    <...snip...>
    devices:
      - /dev/dri:/dev/dri # << container setting to pass through GPU (this requires more steps outside of docker compose though)
```

### SABnzbd Usenet client <br />
If you use SABnzbd instead of qBittorrent then you need to add that to your yml file: <br />

```
  sabnzbd:
    container_name: sabnzbd
    image: ghcr.io/hotio/sabnzbd:latest
    ports:
      - 8080:8080
      - 9090:9090
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - /docker/appdata/sabnzbd:/config
      - /data:/data
```
<br />

Note that if you want to run both - qBittorrent AND sabnzbd - then you will have conflict for port 8080 <br />
as that port is also utilized by qBittorrent. <br />
You will need to change the external port for one of the services to something not used, for example: <br />

```
    ports:
      - 8081:8080
```
<br />

For sabnzbd you can use folder structure shown [HERE](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/) <br />
and then assign categories (similar to what we did in qbittorrent) following [THIS GUIDE](https://trash-guides.info/Downloaders/SABnzbd/Basic-Setup/) <br />