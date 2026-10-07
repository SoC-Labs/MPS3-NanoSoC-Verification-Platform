# -----------------------------------------------------------------------------
# fpga/rp/nanosoc_iice/Makefile — build the nanosoc DFX reconfigurable module
# with SYNPLIFY PREMIER + a Synopsys IDENTIFY IICE instead of Vivado synthesis,
# producing an out-of-context checkpoint the existing DFX flow consumes with
# ZERO changes.
#
#   make lint            cheap, no licence, no tools     <- run this first
#   make prj             generate build/nanosoc_iice.prj (tclsh only, no licence)
#   make synth           Synplify + Identify             <- COSTS A LICENCE SEAT
#   make check-no-bscan  the INVERTED BSCANE2 gate on the EDIF
#   make dcp             EDIF -> OOC checkpoint (Vivado 2024.1)
#   make stage           copy the checkpoint where build_dfx.tcl finds it
#   make clean
#
#   make IICE=0 ...      uninstrumented: plain Synplify swap, no Identify.
#                        The 47-port EDIF top IS the RM top (no shim). This is
#                        the honest A/B baseline for "did swapping Vivado for
#                        Synplify change the RM?" (plan Phase 3).
#
# EVERY TARGET FAILS LOUDLY. There is no "skip if tool missing" path anywhere in
# this file: a silently-skipped synthesis gate on a flow that pushes partial
# bitstreams onto real silicon is worse than a red build.
#
# TOOL PINNING — read this before switching to `module load`
# ---------------------------------------------------------
# `module load synplify/2022.09-SP2` PULLS IN vivado/2026.1 (its modulefile does
# `module load vivado` if none is loaded, and the site default is 2026.1). The
# DFX flow is pinned to Vivado 2024.1. So this Makefile calls both tools by
# ABSOLUTE PATH and never loads a module, which makes the two versions
# co-exist in one shell without a load-order hazard.
# -----------------------------------------------------------------------------

RM_IICE_DIR    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO_ROOT      := $(abspath $(RM_IICE_DIR)/../../..)
RM_NANOSOC_DIR ?= $(REPO_ROOT)/fpga/rp/nanosoc
OUT            ?= $(RM_IICE_DIR)/build

PYTHON     ?= python3
TCLSH      ?= tclsh
VERILATOR  ?= verilator
VIVADO     ?= /apps/Xilinx/Vivado/2024.1/bin/vivado
SFPGA_ROOT ?= /eda/synopsys/2022-23/RHELx86/SFPGA_2022.09-SP2
SYNPLIFY   ?= $(SFPGA_ROOT)/fpga/bin/synplify_premier

# The Synplify/Identify synthesis top. NOT the DFX RM top when instrumented —
# see rp_nanosoc_iice_core.sv for why the boundary is split in two.
SYNTH_TOP ?= rp_nanosoc_iice_core

# 1 = instrument with an IICE (default). 0 = plain Synplify swap.
IICE ?= 1
ifeq ($(IICE),1)
  SYNPLIFY_REV ?= rev_1_identify
else
  SYNPLIFY_REV ?= rev_1
endif

# The instrumentation file. THE .idc IS THE BATCH INTERFACE: run.sh in the
# reference flow copies it to <rev>/identify.idc and identify_compile picks it
# up with no instrumentor shell involved. Do NOT script
# identify_instrumentor_shell — `project open` silently kills script execution
# in 2022.09 (recorded in hello_ident/README.md).
#
# Default is this directory's toolchain-proof stub. Point IDC at Stream A's
# generated file (tests/identify_iice/build/<IICE_NAME>.idc) for anything real.
IDC ?= $(RM_IICE_DIR)/fallback_iice.idc

# The committed flattener that sources the real Vivado-TCL filelist with stub
# procs. gen_prj.tcl shells out to it rather than duplicating the logic.
FLATTENER ?= $(REPO_ROOT)/tests/micropython_flash_boot/collect_filelist.tcl

# --- site-local overrides, NOT tracked ---------------------------------------
# Same seam, same file, same precedence as fpga/dfx/Makefile:64-69: tools.env at
# the repo root is `-include`d FIRST so its values win over the defaults below,
# while an explicit `make VAR=...` still wins over both. See tools.env.example.
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)

