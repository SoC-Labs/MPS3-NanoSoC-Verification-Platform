# -----------------------------------------------------------------------------
# rm_list.tcl — RM (Reconfigurable Module) library definition for the MPS3 DFX
# flow. Sourced by build_dfx.tcl and (indirectly, via its Python callers) by
# gen_manifest.py invocations driven from a Makefile/CI step.
#
# Each RM entry maps 1:1 onto an `overlay/<rm_name>/` directory per
# docs/contracts/overlay-manifest.md, and each `rm_id` value is the constant
# the RM wrapper itself drives out on the `rm_id` partition pin (see
# docs/contracts/partition-pins.md "Status/misc: rm_id | I | 32") — the shell
# reads it back after a load to confirm the right RM landed. rm_id is
# therefore assigned HERE at RM-design time, not computed from the bitstream.
#
# STATUS 2026-07-07: all four RMs are REAL and pr_verified against the real
# 256 KiB shell (static_id 0xECCEDBF3) — greybox (hand tie-off, I19),
# rm_led (Phase 1.1/1.2 counter), rm_nanosoc (real Cortex-M0 SoC, D2b), and
# rm_eth_ss (real AHB MAC + PTP, phase 5.5). Each entry's per-RM STATUS +
# synth_recipe below records its proof; the original placeholder TODO is
# fully resolved.
# -----------------------------------------------------------------------------

# --- RM library ---------------------------------------------------------------
# Keyed by rm_name (matches overlay-manifest.md "rm_name" and the
# overlay/<rm_name>/ directory). `rm_greybox` MUST be first in RM_ORDER: it is
# both the DFX "reference" config (config 1, whose routed checkpoint is
# extracted+locked to make static_routed_locked.dcp) and the artefact that
# ships baked into the shell / boot NVM as the safety fallback (spec §8A,
# D14 "greybox + NVM" default).

# -----------------------------------------------------------------------------
# rm_id ENCODING v2 (2026-07-14) — DESIGN VERSION IN THE HIGH HALF
# docs/VERSIONING_PLAN.md §3.2. `rm_id` is a genuine 32-bit bus across the
# partition boundary (30 SIGNALS = 122 bits; the "30 pins" figure was a signal
# count, not a bit count), fully plumbed RM wrapper -> shell_top -> DFX
# decoupler (WIDTH 32) -> dfx_ctl -> DFXCTL.RM_ID @ 0x44A1_0010. No design
# needs 32 bits of identity, so the top half now carries a semantic version:
#
#   31           24 23           16 15                            0
#  +---------------+---------------+-------------------------------+
#  |   ver major   |   ver minor   |        design_id[15:0]         |
#  +---------------+---------------+-------------------------------+
#
#   rm_id = (major << 24) | (minor << 16) | design_id
#
# Putting the version in the HIGH half preserves every existing id as
# `rm_id & 0xFFFF`, so 7 of the 8 RMs keep the design_id they already had.
#
# WHY THIS IS FREE: the boundary is NOT widened, and `static_id` is NOT
# re-minted — the RMs are re-synthesised against the EXISTING locked static
# (0xE4B1C44A), so fielded shells keep working and the new partials drop
# straight in. `rm_id_valid` (dfx_ctl.sv) is a GATE + stability detector, not
# a zero-check, and firmware never hard-codes an expected rm_id: step_verify()
# compares DFXCTL.RM_ID against the value carried in the pushed partial's own
# 24-byte header, which is generated from the manifest. The id is pure DATA
# end to end; only lookup tables (clcd_rm_name(), the host's known_rm_names)
# interpret it, and they mask with RM_DESIGN(x) = x & 0xFFFF.
#
# VERSION POLICY (initial assignment, 2026-07-14):
#   * v1.0 — every RM that has been pr_verified against a real locked static
#     AND exercised on silicon. That is all seven real designs: they are in
#     the field, they work, and 1.0 is the honest floor for "released".
#   * v0.0 — rm_greybox ONLY. It is an inert tie-off, not a versioned design;
#     leaving it at 0.0 keeps its rm_id EXACTLY 0x00000000, which preserves
#     both the decoupler's DECOUPLED_VALUE 0x0 semantics and the firmware's
#     "0 == greybox / nothing loaded" convention (pyverify's
#     _rm_id_indicates_loaded() reads an all-zero id as "no RM loaded").
#     Hardware would tolerate a non-zero greybox; there is no reason to spend
#     the compatibility.
#   Bump `version` here when an RM's behaviour changes in a way a board should
#   be able to self-report. Patch + git SHA do NOT fit in 32 bits and live
#   host-side only (manifest) — deliberate, per VERSIONING_PLAN.md §3.2.
#
# *** THE ONE BREAKING CHANGE: rm_uart_echo IS RE-NUMBERED. ***
# Its old id was 0x4543484F — ASCII "ECHO", which used ALL 32 bits and so
# collides head-on with the new version field (it would decode as v69.67!).
# It takes design_id 0x0004 (the next free id after greybox 0 / nanosoc 1 /
# eth_ss 2 / nanosoc_multicore 3) => rm_id 0x01000004. Every OTHER id is
# preserved. Anything that hard-codes 0x4543484F must be updated:
#   - fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv   (the localparam)   [done]
#   - fpga/dfx/overlay/uart_echo/manifest.json    (regenerated)      [done]
#   - tests/uart_echo_integration/                (cocotb bench)     [done]
#   - scripts/harness_regression.sh               (HW swap gate)     [done]
#   - firmware/clcd/clcd.c + firmware/test/test_clcd.c  (name/caps table)
#     -> now keyed by RM_DESIGN(rm_id) = rm_id & 0xFFFF, i.e. 0x0004.
# scripts/harness_gates/check_rm_id_encoding.py fails the build if the wrapper
# localparam, this file and the manifest ever disagree again.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# synth_mode — HOW this RM's OOC checkpoint comes into being. REGISTERED, not
# guessed (2026-09-10).
#
#   "inline"    a single, dependency-free .sv at <wrapper_dir>/<top>.sv that
#               build_dfx.tcl may synthesize itself, in seconds, with no
#               external environment. greybox / led / regdemo_a / regdemo_b /
#               uart_echo.
#   "prebuilt"  the RM instantiates a whole external source tree (nanosoc_m0_soc
#               + Arm IP, the eth subsystem, the multicore DUT, $SOCSCOPE_HOME).
#               Its .dcp MUST arrive pre-staged as <out_dir>/<rm_key>_synth.dcp
#               (`make -C fpga/dfx rm-<name>-dcp`).
#
# WHY IT IS A FIELD AND NOT A PATH MATCH. build_dfx.tcl used to answer this with
#
#     string match "fpga/dfx/rms/*" [rm_field $rm_key wrapper_dir]
#
# i.e. "anything under fpga/dfx/rms/ is a single self-contained file". That was
# true of the five demo RMs it was written for and became false the moment
# rm_socscope was added: its wrapper lives under fpga/dfx/rms/ and its SOURCES
# come from $SOCSCOPE_HOME through fpga/dfx/rms/rm_socscope/filelist.tcl. Under
# the heuristic a default `make prod` would inline-synthesize the wrapper ALONE
# — which does not fail; it "succeeds" with the entire trace plane inferred as a
# black box, and poisons every configuration downstream of it. A new RM now
# states what it is, in the same place it states its rm_id, and
# tests/dfx_flow/test_mint_record.py fails if it does not.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# ip_class -- WHO MAY REDISTRIBUTE this RM's partial. REGISTERED, per RM, here
# (2026-09-30, handover G5; docs/contracts/overlay-manifest.md v0.5).
#
#   "arm-aaa"   the RM's sources pull in Arm-licensed IP -- Cortex-M0/M0+, CMSDK
#               (Corstone-101 / BP210), SoC-400, anything reached through
#               CMSDK_DIR, ARM_IP_LIBRARY_PATH or XHB500_IP_DIR (the Arm IP library).
#               The partial embeds Arm Academic Access IP, so its overlay ships
#               only in the PRIVATE bundle.
#   "open"      nothing Arm-licensed in the partial (this repo's RTL, SoCScope's
#               Apache-2.0 RTL, and AMD/Xilinx debug IP generated by script).
#
# Decide it FROM THE RM'S SOURCES, record the evidence in the comment on the
# entry, and when in doubt choose "arm-aaa": marking Arm IP "open" publishes it,
# the reverse only costs a private repo.
#
# gen_manifest.py copies it into every overlay/<rm>/manifest.json as the
# top-level "ip_class" (Harness Manager reads it; anything else shows as
# "unknown"). scripts/harness_gates/check_overlay_ip_class.py (make check
# stage 2) holds every manifest to this file, and fails an RM marked "open"
# whose own sources name an Arm-IP root. The loop under RM_ORDER below refuses
# to source this file if an RM's ip_class is missing or not one of the two.
# -----------------------------------------------------------------------------

