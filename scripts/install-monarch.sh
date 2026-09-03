#!/usr/bin/env bash
set -euo pipefail

# Monarch Media Platform installer.
#
# Two modes:
#   1. Live session (booted from the Monarch live/install USB): installs to a target
#      disk - partitions it, copies the live system, installs GRUB (BIOS and
#      UEFI), then runs the regular install inside the new system.
#   2. Already-installed Linux: installs the Monarch stack (Docker + images + systemd
#      service) into this system.
#
# The offline bundle (monarch-deployment.tar.gz + docker-images-part*.tar.gz) is used
# when present; otherwise the required pieces are fetched from GitHub and Docker
# registries over the network.

TARGET="${MONARCH_TARGET:-/opt/monarch}"

# Resolve the deployment root (the dir that holds docker-compose.yml / the
# deployment payload). The same installer runs from two layouts:
#   * repo layout:       <repo>/scripts/install-monarch.sh  -> ROOT=<repo>
#   * deployed layout:   /opt/monarch/install-monarch.sh         -> ROOT=/opt/monarch
# A naive "dirname $0/.." only matches the repo layout; in the deployed system
# it resolves one level too high (to /opt), which makes install_in_place treat
# ASSET_DIR!=TARGET and recursively rsync /opt into /opt/monarch -- creating a
# stray /opt/monarch/monarch and wiping the real payload.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR"
for cand in "$SCRIPT_DIR" "$(cd "$SCRIPT_DIR/.." && pwd)"; do
  if [ -e "$cand/docker-compose.yml" ] || \
     [ -e "$cand/monarch-deployment.tar.gz" ] || \
     [ -e "$cand/scripts/install-monarch.sh" ]; then
    ROOT="$cand"
    break
  fi
done
ASSET_DIR="${MONARCH_ASSET_DIR:-$ROOT}"
RELEASE_TAG="${MONARCH_RELEASE_TAG:-v1.0.0}"
REPO_SLUG="${MONARCH_REPO:-innotelinc/monarch}"
MONARCH_DISK="${MONARCH_DISK:-}"

# Local login account for the installed system (documented in the README).
# Overridable with MONARCH_USER / MONARCH_PASSWORD.
MONARCH_USER="${MONARCH_USER:-monarch}"
MONARCH_PASSWORD="${MONARCH_PASSWORD:-monarch}"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

is_live_session() {
  # MONARCH_IN_CHROOT guards the second (chrooted) phase of a disk install.
  [ "${MONARCH_IN_CHROOT:-0}" = "1" ] && return 1
  [ -d /run/live ] || [ -d /run/live/medium ] || grep -q ' boot=live ' /proc/cmdline 2>/dev/null
}

install_docker() {
  command -v docker >/dev/null 2>&1 && return 0
  if command -v apt-get >/dev/null 2>&1 && [ "${MONARCH_ALLOW_APT:-1}" = "1" ]; then
    $SUDO apt-get update
    # docker-compose-v2 is the Ubuntu-archive plugin; docker-compose-plugin is
    # the Docker-repo name. Try both so either distro works.
    $SUDO apt-get install -y docker.io docker-compose-v2 curl rsync openssl python3 2>/dev/null || \
      $SUDO apt-get install -y docker.io docker-compose-plugin curl rsync openssl python3
    $SUDO systemctl enable --now docker 2>/dev/null || true
  fi
  command -v docker >/dev/null 2>&1 || {
    echo "Docker is unavailable. Connect to the internet or provide a preinstalled Docker runtime." >&2
    exit 2
  }
}