# --- nanosoc RM environment. The two source-tree variables have NO DEFAULT, on
# purpose, and for the reason fpga/dfx/Makefile:118-126 gives: a default naming
# one workstation's home directory is invisible on that machine and silently
# reads the wrong (or an empty) source tree on every other one, with the failure
# surfacing hours later inside Synplify. Until 2026-09-10 this file baked
# one workstation's {nanosoc_m0_soc,ahb_qspi} checkouts — a public release
# cannot ship that. `make prj` / `synth` now refuse BY NAME, in seconds (see
# needvars). ARM_IP_LIBRARY_PATH (the shared read-only vendor mount) has no
# default either, for the same reason as in fpga/dfx/Makefile.
#
# The ones that DO keep defaults are derived:
#   *_ARCH_TECH/_GEN/BOOTROM  derived from SOCLABS_NANOSOC_SOC_DIR, so naming the
#                        SoC checkout names them all.
# --------------------------------------------------------------------------
SOCLABS_NANOSOC_SOC_DIR       ?=
SOCLABS_NANOSOC_ARCH_TECH_DIR ?= $(SOCLABS_NANOSOC_SOC_DIR)/nanosoc_arch_tech
SOCLABS_NANOSOC_GEN_DIR       ?= $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/nanosoc_gen
ARM_IP_LIBRARY_PATH           ?=
FPGA_BOOTROM_DIR              ?= $(SOCLABS_NANOSOC_SOC_DIR)/imp/fpga/firmware/stage0
SOCLABS_AHB_QSPI_DIR          ?=

# The variables a real (non-probe, non-lint) build cannot proceed without. The
# `needvars` recipe below fails by NAME rather than letting an empty path be
# concatenated into a filelist. Deliberately NOT a dependency of `lint`,
# `probe-softtap` or `probe-dcp`: none of those reads the SoC tree.
BUILD_NEEDVARS := SOCLABS_NANOSOC_SOC_DIR SOCLABS_AHB_QSPI_DIR ARM_IP_LIBRARY_PATH

SOC_ENV = \
  SOCLABS_NANOSOC_SOC_DIR=$(SOCLABS_NANOSOC_SOC_DIR) \
  SOCLABS_NANOSOC_ARCH_TECH_DIR=$(SOCLABS_NANOSOC_ARCH_TECH_DIR) \
  SOCLABS_NANOSOC_GEN_DIR=$(SOCLABS_NANOSOC_GEN_DIR) \
  ARM_IP_LIBRARY_PATH=$(ARM_IP_LIBRARY_PATH) \
  FPGA_BOOTROM_DIR=$(FPGA_BOOTROM_DIR) \
  SOCLABS_AHB_QSPI_DIR=$(SOCLABS_AHB_QSPI_DIR)

# --- R2 gate: the IMEM ROM payload -------------------------------------------
# INIT_00 of .../u_region_imem_0/u_mem/u_sram/mem_reg_0, measured 2026-07-29 and
# proven byte-identical between the Vivado baseline and the Synplify EDIF (with
# vsrc_override/sl_fpga_rom_word.v). ooc_synth_synplify.tcl ERRORS if it changes,
# because a wrong ROM is the one failure here that is completely silent.
# Set EXPECT_IMEM_INIT_00= (empty) to report without gating; update this string
# only in the same commit as an intentional firmware-image change.
EXPECT_IMEM_INIT_00 ?= 256'hFDFBF9F7F5F3F1EFEDEBE9E7E5E3E1DFDDDB0000D700000000000000CFCD8900

FLOW_ENV = \
  EXPECT_IMEM_INIT_00="$(EXPECT_IMEM_INIT_00)" \
  OUT_DIR=$(OUT) \
  RM_IICE_DIR=$(RM_IICE_DIR) \
  RM_NANOSOC_DIR=$(RM_NANOSOC_DIR) \
  REPO_ROOT=$(REPO_ROOT) \
  FLATTENER=$(FLATTENER) \
  SYNTH_TOP=$(SYNTH_TOP) \
  SYNPLIFY_REV=$(SYNPLIFY_REV) \
  IICE=$(IICE)

PRJ      = $(OUT)/nanosoc_iice.prj
EDF      = $(OUT)/$(SYNPLIFY_REV)/$(SYNTH_TOP).edf
DCP      = $(OUT)/rm_nanosoc_synth.dcp
LINT_DIR = $(OUT)/lint

.PHONY: help lint prj synth check-no-bscan dcp stage clean tools probe-softtap probe-dcp needvars

