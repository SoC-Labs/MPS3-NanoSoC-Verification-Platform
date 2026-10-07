#!/usr/bin/env bash
#
# harness_regression.sh — tiered, gated regression for a newly generated HARNESS
# IMAGE (static shell bitstream + re-keyed firmware). Each tier is a hard
# go/no-go before the next; the run STOPS LOUDLY at the first failing gate.
#
# Every gate names the historical escape it would have caught. See
# docs/HARNESS_REGRESSION.md for the tier-by-tier rationale.
#
#   scripts/harness_regression.sh                 # tiers 0..2 (no board)
#   scripts/harness_regression.sh --through 1     # tiers 0..1
#   scripts/harness_regression.sh --only 0        # just tier 0 (seconds)
#   scripts/harness_regression.sh --through 3 --allow-board   # + on-board smoke
#
# Tier 1 cocotb benches need a simulator: `source set_env.sh` first and pass
# SIM=vcs (else they SKIP, and Tier 1 still runs the host-gcc + static gates).
# Tier 2 DCP-open asserts need Vivado and are OPT-IN (--with-vivado) so a busy
# build machine is never disturbed by default.
# Tier 3 REFUSES to touch the board unless the lease is held (mps3_board.sh
# preflight) AND --allow-board is given.
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATES="$ROOT/scripts/harness_gates"
PY="${PY:-python3}"
WAIVERS="$GATES/param_parity_waivers.txt"
VIVADO="${VIVADO:-/apps/Xilinx/Vivado/2024.1/bin/vivado}"
XSDB="${XSDB:-xsdb}"
BOARD_HOST="${MPS3_BOARD_HOST:-192.168.10.101}"
# The board's dataplane IP lives on the fpgahub host's interface, not
# necessarily on the machine running this script. If BOARD_VIA_HUB=1, the
# network gates (ping/swap/console) run THROUGH the hub over ssh. The JTAG gates
# (CSR liveness, mailbox) already route via the hw_server FQDN and need no relay.
BOARD_VIA_HUB="${MPS3_BOARD_VIA_HUB:-0}"
# NOT `${FPGAHUB_HOST:-$MPS3_HUB}`. Under `set -u` that form made an UNSET
# MPS3_HUB abort the whole script on line 36 -- before argument parsing, before
# tier 0 -- with "MPS3_HUB: unbound variable". So `harness_regression.sh --only 0`,
# six board-free gates that need no hub at all, could not run on any box that had
# not exported a hub name. Combined with nothing in CI invoking this script, that
# is the full explanation for how the bug-#1 tier went unrun for months: it was
# both uninvoked AND unrunnable. The hub is needed only for the via-hub dataplane
# relay, so default it empty and refuse LATER, where it is actually used.
HUB_HOST="${FPGAHUB_HOST:-${MPS3_HUB:-}}"
# Tier-3 sub-gate toggles. All the socket gates default ON; the SWD gate is
# opt-in (openocd is slower AND it leaves nanoSoC resident; swapping away from it
# works on the 256 KiB arena -- greybox restored after it on 2026-09-22). Set any
# to 0 to skip that individual gate.
RUN_SWAP="${MPS3_RUN_SWAP:-1}"        # regdemo_b swap + regdemo_a swap-away (a<->b cycle)
RUN_CONSOLE="${MPS3_RUN_CONSOLE:-1}"  # rm_uart_echo swap + TCP 6930 echo round-trip
RUN_SWD="${MPS3_RUN_SWD:-0}"          # nanoSoC swap + openocd JTAG IDCODE first-light (opt-in, slow)
# DUT-RECEPTION probe (dut_rx_check.tcl). Default OFF: unlike the CSR/mailbox
# probes beside it, this one is only meaningful when an RX-capable RM is ALREADY
# resident (eth_ss, nanosoc, nanosoc_multicore -- anything that terminates the
# RMII). Run against a greybox it would fail truthfully and uselessly, which is
# how an opt-out gate teaches people to ignore red. Name the resident RM's id in
# MPS3_DUTRX_RM_ID; the gate takes it as an argument precisely so it is not
# pinned to one DUT (its predecessor was, and rotted).
RUN_DUTRX="${MPS3_RUN_DUTRX:-0}"      # gen_checker -> DUT RMII; RM_STATUS[2] must go 0 -> 1
DUTRX_RM_ID="${MPS3_DUTRX_RM_ID:-0x01000002}"   # default: eth_ss
# The DFX prod dir the swap gates push from. IMPORTANT (dataplane-via-hub): the
# swap gates ssh-run mps3_push.py ON the fpgahub host, where THIS repo is NOT
# visible -- so for an on-board run this MUST point at a prod dir readable ON the
# hub (stage the RM .bin pairs + overlay_inputs.txt + static_id.txt there, with
# overlay_inputs paths rewritten to that hub location). Validated on silicon
# 2026-07-10: greybox->regdemo_b verified with bins staged to /tmp/v3prod. Left as
# the repo path for the local-reachable case.
PROD="${MPS3_PROD_DIR:-fpga/dfx/build_v3/prod}"
# The hub to route dataplane gates through, or "" to run them locally (on-hub).
VIA_HUB=""
if [ "$BOARD_VIA_HUB" = "1" ]; then
  if [ -z "$HUB_HOST" ]; then
    echo "FAIL: MPS3_BOARD_VIA_HUB=1 but neither FPGAHUB_HOST nor MPS3_HUB is set." >&2
    echo "      The dataplane gates relay through the fpgahub host over ssh; its" >&2
    echo "      name is site-specific and is not committed to this tree. Export it:" >&2
    echo "          export MPS3_HUB=<fpgahub-host>" >&2
    exit 2
  fi
  VIA_HUB="$HUB_HOST"
