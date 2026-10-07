#!/bin/bash
# Build the DEBUG / RESCUE boot set for a board window, from the artefacts
# build.sh made — no Buildroot rebuild, the shipped Image is never touched.
#
# Since 2026-09-23 the rootfs is EMBEDDED in Image (IMAGE_CONTRACT §1), so the
# debug set is an OVERLAY: the kernel unpacks its built-in initramfs, then any
# external initrd ON TOP of it (later entries override earlier ones). So:
#
#   rootfs_debug_overlay.cpio.gz  only what differs from the image:
#       /etc/inittab            the image's own inittab (managed block kept),
#                               with (a) -/bin/login on the REAL console device
#                               ttyUL0 instead of `getty console` — /dev/console
#                               cannot be a controlling tty, which is why the July
#                               WAVE-4 debug image showed no login — plus a
#                               sysinit alias so ttyUL0 exists under QEMU, and
#                               (b) the harness respawn line COMMENTED OUT, so a
#                               UIO fault in harnessd cannot take the boot down
#                               before a human is logged in (start it by hand:
#                               /usr/sbin/mps3-harnessd, or the placeholder)
#       /etc/init.d/S99debugmon zero-input banner + /proc/interrupts dump
#       /root/README.DEBUG      how to bisect by hand
#   shell_linux_debug.dtb       shell_linux.dtb + linux,initrd-start/-end STAMPED
#                               for the overlay at 0x8400_0000 (the July root
#                               cause: an unstamped initrd-end made the kernel scan
#                               uninitialised DDR -> "invalid magic")
#   shell_linux_rescue.dtb      shell_linux.dtb + rdinit=/bin/sh: PID 1 is a raw
#                               shell (no rc, no getty, no timer dependence). It MUST
#                               be rdinit=, not init= (an initramfs execs /init;
#                               init= is silently ignored — silicon, 2026-07-18).
#
# Board recipe (xsdb dow, 4 regions): fw_jump.bin@0x80000000, Image@0x80400000,
# shell_linux_debug.dtb@0x82200000, rootfs_debug_overlay.cpio.gz@0x84000000;
# pc=0x80000000 a0=0 a1=0x82200000. The rescue DTB needs no overlay region.
# Root password: the image's (artifacts/ROOT_PASSWD or $MPS3_ROOT_PASSWD).
set -euo pipefail
cd "$(dirname "$0")"
SW=$PWD
ART=${MPS3_ARTIFACTS:-$SW/artifacts}

INITRD_LOAD=$((0x84000000))
DTB_LOAD=$((0x82200000))          # OpenSBI fw_jump next_arg1 (FORCED)

for f in rootfs.cpio.gz shell_linux.dtb Image; do
    [ -f "$ART/$f" ] || { echo "MISSING $ART/$f -- run build.sh first"; exit 1; }
done

fail=0
chk() { if [ "$2" -eq 1 ]; then printf '  OK    %-58s %s\n' "$1" "$3"
        else printf '  FAIL  %-58s %s\n' "$1" "$3"; fail=1; fi; }

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/root/etc/init.d" "$W/root/root"

# --- /etc/inittab: the image's, with login on ttyUL0 and harnessd deferred ----
zcat "$ART/rootfs.cpio.gz" | ( cd "$W" && cpio -i --quiet --to-stdout etc/inittab ) \
    > "$W/root/etc/inittab"
grep -q 'getty' "$W/root/etc/inittab" || { echo "inittab extract failed"; exit 1; }
sed -i '/^# now run any rc scripts/i ::sysinit:/bin/sh -c '\''[ -e /dev/ttyUL0 ] || { a=$(sed -e "s/.* //" /sys/class/tty/console/active 2>/dev/null); [ -n "$a" ] \&\& [ -e /dev/$a ] \&\& ln -sf /dev/$a /dev/ttyUL0; }'\''  # DEBUG: make /dev/ttyUL0 the real console' \
    "$W/root/etc/inittab"
sed -i 's|^console::respawn:/sbin/getty.*$|# DEBUG: -/bin/login on the REAL console tty node (mk_debug.sh)\nttyUL0::respawn:-/bin/login|' \
    "$W/root/etc/inittab"
sed -i 's|^\(::respawn:/usr/sbin/mps3-harnessd.*\)$|# DEBUG-DEFERRED (start by hand): \1|; s|^\(::respawn:/usr/libexec/mps3/harnessd-placeholder.*\)$|# DEBUG-DEFERRED: \1|' \
    "$W/root/etc/inittab"

cat > "$W/root/etc/init.d/S99debugmon" <<'EOS'
#!/bin/sh
# DEBUG zero-input forensics + login banner (mk_debug.sh).
case "$1" in start) ;; *) exit 0 ;; esac
(
  exec > /dev/console 2>&1
  echo ""
  echo "================ MPS3 DEBUG OVERLAY ================"
  echo " Log in on this console: root / the image's root password"
  echo " The harness process is DEFERRED (inittab line commented out)."
  echo "   start it by hand:  /usr/sbin/mps3-harnessd &"
  echo " state: /run/mps3/{persist,net}.state  /run/mps3/boot-health"
  echo "===================================================="
  sleep 20
  echo "==== DEBUGMON /proc/interrupts uptime=$(cut -d' ' -f1 /proc/uptime) ===="
  cat /proc/interrupts
  echo "DEBUGMON_MARK"
) &
EOS
chmod 755 "$W/root/etc/init.d/S99debugmon"

cat > "$W/root/root/README.DEBUG" <<'EOS'
MPS3 harness debug overlay (mk_debug.sh)
=======================================
Login: root / the image's root password (serial console only).