array set RM_LIB {}

# rm_greybox — inert tie-off stub. Legal fabric, does nothing; this is what
# the RP holds immediately after the shell's own bitstream loads, before the
# MicroBlaze streams a real default overlay in from NVM (spec §8A.3). Also
# doubles as the DFX "blank RM" analogue of the Z2 probe's config_blank, AND
# is the DFX *reference config* (RM_ORDER's first entry — see below).
#
# I19 RESOLVED (2026-07-04, A2/W2) -> hand-authored tie-off wrapper, NOT
# Vivado buffer_ports. Full reasoning in the module header of
# fpga/dfx/rms/rm_greybox/rm_greybox.sv ("I19 — greybox-generation decision").
# Short version: buffer_ports is a post-extraction operation on an ALREADY
# black-boxed+locked static, which conflicts with greybox's OTHER role here
# as the pre-extraction reference config; a hand wrapper also lets rm_id
# drive a real, positively-checkable 0x00000000 instead of a floating buffer.
set RM_LIB(rm_greybox,wrapper_dir)  "fpga/dfx/rms/rm_greybox"
set RM_LIB(rm_greybox,synth_mode) "inline"
set RM_LIB(rm_greybox,top)          "rm_greybox"
set RM_LIB(rm_greybox,design_id)    "0x0000"
set RM_LIB(rm_greybox,version)      "0.0.0"
# => rm_id 0x00000000. THE CARVE-OUT: greybox is the one RM held at v0.0, so
# its rm_id stays EXACTLY all-zero (see the encoding note at the top of this
# file). Do not "promote" it to v1.0 — that would make it 0x01000000 and break
# both DECOUPLED_VALUE 0x0 and the "0 == nothing loaded" convention.
set RM_LIB(rm_greybox,rm_name)      "greybox"
set RM_LIB(rm_greybox,ip_class)     "open"   ;# hand tie-off, one self-contained .sv, no dependencies
set RM_LIB(rm_greybox,synth_recipe) "OOC synth_design -mode out_of_context -top rm_greybox against fpga/dfx/rms/rm_greybox/rm_greybox.sv (single file, no deps) — see fpga/dfx/rms/README.md \"Build recipe\""

# rm_regdemo_a / rm_regdemo_b — two minimal register-difference demo RMs (this
# session). IDENTICAL except rm_id + the dut_gpio_o[7:0] LED pattern, so a
# partial swap between them shows a plainly-visible register difference on a
# bit-identical static: rm_id 0x000000A1/0x000000B2 (read at DFXCTL.RM_ID
# 0x44A1_0010) and LED bar 0xA5/0xBA (proof-shell LED[3:0] = 0x5/0xA,
# complementary → 0xF5 vs 0x0A). Single-file OOC like greybox/led.
set RM_LIB(rm_regdemo_a,wrapper_dir)  "fpga/dfx/rms/rm_regdemo_a"
set RM_LIB(rm_regdemo_a,synth_mode) "inline"
set RM_LIB(rm_regdemo_a,top)          "rm_regdemo_a"
set RM_LIB(rm_regdemo_a,design_id)    "0x00A1"
set RM_LIB(rm_regdemo_a,version)      "1.0.0"    ;# => rm_id 0x010000A1
set RM_LIB(rm_regdemo_a,rm_name)      "regdemo_a"
set RM_LIB(rm_regdemo_a,ip_class)     "open"   ;# one self-contained .sv, no dependencies
set RM_LIB(rm_regdemo_a,synth_recipe) "OOC synth_design -mode out_of_context -top rm_regdemo_a against fpga/dfx/rms/rm_regdemo_a/rm_regdemo_a.sv (single file, no deps)"

set RM_LIB(rm_regdemo_b,wrapper_dir)  "fpga/dfx/rms/rm_regdemo_b"
set RM_LIB(rm_regdemo_b,synth_mode) "inline"
set RM_LIB(rm_regdemo_b,top)          "rm_regdemo_b"
set RM_LIB(rm_regdemo_b,design_id)    "0x00B2"
set RM_LIB(rm_regdemo_b,version)      "1.0.0"    ;# => rm_id 0x010000B2
set RM_LIB(rm_regdemo_b,rm_name)      "regdemo_b"
set RM_LIB(rm_regdemo_b,ip_class)     "open"   ;# one self-contained .sv, no dependencies
set RM_LIB(rm_regdemo_b,synth_recipe) "OOC synth_design -mode out_of_context -top rm_regdemo_b against fpga/dfx/rms/rm_regdemo_b/rm_regdemo_b.sv (single file, no deps)"