load_docker_images() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  local found=0
  for archive in "$dir"/*.tar.gz; do
    [ -e "$archive" ] || continue
    found=1
    echo "Loading $(basename "$archive")"
    gzip -dc "$archive" | $SUDO docker load
  done
  if [ "$found" -eq 0 ]; then
    echo "No image archives found in $dir - images will be pulled from the network if needed." >&2
  fi
}

install_service() {
  local root="$1"
  $SUDO mkdir -p "$root/etc/monarch"
  $SUDO cp "$root$TARGET/systemd/monarch.service" "$root/etc/systemd/system/monarch.service" 2>/dev/null || \
    $SUDO cp "$ROOT/systemd/monarch.service" "$root/etc/systemd/system/monarch.service"
  $SUDO sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$TARGET|" "$root/etc/systemd/system/monarch.service"
  # Live-stack drift check: a read-only monitor on a timer (see
  # scripts/drift-check.sh). Installed alongside the main service so every
  # install/update gets it; the ExecStart script path is relative to
  # WorkingDirectory, so only that needs rewriting.
  $SUDO cp "$root$TARGET/systemd/monarch-drift-check.service" "$root/etc/systemd/system/monarch-drift-check.service" 2>/dev/null || \
    $SUDO cp "$ROOT/systemd/monarch-drift-check.service" "$root/etc/systemd/system/monarch-drift-check.service"
  $SUDO cp "$root$TARGET/systemd/monarch-drift-check.timer" "$root/etc/systemd/system/monarch-drift-check.timer" 2>/dev/null || \
    $SUDO cp "$ROOT/systemd/monarch-drift-check.timer" "$root/etc/systemd/system/monarch-drift-check.timer"
  $SUDO sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$TARGET|" "$root/etc/systemd/system/monarch-drift-check.service"
  # Local Nginx Proxy Manager is opt-in via the "npm" compose profile. When
  # NPM_MODE=remote (MONARCH_NPM_LOCAL=0) drop the --profile flag so the unit
  # never starts a local NPM container.
  if [ "${MONARCH_NPM_LOCAL:-1}" != "1" ]; then
    $SUDO sed -i '/--profile npm/d' "$root/etc/systemd/system/monarch.service"
  fi
  # systemctl --root works offline (no running systemd needed), so it also
  # works from inside the installer chroot. Prefer it whenever a root dir is
  # given, or when we're in the chroot phase of a disk install.
  if [ -n "$root" ] || [ "${MONARCH_IN_CHROOT:-0}" = "1" ]; then
    local sysroot="${root:-/}"
    $SUDO systemctl --root "$sysroot" daemon-reload 2>/dev/null || true
    $SUDO systemctl --root "$sysroot" enable monarch.service 2>/dev/null || \
      $SUDO ln -sf /etc/systemd/system/monarch.service "$sysroot/etc/systemd/system/multi-user.target.wants/monarch.service"
    $SUDO systemctl --root "$sysroot" enable monarch-drift-check.timer 2>/dev/null || \
      $SUDO ln -sf /etc/systemd/system/monarch-drift-check.timer "$sysroot/etc/systemd/system/timers.target.wants/monarch-drift-check.timer"
    # start only makes sense with a running systemd (live install)
    if [ -z "$root" ] && [ -d /run/systemd/system ]; then
      $SUDO systemctl start monarch.service 2>/dev/null || true
      $SUDO systemctl start monarch-drift-check.timer 2>/dev/null || true
    fi
  else
    $SUDO systemctl daemon-reload 2>/dev/null || true
    $SUDO systemctl enable monarch.service 2>/dev/null || \
      $SUDO ln -sf /etc/systemd/system/monarch.service /etc/systemd/system/multi-user.target.wants/monarch.service
    $SUDO systemctl enable monarch-drift-check.timer 2>/dev/null || \
      $SUDO ln -sf /etc/systemd/system/monarch-drift-check.timer /etc/systemd/system/timers.target.wants/monarch-drift-check.timer
    $SUDO systemctl start monarch.service 2>/dev/null || true
    $SUDO systemctl start monarch-drift-check.timer 2>/dev/null || true
  fi
}

create_data_dirs() {
  # TRASH-guide folder layout + ownership so hardlinks between downloaders and
  # media work out of the box.
  local root="$1"
  $SUDO mkdir -p "$root/data/usenet/{incomplete,complete}/{tv,movies,music,xxx}" \
                "$root/data/media/{tv,movies,music,xxx}" \
                "$root/data/torrents/{tv,movies,music,xxx}"
  $SUDO chown -R 1000:1000 "$root/data"
  $SUDO chmod -R a=,a+rX,u+w,g+w "$root/data"
  # authentik's image runs as a non-root user (UID 1000) and cannot create its
  # own /media + /templates dirs if the host mounts are root-owned - pre-create
  # them with the same ownership as the rest of the stack. The postgresql data
  # dir is intentionally NOT chowned: postgres:17 runs as UID 999 and owns its
  # own data - chowning it to 1000 breaks the database.
  $SUDO mkdir -p "$root/docker/appdata/authentik/media" \
                "$root/docker/appdata/authentik/templates" \
                "$root/docker/appdata/authentik/redis"
  $SUDO chown -R 1000:1000 "$root/docker/appdata/authentik/media" \
                            "$root/docker/appdata/authentik/templates"
  # redis:alpine's server drops to the redis user (UID 999) - the /data dir
  # must be owned by it or RDB snapshots fail with MISCONF permission errors.
  $SUDO chown -R 999:1000 "$root/docker/appdata/authentik/redis"
}

# ---- Mode 2: install into the running (already-installed) system ----
install_in_place() {
  mkdir -p "$TARGET"
  if [ "$ASSET_DIR" != "$TARGET" ]; then
    $SUDO rsync -a --delete --exclude '.env' "$ASSET_DIR/" "$TARGET/"
  fi
  install_docker
  create_data_dirs ""
  # First-run env: create .env from the sample so every ${VAR} default in
  # docker-compose.yml resolves consistently. Never overwrite an existing .env
  # (the rsync above already excluded it on purpose). Fill in a SESSION_SECRET
  # so the subscription platform can boot.
  if [ ! -f "$TARGET/.env" ] && [ -f "$TARGET/.env.sample" ]; then
    $SUDO cp "$TARGET/.env.sample" "$TARGET/.env"
    if grep -q '^SESSION_SECRET=change-me' "$TARGET/.env"; then
      $SUDO sed -i "s|^SESSION_SECRET=change-me.*|SESSION_SECRET=$(openssl rand -hex 32 2>/dev/null || tr -dc 'a-f0-9' < /dev/urandom | head -c 64)|" "$TARGET/.env"
    fi
    echo "Created $TARGET/.env from .env.sample - edit MONARCH_USERNAME / MONARCH_PASSWORD and the Stripe keys."
  else
    echo ".env already present - leaving it untouched."
  fi
  load_docker_images "$TARGET/dist/docker-images"
  install_service ""
  echo "Monarch Media Platform installed at $TARGET and started."
  echo "Open the Homarr dashboard at http://<this-host>:7575"
}

# ---- Mode 1: live USB -> install to a disk ----
install_to_disk() {
  local disk="$MONARCH_DISK"

  if [ -z "$disk" ]; then
    # Exclude the live medium's disk and prefer a disk with no partitions.
    local live_disk=""
    local src=""
    src="$(findmnt -n -o SOURCE /run/live/medium 2>/dev/null || true)"
    [ -n "$src" ] && live_disk="$(lsblk -no PKNAME "$src" 2>/dev/null || true)"
    for cand in $(lsblk -dn -o NAME,TYPE | \
      awk -v excl="$live_disk" '$2=="disk" && $1 != excl && $1 !~ /^loop|^ram/ {print $1}'); do
      if [ "$(lsblk -ln -o NAME,TYPE "/dev/$cand" 2>/dev/null | awk '$2=="part"' | wc -l)" -eq 0 ]; then
        disk="/dev/$cand"
        break
      fi
    done
    if [ -z "$disk" ]; then
      disk="$(lsblk -dn -o NAME,TYPE | \
        awk -v excl="$live_disk" '$2=="disk" && $1 != excl && $1 !~ /^loop|^ram/ {print "/dev/"$1; exit}')"
    fi
  fi
  if [ -z "$disk" ] || [ ! -b "$disk" ]; then
    echo "No target disk found. Set MONARCH_DISK=/dev/sdX to choose one." >&2
    echo "Disks:" >&2
    lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS 2>/dev/null >&2
    exit 2
  fi

  if [ "${MONARCH_YES:-0}" != "1" ]; then
    echo "======================================================"
    echo " Monarch Media Platform will install to: $disk"
    echo " ALL DATA ON THIS DISK WILL BE DESTROYED."
    echo "======================================================"
    lsblk "$disk"
    local answer=""
    if [ -e /dev/tty ]; then
      read -r -p "Type YES to continue: " answer < /dev/tty || true
    else
      read -r -p "Type YES to continue: " answer || true
    fi
    # Normalize so the check is forgiving of case/spaces (e.g. "yes", " YES ").
    answer="$(printf '%s' "$answer" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')"
    [ "$answer" = "YES" ] || { echo "Aborted."; exit 1; }
  fi

  # Partition: MBR with a small FAT32 ESP/boot partition + ext4 root.
  parted -s "$disk" mklabel msdos
  parted -s "$disk" mkpart primary fat32 1MiB 513MiB
  parted -s "$disk" set 1 boot on
  parted -s "$disk" set 1 esp on
  parted -s "$disk" mkpart primary ext4 513MiB 100%
  sleep 2
  partprobe "$disk" 2>/dev/null || true
  sleep 2

  local parts=()
  while IFS= read -r name; do parts+=("/dev/$name"); done < <(
    lsblk -ln -o NAME,TYPE "$disk" | awk '$2=="part"{print $1}')
  [ "${#parts[@]}" -ge 2 ] || { echo "Failed to create partitions on $disk" >&2; exit 1; }
  local efi_part="${parts[0]}" root_part="${parts[1]}"

  echo "Formatting $efi_part (FAT32) and $root_part (ext4)..."
  mkfs.vfat -F32 "$efi_part"
  mkfs.ext4 -F "$root_part"

  local mnt
  mnt="$(mktemp -d)"
  mount "$root_part" "$mnt"
  mkdir -p "$mnt/boot/efi"
  mount "$efi_part" "$mnt/boot/efi"

  echo "Copying the live system to $root_part..."
  local squash=""
  for candidate in \
    /run/live/medium/live/filesystem.squashfs \
    /live/filesystem.squashfs \
    /media/*/live/filesystem.squashfs; do
    [ -f "$candidate" ] && squash="$candidate" && break
  done
  if [ -n "$squash" ]; then
    unsquashfs -f -d "$mnt" "$squash"
  else
    rsync -aAX --one-file-system \
      --exclude '/proc/*' --exclude '/sys/*' --exclude '/dev/*' \
      --exclude '/run/*' --exclude '/mnt/*' --exclude '/media/*' \
      --exclude '/tmp/*' --exclude '/live' --exclude '/opt/monarch' \
      / "$mnt/"
  fi

  # Stage the Monarch application payload (source/compose/systemd) into the target.
  # The baked ISO copy or the offline bundle on the USB is used when present;
  # otherwise the payload is fetched after boot.
  mkdir -p "$mnt/opt/monarch"
  if [ -f "$ROOT/monarch-deployment.tar.gz" ]; then
    tar -xzf "$ROOT/monarch-deployment.tar.gz" -C "$mnt/opt/monarch"
  elif [ -f /opt/monarch/monarch-deployment.tar.gz ]; then
    tar -xzf /opt/monarch/monarch-deployment.tar.gz -C "$mnt/opt/monarch"
  else
    echo "No deployment bundle found; the target will fetch it after boot." >&2
  fi

  # Stage offline docker images if the bundle is on an attached medium.
  mkdir -p "$mnt/opt/monarch/dist"
  for medium in /media/* /mnt/* /run/live/medium; do
    [ -d "$medium" ] || continue
    if [ -d "$medium/dist/docker-images" ]; then
      echo "Copying offline images from $medium..."
      cp -a "$medium/dist/docker-images" "$mnt/opt/monarch/dist/"
    elif [ -f "$medium/docker-images.tar.gz" ]; then
      echo "Copying offline images from $medium..."
      mkdir -p "$mnt/opt/monarch/dist/docker-images"
      if [ -x "$ROOT/scripts/split-image-bundle.sh" ]; then
        bash "$ROOT/scripts/split-image-bundle.sh" \
          "$medium/docker-images.tar.gz" "$mnt/opt/monarch/dist/docker-images"
      else
        tar -xzf "$medium/docker-images.tar.gz" -C "$mnt/opt/monarch/dist/docker-images" --strip-components=1 2>/dev/null \
          || tar -xzf "$medium/docker-images.tar.gz" -C "$mnt/opt/monarch/dist/docker-images"
      fi
    fi
  done

  # chroot setup
  mount --bind /dev "$mnt/dev"
  mount --bind /dev/pts "$mnt/dev/pts"
  mount --bind /proc "$mnt/proc"
  mount --bind /sys "$mnt/sys"
  cp /etc/resolv.conf "$mnt/etc/resolv.conf" 2>/dev/null || true

  local uuid_root uuid_efi
  uuid_root="$(blkid -s UUID -o value "$root_part")"
  uuid_efi="$(blkid -s UUID -o value "$efi_part")"
  cat > "$mnt/etc/fstab" <<EOF
UUID=$uuid_root / ext4 errors=remount-ro 0 1
UUID=$uuid_efi /boot/efi vfat umask=0077 0 1
EOF

  # The chroot script creates a login user, sets up DHCP networking and
  # enables NetworkManager + docker + the monarch service. systemctl needs
  # `--root /` here because no systemd is running inside the chroot.
  cat > "$mnt/tmp/monarch-chroot.sh" <<'CHROOT'
#!/bin/bash
set -e
if [ -d /sys/firmware/efi ]; then
  echo "Installing GRUB for UEFI..."
  grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=monarch --recheck
else
  echo "Installing GRUB for BIOS..."
  grub-install --target=i386-pc --recheck "$MONARCH_DISK"
fi
update-grub

# ── login user (the live CD's 'user' account only exists in the live session)
# Create a real login account with a known password + sudo + docker groups.
if ! id "$MONARCH_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G sudo,docker "$MONARCH_USER" 2>/dev/null || \
    useradd -m -s /bin/bash -G sudo "$MONARCH_USER"
  echo "$MONARCH_USER:$MONARCH_PASSWORD" | chpasswd
  echo "Created login user: $MONARCH_USER"
fi
# passwordless sudo for the monarch admin user
cat > /etc/sudoers.d/99-monarch-admin <<EOF
$MONARCH_USER ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 /etc/sudoers.d/99-monarch-admin

# ── networking: DHCP on every ethernet interface ──────────────────────────
# The live session gets its IP from NetworkManager; the installed system must
# too. Enable NetworkManager + systemd-networkd and add a netplan rule so
# ethernet comes up with DHCP on first boot (works with or without NM).
mkdir -p /etc/netplan
cat > /etc/netplan/99-monarch-dhcp.yaml <<'NETPLAN'
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    all-eth:
      match:
        name: "en*"
      dhcp4: true
      optional: true
NETPLAN
systemctl --root / enable NetworkManager 2>/dev/null || true
systemctl --root / enable systemd-networkd 2>/dev/null || true
systemctl --root / enable docker 2>/dev/null || true
CHROOT
  chmod 0755 "$mnt/tmp/monarch-chroot.sh"
  MONARCH_DISK="$disk" MONARCH_USER="$MONARCH_USER" MONARCH_PASSWORD="$MONARCH_PASSWORD" \
    chroot "$mnt" /bin/bash /tmp/monarch-chroot.sh
  rm -f "$mnt/tmp/monarch-chroot.sh"

  echo "Installing the Monarch application inside the new system..."
  MONARCH_IN_CHROOT=1 MONARCH_DISK="$disk" \
    chroot "$mnt" /bin/bash /opt/monarch/scripts/install-monarch.sh || {
      echo "Application install inside the target reported an error; the system is still installed." >&2
    }

  umount "$mnt/boot/efi" 2>/dev/null || true
  umount "$mnt/dev/pts" 2>/dev/null || true
  umount "$mnt/dev" 2>/dev/null || true
  umount "$mnt/proc" 2>/dev/null || true
  umount "$mnt/sys" 2>/dev/null || true
  umount "$mnt" 2>/dev/null || true
  rmdir "$mnt" 2>/dev/null || true

  echo "======================================================"
  echo " Monarch Media Platform is installed on $disk."
  echo " Remove the USB stick and reboot into the new system."
  echo "======================================================"
}

if is_live_session; then
  install_to_disk
else
  install_in_place
fi