Deferred at boot: the harness process (its inittab respawn line is commented
out), so a UIO fault in it cannot take the boot down before you are here.
  /usr/sbin/mps3-harnessd &          # watch dmesg + /proc/interrupts
Everything else ran: S12mps3persist, S41mps3net, dropbear, S99mps3health.
  cat /run/mps3/boot-health /run/mps3/persist.state /run/mps3/net.state

If even this faults before login, boot shell_linux_rescue.dtb (rdinit=/bin/sh:
PID 1 is a raw shell). Then: mount -t proc proc /proc; mount -t sysfs sysfs /sys;
mount -t devtmpfs devtmpfs /dev  (rdinit means nothing else is mounted).
EOS

( cd "$W/root" && find . -mindepth 1 | LC_ALL=C sort \
    | cpio -o -H newc --owner +0:+0 --quiet ) > "$W/debug.cpio"
gzip -9 -n < "$W/debug.cpio" > "$ART/rootfs_debug_overlay.cpio.gz"

SIZE=$(stat -c%s "$ART/rootfs_debug_overlay.cpio.gz")
END=$((INITRD_LOAD + SIZE))
printf -v END_HEX '0x%08x' "$END"

echo "=============== debug overlay + DTB asserts ==============="
chk "overlay segment built" 1 "$SIZE bytes @0x84000000, end $END_HEX"
lst=$(cpio -it --quiet < "$W/debug.cpio" | grep -cE '^(\./)?(etc/inittab|etc/init.d/S99debugmon|root/README.DEBUG)$' || true)
chk "overlay lists inittab + debugmon + README" "$([ "$lst" -eq 3 ] && echo 1 || echo 0)" "matches=$lst"
chk "login on ttyUL0 (the real console tty)" \
    "$(grep -q '^ttyUL0::respawn:-/bin/login' "$W/root/etc/inittab" && echo 1 || echo 0)" ""
chk "no getty-on-console line remains" \
    "$(grep -qE '^console::respawn' "$W/root/etc/inittab" && echo 0 || echo 1)" ""
chk "harness respawn deferred" \
    "$(grep -qE '^::respawn:/usr/(sbin/mps3-harnessd|libexec/mps3/harnessd-placeholder)' "$W/root/etc/inittab" && echo 0 || echo 1)" ""
chk "managed inittab block kept" \
    "$(grep -q '^# BEGIN mps3 inittab.d' "$W/root/etc/inittab" && echo 1 || echo 0)" ""

# --- shell_linux_debug.dtb: initrd props for the overlay -----------------------
cp "$ART/shell_linux.dtb" "$ART/shell_linux_debug.dtb"
fdtput -t x "$ART/shell_linux_debug.dtb" /chosen linux,initrd-start 0x84000000
fdtput -t x "$ART/shell_linux_debug.dtb" /chosen linux,initrd-end "$END_HEX"
dtc -I dtb -O dtb -p 4096 -o "$ART/shell_linux_debug.dtb.tmp" "$ART/shell_linux_debug.dtb" 2>/dev/null \
    && mv "$ART/shell_linux_debug.dtb.tmp" "$ART/shell_linux_debug.dtb"
got=$(( $(fdtget "$ART/shell_linux_debug.dtb" /chosen linux,initrd-end) & 0xffffffff ))
chk "debug DTB initrd-end stamped to the overlay size" "$([ "$got" -eq "$END" ] && echo 1 || echo 0)" "$END_HEX"
SZ=$(stat -c%s "$ART/shell_linux_debug.dtb")
chk "debug DTB clears the overlay slot" \
    "$([ $((DTB_LOAD + SZ + 1024)) -le $INITRD_LOAD ] && echo 1 || echo 0)" "$SZ bytes"
KSZ=$(od -A n -t u8 -j 16 -N 8 "$ART/Image" | tr -d ' ')
chk "Image (bss incl.) ends below the DTB slot" \
    "$([ $((0x80400000 + KSZ)) -lt $DTB_LOAD ] && echo 1 || echo 0)" ""

# --- shell_linux_rescue.dtb: rdinit=/bin/sh, no overlay ------------------------
cp "$ART/shell_linux.dtb" "$ART/shell_linux_rescue.dtb"
BA=$(fdtget "$ART/shell_linux_rescue.dtb" /chosen bootargs)
case "$BA" in *rdinit=/bin/sh*) : ;; *) fdtput -t s "$ART/shell_linux_rescue.dtb" /chosen bootargs "$BA rdinit=/bin/sh" ;; esac
dtc -I dtb -O dtb -p 4096 -o "$ART/shell_linux_rescue.dtb.tmp" "$ART/shell_linux_rescue.dtb" 2>/dev/null \
    && mv "$ART/shell_linux_rescue.dtb.tmp" "$ART/shell_linux_rescue.dtb"
got_r=$(fdtget "$ART/shell_linux_rescue.dtb" /chosen bootargs)
chk "rescue DTB bootargs carry rdinit=/bin/sh (not init=)" \
    "$(case "$got_r" in *rdinit=/bin/sh*) echo 1;; *) echo 0;; esac)" "$got_r"

echo "--------------------------------------------------------"
[ $fail -ne 0 ] && { echo "DEBUG SET BROKEN -- refusing to checksum."; exit 1; }
( cd "$ART" && sha256sum rootfs_debug_overlay.cpio.gz shell_linux_debug.dtb shell_linux_rescue.dtb \
      > DEBUG_SHA256SUMS )
echo "installed -> $ART/{rootfs_debug_overlay.cpio.gz, shell_linux_debug.dtb, shell_linux_rescue.dtb}"