# rm_led — the "does the swap machinery work" RM (IMPLEMENTATION_PLAN.md
# Phase 1.1/1.2): a free-running counter blinking a few dut_gpio_o bits, no
# SoC inside. This is the RM the greybox is pr_verify'd against for the
# Phase-1.1 acceptance ("pr_verify clean across greybox<->LED-counter RM")
# and the RM the Phase-1.2 tender-JTOG/XVC swap demo loads and unloads twice.
# rm_id 0x0000001E: the task brief that originated this RM spelled it
# "0x0000_00LE" as a mnemonic, but 'L' is not a legal hex digit — read as
# leetspeak (L->1) it resolves to a legal constant, 0x0000001E ("1E"). See
# fpga/dfx/rms/rm_led/rm_led.sv's rm_id comment; flagged for A6 to confirm.
set RM_LIB(rm_led,wrapper_dir)      "fpga/dfx/rms/rm_led"
set RM_LIB(rm_led,synth_mode)     "inline"
set RM_LIB(rm_led,top)              "rm_led"
set RM_LIB(rm_led,design_id)        "0x001E"
set RM_LIB(rm_led,version)          "1.0.0"      ;# => rm_id 0x0100001E
set RM_LIB(rm_led,rm_name)          "led"
set RM_LIB(rm_led,ip_class)         "open"   ;# one self-contained .sv, no dependencies
set RM_LIB(rm_led,synth_recipe)     "OOC synth_design -mode out_of_context -top rm_led against fpga/dfx/rms/rm_led/rm_led.sv (single file, no deps) — see fpga/dfx/rms/README.md \"Build recipe\""

# rm_uart_echo — the first RM that moves DATA, not just a register. Drives the
# partition-pins.md "Console / trace" AXI-Stream byte pair: emits a reset-time
# ASCII banner on uart_tx (DUT->host) then echoes every uart_rx byte back
# (host->DUT->host round trip), through an internal single-clock FWFT FIFO that
# handles AXIS backpressure on either side with no byte loss. Single-file OOC
# like greybox/led/regdemo (dut_clk-only; the boundary AXIS CDC is the shell's,
# so no <rm>_rm.xdc). rm_id 0x01000004 (design_id 0x0004 @ 1.0.0, read at
# DFXCTL.RM_ID 0x44A1_0010) — it was 0x4543484F = ASCII "ECHO" until the v2
# encoding (see the RE-NUMBERED note below).
set RM_LIB(rm_uart_echo,wrapper_dir)  "fpga/dfx/rms/rm_uart_echo"
set RM_LIB(rm_uart_echo,synth_mode) "inline"
set RM_LIB(rm_uart_echo,top)          "rm_uart_echo"
set RM_LIB(rm_uart_echo,design_id)    "0x0004"
set RM_LIB(rm_uart_echo,version)      "1.0.0"    ;# => rm_id 0x01000004
# *** RE-NUMBERED 2026-07-14 (the one breaking change of the v2 encoding). ***
# WAS 0x4543484F = ASCII "ECHO". That mnemonic spent all 32 bits, so under the
# v2 encoding it would decode as design 0x484F at version 69.67 — it collides
# with the version field and cannot be preserved. uart_echo therefore takes the
# next free design_id, 0x0004. The ASCII cuteness is gone; the id is now a
# plain number like every other RM. See the header block of this file for the
# full list of places that hard-coded the old value.
set RM_LIB(rm_uart_echo,rm_name)      "uart_echo"
set RM_LIB(rm_uart_echo,ip_class)     "open"   ;# one self-contained .sv (its FIFO is inline), no dependencies
set RM_LIB(rm_uart_echo,synth_recipe) "OOC synth_design -mode out_of_context -top rm_uart_echo against fpga/dfx/rms/rm_uart_echo/rm_uart_echo.sv (single file, no deps); standalone timing via fpga/dfx/rms/rm_uart_echo/ooc_synth.tcl"

# rm_nanosoc — the REAL single-core nanosoc Cortex-M0 SoC (I1/D2b resolved:
# module `nanosoc` from the read-only $SOCLABS_NANOSOC_SOC_DIR
# checkout, NOT the multicore system), delivered through fpga/rp/nanosoc/'s
# partition-pin wrapper + uart_axis_shim. rm_id 0x00000001 intentionally
# matches the worked example in overlay-manifest.md AND is now the real
# constant the wrapper drives (RM_ID_NANOSOC) — RM-load verify agrees.
#
# STATUS 2026-07-04 (b3e8e99): FILLED + PROVEN board-free. OOC synth clean
# (0 errors, 7,935 LUTs — the D7 sizing datapoint) via
# fpga/rp/nanosoc/{filelist.tcl,ooc_synth.tcl}; DFX config-3 (nanosoc vs the
# greybox-locked static) pr_verify COMPATIBLE, timing met (WNS +6.05 ns),
# partial (3.6 MB) + clearing (240 KB) emitted — see
# fpga/dfx/proof/build_rm_nanosoc.tcl + proof_results_2026-07-04/
# {pr_verify_nanosoc,timing_nanosoc,util_rm_nanosoc}.rpt.
set RM_LIB(rm_nanosoc,wrapper_dir)  "fpga/rp/nanosoc"
set RM_LIB(rm_nanosoc,synth_mode) "prebuilt"
set RM_LIB(rm_nanosoc,top)          "rp_nanosoc_wrapper"
set RM_LIB(rm_nanosoc,design_id)    "0x0001"
set RM_LIB(rm_nanosoc,version)      "1.0.0"      ;# => rm_id 0x01000001
set RM_LIB(rm_nanosoc,rm_name)      "nanosoc"
set RM_LIB(rm_nanosoc,ip_class)     "arm-aaa"   ;# Cortex-M0 + CMSDK + SoC-400 via $SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl and $ARM_IP_LIBRARY_PATH (fpga/rp/nanosoc/ooc_synth.tcl)
set RM_LIB(rm_nanosoc,synth_recipe) "PROVEN: OOC synth via fpga/rp/nanosoc/ooc_synth.tcl, which sources the external proven flist \$SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl (nanosoc_FPGA.flist from the read-only nanosoc_m0_soc checkout + \$ARM_IP_LIBRARY_PATH, NOT vendored) and then read_verilog's the exp_*-patched nanosoc.sv + uart_axis_shim.sv + rp_nanosoc_wrapper.sv + ahb_clcd. NOTE fpga/rp/nanosoc/filelist.tcl is NOT sourced by this recipe — it is a standalone diagnostic flist only. See fpga/dfx/proof/build_rm_nanosoc.tcl"

