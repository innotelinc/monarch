# Monarch — Deployment & Releases

## Installer & Live USB

The repo ships a single **installer** that runs both as a live-USB→disk
installer and as an in-place installer on an already-running Linux box, plus
a **live/install ISO** you can boot and run the installer from.

### Installer

`scripts/install-monarch.sh` installs the whole platform. Two modes:

- **In-place** (already-installed Linux): installs Docker (if missing),
  creates the `/data` folder layout, creates `.env` from `.env.sample` when
  missing, loads any offline Docker image archives it finds, installs the
  `monarch.service` systemd unit, and starts the stack via
  `docker compose up -d`. Prefer `./setup.sh` which also configures Nginx
  Proxy Manager.
- **Live USB → disk** (booted from the Monarch ISO): partitions the selected
  disk, copies the live system, installs GRUB (BIOS + UEFI), creates a
  `monarch` login user, enables DHCP networking, and runs the in-place
  install inside the new system.

From a checked-out repo on any Debian/Ubuntu box:

```
./scripts/install-monarch.sh
```

By default it installs to `/opt/monarch` and creates a login user `monarch`
(password `monarch`) for disk installs. Override with `MONARCH_TARGET`,
`MONARCH_USER`, `MONARCH_PASSWORD`, `MONARCH_DISK`, `MONARCH_YES=1` (skip
the destructive-disk confirmation). The installer is idempotent and
non-destructive in in-place mode (it excludes `.env` from the payload copy,
and never overwrites an existing `.env`).

### Fresh-install check

`scripts/fresh-install-check.sh` smoke-checks the fresh-install path — the
pieces `./setup.sh` runs on a brand-new box — without touching a real
deployment:

1. Renders a throwaway `.env` from `.env.sample` (random credentials, never
your real `.env`).
2. Renders the Homarr landing board (`homarr/board.default.json`) and asserts
it is valid JSON with no leftover `__DOMAIN__` placeholders, that every tile
links to the apex or a subdomain declared in `npm-hosts.conf`, and that the
main-interface (apex) tile is present.
3. Dry-runs `scripts/npm-proxy-hosts.py` and asserts the apex host
(`MONARCH_DOMAIN` → Homarr) is planned alongside the subdomains.
4. Validates `docker-compose.yml` interpolation with the throwaway `.env`
(uses the docker CLI; no daemon needed).

```
./scripts/fresh-install-check.sh          # safe anywhere - no Docker needed
```

Add `--full` to also boot the real `homarr` container on a **disposable
host/VM** (Docker + sudo required), seed its board exactly like `setup.sh`
does, and verify it serves the board at `http://127.0.0.1:7575`:

```
./scripts/fresh-install-check.sh --full
```

The `--full` stage refuses to run while a live Monarch stack or existing
`/docker/appdata/homarr` state is detected, and shuts the test container down
when it finishes (keep it running with `MONARCH_CHECK_KEEP=1`). Override the
test domain with `MONARCH_CHECK_DOMAIN` (default `monarch-check.test`).

### Live / install USB

Build the ISO locally (Ubuntu 24.04 "noble" host recommended):

```
sudo apt install -y live-build xorriso mtools genisoimage grub-efi-amd64-bin grub-pc-bin isolinux syslinux-common
./scripts/build-live-usb.sh
```

Output: `dist/live-usb/monarch-live-amd64.iso` plus its `.sha256`. Write it
to a USB stick (replace `/dev/sdX` with the whole device):

```
sudo dd if=dist/live-usb/monarch-live-amd64.iso of=/dev/sdX bs=4M status=progress
```

Boot the target computer from the USB, wait for the Xfce desktop, and launch
**Install Monarch**. For a fully **offline** install, also copy the offline
bundle (`monarch-deployment.tar.gz`, `docker-images-part*.tar.gz`,
`SHA256SUMS`) to a FAT32 partition of the USB stick. The ISO is BIOS + UEFI
hybrid (Secure Boot must be disabled); the installed system boots straight
into the stack via `monarch.service`.

### Offline bundle

```
./scripts/build-offline-bundle.sh            # payload + image bundle + checksums
./scripts/build-offline-bundle.sh --deployment-only   # payload + checksums only
```

Output: `dist/offline-bundle/` — the docker image archives are split into
<2 GB parts for GitHub release uploads. To fetch a published release's
bundle:

```
./scripts/fetch-offline-bundle.sh          # -> ~/monarch-offline-bundle
```


## Building a release

Releases are cut by `.github/workflows/release.yml`. The workflow:

1. **Tagged release** (`git tag v1.2.0 && git push --tags`) — or manual
   "Run workflow" (choose a `minor`/`major` bump, or `lightweight` to skip
   the heavy builds for a quick test).
2. **build-images** publishes the first-party images to GHCR
   (`monarch-recs`, `monarch-health` — tagged with the release version
   **and** `latest`).
3. **release** cuts the GitHub release with the deployment payload + source
   bundle + checksums.
4. Two parallel CI jobs build & upload the **docker image bundle** and the
   **live ISO**.

Each release publishes:

- `monarch-live-amd64.iso` + `.sha256` (bootable live/install ISO)
- `docker-images-part*.tar.gz` (offline docker image bundle)
- `monarch-deployment.tar.gz` (source + compose + systemd installer payload)
- `monarch-source-bundle.tar.gz` + checksum
- `SHA256SUMS`
- GHCR images `ghcr.io/innotelinc/monarch-{recs,health}`


## Manual setup (without the installer)

```bash
## Docker
apt update && apt -y upgrade
apt -y install ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt update
apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

## Folder structure (TRASH guide)
mkdir -p /data/{usenet/{incomplete,complete}/{tv,movies,music,xxx},media/{tv,movies,music,xxx},torrents/{tv,movies,music,xxx}}
chown -R 1000:1000 /data && chmod -R a=,a+rX,u+w,g+w /data

cd /opt
git clone https://github.com/innotelinc/monarch
cd monarch
cp .env.sample .env        # then edit MONARCH_USERNAME / MONARCH_PASSWORD etc.
sudo docker compose up -d
```


