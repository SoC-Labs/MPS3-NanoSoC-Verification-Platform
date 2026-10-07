#!/bin/bash
# Validate mk_debug.sh's overlay on qemu-system-riscv32 -M virt: the SHIPPED
# Image (rootfs embedded) + rootfs_debug_overlay.cpio.gz as -initrd, which the
# kernel unpacks OVER the built-in initramfs. Proves:
#   * the overlay unpacks with no "invalid magic"
#   * -/bin/login prompts on the console and the root password logs in
#   * the harness respawn line is deferred (no harness process running)
#   * the rest of the image still came up (S99mps3health ran)
#
#   MPS3_QEMU=<qemu-system-riscv32> ./boot_qemu_debug.sh
set -uo pipefail
cd "$(dirname "$0")"
ART=${MPS3_ARTIFACTS:-artifacts}
LOG=qemu_debug_boot.log
QEMU=${MPS3_QEMU:-$(command -v qemu-system-riscv32 || true)}
[ -n "$QEMU" ] && [ -x "$QEMU" ] || { echo "no qemu-system-riscv32: set MPS3_QEMU"; exit 2; }
for f in Image fw_jump.bin rootfs_debug_overlay.cpio.gz; do
    [ -f "$ART/$f" ] || { echo "MISSING $ART/$f -- run build.sh, then mk_debug.sh"; exit 2; }
done
PW=${MPS3_ROOT_PASSWD:-$(cat "$ART/ROOT_PASSWD" 2>/dev/null || true)}
[ -n "$PW" ] || { echo "no root password"; exit 2; }
: > "$LOG"
trap '' PIPE

FIFO=$(mktemp -u); mkfifo "$FIFO"
"$QEMU" -M virt -m 256M -nographic -no-reboot \
    -bios "$ART/fw_jump.bin" -kernel "$ART/Image" \
    -initrd "$ART/rootfs_debug_overlay.cpio.gz" \
    -append "console=ttyS0 mps3.persist=off" \
    < "$FIFO" >> "$LOG" 2>&1 &
QPID=$!
exec 3> "$FIFO"
die() { echo "$*" | tee -a "$LOG"; exec 3>&-; kill $QPID 2>/dev/null; rm -f "$FIFO"; exit 1; }
waitlog() { local i=0; while ! grep -qE "$1" "$LOG"; do sleep 1; i=$((i+1)); [ $i -ge "$2" ] && return 1; kill -0 $QPID 2>/dev/null || return 1; done; return 0; }

echo "== wait for login prompt"
waitlog 'login:' 300 || die "FAIL: no login prompt"
sleep 1; printf 'root\n' >&3
waitlog 'Password:' 20 || die "FAIL: no password prompt"
sleep 1; printf '%s\n' "$PW" >&3
sleep 3
printf 'echo LOGIN_OK_$((6*7))\n' >&3
waitlog 'LOGIN_OK_42' 30 || die "FAIL: no root shell after login"
# (BusyBox pgrep has no -c: ask pidof, which it has)
printf 'pidof mps3-harnessd >/dev/null && echo HARNESS_RUNNING=1 || echo HARNESS_RUNNING=0; echo DEFER_CHECK_$((1+1))\n' >&3
waitlog 'DEFER_CHECK_2' 30 || die "FAIL: shell stopped answering"
printf 'reboot -f\n' >&3; sleep 5
kill $QPID 2>/dev/null; exec 3>&-; rm -f "$FIFO"

echo "================ VERDICT ================" | tee -a "$LOG"
ok=0
grep -q 'invalid magic' "$LOG" && echo "  overlay unpack : BAD (invalid magic)" || { echo "  overlay unpack : clean"; ok=$((ok+1)); }
grep -q 'LOGIN_OK_42' "$LOG"   && { echo "  root login     : YES"; ok=$((ok+1)); } || echo "  root login     : no"
grep -q 'HARNESS_RUNNING=0' "$LOG" && { echo "  harness        : deferred"; ok=$((ok+1)); } || echo "  harness        : RUNNING (not deferred)"
grep -q 'mps3health:' "$LOG"   && { echo "  rest of rcS    : ran"; ok=$((ok+1)); } || echo "  rest of rcS    : did not finish"
[ $ok -eq 4 ] && { echo "PASS"; exit 0; } || { echo "FAIL ($ok/4): see $LOG"; exit 1; }