# rm_eth_ss — the standalone AHB-MAC + PTP subsystem (ethernet-subsystem-ahb)
# as a second DUT variant (phase 5.5), same partition-pin boundary contract.
#
# STATUS 2026-07-07: FILLED + PROVEN board-free, and pr_verified against the
# real 256 KiB shell (static_id 0xECCEDBF3). The wrapper (fpga/rp/eth_ss/
# rp_eth_ss_wrapper.sv) carries the full partition-pins.md v0.1 boundary
# incl. the I4 dut_gpio_* group (30 ports + NGPIO=16), so it passes the
# readiness filter. Unlike rm_nanosoc this DUT has a REAL MAC (RMII+MDIO
# live, real irq_out) and a DMA SRAM. OOC synth clean (0 errors, 1,769 LUTs
# — the D7 datapoint); DFX config pr_verify COMPATIBLE vs the greybox-locked
# 256 KiB static, partial+clearing emitted — see
# fpga/dfx/prod_results_2026-07-07-256k/{pr_verify,timing,util}_rm_eth_ss.rpt.
set RM_LIB(rm_eth_ss,wrapper_dir)   "fpga/rp/eth_ss"
set RM_LIB(rm_eth_ss,synth_mode)  "prebuilt"
set RM_LIB(rm_eth_ss,top)           "rp_eth_ss_wrapper"
set RM_LIB(rm_eth_ss,design_id)     "0x0002"
set RM_LIB(rm_eth_ss,version)       "1.0.0"      ;# => rm_id 0x01000002
set RM_LIB(rm_eth_ss,rm_name)       "eth_ss"
set RM_LIB(rm_eth_ss,ip_class)      "arm-aaa"   ;# cmsdk_ahb_to_apb.v + cmsdk_ahb_to_sram.v from $CMSDK_DIR (fpga/rp/eth_ss/filelist.tcl, _extra_files); the MAC/PTP themselves are OpenCores
set RM_LIB(rm_eth_ss,synth_recipe) "PROVEN: OOC synth via fpga/rp/eth_ss/ooc_synth.tcl (sources = fpga/rp/eth_ss/filelist.tcl: rp_eth_ss_wrapper + bring-up FSM + ethmac_subsystem_apb.flist internals + AHB-wrapper extras from the read-only ethernet-subsystem-ahb / ethernet-mac-ahb checkouts + \$ARM_IP_LIBRARY_PATH, NOT vendored) — stage via `make rm-eth-ss-dcp` (reuses fpga/rp/eth_ss/build/rm_eth_ss_synth.dcp if present)"

# rm_nanosoc_multicore — the REAL two-core ETHERNET nanoSoC (module
# `nanosoc_multicore_soc` from the read-only multicore DUT checkout
# $NANOSOC_MULTICORE_HOME): network_core (CPU0,
# ethernet/PTP stack) + chip_core (CPU1, chip control), a real OpenCores MAC +
# HA1588 PTP + PHC + IPC mailbox + DMA-250 + QSPI, all behind the same
# partition-pin boundary. Delivered through fpga/rp/nanosoc_multicore/'s wrapper
# + the shared ../nanosoc/uart_axis_shim. This is the richest DUT variant: BOTH
# the Console group (CPU0 UART) AND the Ethernet group (RMII+MDIO) are REAL, and
# irq_out carries the live eth_irq. rm_id 0x00000003 — next free id after
# greybox(0)/nanosoc(1)/eth_ss(2).
#
# STATUS 2026-07-10: FILLED board-free. Wrapper carries the full 30-signal
# partition-pins.md v0.1 boundary (NGPIO=16), so it passes pin_check. The
# tied/open/terminated disposition for D2D / eth_ss_0 test slave / QSPI / PL022
# SPI / JTAG / scan / PMU-NMI-RXEV / hostio4 is copied verbatim from the DUT's
# own proven auto-generated FPGA wrapper (build_soc/rtl/
# nanosoc_multicore_vivado_wrapper.v), incl. the nanosoc_d2d_idle_slave
# terminator on the D2D outbound master. Ships nanosoc_multicore_ooc.xdc
# (dut_clk + phy_rmii_ref_clk + swd_clk) AND nanosoc_multicore_rm.xdc (the
# RM-internal WB<->MII async group). Stage via `make rm-nanosoc-multicore-dcp`;
# add to a locked static without re-minting via `make add-rm-nanosoc-multicore`.
set RM_LIB(rm_nanosoc_multicore,wrapper_dir)  "fpga/rp/nanosoc_multicore"
set RM_LIB(rm_nanosoc_multicore,synth_mode) "prebuilt"
set RM_LIB(rm_nanosoc_multicore,top)          "rp_nanosoc_multicore_wrapper"
set RM_LIB(rm_nanosoc_multicore,design_id)    "0x0003"
set RM_LIB(rm_nanosoc_multicore,version)      "1.0.0"  ;# => rm_id 0x01000003
set RM_LIB(rm_nanosoc_multicore,rm_name)      "nanosoc_multicore"
set RM_LIB(rm_nanosoc_multicore,ip_class)     "arm-aaa"   ;# Cortex-M0+ pair + CMSDK via $NANOSOC_MULTICORE_HOME/pynq/filelist.tcl (CMSDK_DIR, ARM_CORTEXM0PLUS_IP_PATH)
set RM_LIB(rm_nanosoc_multicore,synth_recipe) "OOC synth via fpga/rp/nanosoc_multicore/ooc_synth.tcl (sources = fpga/rp/nanosoc_multicore/filelist.tcl: rp_nanosoc_multicore_wrapper + ../nanosoc/uart_axis_shim + the DUT's proven \$NANOSOC_MULTICORE_HOME/pynq/filelist.tcl fileset — nanosoc_multicore_soc + Cortex-M0+ pair + CMSDK + OpenCores MAC + HA1588 + PHC + DMA-250 + IPC + QSPI + soc_glue, read-only, NOT vendored; ETH_WISHBONE_B3 + RAM_PRELOAD) — stage via `make rm-nanosoc-multicore-dcp`"

