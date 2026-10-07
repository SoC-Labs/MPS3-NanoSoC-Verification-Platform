#!/bin/bash
# run_qemu_swaptest.sh — boot the proven Buildroot rv32 image under the
# Buildroot-built qemu-system-riscv32 (-M virt), attach a payload disk
# carrying mps3_dfx.ko + swaptool + the RECORDED regdemo swap bitstreams,
# run the guest suite, and grade the RESULT lines.
#
# Derivative of linux_soc/linux/boot_qemu.sh (same QEMU invocation + console
# driving); reads that tree READ-ONLY, writes only under this directory.
#
# Usage: ./run_qemu_swaptest.sh [timeout_s]   (default 600)
set -uo pipefail
cd "$(dirname "$0")"

REPO=$(git rev-parse --show-toplevel 2>/dev/null || (cd ../../../../../.. && pwd))
LINUX=${LINUX:-$REPO/src/linux_soc/linux}
BR=$LINUX/buildroot/output
QEMU=$BR/host/bin/qemu-system-riscv32
IMG=$BR/images
PROD=${PROD:-$REPO/fpga/dfx/build_v2enc/prod}
LOG=qemu_swaptest.log
T=${1:-600}

[ -x "$QEMU" ] || { echo "MISSING $QEMU"; exit 1; }
BIOS=$IMG/fw_jump.bin; [ -f "$BIOS" ] || BIOS=$IMG/fw_jump.elf

# ---- build the payload disk (ext2, populated with mke2fs -d) --------------
PAY=payload
rm -rf $PAY payload.ext2
mkdir -p $PAY
cp ../../mps3_dfx.ko $PAY/ || { echo "build the module first (make module)"; exit 1; }
cp swaptool $PAY/          || { echo "build swaptool first (make swaptool)"; exit 1; }
cp guest_test.sh $PAY/
# recorded swap sequences: the real DFX-build clearing+partial pairs
cp "$PROD/config_rm_regdemo_a_pblock_rp_dut_partial_clear.bin" $PAY/clr_a.bin
cp "$PROD/config_rm_regdemo_a_pblock_rp_dut_partial.bin"       $PAY/part_a.bin
cp "$PROD/config_rm_regdemo_b_pblock_rp_dut_partial_clear.bin" $PAY/clr_b.bin
cp "$PROD/config_rm_regdemo_b_pblock_rp_dut_partial.bin"       $PAY/part_b.bin
MKE2FS=$(command -v mke2fs || echo /usr/sbin/mke2fs)
$MKE2FS -q -F -t ext2 -d $PAY -b 1024 payload.ext2 16384 || exit 1

# ---- boot + drive the console ---------------------------------------------
{
    sleep 45; printf 'root\n'
    sleep 5
    printf 'mkdir -p /mnt/p && mount -t ext2 /dev/vdb /mnt/p && sh /mnt/p/guest_test.sh > /tmp/suite.log 2>&1; cat /tmp/suite.log; poweroff\n'
    # The guest powers off itself when the suite ends; poll the tee'd log so
    # this feeder exits promptly instead of pinning the pipeline for the
    # whole ceiling (a lingering feeder makes bash wait even after QEMU
    # exits). Backstop poweroff for a wedged guest.
    waited=0
    while [ "$waited" -lt $((T - 70)) ] && \
          ! grep -q 'ICAP_QEMU_SUITE_DONE' "$LOG" 2>/dev/null; do
        sleep 2; waited=$((waited + 2))
    done
    sleep 8
    printf 'poweroff\n' 2>/dev/null || true
    sleep 5
} | timeout --foreground "$T" "$QEMU" \
        -M virt -m 256M -nographic -no-reboot \
        -bios "$BIOS" -kernel "$IMG/Image" \
        -append "rootwait root=/dev/vda ro console=ttyS0" \
        -drive "file=$IMG/rootfs.ext2,format=raw,id=hd0,if=none" \
        -device virtio-blk-device,drive=hd0 \
        -drive "file=payload.ext2,format=raw,id=hd1,if=none" \
        -device virtio-blk-device,drive=hd1 \
    2>&1 | tee "$LOG"

echo
echo "================ VERDICT ================" | tee -a "$LOG"
# 16 scenario lines expected:
# happy(sd) happy(staged) order second badcrc sdcrc wrongid novalid sysfs
# crstuck wfvstuck decouple release idle happy(lite) reload
EXPECT=16
PASS=$(grep -c 'RESULT [a-z]* PASS' "$LOG" || true)
FAIL=$(grep -c 'RESULT [a-z]* FAIL' "$LOG" || true)
DONE=$(grep -c 'ICAP_QEMU_SUITE_DONE' "$LOG" || true)
echo "  suite completed : $([ "$DONE" -ge 1 ] && echo YES || echo no)" | tee -a "$LOG"
echo "  scenarios PASS  : $PASS / $EXPECT" | tee -a "$LOG"
echo "  scenarios FAIL  : $FAIL" | tee -a "$LOG"
grep 'RESULT ' "$LOG" | sed 's/^/    /' | tee -a "$LOG"

if [ "$DONE" -ge 1 ] && [ "$PASS" -eq "$EXPECT" ] && [ "$FAIL" -eq 0 ]; then
    echo "QEMU SWAP SUITE: PASS" | tee -a "$LOG"
    exit 0
else
    echo "QEMU SWAP SUITE: FAIL/PARTIAL — see $LOG" | tee -a "$LOG"
    exit 1
fi