# --- soft-TAP probe (plan Phase 1) -------------------------------------------
# The throwaway-RM proof the plan asks for BEFORE touching nanosoc. Uses
# fpga/dfx/rms/rm_led/rm_led.sv: a single file, no dependencies, and already
# exactly the 47 contract ports, so it isolates the TOOLCHAIN question
# (does `device jtagport soft` on XCKU115 give an IICE with no BSCANE2 and no
# BUFG?) from the DUT question. Seconds, not the 246-file build.
PROBE_DIR  = $(OUT)/probe_softtap
PROBE_TOP  = rm_led
PROBE_SRC ?= $(REPO_ROOT)/fpga/dfx/rms/rm_led/rm_led.sv
PROBE_REV  = rev_1_identify
PROBE_EDF  = $(PROBE_DIR)/$(PROBE_REV)/$(PROBE_TOP).edf

help:
	@echo "fpga/rp/nanosoc_iice — Synplify + Identify IICE build for the nanosoc RM"
	@echo ""
	@echo "  make lint            pin_check (47-port boundary) + verilator, no licence"
	@echo "  make prj             generate $(PRJ)"
	@echo "  make synth           Synplify Premier + Identify   [LICENCE SEAT]"
	@echo "  make check-no-bscan  INVERTED BSCANE2 gate on the EDIF"
	@echo "  make dcp             EDIF -> $(DCP)"
	@echo "  make stage           copy the .dcp into fpga/dfx/build/prod/"
	@echo "  make tools           report which tools/licences are reachable"
	@echo ""
	@echo "  IICE=1 (default) instrumented | IICE=0 plain Synplify swap"
	@echo "  IDC=$(IDC)"
	@echo "  top=$(SYNTH_TOP) rev=$(SYNPLIFY_REV) out=$(OUT)"

# -----------------------------------------------------------------------------
# tools — report reachability. Read-only, no licence checkout.
# -----------------------------------------------------------------------------
tools:
	@fail=0; \
	for t in "$(TCLSH)" "$(PYTHON)" "$(VERILATOR)"; do \
	  if command -v $$t >/dev/null 2>&1; then echo "  OK    $$t"; \
	  else echo "  MISS  $$t"; fail=1; fi; done; \
	for t in "$(VIVADO)" "$(SYNPLIFY)"; do \
	  if [ -x "$$t" ]; then echo "  OK    $$t"; \
	  else echo "  MISS  $$t"; fail=1; fi; done; \
	echo "  SNPSLMD_LICENSE_FILE=$${SNPSLMD_LICENSE_FILE:-<unset>}"; \
	[ $$fail -eq 0 ] || { echo "ERROR: missing tools (see above)" >&2; exit 1; }

# -----------------------------------------------------------------------------
# lint — the cheap gates. No licence, no Vivado, no Synplify. Run this first and
# run it in CI.
#
#   1. fpga/dfx/pin_check.py on BOTH tops. This is THE boundary gate: 47 ports,
#      correct directions (contract is stated from the SHELL's view, so every
#      direction inverts), correct widths, `parameter int NGPIO = 16` present.
#      An EXTRA port here is what a full re-mint looks like.
#   2. verilator --lint-only on both tops against DERIVED blackbox stubs (see
#      lint/gen_lint_stubs.py). The shim is linted against a stub carrying the
#      POST-INSTRUMENTATION 51-port boundary, so a wrong or missing
#      identify_jtag_* connection is caught before a licence is spent.
# -----------------------------------------------------------------------------
lint:
	@echo "== lint 1/3: boundary contract (fpga/dfx/pin_check.py) =="
	@$(PYTHON) $(REPO_ROOT)/fpga/dfx/pin_check.py \
	    $(RM_IICE_DIR)/rp_nanosoc_iice_shim.sv \
	    $(RM_IICE_DIR)/rp_nanosoc_iice_core.sv
	@echo "== lint 2/3: derive blackbox stubs =="
	@$(PYTHON) $(RM_IICE_DIR)/lint/gen_lint_stubs.py $(REPO_ROOT) $(LINT_DIR)
	@echo "== lint 3/3: verilator --lint-only =="
	@$(VERILATOR) --lint-only -sv \
	    $(LINT_DIR)/rp_nanosoc_wrapper_stub.sv \
	    $(RM_IICE_DIR)/rp_nanosoc_iice_core.sv
	@echo "  OK  lint rp_nanosoc_iice_core.sv (vs the real wrapper's port list)"
	@$(VERILATOR) --lint-only -sv \
	    $(LINT_DIR)/rp_nanosoc_iice_core_iice_stub.sv \
	    $(RM_IICE_DIR)/rp_nanosoc_iice_shim.sv
	@echo "  OK  lint rp_nanosoc_iice_shim.sv (vs the INSTRUMENTED 51-port core)"
	@echo "LINT OK"