# Build order: rm_greybox MUST be first (it is the DFX reference config that
# static_routed_locked.dcp is extracted from). rm_led second: it is the RM
# the Phase 1.1/1.2 acceptance criteria (IMPLEMENTATION_PLAN.md) are written
# against — "pr_verify clean across greybox<->LED-counter RM", then two
# tender-JTAG/XVC swaps. rm_nanosoc third — real and proven; rm_eth_ss fourth
# — real MAC+PTP DUT, wrapper proven and pr_verified against the 256 KiB shell
# (STATUS note above). All four now pass the readiness filter once their OOC
# dcps are staged. Order of the rest is cosmetic/report-ordering.
# rm_nanosoc_upy — the single-core nanoSoC hosting a MicroPython REPL. Ships in
# TWO build flavours of the SAME fabric (same wrapper, boundary and rm_id
# 0x01000005 — see D17 in docs/contracts/OPEN_ISSUES.md and README_XIP.md):
#
#   * SHIPPED DEFAULT = FLASH-BOOT (XiP). Empty IMEM; MicroPython cold-boots from
#     external QSPI flash (aperture 0x7000_0000) through the CG092 cache. This is
#     the PRODUCT path (one config across sim/FPGA/ASIC) and is PROVEN on silicon
#     2026-07-21 (docs/QSPI_RP_BOARD_BRINGUP.md "PRODUCT GATE"): banner +
#     print(1+1)->2, with HOT-copy AND cold-execute-in-place both confirmed by
#     rigorous negative controls. Build: `make rm-nanosoc-upy-dcp`.
#     ⚠ Deploying it needs the boot image programmed at flash 0x0 first (fast:
#     the M0 loader does 160 KB in ~3 min) or the DUT boots to silence.
#
#   * SCAFFOLD variant = fat BRAM (128 KB IMEM) with MicroPython BAKED IN; boots
#     with NO flash. An FPGA-only bring-up/CI convenience that silicon cannot
#     reproduce (a 128 KB SRAM macro is real ASIC area). Build:
#     `make rm-nanosoc-upy-scaffold-dcp`. Not the shipped overlay; do not quote
#     its resource numbers as the SoC's.
#
# (This entry used to be the scaffold, marked "retire once the QSPI RM boots" —
# it now has, so the default flipped to XiP and the scaffold became the variant.)
#
# WHY IT IS FREE: the boundary is NOT widened — rp_nanosoc_upy_wrapper carries
# the identical 30-signal partition-pins.md v0.1 contract and simply
# INSTANTIATES rp_nanosoc_wrapper with two memory-size generics overridden and a
# different baked IMEM image. So `static_id` (0xE4B1C44A) is NOT re-minted, the
# static shell is NOT rebuilt, every fielded overlay keeps working, and the new
# partial just drops in alongside them.
set RM_LIB(rm_nanosoc_upy,wrapper_dir)  "fpga/rp/nanosoc_upy"
set RM_LIB(rm_nanosoc_upy,synth_mode) "prebuilt"
set RM_LIB(rm_nanosoc_upy,top)          "rp_nanosoc_upy_wrapper"
set RM_LIB(rm_nanosoc_upy,design_id)    "0x0005"
set RM_LIB(rm_nanosoc_upy,version)      "1.0.0"  ;# => rm_id 0x01000005
set RM_LIB(rm_nanosoc_upy,rm_name)      "nanosoc_upy"
set RM_LIB(rm_nanosoc_upy,ip_class)     "arm-aaa"   ;# instantiates rp_nanosoc_wrapper, same Arm source set as rm_nanosoc (fpga/rp/nanosoc_upy/ooc_synth.tcl)
set RM_LIB(rm_nanosoc_upy,synth_recipe) "SHIPPED DEFAULT = FLASH-BOOT (XiP): OOC synth via fpga/rp/nanosoc_upy/ooc_synth.tcl with a QSPI-enabled bootrom + an EMPTY IMEM (both env overrides; see fpga/rp/nanosoc_upy/README_XIP.md) so MicroPython boots FROM FLASH, not baked BRAM. Proven on silicon 2026-07-21 (docs/QSPI_RP_BOARD_BRINGUP.md PRODUCT GATE). Stage via `make rm-nanosoc-upy-dcp`; fold in with `make add-rm-nanosoc-upy`. The NO-FLASH baked-BRAM scaffold is the same fabric (same rm_id) built via `make rm-nanosoc-upy-scaffold-dcp` — a bring-up variant, not the shipped overlay."

# rm_socscope — the SoCScope TRACE PLANE as an RM (MPS3_BRINGUP_TESTS.md B1).
#
# The `swo` partition pin has been fully plumbed since the shell was minted —
# decoupler-clamped, FIFO'd, TCP-relayed — and every RM ties it to zero, so its RP
# end has never been driven. This RM drives it. That is the entire delta, and it is
# why this costs a PARTIAL BUILD ONLY: `swo` was already in the boundary (35
# ports then, 47 / 148 bits since the ILA mint's `dbgbscan` group), so
# `static_id` was NOT re-minted and every fielded overlay kept working.
#
# Contents: socscope_trace_top (probe -> record former -> ring -> domain FIFO ->
# framer -> egress, plus an AHB-Lite CSR) driven by socscope_selftest, which
# configures the block over its own CSR port and then drives deterministic
# synthetic AHB traffic past the probe. The host recomputes the expected transfers
# from the documented formula and requires exactly those — the same
# check_selftest.py used for the Icarus bench, so simulation and silicon are
# answered by identical code.
#
# NOT VENDORED: sources come from the SoCScope checkout via SOCSCOPE_HOME (see
# fpga/dfx/rms/rm_socscope/filelist.tcl), the same arrangement rm_nanosoc and
# rm_eth_ss use for their DUTs.
#
# The divisor is 24 and MUST equal firmware's UART_OVER_ETH_SWO_DIVISOR. The bit
# period is divisor+1 cycles on both sides -- but since the Stage-C split the RM
# counts TRACE clocks (phy_rmii_ref_clk) and the shell's swo_uart_rx still counts
# dut_clk, so the two agree only while dut_clk is at its 50 MHz default. Moving
# swo_uart_rx onto the free-running clock is the shell half of the same change.
set RM_LIB(rm_socscope,wrapper_dir)  "fpga/dfx/rms/rm_socscope"
set RM_LIB(rm_socscope,synth_mode) "prebuilt"
set RM_LIB(rm_socscope,top)          "rm_socscope"
set RM_LIB(rm_socscope,design_id)    "0x0006"
set RM_LIB(rm_socscope,version)      "1.0.0"    ;# => rm_id 0x01000006
set RM_LIB(rm_socscope,rm_name)      "socscope"
set RM_LIB(rm_socscope,ip_class)     "open"   ;# only $SOCSCOPE_HOME/hw/rtl socscope_* files (SoCScope: Apache-2.0, states it holds no vendor IP) + rm_socscope.sv
set RM_LIB(rm_socscope,synth_recipe) "OOC synth via fpga/dfx/rms/rm_socscope/ooc_synth.tcl (sources = fpga/dfx/rms/rm_socscope/filelist.tcl: rm_socscope + socscope_selftest + the module list read from \$SOCSCOPE_HOME/hw/rtl/socscope_trace_top.f, read-only, NOT vendored)"

