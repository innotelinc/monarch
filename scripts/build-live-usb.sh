#!/usr/bin/env bash
set -euo pipefail

# Build a bootable ARR Media Stack live/install ISO.
#
# The image boots into an Xfce desktop (autologin as the "user" live account)
# with two launchers:
#   * Download ARR bundle   - fetch the release + offline bundle from GitHub
#   * Install ARR Media     - install to this machine (live USB -> disk, or
#                             onto an already-installed Linux system)
#
# The ISO is BIOS + UEFI hybrid: GRUB el-torito for CD/BIOS, isohybrid MBR for
# BIOS-from-USB, and a GRUB EFI image for UEFI (Secure Boot must be disabled).
#
# Requirements: live-build, xorriso, grub-efi-amd64-bin, mtools, genisoimage,
# isolinux (isohdpfx.bin). Output: dist/live-usb/arr-media-live-amd64.iso

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO/dist/live-usb}"
WORK_DIR="${WORK_DIR:-$REPO/.live-build}"
ISO_NAME="${ISO_NAME:-arr-media-live-amd64.iso}"
ISO_VOLUME="ARR_MEDIA"
BOOTARGS="boot=live components quiet splash"

for cmd in lb xorriso grub-mkimage mformat mcopy; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "required tool missing: $cmd" >&2; exit 1; }
done
[ -f /usr/lib/ISOLINUX/isohdpfx.bin ] || { echo "isolinux (isohdpfx.bin) is required" >&2; exit 1; }

mkdir -p "$OUT_DIR"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

lb config \
  --distribution noble \
  --architectures amd64 \
  --binary-images iso \
  --bootloader grub2 \
  --initramfs live-boot \
  --initsystem systemd \
  --archive-areas "main universe" \
  --bootappend-live "$BOOTARGS" \
  --iso-application "ARR Media Stack" \
  --iso-publisher "Innotel" \
  --iso-volume "$ISO_VOLUME" \
  --zsync false

# ---- chroot package list: desktop + tools + installer dependencies ----
cat > config/package-lists/arr.list.chroot <<'EOF'
# live system bootstrap
live-boot
live-config
live-config-systemd
user-setup
# desktop
xorg
xfce4
lightdm
lightdm-gtk-greeter
xterm
dbus-x11
network-manager
network-manager-gnome
# installer tooling (live -> disk)
squashfs-tools
parted
dosfstools
grub2-common
grub-pc-bin
grub-efi-amd64-bin
efibootmgr
# docker runtime (baked in so the installed system has it offline too)
docker.io
docker-compose-v2
# network + utilities
sudo
curl
wget
ca-certificates
git
rsync
openssl
python3
gnupg
tree
bash-completion
EOF