# -----------------------------------------------------------------------------
# prj — generate the Synplify project from the authoritative filelist.
# tclsh only: no licence, no tools, seconds.
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# needvars — fail BY NAME, in seconds, on a host that has not named its source
# trees. Mirrors fpga/dfx/Makefile:391. There is no default to fall back on and
# no "skip if unset" path: a filelist built from an empty variable resolves to
# paths like `/nanosoc_arch_tech/...` and dies deep inside Synplify.
# -----------------------------------------------------------------------------
needvars:
	@$(foreach v,$(BUILD_NEEDVARS),test -n "$($(v))" || { \
	   echo "error: fpga/rp/nanosoc_iice needs $(v)=<path>. It has NO default --" >&2; \
	   echo "       a default naming one workstation home directory is how a build" >&2; \
	   echo "       silently reads the wrong source tree. Set it in tools.env (see" >&2; \
	   echo "       tools.env.example) or on the command line." >&2; exit 1; };)
	@$(foreach v,$(BUILD_NEEDVARS),test -d "$($(v))" || { \
	   echo "error: $(v)=$($(v)) is not a directory." >&2; exit 1; };)

prj: needvars
	@mkdir -p $(OUT)
	@$(SOC_ENV) $(FLOW_ENV) $(TCLSH) $(RM_IICE_DIR)/gen_prj.tcl \
	   2>&1 | tee $(OUT)/gen_prj.log
	@grep -q GEN_PRJ_COMPLETE $(OUT)/gen_prj.log || \
	   { echo "ERROR: gen_prj.tcl did not complete (see $(OUT)/gen_prj.log)" >&2; exit 1; }
	@test -s $(PRJ) || { echo "ERROR: $(PRJ) missing or empty" >&2; exit 1; }
	@echo "PRJ OK: $(PRJ) ($$(grep -c '^add_file -verilog' $(PRJ)) sources)"

# -----------------------------------------------------------------------------
# synth — Synplify Premier, Identify implementation. COSTS A LICENCE SEAT
# (synplifypremierdp + the `ident` option; both proven held by
# HAPS-work/HAPS-SX/examples/hello_ident/build/synth_identify.log, 2026-07-24).
#
# NORLWRAP=1: both *_shell binaries are rlwrap-wrapped and set
# SYN_BATCH_ENABLED=1 automatically, so no -batch dance is needed and the log
# stays free of terminal escapes.
#
# The IICE arrives via <rev>/identify.idc, seeded here. That IS the interface.
# -----------------------------------------------------------------------------
synth: prj
	@test -x $(SYNPLIFY) || { echo "ERROR: no synplify_premier at $(SYNPLIFY)" >&2; exit 1; }
	@mkdir -p $(OUT)/$(SYNPLIFY_REV)
ifeq ($(IICE),1)
	@test -f $(IDC) || { echo "ERROR: IDC=$(IDC) does not exist. Point IDC at Stream A's generated .idc, or use the default fallback_iice.idc." >&2; exit 1; }
	@grep -q '^device jtagport soft' $(IDC) || \
	   { echo "ERROR: $(IDC) does not set 'device jtagport soft'. 'builtin' emits a BSCANE2 => HDPR-16 Illegal logic inside reconfigurable cell, and the RP pblock has no BSCAN site. Refusing to synthesize." >&2; exit 1; }
	@grep -q '^device xilinxinsertbufg 0' $(IDC) || \
	   { echo "ERROR: $(IDC) does not set 'device xilinxinsertbufg 0'. The RP pblock has ZERO BUFG sites. Refusing to synthesize." >&2; exit 1; }
	@grep -q '^device skewfree 1' $(IDC) || \
	   { echo "ERROR: $(IDC) does not set 'device skewfree 1'. Refusing to synthesize." >&2; exit 1; }
	@cp -f $(IDC) $(OUT)/$(SYNPLIFY_REV)/identify.idc
	@echo "== seeded $(OUT)/$(SYNPLIFY_REV)/identify.idc from $(IDC) =="