# rm_clcd_demo -- a CPU-less test-card generator on the display tunnel. It exists
# to answer ONE question with a photograph: has any DUT ever driven the on-board
# panel? The KVM, the tunnel and the DUT-side socket are all fielded on
# 0xA8C1C535 and none of them has been observed end to end, because every other
# candidate needs a CPU to boot and firmware to be right. This one is pure RTL:
# it starts on reset and paints colour bars plus a 16-cell binary frame counter.
# STATUS: sim-proven (tests/clcd_demo), NEVER on silicon. See
# fpga/rp/clcd_demo/README.md and its "THE PROOF PROCEDURE (Wave C)" section.
#
# REGISTERED 2026-09-14 (MINT-PREP) from fpga/rp/clcd_demo/rm_list_snippet.tcl,
# which is the authoring lane's own copy-paste fragment and stays there as the
# record of what was pasted. Boundary proven before registration: the lane's
# pin_check_mirror.py ran the REAL gate over a scratch mirror (11/11); with this
# entry live the real gate reports 12/12.
#
# version 0.1.0, deliberately NOT 1.0.0: this repo's floor for 1.0.0 is
# "pr_verified against a real locked static AND exercised on silicon" (VERSION
# POLICY at the top of this file) and this RM is neither. => rm_id 0x00010007,
# which is the value fpga/rp/clcd_demo/README.md's PASS criteria already tell the
# operator to expect from `pyverify ping`.
#
# RESOLVED 2026-09-14 (same batch): check_rm_id_encoding.py now reads the
# composed {RM_VER_MAJOR, RM_VER_MINOR, RM_DESIGN_ID} form the template emits
# (it composes the members itself and compares; it trusts nothing), and
# rp_clcd_demo_wrapper.sv says 0/1 => 0x00010007, agreeing with this entry and
# the README. The template now defaults to 0.1 too. tests/integration/
# test_rm_id_encoding_gate.py pins all of it.
set RM_LIB(rm_clcd_demo,wrapper_dir)  "fpga/rp/clcd_demo"
set RM_LIB(rm_clcd_demo,synth_mode)   "prebuilt"
set RM_LIB(rm_clcd_demo,top)          "rp_clcd_demo_wrapper"
set RM_LIB(rm_clcd_demo,design_id)    "0x0007"
set RM_LIB(rm_clcd_demo,version)      "0.1.0"    ;# => rm_id 0x00010007
set RM_LIB(rm_clcd_demo,rm_name)      "clcd_demo"
set RM_LIB(rm_clcd_demo,ip_class)     "open"   ;# shell clcd_core.sv + clcd_demo_gen.sv + the wrapper, all in this repo (filelist.tcl)
# "prebuilt", NOT "inline": this RM has a filelist.tcl (it reads the SHARED
# clcd_core.sv out of fpga/shell/ip/clcd/ rather than vendoring a second copy of
# the one 8080 engine that has a proof). build_dfx.tcl's inline path reads only
# <wrapper_dir>/<top>.sv, which would silently infer clcd_core and clcd_demo_gen
# as BLACK BOXES and "succeed" with an RM that draws nothing -- the exact failure
# the synth_mode block above was added to prevent.
set RM_LIB(rm_clcd_demo,synth_recipe) "OOC synth via fpga/rp/clcd_demo/ooc_synth.tcl (sources = fpga/rp/clcd_demo/filelist.tcl: shell clcd_core.sv + clcd_demo_gen.sv + the wrapper) -- stage via `make -C fpga/dfx rm-clcd-demo-dcp`"

# rm_nanosoc_iice -- the SAME single-core nanosoc DUT as rm_nanosoc, but
# synthesised by Synplify Premier with a Synopsys Identify IICE woven in and the
# soft TAP chained behind the DUT's SoC-400 SWJ-DP on the one jtag_* wire-set
# (IEEE 1149.1 daisy chain: Identify nearest TDI, SWJ-DP nearest TDO, so the DAP
# stays at OpenOCD chain position 0). Boundary is the SAME 47 ports / 148 bits
# as every other RM (fpga/shell/boundary.yaml; 35 / 136 until fda3201 added the
# dbgbscan group), so this is a PARTIAL-ONLY design: it does not move static_id
# and does not re-key a fielded overlay. See fpga/rp/nanosoc_iice/README.md,
# docs/planning/IICE_JTAG_CHAIN.md and tests/jtag_chain/ (7/7 under VCS).
#
# STATUS: no bitstream has ever been produced and no hardware has been touched
# (that directory's "Gate status" ledger is the honest one). version 0.1.0
# => rm_id 0x00010008; design_id 0x0008 is the next free id after clcd_demo's
# 0x0007. Bump to 1.0.0 in the commit that records a pr_verify against a real
# locked static plus a silicon run.
#
# WHY A SEPARATE design_id AND NOT AN ALIAS OF rm_nanosoc. That directory's
# `make stage` copies its checkpoint over fpga/dfx/build/prod/rm_nanosoc_synth.dcp
# so the IICE build BECOMES rm_nanosoc for one prod run. That is fine for a
# one-off A/B, and wrong for a mint: two designs would share rm_id 0x01000001,
# DFXCTL.RM_ID could not tell an operator which of them is in the RP, and
# clcd_rm_name()'s masked lookup would name the wrong one. A mint that carries
# both needs two ids.
#
# RESOLVED 2026-09-14 (same batch): rp_nanosoc_iice_shim.sv now drives its own
# `localparam RM_ID_NANOSOC_IICE = 32'h0001_0008` and leaves the core's
# 0x01000001 dangling; the gate reads it and agrees with this entry. Consequence
# the shim states in full: that directory's `make stage` (hand the checkpoint to
# build_dfx AS rm_nanosoc) is no longer identity-compatible and warns; and the
# prebuilt build/rm_nanosoc_synth.dcp predates the change -- step (1) below
# must be re-run (licence seat) before this RM is minted.
set RM_LIB(rm_nanosoc_iice,wrapper_dir)  "fpga/rp/nanosoc_iice"
set RM_LIB(rm_nanosoc_iice,synth_mode)   "prebuilt"
set RM_LIB(rm_nanosoc_iice,top)          "rp_nanosoc_iice_shim"
set RM_LIB(rm_nanosoc_iice,design_id)    "0x0008"
set RM_LIB(rm_nanosoc_iice,version)      "0.1.0"  ;# => rm_id 0x00010008
set RM_LIB(rm_nanosoc_iice,rm_name)      "nanosoc_iice"
set RM_LIB(rm_nanosoc_iice,ip_class)     "arm-aaa"   ;# the rm_nanosoc DUT (Cortex-M0, CMSDK, SoC-400 SWJ-DP) from $SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl (gen_prj.tcl)
set RM_LIB(rm_nanosoc_iice,synth_recipe) "TWO TOOLS, and only the second is Vivado: (1) Synplify Premier 2022.09-SP2 + Identify instrumentor, LICENCE SEAT REQUIRED -- `make -C fpga/rp/nanosoc_iice prj synth` emits build/rev_1_identify/rp_nanosoc_iice_core.edf from the flattened 240-source nanosoc filelist; (2) `make -C fpga/rp/nanosoc_iice dcp` runs fpga/rp/nanosoc_iice/ooc_synth_synplify.tcl under Vivado 2024.1 (read_edif + black-box stub + the RTL shim -> OOC checkpoint, plus the netlist-level boundary/no-BSCANE2/no-BUFG/BRAM-INIT gates) and writes build/rm_nanosoc_synth.dcp. The dfx Makefile picks that file up through RM_SYNTH_REUSE_rm_nanosoc_iice -- it CANNOT run step (1) itself. Do NOT use that directory's `make stage`: it overwrites rm_nanosoc's checkpoint."

