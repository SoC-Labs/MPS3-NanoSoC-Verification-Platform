#!/bin/sh
# guest_test.sh — runs INSIDE the QEMU rv32 guest (busybox). Drives the real
# mps3_dfx.ko (mock backend) through recorded swap sequences: the actual
# regdemo_a/regdemo_b clearing+partial .bin pairs produced by the DFX build
# (fpga/dfx/build_v2enc/prod), rm_ids from rm_list.tcl v2 encoding.
# Emits one "RESULT <name> PASS/FAIL" line per scenario; the host harness
# counts them.
cd "$(dirname "$0")"

RM_A=0x010000A1   # rm_regdemo_a
RM_B=0x010000B2   # rm_regdemo_b

run() { ./swaptool "$@" || true; }

echo "=== ICAP QEMU SUITE START ==="

# --- default timeouts: functional + wire-header-integrity scenarios ---
insmod ./mps3_dfx.ko mock=1 || echo "RESULT insmod FAIL"
dmesg | grep mps3dfx | tail -1

run happy   clr_a.bin part_a.bin $RM_A sd
run happy   clr_b.bin part_b.bin $RM_B staged
run order   clr_a.bin part_a.bin $RM_A
run second  clr_a.bin
run badcrc  clr_a.bin
run sdcrc   clr_a.bin part_a.bin $RM_A
run wrongid clr_a.bin part_a.bin $RM_A
run novalid clr_a.bin part_a.bin $RM_A

# sysfs progress observability (SERVICE_DISPOSITION §4)
S=/sys/class/misc/mps3dfx
if [ -r "$S/rm_id" ] && [ -r "$S/icap_bytes" ] && [ -r "$S/state" ]; then
    echo "RESULT sysfs PASS (rm_id=$(cat $S/rm_id) icap_bytes=$(cat $S/icap_bytes))"
else
    echo "RESULT sysfs FAIL missing attrs"
fi
rmmod mps3_dfx

# --- short bounded polls: stuck-hardware fail-closed scenarios ---
insmod ./mps3_dfx.ko mock=1 done_poll_max=2000 confirm_poll_max=500
run crstuck  clr_a.bin
run wfvstuck clr_a.bin
run decouple
run release  clr_a.bin part_a.bin $RM_A
rmmod mps3_dfx

# --- short idle_ms: the 30s RX-idle reap (scaled to keep the suite fast;
#     the mechanism is identical, only the module param differs) ---
insmod ./mps3_dfx.ko mock=1 idle_ms=1500
run idle clr_a.bin part_a.bin $RM_A 4000
rmmod mps3_dfx

# --- LITE write protocol (the on-silicon bare-metal shell's mode) ---
insmod ./mps3_dfx.ko mock=1 fifo_mode=0
run happy clr_a.bin part_a.bin $RM_A sd
rmmod mps3_dfx

# --- module reload sanity: init/exit paths survived all of the above ---
insmod ./mps3_dfx.ko mock=1 && rmmod mps3_dfx && echo "RESULT reload PASS" \
    || echo "RESULT reload FAIL"

echo "=== ICAP_QEMU_SUITE_DONE ==="