endif
	cd $(OUT) && NORLWRAP=1 $(SYNPLIFY) -batch nanosoc_iice.prj -log synth.log
	@test -f $(EDF) || { echo "ERROR: no EDIF at $(EDF). Check $(OUT)/synth.log. (If an EDIF exists under another name, that is the [Project 1-68] naming trap.)" >&2; exit 1; }
	@echo "SYNTH OK: $(EDF)"
ifeq ($(IICE),1)
	@$(MAKE) --no-print-directory check-no-bscan
endif

# -----------------------------------------------------------------------------
# check-no-bscan — the INVERTED BSCANE2 gate.
#
# The reference flow gates the OPPOSITE way: hello_ident/run.sh:39 does
#     grep -q 'cellRef BSCANE2' ...edf || exit 1
# because on a HAPS board the built-in TAP is what you want. HERE A BSCANE2 IS A
# FAILURE: it is HDPR-16 "Illegal logic inside reconfigurable cell" (reproduced
# on xcku040 and xczu9eg under Vivado 2024.1), and the RP pblock has zero BSCAN
# sites (dfx_floorplan.xdc:73-95 ranges are SLICE/DSP48E2/RAMB18/RAMB36 only).
# Widening the pblock to reach the CONFIG_SITE column is itself an error
# (HDPR-58).
#
# Two halves, and BOTH must hold — "no BSCANE2" alone is also what an
# un-instrumented build looks like:
#   (a) NO 'cellRef BSCANE2' in the EDIF;
#   (b) IICE logic IS present in the EDIF.
# -----------------------------------------------------------------------------
check-no-bscan:
	@test -f $(EDF) || { echo "ERROR: no EDIF at $(EDF) — run 'make synth' first" >&2; exit 1; }
	@if grep -q 'cellRef BSCANE2' $(EDF); then \
	   echo "ERROR: BSCANE2 present in $(EDF)."; \
	   echo "       => 'device jtagport builtin' leaked into the .idc."; \
	   echo "       => HDPR-16 'Illegal logic inside reconfigurable cell' and there is"; \
	   echo "          no BSCAN site inside the RP pblock. Set 'device jtagport soft'."; \
	   exit 1; \
	 fi
	@echo "  OK  no 'cellRef BSCANE2' in the EDIF (inverted hello_ident/run.sh:39 gate)"
	@# --- no global clock buffers, checked HERE and not 4 minutes later --------
	@# The RP pblock has ZERO BUFG sites. This was originally only caught by the
	@# netlist gate in ooc_synth_synplify.tcl, i.e. after a full `make dcp`; the
	@# EDIF text says it directly, so say it here. MEASURED: the 2026-07-29 run
	@# had exactly 2 'cellRef BUFG' (Vivado then retargets them to BUFGCE --
	@# 'BUFG => BUFGCE: 2 instances' in dcp.log), from a divided QSPI clock and a
	@# reset synchroniser. Both DESIGN nets, not IICE nets.
	@n=$$(grep -c 'cellRef BUFG' $(EDF) || true); \
	 if [ "$$n" -ne 0 ]; then \
	   echo "ERROR: $$n 'cellRef BUFG' in $(EDF). The RP pblock has ZERO BUFG sites,"; \
	   echo "       so this netlist cannot be placed. Offending instances:"; \
	   grep -o '(instance [A-Za-z0-9_]*_cb\|(instance [A-Za-z0-9_]*_buf' $(EDF) | sort -u | sed 's/^/         /'; \
	   echo "       TWO POSSIBLE CAUSES, different fixes:"; \
	   echo "        (a) IICE-inserted on the soft TAP clock -> the .idc needs"; \
	   echo "            'device xilinxinsertbufg 0' AND 'device skewfree 1'."; \
	   echo "            'make synth' already refuses without both, so this is unlikely."; \
	   echo "        (b) Synplify inferred a global buffer on a DESIGN net (high-fanout"; \
	   echo "            clock OR reset). Nothing in the .idc affects this."; \
	   echo "            -> nanosoc_iice.fdc must carry"; \
	   echo "               define_global_attribute syn_noclockbuf {1}"; \
	   echo "            and the FDC must actually be in the project (grep add_file"; \
	   echo "            -fpga_constraint in build/nanosoc_iice.prj)."; \
	   exit 1; \
	 fi
	@echo "  OK  no 'cellRef BUFG' in the EDIF (RP pblock has zero BUFG sites)"