# --- ILA-carrying RMs (ILA mint, docs/planning/ILA_MINT_PLAN_2026-09-23.md) ----
# debug -- NEW KEY: `RM_LIB(<rm>,debug) 1` means "this RM carries its own debug
# hub (debug_bridge mode 1, xsdbm) and at least one ILA", so the flow must
# write its per-RM partial .ltx (write_debug_probes -cell) and carry it in the
# overlay. ABSENT means 0: every RM without the key ties dbg_bscan_tdo to 0.
# Consumers read it WITH A DEFAULT (lane FLOW's build_dfx.tcl). Both RMs below
# are `prebuilt`: an inline RM reads one .sv and cannot carry IP (handover F14).
# The debug IP is created by script (fpga/rp/common/dbg_ip.tcl), never committed.

# rm_dbg_demo -- a 16-bit counter on dut_clk + one ILA (probe0 = counter[15:0],
# probe1 = tick when counter[7:0]==0, 1024 deep) behind the RM's own hub on
# phy_rmii_ref_clk; blinks rm_led's four GPIO legs as a sign of life. The B2
# proof vehicle: the smallest thing that proves an ILA in the RP answers over
# XVC. design_id 0x0009 = the next free id after nanosoc_iice's 0x0008.
set RM_LIB(rm_dbg_demo,wrapper_dir)  "fpga/rp/dbg_demo"
set RM_LIB(rm_dbg_demo,synth_mode)   "prebuilt"
set RM_LIB(rm_dbg_demo,top)          "rp_dbg_demo_wrapper"
set RM_LIB(rm_dbg_demo,design_id)    "0x0009"
set RM_LIB(rm_dbg_demo,version)      "1.0.0"    ;# => rm_id 0x01000009
set RM_LIB(rm_dbg_demo,rm_name)      "dbg_demo"
set RM_LIB(rm_dbg_demo,ip_class)     "open"   ;# a counter + AMD debug_bridge/ILA made by script (fpga/rp/common/dbg_ip.tcl) + rp_dbg_hub.sv; no external source tree
set RM_LIB(rm_dbg_demo,debug)        1
set RM_LIB(rm_dbg_demo,synth_recipe) "OOC synth via fpga/rp/dbg_demo/ooc_synth.tcl: fpga/rp/common/dbg_ip.tcl creates rp_dbg_bridge (debug_bridge 3.0 mode 1, C_DESIGN_TYPE 1, 50 MHz hub clock, no BUFR) + ila_dbg_demo {16,1}x1024 in the stage dir and synth_ip's each (IP XDC OOC); then read_ip + fpga/rp/common/rp_dbg_hub.sv + the wrapper, synth_design -mode out_of_context, dbg_demo_ooc.xdc, and the DBG_CHECK netlist gate (0 BSCANE2, 0 clock cells, 1 xsdbm, >=1 ILA) before the checkpoint -- stage via `make -C fpga/dfx rm-dbg-demo-dcp`"

# rm_nanosoc_ila -- rp_nanosoc_wrapper UNCHANGED (nested as rm_nanosoc_upy
# nests it) + the RM hub + one ILA (16384 deep, capture qualification on) on the
# nets nanosoc DRIVES OUT of its wrapper: the console AXIS byte + valid/ready,
# dut_gpio_o, jtag_tdo, dut_resetn, and a 32-bit cycle stamp. Vivado synthesis
# cannot read into the SoC hierarchy, and netlist insertion was never spiked,
# so boundary-visible nets are all it sees (plan §0b). design_id 0x000A.
# In RM_ORDER: its OOC synth completed 2026-09-23 12:36 (DBG_CHECK all pass,
# 48 RAMB36 incl. nanosoc's own 20.5). Should it ever have to leave the mint,
# drop it from RM_ORDER and fold it in later with
# `make -C fpga/dfx add-rm-nanosoc-ila BUILD=<locked tree>` (no re-key).
set RM_LIB(rm_nanosoc_ila,wrapper_dir)  "fpga/rp/nanosoc_ila"
set RM_LIB(rm_nanosoc_ila,synth_mode)   "prebuilt"
set RM_LIB(rm_nanosoc_ila,top)          "rp_nanosoc_ila_wrapper"
set RM_LIB(rm_nanosoc_ila,design_id)    "0x000A"
set RM_LIB(rm_nanosoc_ila,version)      "1.0.0"  ;# => rm_id 0x0100000A
set RM_LIB(rm_nanosoc_ila,rm_name)      "nanosoc_ila"
set RM_LIB(rm_nanosoc_ila,ip_class)     "arm-aaa"   ;# instantiates rp_nanosoc_wrapper, same Arm source set as rm_nanosoc (fpga/rp/nanosoc_ila/ooc_synth.tcl)
set RM_LIB(rm_nanosoc_ila,debug)        1
set RM_LIB(rm_nanosoc_ila,synth_recipe) "OOC synth via fpga/rp/nanosoc_ila/ooc_synth.tcl: the rm_nanosoc source set through the SAME env contract as fpga/rp/nanosoc/ooc_synth.tcl (SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl + the exp_* shape check + uart_axis_shim + the nanosoc_exp socket + rp_nanosoc_wrapper, read-only), plus fpga/rp/common/dbg_ip.tcl's rp_dbg_bridge + ila_nanosoc {8,1,1,16,1,1,32}x16384 with capture qualification, rp_dbg_hub.sv and the wrapper; nanosoc_ila_ooc.xdc; the DBG_CHECK netlist gate -- stage via `make -C fpga/dfx rm-nanosoc-ila-dcp`"