fi

THROUGH=2
ONLY=""
ALLOW_BOARD=0
WITH_VIVADO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tier|--through) THROUGH="$2"; shift 2 ;;
    --only)           ONLY="$2"; shift 2 ;;
    --allow-board)    ALLOW_BOARD=1; shift ;;
    --with-vivado)    WITH_VIVADO=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

pass=0
step() {   # step "<label> (bug #N)" <command...>
  local label="$1"; shift
  echo ""
  echo ">>> GATE: $label"
  if "$@"; then
    echo "<<< PASS: $label"
    pass=$((pass+1))
  else
    echo "!!! FAIL: $label"
    echo ""
    echo "STOP. Gate failed; higher tiers not run. Fix this before proceeding."
    exit 1
  fi
}

want() {  # want <tier-number> -> true if this tier should run
  local t="$1"
  if [ -n "$ONLY" ]; then [ "$ONLY" = "$t" ]; else [ "$t" -le "$THROUGH" ]; fi
}

# --------------------------------------------------------------------------- #
# TIER 0 — static, seconds, no tools. The artifact-vs-source parity gates.
# --------------------------------------------------------------------------- #
tier0() {
  echo "===================== TIER 0 (static, no tools) ====================="
  step "bench/BD parameter parity — CSR decode width (bug #1)" \
       "$PY" "$GATES/check_bench_param_parity.py" --waivers "$WAIVERS"
  step "shell_bd.tcl CONFIG lint — DECOUPLED_VALUE hex (bug #3)" \
       "$PY" "$GATES/check_bd_config_lint.py"
  step "diag mailbox address + LMB lockstep (bug #4)" \
       "$PY" "$GATES/check_diag_mailbox_parity.py"
  step "packaged-IP staleness guard (bug #2)" \
       "$PY" "$GATES/check_packaged_ip_fresh.py"
}

# --------------------------------------------------------------------------- #
# TIER 1 — minutes. Firmware host-gcc + diag field parity + cocotb benches.
# --------------------------------------------------------------------------- #
tier1() {
  echo ""
  echo "===================== TIER 1 (host-gcc + cocotb) ===================="
  step "diag field parity — publish covers every counter (bug #6)" \
       "$PY" "$GATES/check_diag_field_parity.py"
  # Binary count is NOT hard-coded in the label any more: it said "25 binaries"
  # while firmware/test/Makefile's TESTS list had grown to 38, and a label that
  # drifts is a label nobody trusts. The sub-make prints what it actually built.
  step "firmware host-gcc harness (firmware/test TESTS)" \
       MAKE_C firmware/test test
  # cocotb: needs set_env.sh sourced + SIM=vcs. The two INTEGRATION benches are
  # the ones that close the "join was untested" gaps (csr_decode_width = bug #1
  # at the shipped width; uart_echo_integration = the DUT<->harness CDC).
  if [ -n "${SIM:-}" ]; then
    step "cocotb: csr_decode_width @ C_S_AXI_ADDR_WIDTH=32 (bug #1)" \
         MAKE_C tests/csr_decode_width "SIM=$SIM"
    step "cocotb: uart_echo_integration (DUT<->harness CDC)" \
         MAKE_C tests/uart_echo_integration "SIM=$SIM"
  else
    echo ""
    echo "    SKIP cocotb benches (set SIM=vcs and 'source set_env.sh' to enable)"
  fi
}