ifeq ($(IICE),1)
	@n=$$(grep -ci iice $(EDF) || true); \
	 if [ "$$n" -lt 1 ]; then \
	   echo "ERROR: zero 'iice' references in $(EDF) — instrumentation SILENTLY did not"; \
	   echo "       happen. A clean-but-undebuggable netlist is the failure mode this"; \
	   echo "       gate exists to catch. Check that <rev>/identify.idc was seeded and"; \
	   echo "       that the project carries 'impl -add $(SYNPLIFY_REV) -type identify'"; \
	   echo "       and 'set_option -identify_debug_mode 1'."; \
	   exit 1; \
	 fi; \
	 echo "  OK  $$n 'iice' references in the EDIF (instrumentation present)"
	@if grep -qi 'identify_jtag_tck' $(EDF); then \
	   echo "  OK  identify_jtag_tck present in the EDIF (soft TAP inserted)"; \
	 else \
	   echo "ERROR: no identify_jtag_tck in $(EDF) — 'device jtagport soft' did not take"; \
	   echo "       effect, so there is no way to reach the IICE. Refusing to pass."; \
	   exit 1; \
	 fi
endif
	@echo "CHECK-NO-BSCAN OK"

# -----------------------------------------------------------------------------
# dcp — EDIF -> out-of-context checkpoint at the path build_dfx.tcl expects.
# Vivado 2024.1 by absolute path (see the tool-pinning note at the top).
# -----------------------------------------------------------------------------
dcp:
	@test -x $(VIVADO) || { echo "ERROR: no Vivado at $(VIVADO)" >&2; exit 1; }
	@test -f $(EDF) || { echo "ERROR: no EDIF at $(EDF) — run 'make synth' first" >&2; exit 1; }
	@mkdir -p $(OUT)
	cd $(OUT) && $(FLOW_ENV) $(VIVADO) -mode batch -notrace \
	   -source $(RM_IICE_DIR)/ooc_synth_synplify.tcl \
	   -log $(OUT)/dcp.log -journal $(OUT)/dcp.jou
	@grep -q RM_NANOSOC_IICE_SYNTH_COMPLETE $(OUT)/dcp.log || \
	   { echo "ERROR: ooc_synth_synplify.tcl did not complete (see $(OUT)/dcp.log)" >&2; exit 1; }
	@test -s $(DCP) || { echo "ERROR: $(DCP) missing or empty" >&2; exit 1; }
	@echo "DCP OK: $(DCP)"

# -----------------------------------------------------------------------------
# stage — hand the checkpoint to the DFX flow.
#
# build_dfx.tcl's ensure_rm_synth_dcp looks for <out_dir>/<rm_key>_synth.dcp and
# nothing else, so staging as rm_nanosoc_synth.dcp makes the IICE build BE
# rm_nanosoc for that run: same rm_id, same 47-port boundary, same static, no
# re-mint. That is the intent — but it also means it OVERWRITES whatever
# `make -C fpga/dfx rm-nanosoc-dcp` last staged, so this is deliberately a
# separate, explicit target and never a dependency of anything.
# -----------------------------------------------------------------------------
stage: DFX_PROD = $(REPO_ROOT)/fpga/dfx/build/prod
stage:
	@echo "WARNING: the shim drives rm_id 0x00010008 (its own registry id) since 2026-09-14;" >&2
	@echo "         staged AS rm_nanosoc (manifest 0x01000001) every swap fails RM-load verify." >&2
	@echo "         Build via fpga/dfx (RM_SYNTH_REUSE_rm_nanosoc_iice) instead." >&2
	@test -s $(DCP) || { echo "ERROR: no $(DCP) — run 'make dcp' first" >&2; exit 1; }
	@mkdir -p $(DFX_PROD)
	@if [ -f $(DFX_PROD)/rm_nanosoc_synth.dcp ]; then \
	   echo "NOTE: OVERWRITING the existing $(DFX_PROD)/rm_nanosoc_synth.dcp"; \
	   cp -f $(DFX_PROD)/rm_nanosoc_synth.dcp $(DFX_PROD)/rm_nanosoc_synth.dcp.prev; \
	   echo "      previous copy kept as rm_nanosoc_synth.dcp.prev"; \
	 fi
	cp -f $(DCP) $(DFX_PROD)/rm_nanosoc_synth.dcp
	@echo "STAGED: $(DFX_PROD)/rm_nanosoc_synth.dcp"
	@echo "        next: make -C fpga/dfx add-rm-nanosoc  (or prod), then overlays + verify"