# ---- includes: installer scripts, autologin, sudo, desktop launchers ----
mkdir -p config/includes.chroot/opt/arr
cp "$REPO/scripts/install-arr.sh" config/includes.chroot/opt/arr/
cp "$REPO/scripts/fetch-offline-bundle.sh" config/includes.chroot/opt/arr/
chmod 0755 config/includes.chroot/opt/arr/*.sh

# Bake the deployment bundle into the ISO so an offline install always has the
# full source/compose payload (docker images still come from the USB medium).
if [ -f "$REPO/dist/offline-bundle/arr-deployment.tar.gz" ]; then
  cp "$REPO/dist/offline-bundle/arr-deployment.tar.gz" config/includes.chroot/opt/arr/
fi

mkdir -p config/includes.chroot/etc/lightdm/lightdm.conf.d
cat > config/includes.chroot/etc/lightdm/lightdm.conf.d/50-arr-autologin.conf <<'EOF'
[Seat:*]
autologin-user=user
autologin-user-timeout=0
user-session=xfce
EOF

mkdir -p config/includes.chroot/etc/sudoers.d
cat > config/includes.chroot/etc/sudoers.d/99-arr-live <<'EOF'
user ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 config/includes.chroot/etc/sudoers.d/99-arr-live

mkdir -p config/includes.chroot/etc/skel/Desktop
cat > config/includes.chroot/etc/skel/Desktop/Download-ARR.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Download ARR bundle
Comment=Download the ARR Media Stack release and offline bundle
Exec=x-terminal-emulator -e /opt/arr/fetch-offline-bundle.sh
Icon=network-server
Terminal=true
Categories=System;
EOF
cat > config/includes.chroot/etc/skel/Desktop/Install-ARR.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Install ARR Media
Comment=Install ARR Media Stack (live USB -> internal disk, or this system)
Exec=x-terminal-emulator -e sudo /opt/arr/install-arr.sh
Icon=drive-harddisk
Terminal=true
Categories=System;
EOF
chmod 0644 config/includes.chroot/etc/skel/Desktop/*.desktop

# ---- build ----
lb build

# The ISO produced by live-build is discarded; we re-master from the binary/
# tree so we can add the EFI boot image and isohybrid MBR ourselves.
[ -d binary/live ] || { echo "live-build did not produce binary/live" >&2; exit 1; }
[ -f binary/boot/grub/grub_eltorito ] || { echo "live-build did not produce the GRUB boot image" >&2; exit 1; }

# GRUB 2.12's grub-mkimage requires -p; live-build 3.0's el-torito step
# omits it and produces an empty core, so rebuild grub_eltorito here.
#
# With prefix /boot/grub the PC core resolves modules from the platform
# subdir /boot/grub/i386-pc/normal.mod (not the flat /boot/grub/*.mod that
# live-build copies). A BIOS/CSM boot (e.g. VMware) fails with
# "file /boot/grub/i386-pc/normal.mod not found" unless those modules exist
# there, so copy the full i386-pc module set into that subdirectory.
core_img="$(mktemp)"
grub-mkimage -d /usr/lib/grub/i386-pc -p /boot/grub \
  -o "$core_img" -O i386-pc biosdisk iso9660 \
  normal
cat /usr/lib/grub/i386-pc/cdboot.img "$core_img" > binary/boot/grub/grub_eltorito
rm -f "$core_img"
mkdir -p binary/boot/grub/i386-pc
cp /usr/lib/grub/i386-pc/* binary/boot/grub/i386-pc/

# ---- custom GRUB config: locate the ISO by volume label ----
KERNEL="$(basename "$(echo binary/live/vmlinuz-* | awk '{print $1}')")"
INITRD="initrd.img-${KERNEL#vmlinuz-}"
[ -f "binary/live/$KERNEL" ] || { echo "kernel not found in binary/live" >&2; exit 1; }
[ -f "binary/live/$INITRD" ] || { echo "initrd not found in binary/live" >&2; exit 1; }

cat > binary/boot/grub/grub.cfg <<EOF
set default=0
set timeout=10

insmod all_video
insmod gfxterm
insmod part_gpt
insmod part_msdos
insmod iso9660
insmod fat
insmod ext2
insmod search
insmod search_label

search --no-floppy --set=root --label $ISO_VOLUME

menuentry "ARR Media Stack - Live" {
    linux /live/$KERNEL $BOOTARGS
    initrd /live/$INITRD
}

menuentry "ARR Media Stack - Live (failsafe)" {
    linux /live/$KERNEL boot=live components noapic noacpi nomodeset
    initrd /live/$INITRD
}

menuentry "Reboot" {
    reboot
}
EOF

# ---- EFI boot image (GRUB x86_64-efi in a FAT image) ----
rm -rf efiboot
mkdir -p efiboot/EFI/BOOT
grub-mkimage -p /EFI/BOOT -O x86_64-efi \
  -o efiboot/EFI/BOOT/BOOTX64.EFI \
  normal linux echo configfile search search_label search_fs_uuid \
  loopback test loadenv part_gpt part_msdos iso9660 fat ext2 \
  all_video gfxterm font cat chain boot ls help
cp binary/boot/grub/grub.cfg efiboot/EFI/BOOT/grub.cfg
# Also place the EFI tree in the ISO filesystem root (as Ubuntu ISOs do) for
# compatibility with Windows USB-writing tools and some firmware quirks.
mkdir -p binary/EFI/BOOT
cp efiboot/EFI/BOOT/BOOTX64.EFI binary/EFI/BOOT/
cp efiboot/EFI/BOOT/grub.cfg binary/EFI/BOOT/

EFI_BYTES="$(du -sb efiboot | cut -f1)"
EFI_SECTORS=$(( EFI_BYTES * 3 / 512 + 128 ))
# Keep the image FAT32 (>= 65526 sectors) for widest UEFI firmware support.
# 67584 sectors x 512 B = 33 MiB, divisible by 32 (1 head x 32 sectors/track).
[ "$EFI_SECTORS" -lt 67584 ] && EFI_SECTORS=67584
mformat -C -F -i binary/boot/grub/efi.img -T "$EFI_SECTORS" ::
mcopy -s -i binary/boot/grub/efi.img efiboot/* ::
rm -rf efiboot

# ---- re-master: El Torito (BIOS) + EFI + isohybrid MBR (USB) ----
xorriso -as mkisofs \
  -V "$ISO_VOLUME" \
  -J -R -l -allow-multidot -cache-inodes \
  -A "ARR Media Stack" -publisher "Innotel" \
  -no-emul-boot -boot-load-size 4 -boot-info-table -b boot/grub/grub_eltorito \
  -eltorito-alt-boot -no-emul-boot -e boot/grub/efi.img \
  -isohybrid-mbr /usr/lib/ISOLINUX/isohdpfx.bin \
  -isohybrid-gpt-basdat \
  -o "$OUT_DIR/$ISO_NAME" binary

sha256sum "$OUT_DIR/$ISO_NAME" > "$OUT_DIR/$ISO_NAME.sha256"
echo "Created $OUT_DIR/$ISO_NAME"