# --------------------------------------------------------------------------- #
# TIER 2 — build-time. Assert on the implemented ARTIFACTS, not the log.
# --------------------------------------------------------------------------- #
tier2() {
  echo ""
  echo "===================== TIER 2 (build artifacts) ======================"
  step "implemented-shell reports: WNS>0 / 0 DRC errors / LUTAR-1 (bugs: timing/reset)" \
       "$PY" "$GATES/check_impl_reports.py"
  # DCP-open asserts need Vivado; opt-in so a busy machine is never disturbed.
  local dcp="$ROOT/build/shell_proj_512k/shell_static_synth.dcp"
  [ -f "$dcp" ] || dcp="$ROOT/build/shell_proj_256k/shell_static_synth.dcp"
  if [ "$WITH_VIVADO" = 1 ] && [ -f "$dcp" ]; then
    step "static DCP asserts: CSR blocks / ASYNC_REG / decoupler boundary (bugs #1,#2,#3)" \
         "$VIVADO" -mode batch -notrace -source "$GATES/tier2_dcp_assert.tcl" \
         -tclargs "$dcp" "${EXPECT_LMB_KB:-256}"
  else
    echo ""
    echo "    SKIP static-DCP asserts (--with-vivado to run tier2_dcp_assert.tcl on $dcp)"
  fi
}