# -----------------------------------------------------------------------------
# probe-softtap — plan Phase 1 on a throwaway RM. ONE small licence checkout.
#
# Answers, on the REAL part, in seconds:
#   * are `-technology Kintex-UltraScale-FPGAs / -part XCKU115 /
#     -package FLVB1760 / -speed_grade -1-c` accepted?
#   * does `impl -add <rev> -type identify` + a seeded <rev>/identify.idc
#     actually instrument in plain batch, with no instrumentor shell?
#   * does `device jtagport soft` produce the four identify_jtag_* ports and
#     NO BSCANE2 (the thing HDPR-16 would kill) and NO BUFG (no site in the
#     pblock)?
#   * does `-disable_io_insertion 1` keep it pad-less?
#   * is the EDIF really written as <top>.edf?
#
# Everything here is generated into $(PROBE_DIR) — nothing is committed, and it
# never touches the nanosoc project.
# -----------------------------------------------------------------------------
probe-softtap:
	@test -x $(SYNPLIFY) || { echo "ERROR: no synplify_premier at $(SYNPLIFY)" >&2; exit 1; }
	@test -f $(PROBE_SRC) || { echo "ERROR: no $(PROBE_SRC)" >&2; exit 1; }
	@mkdir -p $(PROBE_DIR)/$(PROBE_REV)
	@printf '%s\n' \
	  '#-- GENERATED by fpga/rp/nanosoc_iice/Makefile (probe-softtap). Throwaway.' \
	  'add_file -verilog -vlog_std sysv "$(PROBE_SRC)"' \
	  'impl -add $(PROBE_REV) -type identify' \
	  'set_option -technology Kintex-UltraScale-FPGAs' \
	  'set_option -part XCKU115' \
	  'set_option -package FLVB1760' \
	  'set_option -speed_grade -1-c' \
	  'set_option -part_companion ""' \
	  'set_option -top_module "$(PROBE_TOP)"' \
	  'set_option -disable_io_insertion 1' \
	  'set_option -identify_debug_mode 1' \
	  'project -result_file "./$(PROBE_REV)/$(PROBE_TOP).edf"' \
	  'impl -active "$(PROBE_REV)"' \
	  > $(PROBE_DIR)/probe.prj
	@printf '%s\n' \
	  'device jtagport soft' \
	  'device xilinxinsertbufg 0' \
	  'device skewfree 1' \
	  'device stop_on_signal_not_found 1' \
	  'iice new {IICE_PROBE} -type regular' \
	  'iice clock   -iice {IICE_PROBE} -edge positive {/dut_clk}' \
	  'iice sampler -iice {IICE_PROBE} -depth 1024' \
	  'iice controller -iice {IICE_PROBE} statemachine' \
	  'iice controller -iice {IICE_PROBE} -triggerconditions 2 -triggerstates 2' \
	  'signals add -iice {IICE_PROBE} -sample -trigger {/blink_counter}' \
	  'signals add -iice {IICE_PROBE} -sample -trigger {/dut_resetn}' \
	  > $(PROBE_DIR)/$(PROBE_REV)/identify.idc
	cd $(PROBE_DIR) && NORLWRAP=1 $(SYNPLIFY) -batch probe.prj -log probe.log
	@test -f $(PROBE_EDF) || { echo "ERROR: no EDIF at $(PROBE_EDF) — see $(PROBE_DIR)/probe.log" >&2; exit 1; }
	@echo "--- probe gates ---"
	@if grep -q 'cellRef BSCANE2' $(PROBE_EDF); then \
	   echo "  FAIL  BSCANE2 present — 'device jtagport soft' did NOT avoid the BSCAN"; exit 1; \
	 else echo "  PASS  no cellRef BSCANE2"; fi
	@if grep -q 'cellRef BUFG' $(PROBE_EDF); then \
	   echo "  FAIL  BUFG present — the RP pblock has no BUFG site"; exit 1; \
	 else echo "  PASS  no cellRef BUFG"; fi
	@grep -qi identify_jtag_tck $(PROBE_EDF) \
	   && echo "  PASS  identify_jtag_tck in the EDIF (soft TAP inserted)" \
	   || { echo "  FAIL  no identify_jtag_tck — no way to reach the IICE"; exit 1; }
	@echo "  INFO  $$(grep -ci iice $(PROBE_EDF)) 'iice' references in the EDIF"
	@echo "PROBE-SOFTTAP OK"

