#!/bin/bash
# Boot the Buildroot result on the Buildroot-built qemu-system-riscv32 (-M virt), log in
# as root, and run commands -- proving a real busybox SHELL, not just a login banner.
# Writes linux/qemu_boot.log.
#
# This proves THE KERNEL AND THE GLIBC USERLAND (rv32imac soft-float, Sv32, built with
# AMD's prebuilt vendor toolchain). It does NOT prove the real SoC -- qemu-virt has a
# CLINT, a PLIC and an ns16550 (ttyS0); linux_soc has none of those and uses uartlite +
# axi_intc + Sstc. See README.md sec.4.
#
# The QEMU command is Buildroot's own documented one for qemu_riscv32_virt
# (board/qemu/riscv32-virt/readme.txt): OpenSBI fw_jump + ext2 rootfs over virtio-blk.
#
# Usage: ./boot_qemu.sh [seconds]   (default 120)
set -uo pipefail
cd "$(dirname "$0")"

BR=buildroot/output
QEMU=$BR/host/bin/qemu-system-riscv32
IMG=$BR/images
LOG=qemu_boot.log
T=${1:-120}

[ -x "$QEMU" ] || { echo "MISSING $QEMU -- run build.sh first"; exit 1; }
BIOS=$IMG/fw_jump.bin; [ -f "$BIOS" ] || BIOS=$IMG/fw_jump.elf

# Drive the console: wait for the login prompt, log in as root (no password), run a few
# commands that can only succeed if a dynamically-linked glibc userland actually executes,
# then power off. Timings are generous; the timeout is the backstop.
{
    sleep 40; printf 'root\n'
    sleep 5;  printf 'uname -a\n'
    sleep 2;  printf 'cat /proc/cpuinfo\n'
    sleep 2;  printf 'ldd /bin/busybox\n'
    sleep 2;  printf 'echo SHELL_PROOF_$((6*7))\n'
    sleep 3;  printf 'poweroff\n'
    sleep 5
} | timeout --foreground "$T" "$QEMU" \
        -M virt -m 256M -nographic -no-reboot \
        -bios "$BIOS" -kernel "$IMG/Image" \
        -append "rootwait root=/dev/vda ro console=ttyS0" \
        -drive "file=$IMG/rootfs.ext2,format=raw,id=hd0,if=none" \
        -device virtio-blk-device,drive=hd0 \
    2>&1 | tee "$LOG"

echo
echo "================ VERDICT ================" | tee -a "$LOG"
# NB: the serial console emits a CR before the prompt, so these must NOT be ^-anchored
# on a bare line start -- "(^|\r)" is deliberate. An earlier ^-anchored regex reported
# FAIL on a boot that had plainly reached the prompt.
ok=0
grep -qE '(^|\r)[[:alnum:]_.-]+ login:' "$LOG" && { echo "  login prompt : YES"; ok=$((ok+1)); } || echo "  login prompt : no"
grep -qE 'SHELL_PROOF_42'                 "$LOG" && { echo "  shell exec   : YES (ran a command as root)"; ok=$((ok+1)); } || echo "  shell exec   : no"
grep -qE 'Linux mbv-linux .* riscv32'     "$LOG" && { echo "  uname        : YES"; ok=$((ok+1)); } || echo "  uname        : no"

if [ "$ok" -ge 2 ]; then
    echo "PASS: booted to a busybox shell" | tee -a "$LOG"
    grep -m1 'Linux version' "$LOG"
    exit 0
else
    echo "FAIL/PARTIAL: see $LOG" | tee -a "$LOG"
    exit 1
fi