# --------------------------------------------------------------------------- #
# TIER 3 — on-board smoke. Lease-gated; refuses without --allow-board.
# --------------------------------------------------------------------------- #
tier3() {
  echo ""
  echo "===================== TIER 3 (ON-BOARD smoke) ======================="
  echo ">>> preflight: board lease must be held before ANY board I/O"
  if ! "$ROOT/scripts/mps3_board.sh" preflight; then
    echo "!!! REFUSE: board lease not held by us. Tier 3 will not touch the board."
    echo "    (acquire with: scripts/mps3_board.sh acquire \"\$MPS3_LEASE_HOLDER\")"
    exit 1
  fi
  if [ "$ALLOW_BOARD" != 1 ]; then
    echo "!!! REFUSE: Tier 3 needs --allow-board (extra safety). It would run, IN ORDER:"
    echo "      1. CSR liveness  : xsdb $GATES/tier3_csr_liveness.tcl        (bug #1)"
    echo "      2. mailbox magic : xsdb scripts/mps3_diag.tcl                (bug #4)"
    echo "      3. DUT reception : xsdb $GATES/dut_rx_check.tcl <rm_id>        (RM_STATUS[2]) [MPS3_RUN_DUTRX, opt-in]"
    echo "      4. ping          : ping_check.py -> shell_id                 (identity)"
    echo "      5. full swap     : swap_check regdemo_b ; rm_id 0xB2 verified (bugs #5,#7)  [MPS3_RUN_SWAP]"
    echo "      6. swap-away     : swap_check regdemo_a ; rm_id 0xA1 (a<->b)  (bug #7)       [MPS3_RUN_SWAP]"
    echo "      7. rm_uart_echo  : swap_check uart_echo + console_check 6930  (join)         [MPS3_RUN_CONSOLE]"
    echo "      8. nanoSoC JTAG  : swap_check nanosoc + swd_check IDCODE 6921 (debug join)   [MPS3_RUN_SWD, opt-in]"
    exit 1
  fi
  # --- 1. CSR liveness (bug #1) — the 5-second worst-bug catch, BEFORE anything
  #        else touches the board. mrd/mwr only, never a `stop`. ---
  step "CSR liveness probe over JTAG (bug #1)" \
       "$XSDB" "$GATES/tier3_csr_liveness.tcl"
  # --- 2. diag mailbox magic scan (bug #4) — read-only mrd ---
  step "diag mailbox magic scan (bug #4)" \
       "$XSDB" "$ROOT/scripts/mps3_diag.tcl"
  # --- 3. DUT RECEPTION (opt-in) — the only witness that a frame crossed the
  #        partition boundary INTO the DUT. The shell's gen_checker drives the
  #        loaded DUT's RMII and the DUT's own eth IRQ latches into
  #        DFXCTL.RM_STATUS[2]; no host-side gate can see this, because the DUT
  #        return path does not exist yet. Third of the three xsdb/MPS3_HW_URL
  #        probes, and like them read-mostly: it writes only GENCHK.CTRL and
  #        restores it, and NEVER stop/con.
  #
  #        Off by default: it needs an RX-capable RM already resident. The
  #        expected rm_id is an ARGUMENT (MPS3_DUTRX_RM_ID), not a constant --
  #        the previous copy of this probe hard-coded one RM and rotted.
  if [ "$RUN_DUTRX" = "1" ]; then
    step "DUT reception: RM_STATUS[2] 0->1 under gen_checker traffic (rm_id $DUTRX_RM_ID)" \
         "$XSDB" "$GATES/dut_rx_check.tcl" "$DUTRX_RM_ID"
  else
    echo ""
    echo "    SKIP DUT-reception probe (opt-in; set MPS3_RUN_DUTRX=1 with an RX-capable"
    echo "         RM resident, e.g. MPS3_DUTRX_RM_ID=0x01000002 for eth_ss)"
  fi

  # --- 4. ping identity ---
  ping_gate="$GATES/ping_check.py"
  if [ "$BOARD_VIA_HUB" = "1" ]; then
    # Feed the gate to the hub's python over stdin -- no nested quoting.
    step "ping -> shell_id on 6900 (via hub $HUB_HOST)" \
         sh -c "ssh -o ControlPath=none -o BatchMode=yes '$HUB_HOST' python3 - '$BOARD_HOST' < '$ping_gate'"
  else
    step "ping -> shell_id on 6900" "$PY" "$ping_gate" "$BOARD_HOST"
  fi

  # --- STAGING (dataplane-via-hub) — the swap gates run mps3_push.py ON the hub,
  #     where THIS repo is not visible, so the RM .bin pairs + manifest + the
  #     pusher itself must be copied there first. Only the RMs the gates actually
  #     use are staged. After this, PROD points at the hub-side dir. Skipped when
  #     not going via the hub (PROD stays the local repo path). Validated on
  #     silicon 2026-07-10 (greybox->regdemo_b verified from a staged /tmp dir).
  if [ "$BOARD_VIA_HUB" = "1" ] && \
     { [ "$RUN_SWAP" = "1" ] || [ "$RUN_CONSOLE" = "1" ] || [ "$RUN_SWD" = "1" ]; }; then
    HUB_PROD="${MPS3_HUB_PROD_DIR:-/tmp/mps3_regr_prod}"
    echo ""
    echo "    staging prod assets -> $HUB_HOST:$HUB_PROD"
    ssh -o ControlPath=none -o BatchMode=yes "$HUB_HOST" "mkdir -p '$HUB_PROD'" || \
      { echo "!!! FAIL: could not mkdir on hub"; exit 1; }
    for rm in regdemo_b regdemo_a uart_echo nanosoc; do
      for suf in _pblock_rp_dut_partial.bin _pblock_rp_dut_partial_clear.bin; do
        f="$PROD/config_rm_${rm}${suf}"
        [ -f "$f" ] && scp -q -o ControlPath=none -o BatchMode=yes "$f" "$HUB_HOST:$HUB_PROD/"
      done
    done
    scp -q -o ControlPath=none -o BatchMode=yes \
        "$PROD/static_id.txt" "$PROD/overlay_inputs.txt" \
        "$ROOT/scripts/mps3_push.py" "$HUB_HOST:$HUB_PROD/" || \
      { echo "!!! FAIL: staging scp failed"; exit 1; }
    # The gates now drive `python3 -m pyverify.cli deploy` on the hub, so the
    # PACKAGE has to be there too -- mps3_push.py finds it beside itself, and
    # swap_check.py points PYTHONPATH at $HUB_PROD.
    scp -qr -o ControlPath=none -o BatchMode=yes \
        "$ROOT/host/pyverify/pyverify" "$HUB_HOST:$HUB_PROD/" || \
      { echo "!!! FAIL: staging pyverify failed"; exit 1; }
    # rewrite the manifest's bin paths to the hub location
    ssh -o ControlPath=none -o BatchMode=yes "$HUB_HOST" \
        "sed -i 's#[^ ]*/config_rm#$HUB_PROD/config_rm#g' '$HUB_PROD/overlay_inputs.txt'"
    # The debug gate needs its OpenOCD cfg on the hub too. RETARGETED 2026-09-09:
    # nanosoc_mps3_jtag.cfg (JTAG/rbb/6921), NOT the swd_*.cfg pair -- 6920's
    # swd_server is dormant on the fielded shell. The cfg is self-contained (no
    # relative `source`), so the single file is enough.
    if [ "$RUN_SWD" = "1" ]; then
      scp -q -o ControlPath=none -o BatchMode=yes \
          "$ROOT/host/openocd/nanosoc_mps3_jtag.cfg" "$HUB_HOST:$HUB_PROD/" 2>/dev/null
    fi
    PROD="$HUB_PROD"        # gates now push mps3_push.py --prod from the hub dir
    MPS3_HUB_PUSH=1; export MPS3_HUB_PUSH
    echo "    staged $(ssh -o ControlPath=none -o BatchMode=yes "$HUB_HOST" "ls '$HUB_PROD'/*.bin 2>/dev/null | wc -l") bins"
  fi

  # --- 5/6. full swap + swap-away — driven through mps3_push.py on the hub.
  #        These exercise the swap FSM ordering (bug #5: rm_id read back AFTER
  #        DECOUPLE releases => the `verified` flag) and the idle-timeout (bug #7)
  #        on real silicon. We automate the WORKING regdemo_b<->regdemo_a pair:
  #        both clearings fit the swap arena, so the a<->b cycle proves clean
  #        re-isolation. (nanoSoC's swap-away is not automated here. The old
  #        "its clearing overflows the arena and routes to QSPI" note is obsolete:
  #        the 256 KiB arena holds the largest clearing, and greybox was restored
  #        after nanoSoC on 2026-09-22; see clearing_fit_waivers.txt.) ---
  if [ "$RUN_SWAP" = "1" ]; then
    # rm_id encoding v2 (docs/VERSIONING_PLAN.md §3.2): rm_id is now
    #     { ver_major[31:24], ver_minor[23:16], design_id[15:0] }
    # and swap_check.py compares DFXCTL.RM_ID for EXACT equality, so these
    # expectations carry the full 32-bit value, version included. The design_ids
    # (0xB2, 0xA1, ...) are unchanged and still visible in the low half.
    step "full swap -> regdemo_b ; rm_id 0x010000B2 verified (bugs #5,#7)" \
         "$PY" "$GATES/swap_check.py" --rm regdemo_b --expect-rm-id 0x010000B2 \
         --prod "$PROD" --board "$BOARD_HOST" --via-hub "$VIA_HUB"
    step "swap-away -> regdemo_a ; rm_id 0x010000A1 (a<->b re-isolate, bug #7)" \
         "$PY" "$GATES/swap_check.py" --rm regdemo_a --expect-rm-id 0x010000A1 \
         --prod "$PROD" --board "$BOARD_HOST" --via-hub "$VIA_HUB"
  else
    echo ""
    echo "    SKIP swap/swap-away gates (MPS3_RUN_SWAP=0)"
  fi

  # --- 7. console — swap rm_uart_echo in, then round-trip a probe on TCP 6930.
  #        The on-silicon twin of the uart_echo_integration cocotb bench: proves
  #        the DUT->uart_bridge->CSR->uart_over_eth->TCP join actually carries a
  #        byte. console_check.py is dependency-free, so (like the ping gate) it
  #        is fed to the hub over stdin when BOARD_VIA_HUB=1. ---
  if [ "$RUN_CONSOLE" = "1" ]; then
    # RE-NUMBERED 2026-07-14: uart_echo's id was 0x4543484F (ASCII "ECHO"), which
    # used all 32 bits and collided with the v2 version field. It is now
    # design_id 0x0004 @ v1.0 => 0x01000004. The ONLY RM whose id changed.
    step "swap -> rm_uart_echo ; rm_id 0x01000004 verified (join setup)" \
         "$PY" "$GATES/swap_check.py" --rm uart_echo --expect-rm-id 0x01000004 \
         --prod "$PROD" --board "$BOARD_HOST" --via-hub "$VIA_HUB"
    console_gate="$GATES/console_check.py"
    probe="MPS3-HARNESS-ECHO"
    if [ "$BOARD_VIA_HUB" = "1" ]; then
      step "console echo round-trip on 6930 (via hub $HUB_HOST)" \
           sh -c "ssh -o ControlPath=none -o BatchMode=yes '$HUB_HOST' python3 - '$BOARD_HOST' --send '$probe' < '$console_gate'"
    else
      step "console echo round-trip on 6930" \
           "$PY" "$console_gate" "$BOARD_HOST" --send "$probe"
    fi
  else
    echo ""
    echo "    SKIP console gate (MPS3_RUN_CONSOLE=0)"
  fi

  # --- 8. DEBUG CHANNEL — swap nanoSoC in, then openocd JTAG first-light on 6921.
  #        Opt-in (MPS3_RUN_SWD=1): openocd is slower, and nanoSoC is left resident,
  #        so this runs LAST. (The old "cannot cleanly swap away" note is obsolete:
  #        the 256 KiB arena holds the largest clearing, and on 2026-09-22 greybox was
  #        restored straight after nanoSoC -- docs/evidence/2026-09-w2/
  #        summary_20260922T113359Z.txt.) The
  #        channel is gated until a swap reaches DONE — the nanoSoC swap provides it.
  #
  #        RETARGETED 2026-09-09 from SWD/6920/DPIDR to JTAG/6921/IDCODE. 6920
  #        (swd_server) was DORMANT on the shells of that time, and the fielded
  #        0x72BB0A36's v0.11 firmware no longer lists it
  #        (docs/evidence/2026-09-w3/v011_volatile_20260924.txt); the live,
  #        silicon-proven debug path is jtag_server remote_bitbang on 6921, TAP
  #        0x6ba00477. The env var stays MPS3_RUN_SWD so existing runbooks and
  #        CI wiring keep working. NOT YET RUN ON THE BOARD in this form -- the
  #        argv is gated board-free by tests/integration/test_swd_check_argv.py
  #        (`swd_check.py --dry-run`); the on-silicon run is pending a lease. ---
  if [ "$RUN_SWD" = "1" ]; then
    step "swap -> nanosoc ; rm_id 0x01000001 verified (SWD ungate)" \
         "$PY" "$GATES/swap_check.py" --rm nanosoc --expect-rm-id 0x01000001 \
         --prod "$PROD" --board "$BOARD_HOST" --via-hub "$VIA_HUB"
    step "JTAG TAP IDCODE 0x6ba00477 on 6921 (debug-channel join)" \
         "$PY" "$GATES/swd_check.py" --board "$BOARD_HOST" --via-hub "$VIA_HUB" \
         --cfg-dir "$([ "$BOARD_VIA_HUB" = "1" ] && echo "$PROD" || echo host/openocd)"
  else
    echo ""
    echo "    SKIP debug-channel gate (opt-in; set MPS3_RUN_SWD=1 to run nanoSoC swap + openocd JTAG IDCODE)"
  fi
}

# --------------------------------------------------------------------------- #
# make wrapper usable as a `step` command (functions are callable via "$@").
MAKE_C() { make --no-print-directory -C "$ROOT/$1" "${@:2}"; }

echo "harness-regression: repo=$ROOT  through=tier$THROUGH${ONLY:+  only=tier$ONLY}"
want 0 && tier0
want 1 && tier1
want 2 && tier2
want 3 && tier3

echo ""
echo "====================================================================="
echo "HARNESS-REGRESSION OK — $pass gate(s) passed."