# -----------------------------------------------------------------------------
# probe-dcp — the OTHER half of the Phase-1 gate, and the one that de-risks THIS
# flow's own design: can Vivado take the 51-port instrumented EDIF as the CHILD
# of a 47-port RTL parent and emit an OOC checkpoint with a 47-port boundary?
#
# That is exactly the mechanism `dcp` uses for the real build. No Synplify
# licence, no shared seat — just Vivado 2024.1 batch. Run it after
# `probe-softtap`.
# -----------------------------------------------------------------------------
probe-dcp:
	@test -x $(VIVADO) || { echo "ERROR: no Vivado at $(VIVADO)" >&2; exit 1; }
	@test -f $(PROBE_EDF) || { echo "ERROR: no $(PROBE_EDF) — run 'make probe-softtap' first" >&2; exit 1; }
	@$(PYTHON) $(RM_IICE_DIR)/probe_shim_gen.py $(REPO_ROOT) $(PROBE_SRC) \
	   $(PROBE_DIR)/$(PROBE_TOP)_iice_shim.sv
	@$(PYTHON) $(RM_IICE_DIR)/probe_shim_gen.py --blackbox $(REPO_ROOT) $(PROBE_SRC) \
	   $(PROBE_DIR)/$(PROBE_TOP)_bb.sv
	@printf '%s\n' \
	  'set part xcku115-flvb1760-1-c' \
	  'create_project -in_memory -part $$part' \
	  '# read_edif FIRST, then the (* black_box *) stub, then the RTL shim.' \
	  '# Measured dead ends: no stub => [Synth 8-439] module not found;' \
	  '# link_design -top <shim> => [Project 1-68] no files match top module.' \
	  'read_edif $(PROBE_EDF)' \
	  'read_verilog -sv $(PROBE_DIR)/$(PROBE_TOP)_bb.sv' \
	  'read_verilog -sv $(PROBE_DIR)/$(PROBE_TOP)_iice_shim.sv' \
	  'synth_design -mode out_of_context -top $(PROBE_TOP)_iice_shim -part $$part' \
	  'if { [get_property IS_BLACKBOX [get_cells u_core]] } { error "u_core still a black box - EDIF not linked" }' \
	  'set n 0; set names [dict create]' \
	  'foreach p [get_ports *] { set nm [get_property NAME $$p]; regsub {\[\d+\]$$} $$nm {} nm; dict incr names $$nm 1 }' \
	  'dict for {k v} $$names { incr n $$v }' \
	  'puts "PROBE_DCP_CELLS [llength [get_cells -quiet -hier]]"' \
	  'puts "PROBE_DCP_PORTS [dict size $$names] PROBE_DCP_BITS $$n"' \
	  'if { [llength [get_cells -quiet -hier -filter {REF_NAME =~ "BSCANE2"}]] } { error "BSCANE2 in netlist" }' \
	  'if { [llength [get_cells -quiet -hier -filter {REF_NAME =~ "BUFG*"}]] } { error "BUFG in netlist" }' \
	  'report_utilization -file $(PROBE_DIR)/probe_util.rpt' \
	  'write_checkpoint -force $(PROBE_DIR)/probe_rm.dcp' \
	  'puts "PROBE_DCP_COMPLETE"' \
	  > $(PROBE_DIR)/probe_dcp.tcl
	cd $(PROBE_DIR) && $(VIVADO) -mode batch -notrace -source probe_dcp.tcl \
	   -log probe_dcp.log -journal probe_dcp.jou
	@grep -q PROBE_DCP_COMPLETE $(PROBE_DIR)/probe_dcp.log || \
	   { echo "ERROR: probe_dcp.tcl did not complete (see $(PROBE_DIR)/probe_dcp.log)" >&2; exit 1; }
	@grep -o 'PROBE_DCP_PORTS [0-9]* PROBE_DCP_BITS [0-9]*' $(PROBE_DIR)/probe_dcp.log | tail -1
	@grep -q 'PROBE_DCP_PORTS 47 PROBE_DCP_BITS 148' $(PROBE_DIR)/probe_dcp.log || \
	   { echo "ERROR: the checkpoint boundary is NOT 47 ports / 148 bits. The EDIF+RTL" >&2; \
	     echo "       wrapping mechanism did not reduce the boundary, so this flow cannot" >&2; \
	     echo "       ship without a partition-pin re-mint. See $(PROBE_DIR)/probe_dcp.log" >&2; exit 1; }
	@echo "PROBE-DCP OK: 47 ports / 148 bits from a 51-port instrumented EDIF"

clean:
	rm -rf $(OUT)