set RM_ORDER [list rm_greybox rm_regdemo_a rm_regdemo_b rm_led rm_uart_echo rm_nanosoc rm_eth_ss rm_nanosoc_multicore rm_nanosoc_upy rm_socscope rm_clcd_demo rm_nanosoc_iice rm_dbg_demo rm_nanosoc_ila]

# --- rm_id DERIVATION (encoding v2) -------------------------------------------
# rm_id is COMPUTED from (design_id, version), never hand-written, so the
# wrapper localparam and the manifest cannot silently drift from each other.
# RM_LIB(<rm>,rm_id) is still populated as a plain array entry, so every
# existing consumer -- build_dfx.tcl's `rm_field $rm_key rm_id` (both the full
# and the incremental-add paths), which feeds overlay_inputs.txt and hence
# gen_manifest.py -- keeps working with NO restructuring.

# rm_parse_semver -- "M.m" or "M.m.p" -> {major minor patch}. Rejects anything
# that would silently truncate into the 8-bit version fields.
proc rm_parse_semver { ver rm_key } {
    set parts [split $ver "."]
    if { [llength $parts] < 2 || [llength $parts] > 3 } {
        error "rm_list.tcl: RM '$rm_key' version '$ver' is not M.m or M.m.p"
    }
    lassign $parts major minor patch
    if { $patch eq "" } { set patch 0 }
    foreach {label v} [list major $major minor $minor patch $patch] {
        if { ![string is integer -strict $v] } {
            error "rm_list.tcl: RM '$rm_key' version '$ver' has a non-integer $label ('$v')"
        }
    }
    # major/minor are the only components that FIT in rm_id (8 bits each).
    # patch is manifest-only (VERSIONING_PLAN.md §3.2) but is still range-checked
    # so a bogus value cannot sneak into the host-side record.
    foreach {label v} [list major $major minor $minor] {
        if { $v < 0 || $v > 255 } {
            error "rm_list.tcl: RM '$rm_key' version '$ver' $label=$v does not fit the 8-bit rm_id field (0..255)"
        }
    }
    return [list $major $minor $patch]
}

# rm_id_of -- the single source of truth for the v2 encoding:
#   rm_id = (major << 24) | (minor << 16) | design_id
proc rm_id_of { rm_key } {
    global RM_LIB
    foreach f {design_id version} {
        if { ![info exists RM_LIB(${rm_key},${f})] } {
            error "rm_list.tcl: RM '$rm_key' has no '$f' (encoding v2 requires design_id + version)"
        }
    }
    set design_id [expr { $RM_LIB(${rm_key},design_id) }]
    if { $design_id < 0 || $design_id > 0xFFFF } {
        error "rm_list.tcl: RM '$rm_key' design_id [format 0x%X $design_id] does not fit 16 bits"
    }
    lassign [rm_parse_semver $RM_LIB(${rm_key},version) $rm_key] major minor patch
    return [format "0x%08X" [expr { ($major << 24) | ($minor << 16) | $design_id }]]
}

# Populate RM_LIB(<rm>,rm_id) for every RM, require a legal ip_class, and
# enforce design_id uniqueness --
# two RMs sharing a design_id would make RM-load verify ambiguous and would
# make clcd_rm_name()'s masked lookup return the wrong DUT's name.
array set _RM_SEEN_DESIGN_ID {}
foreach _rm $RM_ORDER {
    set RM_LIB(${_rm},rm_id) [rm_id_of $_rm]
    if { ![info exists RM_LIB(${_rm},ip_class)] ||
         [lsearch -exact {open arm-aaa} $RM_LIB(${_rm},ip_class)] < 0 } {
        error "rm_list.tcl: RM '${_rm}' needs RM_LIB(${_rm},ip_class) set to \"open\" or \"arm-aaa\" (see the ip_class block above)"
    }
    set _did [format "0x%04X" [expr { $RM_LIB(${_rm},design_id) }]]
    if { [info exists _RM_SEEN_DESIGN_ID($_did)] } {
        error "rm_list.tcl: design_id $_did is used by BOTH '$_RM_SEEN_DESIGN_ID($_did)' and '${_rm}' -- design_ids must be unique"
    }
    set _RM_SEEN_DESIGN_ID($_did) $_rm
}
unset -nocomplain _rm _did
array unset _RM_SEEN_DESIGN_ID

# --- Accessor helpers used by build_dfx.tcl -----------------------------------
proc rm_field { rm_key field } {
    global RM_LIB
    set key "${rm_key},${field}"
    if { ![info exists RM_LIB($key)] } {
        error "rm_list.tcl: no '$field' recorded for RM '$rm_key'"
    }
    return $RM_LIB($key)
}

proc rm_all_names {} {
    global RM_ORDER
    return $RM_ORDER
}

# RESOLVED (2026-07-09) — was TODO(A2): "add a `pin_check` recipe ... that diffs
# each wrapper's port list against docs/contracts/partition-pins.md mechanically,
# so a drifted RM wrapper fails fast in CI instead of surfacing as a pr_verify
# failure late in build_dfx.tcl."
#
# Implemented as a separate lint step (Python, not Tcl — it has to parse both
# Markdown and SystemVerilog):
#
#     fpga/dfx/pin_check.py          the checker
#     make -C fpga/dfx pin-check     the recipe
#     root `make check` stage [2/8]  the CI gate
#
# It verifies, for every wrapper in RM_ORDER's directories: all 30 contract
# signals present, direction inverted from the shell's view, exact widths, no
# extra ports, and `parameter int NGPIO = 16`. Negative-controlled against all
# five drift classes (missing / wrong-direction / wrong-width / extra / bad
# NGPIO). Keep RM_ORDER and pin_check.py's WRAPPERS list in step when adding
# an RM.
