# -----------------------------------------------------------------------------
# fpga/dfx/Makefile — one-command drivers for the KU115 DFX flow (A2).
#
# Targets (see fpga/dfx/README.md "Build + overlay quickstart"):
#   make proof           - re-run the greybox<->led proof (fpga/dfx/proof/) into
#                          build/proof/. Produces static_synth.dcp, the stand-in
#                          static the production flow links against until A1's
#                          real shell checkpoint lands.
#   make rm-nanosoc-dcp  - OOC-synth the real nanosoc RM (fpga/rp/nanosoc/
#                          ooc_synth.tcl, sourced not edited) into
#                          build/rm_nanosoc_synth/, then stage the .dcp where
#                          build_dfx.tcl's readiness filter picks it up.
#   make rm-eth-ss-dcp   - stage the eth_ss RM OOC checkpoint: reuse the
#                          pre-produced fpga/rp/eth_ss/build/rm_eth_ss_synth.dcp
#                          if present, else re-synth via fpga/rp/eth_ss/
#                          ooc_synth.tcl (sourced not edited).
#   make prod            - run the PRODUCTION build_dfx.tcl end to end
#                          (N-config loop, readiness filter, pr_verify,
#                          partial+clearing pairs, static_id) into build/prod/.
#                          Needs a shell DCP (STATIC_DCP default = the current
#                          shell, ./build/shell_proj/shell_static_synth.dcp), and
#                          `make rm-nanosoc-dcp` / `rm-eth-ss-dcp` first for
#                          those RMs to be in the default build set -- which is
#                          why a `prod` run without them re-mints only 5 of 7.
#   make add-rm-eth-ss   - INCREMENTAL: add rm_eth_ss to an existing build/prod/
#                          run (reuses its locked static + static_id, only
#                          routes eth_ss + pr_verifies vs greybox). Use this to
#                          fold a newly-ready RM into a shell the fleet is
#                          already keyed to, WITHOUT re-minting static_id.
#                          Follow with `make overlays && make verify`.
#   make overlays        - drive gen_manifest.py over build/prod/'s emitted
#                          pairs -> verified overlay/<rm_name>/ triples
#                          (payloads gitignored; manifests small + committable)
#                          + overlay/mps3_shell_static_id.c, the generated
#                          strong override for firmware's weak
#                          mps3_shell_static_id() seam (A3 links it into the
#                          shell image; see README "static_id scheme").
#   make verify          - re-run `gen_manifest.py verify` on every existing
#                          overlay/*/manifest.json (CI-shaped round-trip check).
#   make clean           - delete build/ (never touches overlay/ or proof
#                          evidence dirs).
#
# All Vivado work dirs live under fpga/dfx/build/ (gitignored via the repo's
# global `build/` rule); each run cd's into its own out dir so .Xil/ droppings
# land there too.
# -----------------------------------------------------------------------------

DFX_DIR   := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO_ROOT := $(abspath $(DFX_DIR)/../..)

# bash, not sh: several recipes source a DUT's set_env.sh and use `[[`-free but
# bash-flavoured constructs. The multicore recipe used to wrap itself in
# `bash -c '...'` for exactly this reason; one declaration replaces that.
SHELL := /bin/bash

# A NON-$(MAKE) spelling for sub-makes inside the mint chain. GNU make treats a
# recipe line containing the literal $(MAKE) as "run this even under -n", which
# would make `make -n mint` recurse into firmware/platform and print that build's
# plan instead of this one's. The mint is a serial Vivado flow, so nothing is
# lost by not sharing the jobserver, and `make -n` becomes a plan you can read.
MK := $(MAKE)

# --- site-local overrides, NOT tracked ---------------------------------------
# tools.env (repo root) is included FIRST so its values win over the defaults
# below, while an explicit `make VAR=...` still wins over both. It is where a
# host names its Vivado, its SoCScope checkout, and its MINT_HUB destination.
# See tools.env.example; there is no default host anywhere in this file.
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)

# --- the CPU seam, and the ONE Vivado that runs every stage (FLOW, 2026-09-23) --
# SHELL_CPU selects the static's CPU subsystem (fpga/shell/bd/cpu_$(SHELL_CPU).tcl,
# docs/planning/linux_lanes/SHELL_CONTRACT.md):
#   unset / mb -- the classic MicroBlaze v11, bare-metal firmware: today's shell,
#                 Vivado 2024.1. Unset and `mb` are the SAME build, character for
#                 character: nothing below changes a single printed command
#                 unless SHELL_CPU=mbv.
#   mbv        -- MicroBlaze V + DDR4, Linux (LINUX_HARNESS_PLAN §3). Needs Vivado
#                 >= 2026.1 (the MBV S-mode/SSTC IP revisions); a 2026.1 checkpoint
#                 cannot be opened by 2024.1 afterwards. Stage 6 becomes the stage0
#                 bake (mint-stage0) and a Linux mint ends in mint-linux-image.
# VIVADO is a PATH, or a bare VERSION that names one of the installs below
# (VIVADO=2026.1). With VIVADO unset, SHELL_CPU picks it. VIVADO_VER follows the
# path when the path says its version, and must agree with it when both are given
# -- a 2026.1 binary labelled 2024.1 stamps "2024.1" into every manifest and into
# static_canon, which is how two different builds come to look like one.
SHELL_CPU ?=
SHELL_CPU_EFF := $(if $(strip $(SHELL_CPU)),$(strip $(SHELL_CPU)),mb)
MBV_VIVADO_MIN := 2026.1
VIVADO_BIN_2024.1 ?= /apps/Xilinx/Vivado/2024.1/bin/vivado
VIVADO_BIN_2026.1 ?= /research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado
ifeq ($(origin VIVADO),undefined)
VIVADO := $(if $(filter mbv,$(SHELL_CPU_EFF)),$(VIVADO_BIN_$(MBV_VIVADO_MIN)),/apps/Xilinx/Vivado/2024.1/bin/vivado)
endif
ifeq ($(findstring /,$(VIVADO)),)
ifneq ($(origin VIVADO_BIN_$(strip $(VIVADO))),undefined)
VIVADO_REQ_VER := $(strip $(VIVADO))
override VIVADO := $(VIVADO_BIN_$(VIVADO_REQ_VER))
endif
endif
# the version the install path names (/.../2026.1/...), or empty
VIVADO_PATH_VER := $(firstword $(foreach w,$(filter 20%,$(subst /, ,$(VIVADO))),$(if $(findstring .,$(w)),$(w))))
VIVADO_VER  ?= $(or $(VIVADO_REQ_VER),$(VIVADO_PATH_VER),2024.1)
PYTHON      ?= python3
ifneq ($(VIVADO_PATH_VER),)
ifneq ($(VIVADO_PATH_VER),$(strip $(VIVADO_VER)))
$(error VIVADO=$(VIVADO) is a $(VIVADO_PATH_VER) install but VIVADO_VER=$(VIVADO_VER). \
Every manifest, static_canon and mint.json would record the wrong tool. Drop \
VIVADO_VER (it follows the path) or make the two agree)
endif
endif
# SHELL_CPU=mbv under Vivado < 2026.1, or any value but mb/mbv, never plans.
# Only asked when SHELL_CPU is set, so the default path runs no extra process.
ifneq ($(strip $(SHELL_CPU)),)
FLOW_CPU_ERR := $(shell $(PYTHON) $(DFX_DIR)/tools/vivado_guard.py cpu \
  --shell-cpu '$(strip $(SHELL_CPU))' --vivado-ver '$(strip $(VIVADO_VER))' --vivado '$(VIVADO)' 2>&1)
ifneq ($(strip $(FLOW_CPU_ERR)),)
$(error $(FLOW_CPU_ERR))
endif
endif
# What the recipes put in front of a tool for the mbv build, and NOTHING (not
# even a space) otherwise: they sit directly before $(VIVADO), so the default
# path prints byte-for-byte what it printed before the seam existed.
SHELL_CPU_ENV = $(if $(filter mbv,$(SHELL_CPU_EFF)),SHELL_CPU=mbv )
DFX_CPU_ENV   = $(if $(filter mbv,$(SHELL_CPU_EFF)),DFX_SHELL_CPU=mbv )

# An mbv build gets its own default BUILD (one build dir, one Vivado) and its own
# default OVERLAY_ROOT, INSIDE that build dir: fpga/dfx/overlay/ is the tracked,
# fielded bare-metal set, and stage 5 re-keys whatever OVERLAY_ROOT names. A
# Linux mint re-keying it would strand every bare-metal overlay on the board.
BUILD        ?= $(if $(filter mbv,$(SHELL_CPU_EFF)),$(DFX_DIR)/build_mbv,$(DFX_DIR)/build)
OVERLAY_ROOT ?= $(if $(filter mbv,$(SHELL_CPU_EFF)),$(BUILD)/overlay_mbv,$(DFX_DIR)/overlay)
PROD          = $(BUILD)/prod
ifeq ($(SHELL_CPU_EFF),mbv)
ifeq ($(abspath $(OVERLAY_ROOT)),$(DFX_DIR)/overlay)
$(error SHELL_CPU=mbv with OVERLAY_ROOT=$(OVERLAY_ROOT): that is the tracked bare-metal \
overlay set, and stage 5 would re-key it to a Linux static. Leave OVERLAY_ROOT unset \
(an mbv build keeps its overlays in $$(BUILD)/overlay_mbv) or name the Linux set)
endif
endif

# DRYRUN=1 -- print what every stage WOULD run and launch nothing. This is a
# real guard, not a promise: the mint script this Makefile replaced documented
# DRYRUN=1 as "prints the plan, launches nothing" while its re-key stage ran for
# real, silently re-keying every tracked overlay off the shell on the board.
DRYRUN ?= 0

# --- production build_dfx.tcl inputs -----------------------------------------
# STATIC_DCP: the static shell checkpoint config-1 links against. Default =
# A1's REAL shell post-synth checkpoint (fpga/shell/build_shell.tcl output).
# CURRENT = the 256 KiB-LMB shell (build/shell_proj_256k, fixes the lwIP
# memory gate; static_id 0xECCEDBF3). The 128 KiB build/shell_proj_a1
# (0x628E2D0D) and the proof stand-in remain usable via an explicit
# STATIC_DCP=... override. RP_INST=u_rp_dut is the RP cell path in every
# variant (shell_top/rp_shell_top is the netlist top, so no u_top/ prefix
# appears in Vivado cell paths).
# Default = the canonical output of build_shell.tcl's own default proj_dir
# (./build/shell_proj). This USED to point at build/shell_proj_256k/, which is a
# stale 256 KiB-LMB shell from 2026-07-08 that still exists on disk -- so `make
# prod` silently re-minted every overlay against a two-generations-obsolete
# bitstream instead of failing. That is invisible to the R9 gate
# (check_overlay_static_id.py): `prod` regenerates the manifests AND
# overlay/mps3_shell_static_id.c from the same run, so they agree with each other
# and disagree only with the bitstream actually on the board. Point this at the
# DCP of the shell you intend to run, or let it fail loudly.
# SHELL_PROJ is the shell project dir (build_shell.tcl's output, and the `xsa
# dir` the mint takes); SHELL_DCP is the checkpoint inside it. STATIC_DCP keeps
# its exact former default -- build/shell_proj/shell_static_synth.dcp -- and is
# now spelled through SHELL_PROJ so naming the project names the checkpoint.
SHELL_PROJ ?= $(REPO_ROOT)/build/shell_proj
SHELL_DCP   = $(SHELL_PROJ)/shell_static_synth.dcp
STATIC_DCP ?= $(SHELL_DCP)
RP_INST    ?= u_rp_dut
RP_PBLOCK  ?= pblock_rp_dut
RM_SUBSET  ?=

# --- DFX_JOBS: stage 4's RM configs as N concurrent Vivados (MINT-SPEED) -----
# Once the reference config (rm_greybox) is routed and the static is LOCKED,
# every other RM config -- open the locked static, link the RM, place, route,
# pr_verify against the reference, write its partial pair + .ltx -- depends on
# nothing but the locked static. DFX_JOBS=N (N >= 2) runs up to N of them at
# once (tools/dfx_jobs.py; phases in tools/dfx_jobs.tcl; FLOW_CONTRACT.md §8):
#   make -C fpga/dfx mint ... DFX_JOBS=4        (`prod` takes it too)
# Unset or 1: stage 4 is the one-process flow, and `make -n` prints it byte for
# byte as before -- DFX_STAGE4_VIVADO below is then exactly $(VIVADO). With N >=
# 2 it is dfx_jobs.py, handed the SAME Vivado arguments: ref phase -> N workers ->
# finish, one assembled build.log, and the recipe's gates unchanged. A failed RM
# config does not stop the others; the stage then fails naming it, leaves NO
# static_id.txt, and re-running the same command resumes (only the missing
# configs run, against the same locked static). DFX_JOB_MEM_GB: a worker starts
# only while MemAvailable is at least this (an mbv config peaked at 7.6 GB).
DFX_JOBS       ?=
DFX_JOB_MEM_GB ?= 16
ifneq ($(strip $(DFX_JOBS)),)
ifeq ($(shell echo '$(strip $(DFX_JOBS))' | grep -xE '[1-9][0-9]*'),)
$(error DFX_JOBS=$(DFX_JOBS): want a positive integer (unset or 1 = the one-process stage 4))
endif
endif
DFX_STAGE4_VIVADO = $(if $(filter-out 1,$(strip $(DFX_JOBS))),$(PYTHON) $(DFX_DIR)/tools/dfx_jobs.py run --jobs $(strip $(DFX_JOBS)) --mem-per-job-gb $(strip $(DFX_JOB_MEM_GB)) --vivado $(VIVADO) --,$(VIVADO))

# --- nanosoc RM environment (consumed by fpga/rp/nanosoc/ooc_synth.tcl and
# the proven nanosoc_m0_soc/pynq/filelist.tcl it sources). Site paths have no
# default: set them in tools.env (see tools.env.example). ----------------------
# NO DEFAULT, deliberately (2026-09-10). This used to name one workstation's
# home directory. A default that points at a source tree only one machine has is
# worse than no default: on that machine it is invisible, and on every other one
# it silently reads the wrong tree (or an empty one) and the failure surfaces
# hours later inside Vivado. Set it in tools.env (see tools.env.example) or on
# the command line; the RM staging rule refuses to synthesise without it, by
# name, in seconds.
SOCLABS_NANOSOC_SOC_DIR       ?=
SOCLABS_NANOSOC_ARCH_TECH_DIR ?= $(SOCLABS_NANOSOC_SOC_DIR)/nanosoc_arch_tech
SOCLABS_NANOSOC_GEN_DIR       ?= $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/nanosoc_gen
# The shared, read-only Arm IP library. No default, for the same reason: it is a
# site mount. Every RM synth that reads it names it in RM_SYNTH_NEEDVAR_*.
ARM_IP_LIBRARY_PATH           ?=
FPGA_BOOTROM_DIR              ?= $(SOCLABS_NANOSOC_SOC_DIR)/imp/fpga/firmware/stage0
# ahb_qspi IP repo — the SoC now instantiates the QSPI flash controller
# unconditionally (product config), so pynq/filelist.tcl reads its RTL. Every
# nanosoc RM synth (stock, scaffold, monolithic) needs this on the env.
# No default, for the same reason as SOCLABS_NANOSOC_SOC_DIR above.
SOCLABS_AHB_QSPI_DIR          ?=
# The SHARED CoreSight SoC-400 tech block. nanosoc_m0_soc's HEAD ("consume shared
# SoC-400 tech block for the DAP") reads it through $SOCLABS_CORESIGHT_SOC400_TECH_DIR
# and DEFAULTS to $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/rtl/coresight_soc400_tech -- a path
# that only exists once arch_tech's nested submodule is initialised.
#
# It is a genuine submodule (gitlink 9bdae19 -> SoC-Labs/SoC-Labs-SoC400-Tech), NOT a
# directory to be substituted: two other trees in this workspace contain a
# coresight_soc400/ folder and NEITHER carries that commit -- they are their own
# projects' copies. Building the DAP against one of those would produce a bitstream
# that works until it does not.
#
# Left unset, an RM build that needs the DAP dies with "File ... does not exist" and
# Vivado STILL EXITS 0, because a Tcl error is not tagged ERROR: -- which is why the
# recipes below grep for their COMPLETE markers rather than trusting a return code.
# DERIVED default (2026-09-16). Leaving this undefined did NOT fall through to the
# SoC filelist's own default: the RM_SYNTH_ENV_* lists below forward it
# unconditionally, so make passed SOCLABS_CORESIGHT_SOC400_TECH_DIR= (present, EMPTY),
# the filelist's `info exists env(...)` was true, and the DAP was sourced from
# "/rtl/nanosoc_dap_ahb_xlate.v". The default below is the SAME path the filelist
# picks for itself -- the canonical, git-tracked copy pinned at arch_tech's nested
# gitlink -- so nothing is shadowed by a second clone. Override it in tools.env or
# on the command line only to build against a tech block elsewhere.
SOCLABS_CORESIGHT_SOC400_TECH_DIR ?= $(SOCLABS_NANOSOC_ARCH_TECH_DIR)/rtl/coresight_soc400_tech

# --- multicore-ethernet RM environment (consumed by fpga/rp/nanosoc_multicore/
# ooc_synth.tcl and the DUT's proven $NANOSOC_MULTICORE_HOME/pynq/filelist.tcl).
# That flist needs ~14 read-only IP env vars; the multicore DUT's set_env.sh
# exports them all, so the target sources it rather than re-listing them here.
# Set it in tools.env; it has no default. ---------------------------------------
# No default, for the same reason as SOCLABS_NANOSOC_SOC_DIR above.
NANOSOC_MULTICORE_HOME        ?=

# NOTE what is NOT here: rm-*-dcp and add-rm-* are served by PATTERN rules, and
# make does not apply a pattern rule to a target declared .PHONY. Listing them
# would silently turn every one of them back into "Nothing to be done".
.PHONY: help proof proof-nanosoc prod overlays verify pin-check clean

help:
	@echo "THE ONE ENTRY POINT"
	@echo "  make mint BUILD=<dir> SHELL_PROJ=<dir> RM_SET=\"greybox led ...\" SHELL_TOUCH=0|1 TOUCH=0|1"
	@echo "            [SHELL_REALPHY=0|1 [REALPHY_RATE=10|100]] (default 0 = virtual PHY)"
	@echo "            [REUSE_DCPS_FROM=<prod dir>] [MINT_HUB=<rsync dest>] [DRYRUN=1]"
	@echo "    shell -> RM checkpoints -> prod -> overlays -> firmware -> mint.json -> hub copy."
	@echo "    Runbook: docs/BUILD_AND_MINT.md.  Plan only: make -n mint ...  (or DRYRUN=1)"
	@echo ""
	@echo "STAGES (each is also a target, and each is a no-op when already done)"
	@echo "  mint-preflight | mint-shell | mint-rms | mint-prod | mint-overlays |"
	@echo "  mint-firmware  | mint-record | mint-hub-copy"
	@echo ""
	@echo "PIECES"
	@echo "  rm-<name>-dcp      stage ONE RM OOC checkpoint (rm-nanosoc-dcp, rm-eth-ss-dcp, ...)"
	@echo "  add-rm-<name>      fold ONE RM into an EXISTING locked static (no re-mint)"
	@echo "  prod | overlays | verify | pin-check | proof | clean"
	@echo "  fielded-files      the fielding copy set (incl. every .ltx + sidecar + its partial pair)"
	@echo ""
	@echo "MICROBLAZE V / LINUX (SHELL_CPU=mbv; Vivado 2026.1 -- FLOW_CONTRACT.md)"
	@echo "  make mint SHELL_CPU=mbv [VIVADO=2026.1] BUILD=<dir> SHELL_TOUCH=1  stage 6 = mint-stage0"
	@echo "  mint-stage0        build stage0 FOR this static + updatemem it into the MBV LMB"
	@echo "  linux-image-env    MPS3_STATIC_ID/HARNESS_VER32/STAGE0_SHA256 for IMAGE's build"
	@echo "  mint-linux-image   IMAGE's artefacts (LINUX_ARTIFACTS=<dir>) -> prod/linux_bundle.json"
	@echo "  linux-bundle-check the image<->static<->overlays gate on prod/"
	@echo "  pmint | pmint-plan the prototype: MINT_RMS=\"$(PMINT_RMS)\", never fielded"
	@echo "  VIVADO=<path|2024.1|2026.1>; one BUILD dir = one Vivado (checked before any Vivado stage)"
	@echo ""
	@echo "IDENTITY (board-free, no Vivado)"
	@echo "  canon              SHA-256 of the SOURCES that decide the static (static_canon)."
	@echo "                     Same inputs -> same hash; static_id cannot say that."
	@echo "  canon-check        check fpga/dfx/tools/static_inputs.txt: no stale glob, no"
	@echo "                     undeclared design source, every exclusion has a reason"
	@echo "  overlay-usercode       stamp USERID (REGISTER.USERCODE) from the full static .bit"
	@echo "                         into every overlay manifest -- runs inside mint stage 5"
	@echo "  overlay-usercode-check the same comparison, writing nothing"
	@echo ""
	@echo "see fpga/dfx/README.md and docs/BUILD_AND_MINT.md for details"

# --- proof flow (fpga/dfx/proof/build_proof.tcl) -----------------------------
proof:
	mkdir -p $(BUILD)/proof
	cd $(BUILD)/proof && $(VIVADO) -mode batch \
	  -source $(DFX_DIR)/proof/build_proof.tcl \
	  -journal $(BUILD)/proof/proof.jou -log $(BUILD)/proof/proof.log \
	  -tclargs $(REPO_ROOT) $(BUILD)/proof
	@grep -q "DFX_PROOF_COMPLETE" $(BUILD)/proof/proof.log && echo "PROOF OK"

# optional: the proof-side nanosoc config-3 (against the PROOF static — the
# production flow builds its own nanosoc config, so this is only for
# regenerating fpga/dfx/proof/proof_results_* evidence)
proof-nanosoc:
	@test -f $(BUILD)/proof/static_locked.dcp || { echo "error: run 'make proof' first"; exit 1; }
	@test -f $(BUILD)/rm_nanosoc_synth/rm_nanosoc_synth.dcp || { echo "error: run 'make rm-nanosoc-dcp' first"; exit 1; }
	mkdir -p $(BUILD)/proof_nanosoc
	cd $(BUILD)/proof_nanosoc && $(VIVADO) -mode batch \
	  -source $(DFX_DIR)/proof/build_rm_nanosoc.tcl \
	  -journal $(BUILD)/proof_nanosoc/dfx.jou -log $(BUILD)/proof_nanosoc/dfx.log \
	  -tclargs $(BUILD)/proof $(BUILD)/rm_nanosoc_synth/rm_nanosoc_synth.dcp $(BUILD)/proof_nanosoc

# =============================================================================
# RM OOC SYNTH CHECKPOINTS — one generic rule, one registration per RM
# =============================================================================
# WHAT CHANGED (2026-09-10). There used to be six near-identical recipes here,
# one per externally-sourced RM, each ~20 lines of mkdir / env / vivado / grep /
# cp that differed in four values. Adding an RM meant copying one of them, and
# the copy that got made was whichever one was nearest — which is how
# `rebuild_jtag_uart.sh` ended up defaulting BUILD to a tree two shells old
# (copied from its predecessor and never edited).
#
# Now every RM that needs a pre-built checkpoint REGISTERS its four values
# below, and ONE pattern rule stages it:
#
#   $(BUILD)/prod/<rm_key>_synth.dcp   <- the file build_dfx.tcl looks for
#   make rm-<name>-dcp                 <- the alias people type (unchanged)
#
# The registration set here must match rm_list.tcl's `synth_mode prebuilt` set;
# tests/dfx_flow/test_mint_record.py fails if they drift. An `inline` RM
# (greybox, led, regdemo_a/b, uart_echo) is synthesised by build_dfx.tcl itself
# and is deliberately NOT registered here — staging one would be a lie about
# where its netlist came from.
#
# Per-RM knobs (all optional except _TCL and _MARK):
#   RM_SYNTH_TCL_<key>      the OOC synth script (also the make prerequisite)
#   RM_SYNTH_MARK_<key>     the log marker that means "it really finished"
#                           (Vivado exits 0 on a Tcl error, so the marker — not
#                           the return code — is the proof)
#   RM_SYNTH_ENV_<key>      NAME=VALUE pairs passed via `env`
#   RM_SYNTH_REUSE_<key>    a pre-produced .dcp to copy instead of synthesising
#   RM_SYNTH_REQUIRE_<key>  a directory that must exist to synthesise at all
#   RM_SYNTH_PREP_<key>     shell run before synth (must end with `;`)
#   RM_SYNTH_POST_<key>     shell run after synth  (must end with `;`)
#   RM_SYNTH_WRAP_<key>     shell prefix inside the synth subshell (e.g. a
#                           `source set_env.sh &&`)
#   RM_SYNTH_STAGE_AS_<key> also stage the .dcp under this OTHER key's name
#                           (build VARIANTS: same RM, different bake)
# -----------------------------------------------------------------------------

# --- rm_nanosoc — the real single-core Cortex-M0 SoC --------------------------
# fpga/rp/nanosoc/ooc_synth.tcl is read-only to this Makefile; all wiring is
# through its documented env contract.
RM_SYNTH_TCL_rm_nanosoc     := $(REPO_ROOT)/fpga/rp/nanosoc/ooc_synth.tcl
RM_SYNTH_NEEDVAR_rm_nanosoc := SOCLABS_NANOSOC_SOC_DIR SOCLABS_AHB_QSPI_DIR ARM_IP_LIBRARY_PATH
RM_SYNTH_MARK_rm_nanosoc := RM_NANOSOC_SYNTH_COMPLETE
RM_SYNTH_ENV_rm_nanosoc  := \
  SOCLABS_NANOSOC_SOC_DIR=$(SOCLABS_NANOSOC_SOC_DIR) \
  SOCLABS_NANOSOC_ARCH_TECH_DIR=$(SOCLABS_NANOSOC_ARCH_TECH_DIR) \
  SOCLABS_NANOSOC_GEN_DIR=$(SOCLABS_NANOSOC_GEN_DIR) \
  ARM_IP_LIBRARY_PATH=$(ARM_IP_LIBRARY_PATH) \
  FPGA_BOOTROM_DIR=$(FPGA_BOOTROM_DIR) \
  SOCLABS_AHB_QSPI_DIR=$(SOCLABS_AHB_QSPI_DIR) \
  SOCLABS_CORESIGHT_SOC400_TECH_DIR=$(SOCLABS_CORESIGHT_SOC400_TECH_DIR) \
  RM_NANOSOC_DIR=$(REPO_ROOT)/fpga/rp/nanosoc

# --- rm_nanosoc_upy — the SHIPPED flash-boot (XiP) MicroPython RM -------------
# Proven on silicon 2026-07-21 (docs/QSPI_RP_BOARD_BRINGUP.md "PRODUCT GATE
# PASSED"). SAME wrapper and SAME boundary as rm_nanosoc, so no shell rebuild
# and no static_id re-mint. It differs by exactly two synth-time overrides:
#   FPGA_BOOTROM_DIR -> a locally built QSPI-ENABLED stage-0 bootrom (the
#       on-disk fw_config bootrom has QSPI disabled), built here so the shared
#       SoC tree is never mutated;
#   UPY_IMEM_IMG     -> an ALL-ZEROS IMEM image. Load-bearing: with an empty
#       IMEM, a running MicroPython proves the code came from FLASH and not from
#       a baked BRAM preload. The POST check below asserts the override actually
#       took — a silent fallback to the default image would "pass" while proving
#       nothing.
XIP_STAGE       ?= $(BUILD)/rm_nanosoc_upy_synth
XIP_BOOTROM_OUT ?= $(XIP_STAGE)/bootrom
XIP_IMEM        ?= $(XIP_STAGE)/imem_empty_word.hex
RM_SYNTH_TCL_rm_nanosoc_upy     := $(REPO_ROOT)/fpga/rp/nanosoc_upy/ooc_synth.tcl
RM_SYNTH_NEEDVAR_rm_nanosoc_upy := SOCLABS_NANOSOC_SOC_DIR SOCLABS_AHB_QSPI_DIR ARM_IP_LIBRARY_PATH
RM_SYNTH_MARK_rm_nanosoc_upy := RM_NANOSOC_UPY_SYNTH_COMPLETE
# The flash image on the board is built against ONE stage-0 (fpga/rp/nanosoc_upy/
# flash_image.json "bootrom.stage0_bin_md5"). build_bootrom.sh reads the
# gitignored, regenerable $SOC/imp/fpga/fw_config, so a regen can change the ROM
# silently. Say so loudly in the log (warn, do not fail: the pair is re-recorded
# at the board, not here).
UPY_STAGE0_MD5 ?= 7d7ee052470e0a6d5180bf91495901d3
RM_SYNTH_PREP_rm_nanosoc_upy := \
  bash $(REPO_ROOT)/tests/micropython_flash_boot/tools/build_bootrom.sh $(XIP_BOOTROM_OUT) && \
  { got=$$(md5sum < $(XIP_BOOTROM_OUT)/stage0.bin | cut -c1-32); \
    if [ "$$got" = "$(UPY_STAGE0_MD5)" ]; then echo "INFO: nanosoc_upy stage0.bin md5 $$got == flash_image.json"; \
    else echo "WARNING: nanosoc_upy stage0.bin md5 $$got != flash_image.json $(UPY_STAGE0_MD5) -- the QSPI image and this RM are NOT a recorded pair; rebuild/record the image (fpga/rp/nanosoc_upy/README.md)"; fi; } && \
  $(PYTHON) $(REPO_ROOT)/fpga/rp/nanosoc_upy/gen_empty_imem.py -o $(XIP_IMEM);
RM_SYNTH_POST_rm_nanosoc_upy := \
  grep -q "baking MicroPython IMEM image: $(XIP_IMEM)" "$$LOG" || \
    { echo "error: synth did not bake the EMPTY IMEM ($(XIP_IMEM)) -- the UPY_IMEM_IMG override did not take, so this checkpoint does NOT prove flash boot"; exit 1; };
RM_SYNTH_ENV_rm_nanosoc_upy  := \
  SOCLABS_NANOSOC_SOC_DIR=$(SOCLABS_NANOSOC_SOC_DIR) \
  SOCLABS_NANOSOC_ARCH_TECH_DIR=$(SOCLABS_NANOSOC_ARCH_TECH_DIR) \
  SOCLABS_NANOSOC_GEN_DIR=$(SOCLABS_NANOSOC_GEN_DIR) \
  ARM_IP_LIBRARY_PATH=$(ARM_IP_LIBRARY_PATH) \
  FPGA_BOOTROM_DIR=$(XIP_BOOTROM_OUT) \
  UPY_IMEM_IMG=$(XIP_IMEM) \
  SOCLABS_AHB_QSPI_DIR=$(SOCLABS_AHB_QSPI_DIR) \
  RM_NANOSOC_UPY_DIR=$(REPO_ROOT)/fpga/rp/nanosoc_upy

# --- rm_nanosoc_upy_scaffold — the NO-FLASH bring-up variant ------------------
# MicroPython baked into IMEM BRAM (boots with NO flash). NOT the shipped
# default; it is the same RM, differently baked, so it stages under the
# rm_nanosoc_upy name (STAGE_AS) and needs no separate rm_list.tcl entry.
# The MicroPython image must exist first (`make -C firmware/micropython`);
# ooc_synth.tcl errors out with that instruction rather than silently
# synthesising a DUT with an empty IMEM.
RM_SYNTH_TCL_rm_nanosoc_upy_scaffold      := $(REPO_ROOT)/fpga/rp/nanosoc_upy/ooc_synth.tcl
RM_SYNTH_NEEDVAR_rm_nanosoc_upy_scaffold  := SOCLABS_NANOSOC_SOC_DIR SOCLABS_AHB_QSPI_DIR ARM_IP_LIBRARY_PATH
RM_SYNTH_MARK_rm_nanosoc_upy_scaffold     := RM_NANOSOC_UPY_SYNTH_COMPLETE
RM_SYNTH_STAGE_AS_rm_nanosoc_upy_scaffold := rm_nanosoc_upy
RM_SYNTH_ENV_rm_nanosoc_upy_scaffold      := \
  SOCLABS_NANOSOC_SOC_DIR=$(SOCLABS_NANOSOC_SOC_DIR) \
  SOCLABS_NANOSOC_ARCH_TECH_DIR=$(SOCLABS_NANOSOC_ARCH_TECH_DIR) \
  SOCLABS_NANOSOC_GEN_DIR=$(SOCLABS_NANOSOC_GEN_DIR) \
  ARM_IP_LIBRARY_PATH=$(ARM_IP_LIBRARY_PATH) \
  FPGA_BOOTROM_DIR=$(FPGA_BOOTROM_DIR) \
  SOCLABS_AHB_QSPI_DIR=$(SOCLABS_AHB_QSPI_DIR) \
  SOCLABS_CORESIGHT_SOC400_TECH_DIR=$(SOCLABS_CORESIGHT_SOC400_TECH_DIR) \
  RM_NANOSOC_UPY_DIR=$(REPO_ROOT)/fpga/rp/nanosoc_upy

# --- rm_eth_ss — the real AHB MAC + PTP subsystem ----------------------------
# PREFERS the pre-produced dcp (the rm_eth_ss agent's output); only
# re-synthesises when that is absent, for reproducibility.
RM_SYNTH_TCL_rm_eth_ss   := $(REPO_ROOT)/fpga/rp/eth_ss/ooc_synth.tcl
RM_SYNTH_MARK_rm_eth_ss  := RM_ETH_SS_SYNTH_COMPLETE
# ETH_SS_HOME = the ethernet-subsystem-ahb checkout (fpga/rp/eth_ss/filelist.tcl).
# No default; only needed when the pre-produced checkpoint is absent.
ETH_SS_HOME              ?=
RM_SYNTH_NEEDVAR_rm_eth_ss := ETH_SS_HOME ARM_IP_LIBRARY_PATH
RM_SYNTH_ENV_rm_eth_ss   := ETH_SS_HOME=$(ETH_SS_HOME) ARM_IP_LIBRARY_PATH=$(ARM_IP_LIBRARY_PATH)
RM_SYNTH_REUSE_rm_eth_ss := $(REPO_ROOT)/fpga/rp/eth_ss/build/rm_eth_ss_synth.dcp

# --- rm_nanosoc_multicore — the multicore-ethernet DUT ------------------------
# Its pynq flist needs ~14 read-only IP env vars, so the recipe SOURCES the
# DUT's own set_env.sh (which exports them all) instead of re-listing them here
# — robust whether or not the caller pre-sourced it.
RM_SYNTH_TCL_rm_nanosoc_multicore     := $(REPO_ROOT)/fpga/rp/nanosoc_multicore/ooc_synth.tcl
RM_SYNTH_NEEDVAR_rm_nanosoc_multicore := NANOSOC_MULTICORE_HOME
RM_SYNTH_MARK_rm_nanosoc_multicore := RM_NANOSOC_MULTICORE_SYNTH_COMPLETE
RM_SYNTH_WRAP_rm_nanosoc_multicore := source $(NANOSOC_MULTICORE_HOME)/set_env.sh >/dev/null 2>&1 &&

# --- rm_socscope — the SoCScope trace plane ----------------------------------
# Sources come from the SoCScope checkout via SOCSCOPE_HOME (default: sibling of
# this repo), NOT vendored -- same arrangement rm_nanosoc/rm_eth_ss use for
# their DUTs, and for the same reason: a second copy drifts.
SOCSCOPE_HOME ?= $(REPO_ROOT)/../SoCScope
RM_SYNTH_TCL_rm_socscope     := $(REPO_ROOT)/fpga/dfx/rms/rm_socscope/ooc_synth.tcl
RM_SYNTH_MARK_rm_socscope    := RM_SOCSCOPE_SYNTH_COMPLETE
RM_SYNTH_ENV_rm_socscope     := SOCSCOPE_HOME=$(SOCSCOPE_HOME)
RM_SYNTH_REUSE_rm_socscope   := $(REPO_ROOT)/fpga/dfx/rms/rm_socscope/build/rm_socscope_synth.dcp
RM_SYNTH_REQUIRE_rm_socscope := $(SOCSCOPE_HOME)/hw/rtl

# --- rm_clcd_demo — the CPU-less test card on the display tunnel -------------
# Registered 2026-09-14 (MINT-PREP). The one line fpga/rp/clcd_demo/README.md
# says must land beside the rm_list.tcl entry, plus the log marker, because
# Vivado exits 0 after a Tcl error and the marker is the only proof.
# No _REQUIRE and no _ENV: every source is in this repo (the wrapper, the
# generated init table, and fpga/shell/ip/clcd/clcd_core.sv, SHARED not copied).
RM_SYNTH_TCL_rm_clcd_demo  := $(REPO_ROOT)/fpga/rp/clcd_demo/ooc_synth.tcl
RM_SYNTH_MARK_rm_clcd_demo := RM_CLCD_DEMO_SYNTH_COMPLETE

# --- rm_nanosoc_iice — nanosoc through Synplify with an Identify IICE --------
# TWO TOOLS, and this Makefile can only drive the second one. Step 1 is
# Synplify Premier 2022.09-SP2 + the Identify instrumentor and needs a LICENCE
# SEAT; step 2 is Vivado over the EDIF it emits. So this RM is registered the
# way rm_socscope is — through _REUSE, pointing at the checkpoint its own
# directory produces:
#
#     make -C fpga/rp/nanosoc_iice prj synth   # LICENCE SEAT, ~4 min map
#     make -C fpga/rp/nanosoc_iice dcp         # -> build/rm_nanosoc_synth.dcp
#
# Do NOT use that directory's `make stage`: it copies its checkpoint OVER
# $(PROD)/rm_nanosoc_synth.dcp, i.e. it makes the IICE build BE rm_nanosoc for
# one prod run. That is a deliberate A/B affordance and it is wrong for a mint,
# where the two designs must be two RMs with two rm_ids.
#
# _REUSE names build/rm_nanosoc_synth.dcp (not ..._iice_...): the file is named
# after the design inside it, and ooc_synth_synplify.tcl hard-codes that name.
# The staging rule copies it to $(PROD)/rm_nanosoc_iice_synth.dcp, which is
# where build_dfx.tcl looks. If the reuse file is absent the rule falls through
# to running ooc_synth_synplify.tcl under Vivado in a fresh stage dir, where the
# EDIF does not exist — it fails loudly, naming the missing .edf, which is the
# correct outcome: there is no way to conjure a Synplify run from here.
RM_SYNTH_TCL_rm_nanosoc_iice   := $(REPO_ROOT)/fpga/rp/nanosoc_iice/ooc_synth_synplify.tcl
RM_SYNTH_MARK_rm_nanosoc_iice  := RM_NANOSOC_IICE_SYNTH_COMPLETE
RM_SYNTH_REUSE_rm_nanosoc_iice := $(REPO_ROOT)/fpga/rp/nanosoc_iice/build/rm_nanosoc_synth.dcp
RM_SYNTH_POST_rm_nanosoc_iice  := cp -f "$$STAGE/rm_nanosoc_synth.dcp" "$$DCP";

# --- rm_dbg_demo — a counter + one ILA behind the RM's own debug hub ----------
# ILA mint (docs/planning/ILA_MINT_PLAN_2026-09-23.md §2 decision 4): the B2
# proof vehicle. Every source is in this repo; the debug IP is created BY SCRIPT
# in the stage dir (fpga/rp/common/dbg_ip.tcl), never a committed .xci. The
# script prints DBG_CHECK lines (0 BSCANE2, 0 clock cells, 1 xsdbm, >=1 ILA)
# and refuses the marker if any fails.
RM_SYNTH_TCL_rm_dbg_demo  := $(REPO_ROOT)/fpga/rp/dbg_demo/ooc_synth.tcl
RM_SYNTH_MARK_rm_dbg_demo := RM_DBG_DEMO_SYNTH_COMPLETE
# --- end rm_dbg_demo ----------------------------------------------------------

# --- rm_nanosoc_ila — rm_nanosoc + debug hub + one ILA on its boundary nets ---
# Same env contract as rm_nanosoc (fpga/rp/nanosoc/ooc_synth.tcl, which is NOT
# modified -- fpga/rp/nanosoc_ila/ooc_synth.tcl carries a copy of its
# source-reading logic), plus the debug IP by script as rm_dbg_demo.
RM_SYNTH_TCL_rm_nanosoc_ila     := $(REPO_ROOT)/fpga/rp/nanosoc_ila/ooc_synth.tcl
RM_SYNTH_NEEDVAR_rm_nanosoc_ila := SOCLABS_NANOSOC_SOC_DIR SOCLABS_AHB_QSPI_DIR ARM_IP_LIBRARY_PATH
RM_SYNTH_MARK_rm_nanosoc_ila    := RM_NANOSOC_ILA_SYNTH_COMPLETE
RM_SYNTH_ENV_rm_nanosoc_ila     := \
  SOCLABS_NANOSOC_SOC_DIR=$(SOCLABS_NANOSOC_SOC_DIR) \
  SOCLABS_NANOSOC_ARCH_TECH_DIR=$(SOCLABS_NANOSOC_ARCH_TECH_DIR) \
  SOCLABS_NANOSOC_GEN_DIR=$(SOCLABS_NANOSOC_GEN_DIR) \
  ARM_IP_LIBRARY_PATH=$(ARM_IP_LIBRARY_PATH) \
  FPGA_BOOTROM_DIR=$(FPGA_BOOTROM_DIR) \
  SOCLABS_AHB_QSPI_DIR=$(SOCLABS_AHB_QSPI_DIR) \
  SOCLABS_CORESIGHT_SOC400_TECH_DIR=$(SOCLABS_CORESIGHT_SOC400_TECH_DIR) \
  RM_NANOSOC_DIR=$(REPO_ROOT)/fpga/rp/nanosoc \
  RM_NANOSOC_ILA_DIR=$(REPO_ROOT)/fpga/rp/nanosoc_ila
# --- end rm_nanosoc_ila -------------------------------------------------------

# Every RM key rm_list.tcl knows about, read from rm_list.tcl itself so a typo
# in `make add-rm-<name>` fails with a list instead of dying inside Vivado.
ALL_RM_KEYS := $(shell sed -n 's/^set RM_LIB(\([a-z0-9_]*\),rm_name).*/\1/p' $(DFX_DIR)/rm_list.tcl)

# Every RM rm_list.tcl declares as carrying ILAs: `set RM_LIB(<rm>,debug) 1`
# (absent = 0). build_dfx.tcl reads the same key through rm_debug_declared
# (tools/debug_probes.tcl); this is the SECOND, independent derivation that
# tools/ltx_sidecar.py holds the build dir to -- declared debug => an .ltx,
# not declared => none.
DEBUG_RM_KEYS := $(shell sed -n 's/^[[:space:]]*set[[:space:]][[:space:]]*RM_LIB(\([a-z0-9_]*\),debug)[[:space:]][[:space:]]*"\{0,1\}1"\{0,1\}[[:space:]]*\(;.*\)\{0,1\}$$/\1/p' $(DFX_DIR)/rm_list.tcl)
LTX_GATE = $(PYTHON) $(DFX_DIR)/tools/ltx_sidecar.py gate --rp-inst $(RP_INST) --debug-keys '$(DEBUG_RM_KEYS)'
# The STATIC's probes file (FLOW_CONTRACT §6.3; tools/debug_probes.tcl
# write_static_debug_probes): config_rm_greybox_static.ltx exists iff the static
# holds debug cores -- the MicroBlaze V static does (SEAM-8's MIG hub + the DDR4
# calibration slave), the bare-metal static must NOT. Run on every fresh stage-4
# tree, both CPUs; not on `make overlays`, which re-keys trees built before it.
STATIC_LTX_GATE = $(PYTHON) $(DFX_DIR)/tools/ltx_sidecar.py static --ref-key rm_greybox --rp-inst $(RP_INST) --shell-cpu $(SHELL_CPU_EFF)
STATIC_LTX      = $(PROD)/config_rm_greybox_static.ltx

# rm_key -> the make-target spelling and back. Target names use dashes
# (`rm-eth-ss-dcp`), rm_list.tcl keys use underscores (`rm_eth_ss`); the two
# spellings have coexisted since the first RM and neither is worth churning.
rmkey = rm_$(subst -,_,$*)

.SECONDEXPANSION:

# THE staging rule. Output: $(PROD)/<rm_key>_synth.dcp — exactly where
# build_dfx.tcl's readiness filter and ensure_rm_synth_dcp look for a pre-built
# checkpoint. Prerequisite: that RM's registered OOC synth script, so editing
# the script re-stages the checkpoint and nothing else does.
#
# .PRECIOUS matters here: these .dcps are only ever reached THROUGH a pattern
# rule (the mint chain asks for them by name), which makes them "intermediate"
# in make's eyes — and make deletes intermediates when it is done. Deleting a
# 4-hour nanosoc checkpoint after a successful mint would be a spectacular way
# to lose a build window.
.PRECIOUS: $(PROD)/%_synth.dcp
$(PROD)/%_synth.dcp: $$(RM_SYNTH_TCL_$$*)
	@echo "== mint [3/8] RM synth checkpoints -- $* =="
	@echo "   in : $(if $(RM_SYNTH_TCL_$*),$(RM_SYNTH_TCL_$*),<unregistered>)$(if $(REUSE_DCPS_FROM), (REUSE_DCPS_FROM=$(REUSE_DCPS_FROM)),)"
	@echo "   out: $@"
	@test -n "$(RM_SYNTH_TCL_$*)" || { \
	  echo "error: RM '$*' has no registered OOC synth recipe (RM_SYNTH_TCL_$* in fpga/dfx/Makefile)."; \
	  echo "       Inline RMs are synthesised by build_dfx.tcl itself and are never staged here;"; \
	  echo "       registered RMs: $(patsubst RM_SYNTH_TCL_%,%,$(filter RM_SYNTH_TCL_%,$(.VARIABLES)))"; \
	  exit 1; }
	@set -e; \
	 STAGE=$(BUILD)/$*_synth; LOG=$$STAGE/synth.log; DCP=$$STAGE/$*_synth.dcp; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(VIVADO) -mode batch -source $(RM_SYNTH_TCL_$*) -> $@"; \
	   exit 0; \
	 fi; \
	 mkdir -p $$STAGE $(PROD); \
	 if [ -n "$(REUSE_DCPS_FROM)" ] && [ -f "$(REUSE_DCPS_FROM)/$*_synth.dcp" ]; then \
	   echo "   REUSE (REUSE_DCPS_FROM): $(REUSE_DCPS_FROM)/$*_synth.dcp -- NOT re-synthesised;"; \
	   echo "   its provenance is that tree's synth.log, and mint.json records that it was reused."; \
	   cp -f "$(REUSE_DCPS_FROM)/$*_synth.dcp" "$$DCP"; \
	   cp -f "$(REUSE_DCPS_FROM)/$*_provenance.json" "$$STAGE/" 2>/dev/null || true; \
	 elif [ -n "$(RM_SYNTH_REUSE_$*)" ] && [ -f "$(RM_SYNTH_REUSE_$*)" ]; then \
	   echo "   REUSE (pre-produced): $(RM_SYNTH_REUSE_$*)"; \
	   cp -f "$(RM_SYNTH_REUSE_$*)" "$$DCP"; \
	   cp -f "$$(dirname "$(RM_SYNTH_REUSE_$*)")/$*_provenance.json" "$$STAGE/" 2>/dev/null || true; \
	 else \
	   $(if $(RM_SYNTH_REQUIRE_$*),test -d "$(RM_SYNTH_REQUIRE_$*)" || { echo "error: $* needs $(RM_SYNTH_REQUIRE_$*) -- point its source-tree variable at a checkout or drop $* from RM_SET"; exit 1; };) \
	   $(foreach v,$(RM_SYNTH_NEEDVAR_$*),test -n "$($(v))" || { echo "error: $* needs $(v)=<path>. It has NO default -- a default naming one workstation home directory is how a build silently reads the wrong source tree. Set it in tools.env; see tools.env.example."; exit 1; };) \
	   $(RM_SYNTH_PREP_$*) \
	   ( $(RM_SYNTH_WRAP_$*) cd "$$STAGE" && env $(RM_SYNTH_ENV_$*) OUT_DIR="$$STAGE" \
	     $(VIVADO) -mode batch -source $(RM_SYNTH_TCL_$*) \
	       -journal "$$STAGE/synth.jou" -log "$$LOG" ); \
	   grep -q "$(RM_SYNTH_MARK_$*)" "$$LOG" || { \
	     echo "error: $* synth never printed $(RM_SYNTH_MARK_$*) -- see $$LOG."; \
	     echo "       (Vivado EXITS 0 after a Tcl error, so the marker is the only proof.)"; exit 1; }; \
	   $(RM_SYNTH_POST_$*) \
	 fi; \
	 cp -f "$$DCP" $@; \
	 if [ -f "$$STAGE/$*_provenance.json" ]; then cp -f "$$STAGE/$*_provenance.json" $(PROD)/; fi; \
	 $(if $(RM_SYNTH_STAGE_AS_$*),cp -f "$$DCP" $(PROD)/$(RM_SYNTH_STAGE_AS_$*)_synth.dcp;) \
	 echo "   STAGED $@"

# The alias people type, and every one that existed before this rule: the
# explicit names (rm-nanosoc-dcp, rm-eth-ss-dcp, rm-nanosoc-multicore-dcp,
# rm-socscope-dcp, rm-nanosoc-upy-dcp, rm-nanosoc-upy-scaffold-dcp) all resolve
# through it. They are NOT .PHONY: make does not apply pattern rules to phony
# targets, so declaring them would break the alias.
rm-%-dcp: $$(PROD)/rm_$$(subst -,_,$$*)_synth.dcp
	@echo "RM CHECKPOINT READY: $<"

# =============================================================================
# INCREMENTAL ADD — fold ONE new RM into an EXISTING locked static
# =============================================================================
# static_id is READ, not recomputed, so the shell is NOT re-minted and every
# overlay already fielded stays valid. Point BUILD at the locked tree the
# overlays are currently keyed to (docs/FIELDED_SHELL.md says which shell that
# is; fielded/<id>/ preserves its locked static):
#
#   make -C fpga/dfx add-rm-socscope BUILD=$(DFX_DIR)/build_mint
#
# then `make overlays && make verify` to fold the new triple in. FOREGROUND.
# Replaces the four hand-copied add-rm-* recipes; those names still work.
add-rm-%: $$(if $$(RM_SYNTH_TCL_rm_$$(subst -,_,$$*)),$$(PROD)/rm_$$(subst -,_,$$*)_synth.dcp)
	@echo "== add-rm: $(rmkey) into $(PROD) (static_id READ, not re-minted) =="
	@echo "   in : $(PROD)/static_routed_locked.dcp + $(PROD)/static_id.txt + this RM's checkpoint"
	@echo "   out: $(PROD)/config_$(rmkey)_$(RP_PBLOCK)_partial{,_clear}.{bit,bin} + a row in overlay_inputs.txt"
	@echo "        (+ $(PROD)/config_$(rmkey).ltx{,.json} when rm_list.tcl declares it debug 1)"
	@echo "$(ALL_RM_KEYS)" | tr ' ' '\n' | grep -qx "$(rmkey)" || { \
	  echo "error: '$(rmkey)' is not an RM in fpga/dfx/rm_list.tcl. Known RMs:"; \
	  echo "       $(ALL_RM_KEYS)"; exit 1; }
	@test -f $(PROD)/static_routed_locked.dcp || { echo "error: no $(PROD)/static_routed_locked.dcp -- point BUILD at a locked tree (e.g. BUILD=$(DFX_DIR)/build_mint), or run 'make prod' first"; exit 1; }
	@test -f $(PROD)/config_rm_greybox_routed.dcp || { echo "error: no $(PROD)/config_rm_greybox_routed.dcp (the pr_verify reference)"; exit 1; }
	@test -f $(PROD)/static_id.txt || { echo "error: no $(PROD)/static_id.txt"; exit 1; }
	@set -e; \
	 LOG=$(PROD)/add_$(subst -,_,$*).log; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): DFX_ADD_RMS=$(rmkey) $(VIVADO) -mode batch -source $(DFX_DIR)/build_dfx.tcl -> $$LOG"; \
	   echo "  (DRYRUN, not executed): .ltx: write_debug_probes -cell $(RP_INST) -> config_$(rmkey).ltx if debug 1 (declared debug: $(or $(DEBUG_RM_KEYS),none)); grep -E '^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED' $$LOG; $(LTX_GATE) --build-dir $(PROD) --rm-keys $(rmkey)"; \
	   exit 0; \
	 fi; \
	 cd $(PROD) && \
	   DFX_ADD_RMS=$(rmkey) \
	   DFX_REUSE_LOCKED=$(PROD)/static_routed_locked.dcp \
	   DFX_STATIC_ID_FILE=$(PROD)/static_id.txt \
	   DFX_REF_ROUTED=$(PROD)/config_rm_greybox_routed.dcp \
	   SOCSCOPE_HOME=$(SOCSCOPE_HOME) \
	   $(DFX_CPU_ENV)$(VIVADO) -mode batch -source $(DFX_DIR)/build_dfx.tcl \
	     -journal $(PROD)/add_$(subst -,_,$*).jou -log "$$LOG" \
	     -tclargs $(REPO_ROOT) $(PROD) $(PROD)/static_routed_locked.dcp "" $(RP_INST); \
	 if grep -qE "^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" "$$LOG"; then \
	   echo "error: a build_dfx.tcl gate (.ltx / HDPR+DRC / RP pins / XDC) FAILED for $(rmkey):"; grep -E "^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" "$$LOG" | head -5; exit 1; fi; \
	 grep -q "DFX_ADD_COMPLETE" "$$LOG" || { echo "error: add did not reach DFX_ADD_COMPLETE -- see $$LOG"; exit 1; }; \
	 $(LTX_GATE) --build-dir $(PROD) --rm-keys "$(rmkey)" || { \
	   echo "error: the .ltx gate (tools/ltx_sidecar.py) refused $(rmkey) -- see DFX_LTX_GATE_FAILED above"; exit 1; }; \
	 echo "ADD $(rmkey) OK (next: make -C fpga/dfx overlays verify BUILD=$(BUILD))"

# --- PRODUCTION flow (build_dfx.tcl) ------------------------------------------
prod:
	@test -f $(STATIC_DCP) || { echo "error: STATIC_DCP=$(STATIC_DCP) not found — regenerate the shell (vivado -mode batch -source fpga/shell/build_shell.tcl -tclargs ./build/shell_proj) or pass STATIC_DCP=<checkpoint> (proof stand-in: $(BUILD)/proof/static_synth.dcp after 'make proof')"; exit 1; }
	mkdir -p $(BUILD)/prod
	cd $(BUILD)/prod && $(DFX_CPU_ENV)$(DFX_STAGE4_VIVADO) -mode batch \
	  -source $(DFX_DIR)/build_dfx.tcl \
	  -journal $(BUILD)/prod/build.jou -log $(BUILD)/prod/build.log \
	  -tclargs $(REPO_ROOT) $(BUILD)/prod $(STATIC_DCP) "$(RM_SUBSET)" $(RP_INST)
	@if grep -qE "^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" $(BUILD)/prod/build.log; then \
	   echo "error: a build_dfx.tcl gate (.ltx / HDPR+DRC / RP pins / XDC) FAILED:"; grep -E "^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" $(BUILD)/prod/build.log | head -5; exit 1; fi
	@grep -q "DFX_BUILD_COMPLETE" $(BUILD)/prod/build.log && echo "PROD BUILD OK"
	@$(LTX_GATE) --build-dir $(BUILD)/prod --overlay-inputs $(BUILD)/prod/overlay_inputs.txt
	@$(STATIC_LTX_GATE) --build-dir $(BUILD)/prod

# --- overlay triples out of the production build ------------------------------
# build_dfx.tcl emits build/prod/overlay_inputs.txt (one line per built RM:
# rm_key rm_name rm_id partial.bin clearing.bin) + static_id.txt — this target
# turns each line into a verified overlay/<rm_name>/ triple. rm_socscope's
# manifest additionally gets socscope_rev = `git -C $(SOCSCOPE_HOME) rev-parse
# --short=12 HEAD` (+ `-dirty` if that tree has uncommitted changes) -- see the
# comment inside the recipe and docs/SOCSCOPE_INTEGRATION.md.
overlays:
	@test -f $(BUILD)/prod/overlay_inputs.txt || { echo "error: $(BUILD)/prod/overlay_inputs.txt not found — run 'make prod' first"; exit 1; }
	@test -f $(BUILD)/prod/static_id.txt || { echo "error: $(BUILD)/prod/static_id.txt not found — run 'make prod' first"; exit 1; }
	@# Provenance + staleness guard. This target rewrites BOTH
	@# overlay/*/manifest.json AND overlay/mps3_shell_static_id.c from
	@# $(BUILD)/prod/static_id.txt, so a stale prod tree makes them agree with each
	@# other and disagree only with the bitstream on the board -- which
	@# check_overlay_static_id.py (R9) cannot see, because it compares those two
	@# files to each other.
	@#
	@# The authority for "which shell did this tree come from" is the tree itself:
	@# build_dfx.tcl records the static DCP on build.log's `# Command line:` line.
	@# Do NOT compare against $(STATIC_DCP) unless the caller passed one -- its
	@# default names a shell this tree may have nothing to do with, and any newer
	@# unrelated checkpoint on disk would then veto a perfectly good re-mint.
	@set -e; \
	 sid=$$(cat $(BUILD)/prod/static_id.txt); \
	 rec=$$(grep -m1 '# Command line:' $(BUILD)/prod/build.log 2>/dev/null | grep -oE '[^ ]*\.dcp' | tail -1); \
	 echo "overlays: BUILD=$(BUILD)  static_id=$$sid  built-from=$${rec:-<unrecorded>}"; \
	 if [ -n "$$rec" ] && [ -f "$$rec" ] && [ "$$rec" -nt "$(BUILD)/prod/static_id.txt" ]; then \
	   echo "error: the shell this tree was built from has been REBUILT since:"; \
	   echo "         $$rec ($$(date -r $$rec '+%F %H:%M'))"; \
	   echo "       is newer than $(BUILD)/prod/static_id.txt ($$sid, $$(date -r $(BUILD)/prod/static_id.txt '+%F %H:%M'))."; \
	   echo "       Minting overlays now would stamp a dead static_id into mps3_shell_static_id.c"; \
	   echo "       AND every manifest, self-consistently. Re-run 'make -C fpga/dfx prod'."; \
	   exit 1; \
	 fi; \
	 if [ "$(origin STATIC_DCP)" = "command line" ] && [ -n "$$rec" ] && \
	    [ "$$(readlink -f $(STATIC_DCP) 2>/dev/null)" != "$$(readlink -f $$rec 2>/dev/null)" ]; then \
	   echo "error: this prod tree was built from"; \
	   echo "         $$rec"; \
	   echo "       but you passed STATIC_DCP=$(STATIC_DCP)."; \
	   echo "       Its static_id ($$sid) describes the former, not the latter."; \
	   echo "       Pass BUILD=<the tree built from that DCP>, or re-run 'make prod' with this STATIC_DCP."; \
	   exit 1; \
	 fi
	@# SoCScope provenance (socscope_rev). rm_socscope's RTL is read from
	@# $(SOCSCOPE_HOME) at synth time and never vendored, so its manifest must say
	@# WHICH revision -- check_socscope_overlay_rev.py (make check stage 2) refuses
	@# an overlay that does not. Stamped for rm_socscope ONLY: rm_nanosoc consumes
	@# SoCScope only as its SOCSCOPE=1 (B2) variant, and that flag reaches
	@# fpga/rp/nanosoc/ooc_synth.tcl alone -- the staged .dcp carries no marker
	@# into overlay_inputs.txt, so this recipe cannot tell a B2 nanosoc from the
	@# shipped one. `-dirty` is recorded when SoCScope has uncommitted changes and
	@# the gate then FAILS, which is the point: a dirty build is unreproducible.
	@# The rev is read NOW, not at synth time: run add-rm-socscope and this
	@# target back to back, or the stamp names a commit the RTL is not.
	@# ROW PATHS. overlay_inputs.txt rows are RELATIVE to the build dir
	@# (fpga/dfx/tools/overlay_inputs.tcl): the artefacts sit next to the file
	@# that lists them, so that is the only way the record travels with them --
	@# fielded/0xA8C1C535/overlay_inputs.txt is ten rows of one workstation's
	@# home directory, and could not. Rows written before 2026-09-10 are
	@# ABSOLUTE and are still accepted below: a prod tree from last month is not
	@# a tree anyone should re-mint merely to re-key it.
	@set -e; \
	 ssprov=$(BUILD)/prod/rm_socscope_provenance.json; \
	 if [ -f "$$ssprov" ]; then \
	   ssrev=$$($(PYTHON) -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["socscope_rev"]+("-dirty" if d.get("socscope_dirty") else ""))' "$$ssprov"); \
	   echo "overlays: socscope_rev=$$ssrev from the SYNTH-TIME record $$ssprov"; \
	 elif git -C $(SOCSCOPE_HOME) rev-parse --short=12 HEAD >/dev/null 2>&1; then \
	   ssrev=$$(git -C $(SOCSCOPE_HOME) rev-parse --short=12 HEAD); \
	   [ -z "$$(git -C $(SOCSCOPE_HOME) status --porcelain 2>/dev/null)" ] || ssrev="$$ssrev-dirty"; \
	   echo "overlays: socscope_rev=$$ssrev from git HEAD (FALLBACK -- no synth-time record; this stamp can name a commit the RTL is not)"; \
	 else \
	   ssrev=unknown; \
	 fi; \
	 echo "overlays: SOCSCOPE_HOME=$(SOCSCOPE_HOME)  socscope_rev=$$ssrev (stamped on rm_socscope only)"; \
	 echo "overlays: .ltx gate (declared debug: $(or $(DEBUG_RM_KEYS),none)) -- an .ltx must come from the same routed config as its partial"; \
	 $(LTX_GATE) --check --build-dir $(BUILD)/prod --overlay-inputs $(BUILD)/prod/overlay_inputs.txt || { \
	   echo "error: the .ltx gate refused this prod tree (DFX_LTX_GATE_FAILED above). Not re-keying."; exit 1; }; \
	 while read -r rm_key rm_name rm_id partial clearing; do \
	  case "$$rm_key" in ""|\#*) continue;; esac; \
	  case "$$partial"  in /*) ;; *) partial="$(BUILD)/prod/$$partial";;  esac; \
	  case "$$clearing" in /*) ;; *) clearing="$(BUILD)/prod/$$clearing";; esac; \
	  case "$$rm_key" in rm_socscope) ssarg="--socscope-rev $$ssrev";; *) ssarg="";; esac; \
	  ltx="$(BUILD)/prod/config_$$rm_key.ltx"; ltxarg=""; \
	  if [ -f "$$ltx" ]; then ltxarg="--ltx $$ltx --ltx-sidecar $$ltx.json"; fi; \
	  echo "== overlay: $$rm_name ($$rm_key, rm_id=$$rm_id)$${ltxarg:+ + .ltx} =="; \
	  $(PYTHON) $(DFX_DIR)/gen_manifest.py build \
	    --rm-name $$rm_name --rm-id $$rm_id \
	    --static-id-file $(BUILD)/prod/static_id.txt \
	    --partial $$partial --clearing $$clearing $$ssarg $$ltxarg \
	    --vivado $(VIVADO_VER) --copy --out-root $(OVERLAY_ROOT) || exit 1; \
	  $(PYTHON) $(DFX_DIR)/gen_manifest.py verify $(OVERLAY_ROOT)/$$rm_name/manifest.json || exit 1; \
	  case "$$rm_key" in rm_socscope) [ -f "$$ssprov" ] && cp -f "$$ssprov" $(OVERLAY_ROOT)/$$rm_name/provenance.json;; esac; \
	 done < $(BUILD)/prod/overlay_inputs.txt
	@sid=$$(cat $(BUILD)/prod/static_id.txt); \
	{ printf '/* mps3_shell_static_id.c -- GENERATED by `make -C fpga/dfx overlays`; do not edit.\n'; \
	  printf ' *\n'; \
	  printf ' * Strong override for the weak mps3_shell_static_id() seam\n'; \
	  printf ' * (firmware/coordinator/coordinator.c). A3: compile+link this file into\n'; \
	  printf ' * the shell firmware image so the running shell reports the static_id of\n'; \
	  printf ' * the exact locked static (static_routed_locked.dcp) these overlay\n'; \
	  printf ' * triples were generated against -- the pusher refuses on mismatch\n'; \
	  printf ' * (overlay-manifest.md; net-protocol.md ping.shell_id).\n'; \
	  printf ' *\n'; \
	  printf ' * static_id = CRC-32 (zlib) of static_routed_locked.dcp -- see\n'; \
	  printf ' * fpga/dfx/README.md "static_id scheme". Value below matches every\n'; \
	  printf ' * overlay/<rm>/manifest.json generated in the same run.\n'; \
	  printf ' */\n'; \
	  printf '#include <stdint.h>\n\n'; \
	  printf 'uint32_t mps3_shell_static_id(void)\n'; \
	  printf '{\n'; \
	  printf '    return %sUL;\n' "$$sid"; \
	  printf '}\n'; \
	} > $(OVERLAY_ROOT)/mps3_shell_static_id.c; \
	echo "GEN $(OVERLAY_ROOT)/mps3_shell_static_id.c (static_id $$sid)"
	@echo "OVERLAYS OK: $$(ls -d $(OVERLAY_ROOT)/*/ 2>/dev/null | tr '\n' ' ')"

# --- standalone round-trip check of whatever triples exist ---------------------
# Two independent checks, failures aggregated so you see BOTH in one run:
#   1. per-manifest CRC/size round-trip (gen_manifest.py verify);
#   2. cross-manifest static_id lockstep vs the authoritative shell id
#      (check_overlay_static_id.py, R9) -- gen_manifest.py's verify NEVER
#      compares static_id against the shell, so an overlay set keyed to a dead
#      shell would otherwise pass here and only fail two `make check` stages
#      later as a confusing pyverify e2e mismatch.
verify:
	@fail=0; found=0; \
	for m in $(OVERLAY_ROOT)/*/manifest.json; do \
	  [ -f "$$m" ] || continue; found=1; \
	  $(PYTHON) $(DFX_DIR)/gen_manifest.py verify "$$m" || fail=1; \
	done; \
	[ $$found -eq 1 ] || { echo "no overlay/*/manifest.json found — run 'make overlays'"; exit 1; }; \
	$(PYTHON) $(REPO_ROOT)/scripts/harness_gates/check_overlay_static_id.py --repo $(REPO_ROOT) || fail=1; \
	exit $$fail

# --- RM boundary conformance (closes rm_list.tcl's TODO(A2)) -------------------
# Mechanically diffs every RM wrapper's port list against the frozen boundary
# declared in fpga/shell/boundary.yaml (the table pin_check carries is generated
# from it by tools/gen_boundary.py), so a drifted wrapper fails FAST here instead
# of surfacing as a pr_verify failure late in build_dfx.tcl (or, worse, as a
# partial pushed onto real silicon against a static it does not match).
pin-check:
	@$(PYTHON) $(DFX_DIR)/pin_check.py

# --- static_canon: the CONTENT identity of the static -------------------------
# static_id is a CRC of a TIMESTAMPED artefact, so the same sources built twice
# mint two different ids and "did anything actually change?" has no answer
# anywhere. static_canon is a SHA-256 over the SOURCES that decide the static --
# no timestamps, no artefacts, no Vivado -- so two builds from identical inputs
# produce the identical hash and a one-bit source change does not.
#
# static_id is NOT replaced and NOT touched: it is baked into every fielded
# overlay and changing it is a re-mint. These are two axes, and this is the one
# static_id cannot cover (docs/VERSIONING_PLAN.md §3.0, now three axes).
STATIC_CANON_TOOL = $(DFX_DIR)/tools/static_canon.py
STATIC_CANON_DECL = $(DFX_DIR)/tools/static_inputs.txt
STATIC_CANON_JSON = $(BUILD)/static_canon.json
# SHELL_REALPHY IS a mint knob as of 2026-09-14 (MINT-PREP). It was not: stage 2
# forwarded only SHELL_TOUCH, so the real-PHY gate took whatever the ambient
# shell happened to have -- while fpga/shell/build_shell.tcl's own header said,
# in terms, "MINT KNOB ... reach this script through the ENVIRONMENT, exactly
# like SHELL_TOUCH: `make -C fpga/dfx mint` forwards [them]". That sentence was
# false when it was written; stage 2 now forwards both, so it is true, and the
# hash below records the value that was actually in force rather than a guess.
#
# WHAT IT CHANGES (docs/planning/REALPHY_RATE_DECISION.md; landed 31dffb6):
#   0 (DEFAULT, and what is fielded) -- the shell's virtual PHY drives the DUT's
#     RMII from inside the fabric. No external PHY, no shield pins. The unset
#     and =0 block designs are byte-identical by md5, so the default bundle is
#     provably unmoved by the knob existing.
#   1 -- shell_top gets the MPS3_SHELL_REALPHY verilog define, nine phy_pad_*
#     boundary ports appear, and fpga/shell/constraints_realphy/{pins,timing}.xdc
#     are added, pinned to the MEASURED J28 map. The =1 design differs from =0
#     by exactly those nine ports. It is a DIFFERENT STATIC: different netlist,
#     different static_id, and no fielded overlay is valid against it.
# REALPHY_RATE (10|100, default 100) is the link rate the variant is built and
# CONSTRAINED for. It is forwarded ONLY when the caller set it: build_shell.tcl
# hard-errors on REALPHY_RATE set with SHELL_REALPHY=0, the same silent-half-
# config guard shape as SHELL_TOUCH/TOUCH, and passing an empty value would trip
# it on every ordinary mint.
SHELL_REALPHY ?= 0
REALPHY_RATE  ?=
SHELL_REALPHY_ENV = SHELL_REALPHY=$(strip $(SHELL_REALPHY)) \
                    $(if $(strip $(REALPHY_RATE)),REALPHY_RATE=$(strip $(REALPHY_RATE)),)
# SHELL_CPU=mbv is a DIFFERENT STATIC and is hashed as one: the flag below, the
# tool version (2026.1), and the seam files themselves (fpga/shell/bd/*.tcl,
# fpga/shell/constraints/mbv/*.xdc -- tools/static_inputs.txt). Unset and `mb`
# add no flag, so the default digest is exactly what it was.
STATIC_CANON_FLAGS = --flag SHELL_TOUCH=$(strip $(SHELL_TOUCH)) \
                     --flag SHELL_REALPHY=$(strip $(SHELL_REALPHY))$(if $(filter mbv,$(SHELL_CPU_EFF)), --flag SHELL_CPU=mbv)
# REAL prerequisites: the declared inputs themselves, listed by the tool. Under
# .SECONDEXPANSION (enabled above) this $(shell) runs only when make actually
# considers the target -- not on every `make help`. Without it the target would
# need a FORCE prerequisite, which would make `make -n mint` print work to do on
# an up-to-date tree and break the no-op gate in tests/dfx_flow.
STATIC_CANON_SRCS = $(shell $(PYTHON) $(STATIC_CANON_TOOL) list --absolute --repo $(REPO_ROOT) 2>/dev/null)

.PHONY: canon canon-check overlay-usercode overlay-usercode-check
$(STATIC_CANON_JSON): $(STATIC_CANON_TOOL) $(STATIC_CANON_DECL) $$(STATIC_CANON_SRCS)
	@echo "== static_canon: hashing the sources that decide the static =="
	@echo "   in : $(STATIC_CANON_DECL) + the tracked sources it declares"
	@echo "   out: $@"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(PYTHON) $(STATIC_CANON_TOOL) compute -> $@"; \
	   exit 0; \
	 fi; \
	 mkdir -p $(BUILD); \
	 $(PYTHON) $(STATIC_CANON_TOOL) compute --repo $(REPO_ROOT) \
	   --vivado $(VIVADO_VER) $(STATIC_CANON_FLAGS) \
	   $(if $(wildcard $(SHELL_DCP)),--linked-dcp $(SHELL_DCP),) -o $@

canon: $(STATIC_CANON_JSON)
	@$(PYTHON) -c "import json,sys; r=json.load(open('$(STATIC_CANON_JSON)')); \
	print('static_canon %s  (%d inputs, part %s, vivado %s, flags %s)' % \
	      (r['digest'], r['input_count'], r['part'], r['vivado'], r['flags']))"

# The declaration gate on its own: every include glob matches something, every
# design source under a declared root is declared, every exclusion has a reason.
# Board-free, tool-free, sub-second -- this is the one that stops the hash from
# quietly ceasing to cover a file somebody added.
canon-check:
	@$(PYTHON) $(STATIC_CANON_TOOL) check --repo $(REPO_ROOT)

# --- static_usercode: bind every overlay to the BITSTREAM BUILD ---------------
# gen_manifest.py has carried `static_usercode` since c0784ed, pyverify reads it
# and check_image_overlay_match.py gates it -- but nothing ever passed it, so not
# one manifest in $(OVERLAY_ROOT) had the field. A guard with no data in it is
# not a guard. This stage reads USERID out of the full static .bit and writes it
# into every overlay manifest beside it.
OVERLAY_USERCODE_BIT = $(PROD)/config_rm_greybox.bit
overlay-usercode:
	@$(PYTHON) $(DFX_DIR)/tools/stamp_usercode.py \
	   --overlay-root $(OVERLAY_ROOT) --static-bit $(OVERLAY_USERCODE_BIT) \
	   --stamp-json $(PROD)/static_stamp.json --repo $(REPO_ROOT)

overlay-usercode-check:
	@$(PYTHON) $(DFX_DIR)/tools/stamp_usercode.py --check \
	   --overlay-root $(OVERLAY_ROOT) --static-bit $(OVERLAY_USERCODE_BIT) \
	   --stamp-json $(PROD)/static_stamp.json --repo $(REPO_ROOT)

clean:
	rm -rf $(BUILD)



# =============================================================================
# THE MINT — one entry point, eight stages, make's own dependency tracking
# =============================================================================
#   make -C fpga/dfx mint \
#        BUILD=<dfx build dir> SHELL_PROJ=<shell project dir> \
#        RM_SET="greybox led nanosoc ..." SHELL_TOUCH=0|1 TOUCH=0|1 \
#        [SHELL_REALPHY=0|1 [REALPHY_RATE=10|100]] \
#        [REUSE_DCPS_FROM=<a prod dir whose RM checkpoints to reuse>] \
#        [MINT_HUB=<rsync destination>] [DRYRUN=1]
#
# Full runbook: docs/BUILD_AND_MINT.md. That document replaces
# fpga/dfx/qspi_kvm_rekey/{MINT_RUNBOOK.md,rebuild_jtag_uart.sh}, which were
# deleted once every step they described had a make target here.
#
# WHY IT MOVED INTO make. The mint used to be a 516-line bash script that
# orchestrated these same make targets. Two copies of it existed and had
# diverged by 144 lines; the shell actually on the board was built by NEITHER,
# by hand, stage by stage (fielded/0xA8C1C535/README.md "Provenance"). A script
# that reimplements a build graph cannot resume, cannot skip what is already
# done, and cannot be asked what it would do — so when a stage failed at hour
# three, the operator hand-ran the rest, and the script stopped being the
# record. Every stage below is a FILE target with real prerequisites:
#
#   * re-running after a failure repeats only what is missing or stale;
#   * `make -n` prints the plan, honestly, because nothing is hidden in bash;
#   * `DRYRUN=1` launches nothing (the old script's DRYRUN=1 re-keyed every
#      tracked overlay for real — it went through `make overlays`, which WRITES);
#   * a re-run with nothing changed does nothing at all.
#
# THE TWO KNOBS a maintainer sets are SHELL_TOUCH (fabric) and TOUCH (firmware),
# and they must agree — see the lockstep check below. A THIRD, SHELL_REALPHY,
# selects a DIFFERENT STATIC (external LAN8720 instead of the in-fabric virtual
# PHY) and defaults to 0, which is what is fielded; it is documented where it is
# declared, above the static_canon block. Everything else has a default that is
# either the product configuration or this repo's layout.
# -----------------------------------------------------------------------------

# Firmware workspace + the ELF `make -C firmware/platform elf` really emits.
FW_WS  ?= $(REPO_ROOT)/build/vitis_fw
FW_ELF ?= $(FW_WS)/shell_fw/shell_fw.elf
# The standalone BSP that ELF compiles against (firmware/platform/Makefile BSP :=).
# A fresh worktree has none, and the mb mint used to fail at stage 6, hours in,
# on xil_printf.h (findings #4). Preflight names it; stage 6 creates it from THIS
# mint's XSA when it is missing. mbv: stage 6 is stage0, which needs no BSP.
FW_BSP         = $(FW_WS)/shell_platform/microblaze_0/standalone_domain/bsp/microblaze_0
FW_BSP_XPARAMS = $(FW_BSP)/include/xparameters.h
FW_BSP_FILES   = $(FW_BSP_XPARAMS) $(FW_BSP)/include/xil_printf.h $(FW_BSP)/lib/libxil.a
FW_BSP_MISSING = $(if $(filter mbv,$(SHELL_CPU_EFF)),,$(if $(wildcard $(FW_BSP_XPARAMS)),,1))
SHELL_XSA      = $(SHELL_PROJ)/shell_harness.xsa
FW_BSP_CREATE  = $(XSCT) -nodisp $(REPO_ROOT)/firmware/platform/create_platform.tcl $(SHELL_XSA) $(FW_WS)

# Vitis tools. updatemem and mb-nm are NOT in the Vivado bin dir.
XSCT           ?= /apps/Xilinx/Vitis/$(VIVADO_VER)/bin/xsct
UPDATEMEM      ?= /apps/Xilinx/Vitis/$(VIVADO_VER)/bin/updatemem
MBNM           ?= /apps/Xilinx/Vitis/$(VIVADO_VER)/gnu/microblaze/lin/bin/mb-nm
MBOBJDUMP      ?= /apps/Xilinx/Vitis/$(VIVADO_VER)/gnu/microblaze/lin/bin/mb-objdump
VITIS_SETTINGS ?= /apps/Xilinx/Vitis/$(VIVADO_VER)/settings64.sh
VITIS_DIR      ?= /apps/Xilinx/Vitis/$(VIVADO_VER)

# The RM set to route against the ONE new locked static, greybox FIRST (it is
# the DFX reference config, extracted+locked into static_routed_locked.dcp).
# Accepts short names or rm_ keys: "greybox led" == "rm_greybox rm_led".
#
# clcd_demo JOINED THE DEFAULT SET 2026-09-14 (MINT-PREP): it is 235 LUT / 133 FF
# / 1 RAMB18 (0.55% of the pblock, about twice rm_led), every source is in this
# repo, and it is the only artefact that can answer "has a DUT ever driven the
# panel?" in the W2 board window. Costing it into the mint is cheaper than a
# later add-rm, and an add-rm cannot happen at all if the locked static is lost.
#
# nanosoc_iice is REGISTERED but deliberately NOT in the default set. Two
# reasons, both blocking: its checkpoint needs a Synplify/Identify licence seat
# this Makefile cannot take (see its registration block above), and its shim does
# not yet drive its own rm_id (see rm_list.tcl). Add it explicitly once both are
# closed — RM_SET="... nanosoc_iice" — or fold it into the locked static
# afterwards with `make add-rm-nanosoc-iice`, which does NOT re-key anything.
#
# dbg_demo JOINS THE DEFAULT SET 2026-09-23 (the ILA mint, ILA_MINT_PLAN §0b):
# the B2 proof vehicle -- a counter + one ILA behind the RM's own debug hub,
# ~1 BRAM, ~12 min of route. Conditional on its registration (lane DEBUG-RM's
# `# --- rm_dbg_demo` block above) so a tree without it still plans. nanosoc_ila
# is NOT defaulted: it rides the mint only if its OOC synth finished in time
# (RM_SET="... nanosoc_ila"), otherwise `make add-rm-nanosoc-ila` after fielding.
RM_SET  ?= greybox regdemo_a regdemo_b led uart_echo nanosoc eth_ss nanosoc_multicore socscope nanosoc_upy clcd_demo \
           $(if $(RM_SYNTH_TCL_rm_dbg_demo),dbg_demo)
# MINT_RMS is RM_SET under the name the Linux plan uses (LINUX_HARNESS_PLAN §4:
# `SHELL_CPU=mbv VIVADO=2026.1 MINT_RMS="greybox led dbg_demo nanosoc"`). Both
# given and different is refused rather than guessed between.
MINT_RMS ?=
ifneq ($(strip $(MINT_RMS)),)
ifeq ($(origin RM_SET),command line)
ifneq ($(strip $(RM_SET)),$(strip $(MINT_RMS)))
$(error MINT_RMS="$(strip $(MINT_RMS))" and RM_SET="$(strip $(RM_SET))" disagree -- pass one)
endif
endif
override RM_SET := $(MINT_RMS)
endif
RM_KEYS  = $(foreach r,$(RM_SET),$(if $(filter rm_%,$(r)),$(r),rm_$(subst -,_,$(r))))

# ONE BUILD DIR, ONE VIVADO (tools/vivado_guard.py). Before any goal that runs
# Vivado, every checkpoint this run would read back -- the build dir's own, the
# shell checkpoint it links, a pre-produced or REUSE_DCPS_FROM RM checkpoint --
# must have been written by $(VIVADO_VER). An older checkpoint opens SILENTLY in
# a newer Vivado and is linked as-is (the July 2026 VERSION TRAP); a newer one
# fails an hour in. Silent when clean, so the default plan prints what it did.
FLOW_GUARD_GOALS := $(filter mint mint-% prod add-rm-% rm-%-dcp,$(MAKECMDGOALS))
ifneq ($(FLOW_GUARD_GOALS),)
FLOW_GUARD_KEYS := $(sort $(RM_KEYS) \
  $(foreach g,$(filter rm-%-dcp,$(FLOW_GUARD_GOALS)),rm_$(subst -,_,$(patsubst rm-%-dcp,%,$(g)))) \
  $(foreach g,$(filter add-rm-%,$(FLOW_GUARD_GOALS)),rm_$(subst -,_,$(patsubst add-rm-%,%,$(g)))))
FLOW_VERSION_MIX := $(shell $(PYTHON) $(DFX_DIR)/tools/vivado_guard.py mix --want '$(strip $(VIVADO_VER))' \
  --build '$(BUILD)' --dcp '$(SHELL_DCP)' \
  $(foreach k,$(FLOW_GUARD_KEYS),$(if $(RM_SYNTH_REUSE_$(k)),--dcp '$(RM_SYNTH_REUSE_$(k))') \
    $(if $(REUSE_DCPS_FROM),--dcp '$(REUSE_DCPS_FROM)/$(k)_synth.dcp')) 2>&1)
ifneq ($(strip $(FLOW_VERSION_MIX)),)
$(error $(FLOW_VERSION_MIX))
endif
endif
# Only the RMs that need a pre-staged checkpoint are prerequisites of the prod
# stage; inline RMs are synthesised by build_dfx.tcl (rm_list.tcl synth_mode).
MINT_RM_DCPS = $(foreach k,$(RM_KEYS),$(if $(RM_SYNTH_TCL_$(k)),$(PROD)/$(k)_synth.dcp))

# THE FIELDED PAIR. SHELL_TOUCH puts the AXI IIC master in the fabric; TOUCH
# compiles the driver that talks to it. A fabric with the IIC and firmware
# without the driver is a SILENT half-mint: it pings, reports the right
# static_id and passes every acceptance gate (that is exactly what shipped once
# — docs/FIELDED_SHELL.md, "A third fact"). Refuse the plan, not the artefact.
SHELL_TOUCH ?= 0
TOUCH       ?= $(SHELL_TOUCH)
ifeq ($(strip $(SHELL_TOUCH)),1)
ifneq ($(strip $(TOUCH)),1)
$(error SHELL_TOUCH=1 but TOUCH=$(strip $(TOUCH)): the fabric would carry the AXI IIC \
and the firmware would not carry the touch driver. They move together — see \
docs/BUILD_AND_MINT.md. Pass TOUCH=1, or build the fabric with SHELL_TOUCH=0.)
endif
endif

# The firmware flag set, as ONE name. PRODUCT=1 expands (firmware/platform/
# Makefile) to the set the fielded image was built with; TOUCH is passed
# explicitly AFTER it because an explicit knob beats PRODUCT's `?=`, so this
# means "the fielded set, with touch exactly as the caller asked". Folding TOUCH
# into PRODUCT would silently re-enable it on a fabric built without the IIC.
FW_FLAGS_STR   := PRODUCT=1 TOUCH=$(strip $(TOUCH)) SHELL_TOUCH=$(strip $(SHELL_TOUCH)) RM_SET=$(RM_KEYS)
FW_FLAGS_STAMP  = $(BUILD)/fw_flags.stamp
# make keys off timestamps and CANNOT see a changed FLAG: without this, main.o
# stays compiled without -DMPS3_HAS_CLCD, --gc-sections strips the freshly built
# clcd.o straight back out, and you get a byte-identical ELF with the drivers
# silently absent (observed). The stamp turns the flag set into a file, so a
# flag change is a dependency change. READ at parse time, WRITTEN only by the
# recipe, so `make -n` stays honest.
FW_FLAGS_CHANGED := $(shell [ "$$(cat $(BUILD)/fw_flags.stamp 2>/dev/null)" = "$(FW_FLAGS_STR)" ] || echo MINT_FLAGS_CHANGED)

# Where a copy of the irreplaceable artefacts goes. NO DEFAULT HOST, ever: this
# repo is public-bound and a hostname baked into a Makefile is a hostname that
# outlives the machine. Set it in tools.env (see tools.env.example) or on the
# command line; unset, stage 8 is a no-op that says so.
MINT_HUB ?=

# THE FLASHABLE BASE -- what the MCC loads from its config SD. mb: the greybox
# base with the MicroBlaze firmware baked in (stage 6 below). mbv: the greybox
# base with STAGE0 baked into the MicroBlaze V LMB (mint-stage0, further down).
# Two names on purpose: an mb base and an mbv base must never be mistaken for
# one another on a hub or an SD card.
FLASHABLE_NAME = $(if $(filter mbv,$(SHELL_CPU_EFF)),config_rm_greybox_stage0.bit,config_rm_greybox_fw.bit)
FLASHABLE_BIT  = $(PROD)/$(FLASHABLE_NAME)

.PHONY: mint mint-preflight mint-shell mint-rms mint-prod mint-overlays \
        mint-firmware mint-record mint-hub-copy MINT_FLAGS_CHANGED
MINT_FLAGS_CHANGED: ;

# --- stage 1/8: preflight -----------------------------------------------------
# Seconds, and they fail on the two things that otherwise fail at hour three: a
# BD CONFIG that dies at IP generation, and an RM wrapper off the frozen
# partition boundary.
#
# PLUS one bench, ~5 min: tests/usd_spi `make premint` -- usd_spi (D13, USD
# @0x44A4_0000) at EXACTLY the parameterisation shell_bd.tcl ships (width 32 AND
# the 10 ms DEBOUNCE_CYCLES default). `make check` runs that bench with a
# 64-cycle debounce (bdfast) to stay quick, so this is the only place the
# shipped debounce is simulated, and it must be before the static is built:
# a card-detect that never settles is a board where the store never loads.
# Needs VCS + cocotb (set_env.sh is sourced here if `vcs` is not on PATH). No
# simulator => FAIL. USD_PREMINT=skip is the explicit, LOUD escape hatch (a
# licence outage); it is echoed into the log and the stamp.
USD_PREMINT ?= run
$(BUILD)/preflight.stamp: $(REPO_ROOT)/fpga/shell/bd/shell_bd.tcl \
                          $(REPO_ROOT)/scripts/harness_gates/check_bd_config_lint.py \
                          $(DFX_DIR)/pin_check.py $(DFX_DIR)/rm_list.tcl \
                          $(REPO_ROOT)/fpga/shell/ip/usd_spi/usd_spi.sv \
                          $(REPO_ROOT)/tests/usd_spi/test_usd_spi.py
	@echo "== mint [1/8] preflight gates =="
	@echo "   in : fpga/shell/bd/shell_bd.tcl, fpga/dfx/rm_list.tcl, every RM wrapper, fpga/shell/ip/usd_spi"
	@echo "   out: $@"
	$(if $(FW_BSP_MISSING),@echo "   BSP: $(FW_BSP_XPARAMS) is MISSING (a fresh worktree?)"; \
	 echo "        stage 6 creates it from this mint's XSA: $(FW_BSP_CREATE)"; \
	 if [ "$(DRYRUN)" = "1" ]; then exit 0; fi; \
	 test -x $(XSCT) || { echo "FAIL preflight: no xsct at $(XSCT) so stage 6 could not create the BSP -- pass XSCT=<path> or create the BSP first (command above)"; exit 1; }; \
	 test -f $(REPO_ROOT)/firmware/platform/create_platform.tcl || { echo "FAIL preflight: firmware/platform/create_platform.tcl is missing"; exit 1; })
	@if [ "$(DRYRUN)" = "1" ]; then echo "  (DRYRUN, not executed): check_bd_config_lint.py + pin_check.py + tests/usd_spi premint (USD_PREMINT=$(USD_PREMINT))"; exit 0; fi; \
	 set -e; \
	 mkdir -p $(BUILD); \
	 $(PYTHON) $(REPO_ROOT)/scripts/harness_gates/check_bd_config_lint.py; \
	 $(PYTHON) $(DFX_DIR)/pin_check.py; \
	 if [ "$(USD_PREMINT)" = "skip" ]; then \
	   echo "!!! NOT RUN: tests/usd_spi premint (USD_PREMINT=skip) -- the shipped 10 ms card-detect"; \
	   echo "!!!          debounce of usd_spi was NOT simulated before this mint."; \
	   usd_note="usd_premint=SKIPPED"; \
	 else \
	   ( command -v vcs >/dev/null 2>&1 || . $(REPO_ROOT)/set_env.sh >/dev/null; \
	     command -v vcs >/dev/null 2>&1 || { echo "FAIL preflight: no vcs on PATH, even after set_env.sh -- the usd_spi pre-mint bench cannot run (USD_PREMINT=skip to override, loudly)"; exit 1; }; \
	     nice -n 19 $(MK) --no-print-directory -C $(REPO_ROOT)/tests/usd_spi premint SIM=vcs ); \
	   usd_note="usd_premint=PASS"; \
	 fi; \
	 echo "$$usd_note" > $@
mint-preflight: $(BUILD)/preflight.stamp

# --- stage 2/8: the static shell ---------------------------------------------
# build_shell.tcl packages the CSR IP, assembles + VALIDATES shell_bd.tcl,
# synthesises shell_top -> shell_static_synth.dcp, then impls to a bitstream and
# re-exports the XSA. SHELL_TOUCH reaches it through the environment, which is
# the contract that file documents.
#
# If you are minting against a shell you already have (the usual case for an
# incremental add), point SHELL_PROJ at it and this stage is a no-op.
# mbv: the static is ALSO a function of the CPU seam (cpu_mbv.tcl, ddr4_mig.tcl,
# constraints/mbv/...). With only the two files above as prerequisites, an edit to
# cpu_mbv.tcl left a stale shell checkpoint "up to date" -- found when the P-mint's
# SEAM-8 fix did not rebuild the shell (2026-09-23). For mbv the prerequisites are
# every fpga/shell source static_canon declares. The mb path is left exactly as it
# was: a wider list there could rebuild, in place, a shell someone pointed
# SHELL_PROJ at in order to REUSE it (the incremental-add workflow above).
SHELL_DCP_MBV_PREREQS = $(if $(filter mbv,$(SHELL_CPU_EFF)),$(filter $(REPO_ROOT)/fpga/shell/%,$(STATIC_CANON_SRCS)))
$(SHELL_DCP): $(REPO_ROOT)/fpga/shell/build_shell.tcl \
              $(REPO_ROOT)/fpga/shell/bd/shell_bd.tcl \
              $$(SHELL_DCP_MBV_PREREQS) \
              | $(BUILD)/preflight.stamp
	@echo "== mint [2/8] static shell (validate BD -> synth -> impl -> bit + XSA) =="
	@echo "   in : fpga/shell/build_shell.tcl + fpga/shell/bd/shell_bd.tcl  SHELL_TOUCH=$(SHELL_TOUCH) $(SHELL_REALPHY_ENV)$(SHELL_CPU_ENV)"
	@echo "   out: $@ (+ $(SHELL_PROJ)/shell_harness.xsa)"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): SHELL_TOUCH=$(SHELL_TOUCH) $(SHELL_REALPHY_ENV) $(SHELL_CPU_ENV)$(VIVADO) -mode batch -source $(REPO_ROOT)/fpga/shell/build_shell.tcl -tclargs $(SHELL_PROJ)"; \
	   exit 0; \
	 fi; \
	 mkdir -p $(SHELL_PROJ) $(BUILD)/logs; \
	 env SHELL_TOUCH=$(SHELL_TOUCH) $(SHELL_REALPHY_ENV) $(SHELL_CPU_ENV)$(VIVADO) -mode batch \
	   -source $(REPO_ROOT)/fpga/shell/build_shell.tcl \
	   -journal $(BUILD)/logs/shell_build.jou -log $(BUILD)/logs/shell_build.log \
	   -tclargs $(SHELL_PROJ); \
	 test -f $@ || { echo "error: the shell rebuild did not produce $@ -- see $(BUILD)/logs/shell_build.log"; exit 1; }
mint-shell: $(SHELL_DCP)

mint-rms: $(MINT_RM_DCPS)

# --- stage 4/8: DFX prod — where the new static_id is born --------------------
# Link static + reference RM, ROUTE, black-box + lock the static
# (static_routed_locked.dcp), MINT static_id = CRC-32 of that locked .dcp, route
# every other RM, pr_verify, write the partial+clearing pairs.
#
# static_id.txt is the stage's declared output; overlay_inputs.txt,
# config_rm_greybox.bit and config_rm_greybox_routed.dcp come out of the same
# run and are asserted (not rebuilt) below.
$(PROD)/static_id.txt: $(SHELL_DCP) $(DFX_DIR)/build_dfx.tcl $(DFX_DIR)/rm_list.tcl \
                       $(DFX_DIR)/tools/overlay_inputs.tcl $(DFX_DIR)/tools/debug_probes.tcl \
                       $(DFX_DIR)/tools/dfx_jobs.tcl \
                       $(REPO_ROOT)/scripts/harness_gates/check_hdpr_reports.py \
                       $(MINT_RM_DCPS)
	@echo "== mint [4/8] DFX prod: route every RM against ONE locked static, MINT static_id =="
	@echo "   in : $(SHELL_DCP)  rm_set: $(RM_KEYS)"
	@echo "   out: $@ + $(PROD)/overlay_inputs.txt + $(PROD)/static_routed_locked.dcp + partial/clearing pairs"
	@echo "        + config_<rm_key>.ltx{,.json} for each RM declared debug 1: $(or $(filter $(DEBUG_RM_KEYS),$(RM_KEYS)),none in this RM_SET)"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): cd $(PROD) && $(DFX_CPU_ENV)$(DFX_STAGE4_VIVADO) -mode batch -source $(DFX_DIR)/build_dfx.tcl -tclargs $(REPO_ROOT) $(PROD) $(SHELL_DCP) '$(RM_KEYS)' $(RP_INST)"; \
	   echo "  (DRYRUN, not executed): .ltx: per config, write_debug_probes -force -cell $(RP_INST) $(PROD)/config_<rm_key>.ltx when its RP holds a debug core"; \
	   echo "  (DRYRUN, not executed): .ltx gate: declared debug 1 = {$(filter $(DEBUG_RM_KEYS),$(RM_KEYS))} MUST produce one, every other RM must NOT"; \
	   echo "  (DRYRUN, not executed): per config, in build_dfx.tcl: check_hdpr_reports.py on drc_hdpr_<rm>_prelink.rpt (before opt) + drc_<rm>.rpt (routed); RP pin count == boundary.yaml totals.bits after link AND after opt_design"; \
	   echo "  (DRYRUN, not executed): grep -E '^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED' $(PROD)/build.log -> fail"; \
	   echo "  (DRYRUN, not executed): $(LTX_GATE) --build-dir $(PROD) --rm-keys '$(RM_KEYS)'  -> config_<rm_key>.ltx.json (ILA uuids + ltx/partial crc32)"; \
	   echo "  (DRYRUN, not executed): $(STATIC_LTX_GATE) --build-dir $(PROD)  -> $(STATIC_LTX){,.json} iff the static holds debug cores ($(SHELL_CPU_EFF): $(if $(filter mbv,$(SHELL_CPU_EFF)),MIG hub + DDR4 slave REQUIRED,none ALLOWED))"; \
	   echo "  (DRYRUN, not executed): $(PYTHON) $(DFX_DIR)/tools/bit_identity.py $(PROD)/config_rm_greybox.bit  -> ONE USR_ACCESS word in EVERY SLR stream (or none: DFX_NO_VERSION_STAMP)"; \
	   exit 0; \
	 fi; \
	 mkdir -p $(PROD); \
	 cd $(PROD) && $(DFX_CPU_ENV)$(DFX_STAGE4_VIVADO) -mode batch \
	   -source $(DFX_DIR)/build_dfx.tcl \
	   -journal $(PROD)/build.jou -log $(PROD)/build.log \
	   -tclargs $(REPO_ROOT) $(PROD) $(SHELL_DCP) "$(RM_KEYS)" $(RP_INST); \
	 if grep -qE "^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" $(PROD)/build.log; then \
	   echo "error: a build_dfx.tcl gate FAILED -- .ltx / HDPR+DRC / RP pin count / XDC (Vivado exits 0 after a Tcl error; the marker is the proof):"; \
	   grep -E "^DFX_(LTX|HDPR|RP_PIN|XDC)_GATE_FAILED" $(PROD)/build.log | head -5; exit 1; fi; \
	 grep -q "DFX_BUILD_COMPLETE" $(PROD)/build.log || { echo "error: build_dfx.tcl never printed DFX_BUILD_COMPLETE -- see $(PROD)/build.log"; exit 1; }; \
	 $(LTX_GATE) --build-dir $(PROD) --rm-keys "$(RM_KEYS)" || { \
	   echo "error: the .ltx gate (tools/ltx_sidecar.py) refused this build -- DFX_LTX_GATE_FAILED above."; \
	   echo "       static_id.txt exists, so a re-run will not re-route; stage 5 runs the same gate"; \
	   echo "       (--check) and will keep refusing until the .ltx set is right."; exit 1; }; \
	 $(STATIC_LTX_GATE) --build-dir $(PROD) || { \
	   echo "error: the STATIC .ltx gate refused this build (DFX_LTX_GATE_FAILED static above):"; \
	   echo "       config_rm_greybox_static.ltx must exist iff the static holds debug cores."; exit 1; }; \
	 $(PYTHON) $(DFX_DIR)/tools/bit_identity.py $(PROD)/config_rm_greybox.bit || { \
	   echo "error: $(PROD)/config_rm_greybox.bit does not write ONE USR_ACCESS word in EVERY SLR stream (above;"; \
	   echo "       tools/bit_identity.py walks the packet stream). Stage 6 (stage0 identity / bake) refuses it too."; exit 1; }; \
	 echo "   MINTED static_id = $$(cat $@)"
mint-prod: $(PROD)/static_id.txt

# Co-products of stage 4. Asserted, never rebuilt: if the prod run is up to date
# and these are missing, the tree is damaged and a silent re-route would re-mint
# static_id and strand every fielded overlay.
#
# ORDER-ONLY on static_id.txt (2026-09-24, the P-mint). build_dfx.tcl writes
# config_rm_greybox_routed.dcp BEFORE it mints static_id.txt, so as a normal
# prerequisite the co-product was always "older than" static_id.txt: make ran
# this assert on every invocation, counted the routed dcp as remade, and every
# rule downstream of it (the MMI, then the flashable base) re-ran -- a re-bake,
# write_mem_info + updatemem, on every `make mint` or `mint-linux-image` of a
# finished tree. Order-only keeps the ordering (stage 4 first) and the assert
# (a missing co-product still errors) without that cascade.
$(PROD)/overlay_inputs.txt $(PROD)/static_routed_locked.dcp \
$(PROD)/config_rm_greybox_routed.dcp $(PROD)/config_rm_greybox.bit: | $(PROD)/static_id.txt
	@if [ "$(DRYRUN)" = "1" ]; then echo "  (DRYRUN: would assert exists: $@)"; exit 0; fi; \
	 test -f $@ || { echo "error: $@ is missing from a prod tree that otherwise looks finished."; \
	  echo "       Do NOT re-run 'make prod' to get it back: that re-mints static_id and strands"; \
	  echo "       every fielded overlay. Recover the pairs with fpga/dfx/tools/rekey_recover.sh."; exit 1; }

# --- stage 5/8: overlays — re-key every manifest to the new static_id ---------
$(OVERLAY_ROOT)/mps3_shell_static_id.c: $(PROD)/static_id.txt
	@echo "== mint [5/8] overlays: re-key every manifest + the firmware static_id seam =="
	@echo "   in : $(PROD)/static_id.txt + $(PROD)/overlay_inputs.txt + $(PROD)/config_rm_greybox.bit"
	@echo "   out: $(OVERLAY_ROOT)/<rm>/manifest.json (+ payloads, + <rm>.ltx for debug RMs, + static_usercode) + $@"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(MK) -C $(DFX_DIR) overlays BUILD=$(BUILD) OVERLAY_ROOT=$(OVERLAY_ROOT)"; \
	   echo "  (DRYRUN, not executed): $(MK) -C $(DFX_DIR) overlay-usercode BUILD=$(BUILD) OVERLAY_ROOT=$(OVERLAY_ROOT)"; \
	   echo "  (DRYRUN: overlays runs the .ltx gate --check first, then passes gen_manifest.py --ltx <prod>/config_<rm_key>.ltx --ltx-sidecar <...>.ltx.json for every RM that has one -> $(OVERLAY_ROOT)/<rm>/<rm>.ltx + manifest ltx/ltx_uuids)"; \
	   echo "  (this stage WRITES tracked files -- it is the re-key)"; \
	   exit 0; \
	 fi; \
	 $(MK) -C $(DFX_DIR) overlays BUILD=$(BUILD) OVERLAY_ROOT=$(OVERLAY_ROOT); \
	 $(MK) -C $(DFX_DIR) overlay-usercode BUILD=$(BUILD) OVERLAY_ROOT=$(OVERLAY_ROOT)
mint-overlays: $(OVERLAY_ROOT)/mps3_shell_static_id.c

# --- stage 6/8: firmware + updatemem -----------------------------------------
# The new static_id must reach the RUNNING shell firmware, or `ping.shell_id`
# still reports the old id and the pusher rejects every fresh overlay.
#   gen_greybox_blob.py (driven by the firmware Makefile) reads the re-keyed
#   overlay/greybox/manifest.json -> generated/greybox_blob.c -> ELF ->
#   updatemem bakes the ELF into the DFX greybox bitstream's MicroBlaze LMB.
#
# The four things that were each wrong once (fpga/dfx/tools/finish_rekey.sh is
# the proven recipe this follows):
#   1. ELF   = $(FW_WS)/shell_fw/shell_fw.elf -- what `make elf` really emits.
#   2. BIT   = the DFX config_rm_greybox.bit, NOT the non-DFX shell_harness.bit
#              (that one stubs the RP and cannot accept partials at all).
#   3. -proc = the FULL hierarchical path; a bare `microblaze_0` does not
#              resolve in the DFX config's hierarchy.
#   4. MMI   = from the greybox ROUTED dcp. The DFX config is a different
#              implementation from shell_harness, so their BRAM placements
#              differ and the wrong MMI corrupts the embed while exiting 0.
MB_PROC ?= u_shell/shell_bd_i/microblaze_0

# 2026-09-16: THREE THINGS THIS RECIPE LEARNED THE HARD WAY.
#   1. `make -C fpga/dfx mint BUILD=<dir>` EXPORTS that BUILD= to every sub-make,
#      and a command-line variable OVERRIDES the firmware Makefile's own
#      `BUILD := $(WS)/shell_fw`. So the firmware built fine -- into <dir>/ --
#      while `test -f $(FW_ELF)` looked at $(FW_WS)/shell_fw/, found the ELF of
#      the PREVIOUS bake sitting there (rc4.2, 2026-09-14, baked for 0xA8C1C535,
#      no USRACC read, no dutrx verb), and updatemem put THAT into the 2026-09
#      base bitstream. The sub-make now gets BUILD= explicitly, and the stale
#      file is removed BEFORE the build so a leftover can never satisfy `test -f`.
#   2. A bitstream cannot say which ELF is inside it, but the ELF can say which
#      fabric it is for: tools/elf_static_id.py reads the baked greybox
#      static_id and it MUST equal $(PROD)/static_id.txt.
#   3. PRODUCT=1 means "the firmware for the product fabric", so the ELF must
#      actually address every block that fabric carries. The mb-nm symbol
#      checks below cover CLCD/touch (separate source files); the DUTEGR and
#      USRACC handlers live inside coordinator.c and get inlined, so those are
#      witnessed by the disassembly referencing the block's base address
#      (`imm 0x44b2` / `imm 0x44b3` -- MicroBlaze loads a 32-bit address as an
#      imm of the upper half; objdump prints it in decimal).
#
# The BSP (findings #4). Order-only: an existing BSP is never regenerated here --
# the main checkout's build/vitis_fw is the one every fielded mint was built with.
# A MISSING one is created from THIS mint's XSA (stage 2 writes it beside the shell
# checkpoint), not from whichever shell project another checkout had.
$(FW_BSP_XPARAMS): | $(SHELL_DCP)
	@echo "== mint [6/8] BSP (missing: created from this mint's XSA) =="
	@echo "   in : $(SHELL_XSA)"
	@echo "   out: $(FW_BSP)"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then echo "  (DRYRUN, not executed): $(FW_BSP_CREATE)"; exit 0; fi; \
	 test -f $(SHELL_XSA) || { echo "error: no XSA at $(SHELL_XSA) -- stage 2 writes it beside $(SHELL_DCP)"; exit 1; }; \
	 mkdir -p $(BUILD)/logs; \
	 $(FW_BSP_CREATE) > $(BUILD)/logs/create_platform.log 2>&1 || { \
	   echo "error: create_platform.tcl failed -- see $(BUILD)/logs/create_platform.log"; exit 1; }; \
	 for f in $(FW_BSP_FILES); do test -f $$f || { \
	   echo "error: create_platform.tcl exited 0 but $$f is missing -- see $(BUILD)/logs/create_platform.log"; exit 1; }; done
$(FW_ELF): $(OVERLAY_ROOT)/mps3_shell_static_id.c $(FW_FLAGS_CHANGED) | $(FW_BSP_XPARAMS)
	@echo "== mint [6/8] firmware ($(FW_FLAGS_STR)) =="
	@echo "   in : firmware/platform + $(OVERLAY_ROOT)/greybox/manifest.json"
	@echo "   out: $@"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(MK) -C $(REPO_ROOT)/firmware/platform clean elf PRODUCT=1 TOUCH=$(TOUCH) WS=$(FW_WS)"; \
	   echo "  (DRYRUN: would then require clcd_init/clcd_poll$(if $(filter 1,$(TOUCH)), and touch_init/touch_poll,) in the ELF)"; \
	   exit 0; \
	 fi; \
	 rm -f $@; \
	 $(MK) -C $(REPO_ROOT)/firmware/platform clean WS=$(FW_WS) BUILD=$(FW_WS)/shell_fw; \
	 $(MK) -C $(REPO_ROOT)/firmware/platform elf PRODUCT=1 TOUCH=$(TOUCH) WS=$(FW_WS) BUILD=$(FW_WS)/shell_fw; \
	 test -f $@ || { echo "error: the firmware build produced no ELF at $@ (it built, but not HERE --"; \
	   echo "       see the 2026-09-16 note above this recipe)"; exit 1; }; \
	 $(PYTHON) $(DFX_DIR)/tools/elf_static_id.py $@ --expect $$(tr -d ' \n' < $(PROD)/static_id.txt) || { \
	   echo "error: the ELF at $@ is baked for a DIFFERENT static_id than $(PROD)/static_id.txt."; \
	   echo "       Nothing built this run is in it. Refusing to bake it."; exit 1; }; \
	 syms="$$( { . $(VITIS_SETTINGS) >/dev/null 2>&1 || true; $(MBNM) $@ 2>/dev/null; } )"; \
	 if [ -z "$$syms" ]; then \
	   echo "error: mb-nm produced NO symbols for $@. That is a Vitis env/tool problem, NOT"; \
	   echo "       necessarily a bad ELF -- verify with a sourced-Vitis mb-nm before assuming"; \
	   echo "       the drivers are absent. (This guard has false-fired in a nice'd background"; \
	   echo "       shell with the Vitis env unsourced.)"; exit 1; fi; \
	 printf '%s\n' "$$syms" | grep -qiE " clcd_init| clcd_poll" || { \
	   echo "error: the ELF lacks clcd_init/clcd_poll -- CLCD=1 did not take (stale objects?)."; exit 1; }; \
	 if [ "$(TOUCH)" = "1" ]; then \
	   printf '%s\n' "$$syms" | grep -qiE " touch_init| touch_poll" || { \
	     echo "error: TOUCH=1 was requested but the ELF lacks touch_init/touch_poll -- the fabric"; \
	     echo "       would carry the AXI IIC and the firmware would not carry its driver."; exit 1; }; \
	 fi; \
	 dis="$$($(MBOBJDUMP) -d $@ 2>/dev/null)"; \
	 for blk in "0x44b2 17586 DUTEGR dut_egress DUT_EGRESS=1" "0x44b3 17587 USRACC usr_access_rd (always in PRODUCT=1)"; do \
	   set -- $$blk; \
	   printf '%s\n' "$$dis" | grep -qE "imm[[:space:]]+($$1|$$2)" || { \
	     echo "error: PRODUCT=1 firmware never touches the $$3 block ($$1_0000, fpga/shell/ip/$$4): the"; \
	     echo "       fabric carries it and this ELF would answer its verb 'not present'. Half-mint;"; \
	     echo "       the knob is $$5 in firmware/platform/Makefile. Refusing to bake it."; exit 1; }; \
	 done; \
	 echo '$(FW_FLAGS_STR)' > $(FW_FLAGS_STAMP)
mint-firmware: $(FLASHABLE_BIT)

$(PROD)/config_rm_greybox.mmi: $(PROD)/config_rm_greybox_routed.dcp
	@echo "   [6/8] MMI from the greybox ROUTED checkpoint (not the shell project's)"
	@echo "   in : $<"
	@echo "   out: $@"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(VIVADO) -mode batch -source $(DFX_DIR)/tools/write_mem_info.tcl -tclargs $< $@"; \
	   exit 0; \
	 fi; \
	 mkdir -p $(BUILD)/logs; \
	 $(VIVADO) -mode batch -source $(DFX_DIR)/tools/write_mem_info.tcl \
	   -journal $(BUILD)/logs/write_mem_info.jou -log $(BUILD)/logs/write_mem_info.log \
	   -tclargs $< $@; \
	 test -f $@ || { echo "error: write_mem_info produced no MMI at $@"; exit 1; }

# The static acceptance gate on the BAKED image (firmware/platform/
# verify_shell_image.py): ELF static_id == the fabric's, MMI -proc resolves, the
# ELF fits the LMB, and -- the reason it joined the mint on 2026-09-23 -- the
# firmware serves XVC through the Debug Bridge. Every RM-internal ILA is reached
# over XVC -> debug_bridge_0 -> the RM's own hub; a `jtagbb` (Identify) image
# answers XVC on the bit-bang page instead and cannot see a single ILA (handover
# F11, §4.9.1). It needs --bit, so it runs here, after updatemem, beside the
# elf_static_id.py/disassembly guards of the ELF rule above; a FAIL deletes the
# output so make cannot treat a refused bake as done.
VERIFY_SHELL_IMAGE_CMD = $(PYTHON) $(REPO_ROOT)/firmware/platform/verify_shell_image.py \
    --bit $@ --base-bit $(PROD)/config_rm_greybox.bit --elf $(FW_ELF) \
    --meminfo $(PROD)/config_rm_greybox.mmi --expect-proc $(MB_PROC) \
    --expect-static-id $$(tr -d ' \n' 2>/dev/null < $(PROD)/static_id.txt) \
    --expect-xvc-target dbgbr --vitis $(VITIS_DIR)

$(PROD)/config_rm_greybox_fw.bit: $(FW_ELF) $(PROD)/config_rm_greybox.mmi $(PROD)/config_rm_greybox.bit
	@echo "   [6/8] updatemem: bake the ELF into the greybox base bitstream"
	@echo "   in : $(FW_ELF) + $(PROD)/config_rm_greybox.mmi + $(PROD)/config_rm_greybox.bit"
	@echo "   out: $@   <- THIS is the flashable base"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(UPDATEMEM) -force -meminfo ... -data $(FW_ELF) -proc $(MB_PROC) -bit ... -out $@"; \
	   echo "  (DRYRUN, not executed): $(VERIFY_SHELL_IMAGE_CMD)"; \
	   echo "  (DRYRUN: a FAIL there deletes $@ and fails the bake)"; \
	   exit 0; \
	 fi; \
	 $(UPDATEMEM) -force \
	   -meminfo $(PROD)/config_rm_greybox.mmi \
	   -data    $(FW_ELF) \
	   -proc    $(MB_PROC) \
	   -bit     $(PROD)/config_rm_greybox.bit \
	   -out     $@; \
	 test -f $@ || { echo "error: updatemem produced nothing at $@."; \
	   echo "       It EXITS 0 after printing its usage text when an input path is wrong, so the"; \
	   echo "       artefact is the only proof -- never trust its return code."; exit 1; }; \
	 $(VERIFY_SHELL_IMAGE_CMD) || { \
	   rm -f $@; \
	   echo "error: verify_shell_image.py refused the baked image (see its FAIL lines above) --"; \
	   echo "       most likely an XVC_TARGET other than the Debug Bridge (dbgbr): the RM ILAs"; \
	   echo "       are reachable ONLY through dbgbr (handover F11, §4.9). $@ removed so a"; \
	   echo "       re-run cannot mistake it for a finished bake."; exit 1; }; \
	 echo "   flashable base: $@ ($$(stat -c %s $@) bytes)"

# --- stage 7/8: the mint record ----------------------------------------------
# Everything the run knew and nothing it did not. See fpga/dfx/mint.json.example
# and docs/BUILD_AND_MINT.md "The mint record".
#
# static_canon.json is listed FIRST on purpose. make updates prerequisites left
# to right, so the source hash is taken BEFORE static_id.txt pulls the four-hour
# chain -- i.e. against the tree the build is about to read, not against
# whatever it has become by the time the record is written.
# mbv: no MicroBlaze firmware and no PRODUCT=1 set exist; the firmware slot
# records the stage0 ELF that was baked and the flags that say so.
MR_PRODUCT_ARG = $(if $(filter mbv,$(SHELL_CPU_EFF)),,--product-makefile $(REPO_ROOT)/firmware/platform/Makefile)
MR_FW_FLAGS    = $(if $(filter mbv,$(SHELL_CPU_EFF)),SHELL_CPU=mbv STAGE0=1 MINT_KIND=$(MINT_KIND),TOUCH=$(TOUCH))
MR_FW_ELF      = $(if $(filter mbv,$(SHELL_CPU_EFF)),$(STAGE0_ELF),$(FW_ELF))
$(PROD)/mint.json: $(STATIC_CANON_JSON) $(PROD)/static_id.txt $(FLASHABLE_BIT)
	@echo "== mint [7/8] mint record =="
	@echo "   in : $(PROD)/{static_id.txt,overlay_inputs.txt,static_stamp.json,*.dcp} + $(STATIC_CANON_JSON) + firmware flags + source revisions"
	@echo "   out: $@"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): write $@ from $(PROD)"; exit 0; \
	 fi; \
	 $(PYTHON) $(DFX_DIR)/tools/mint_record.py record \
	   --build-dir $(PROD) --repo $(REPO_ROOT) --shell-proj $(SHELL_PROJ) \
	   $(MR_PRODUCT_ARG) \
	   --fw-flags "$(MR_FW_FLAGS)" --fw-elf $(MR_FW_ELF) \
	   --rm-set "$(RM_KEYS)" \
	   --nanosoc-home $(SOCLABS_NANOSOC_SOC_DIR) --socscope-home $(SOCSCOPE_HOME) \
	   $(if $(REUSE_DCPS_FROM),--reuse-dcps-from $(REUSE_DCPS_FROM),) \
	   --static-canon $(STATIC_CANON_JSON) --static-stamp $(PROD)/static_stamp.json \
	   --static-bit $(PROD)/config_rm_greybox.bit \
	   --vivado-version $(VIVADO_VER) -o $@; \
	 $(PYTHON) $(DFX_DIR)/tools/mint_record.py verify $@
mint-record: $(PROD)/mint.json

# --- stage 8/8: hub copy ------------------------------------------------------
# static_routed_locked.dcp is the ONLY thing that can ever add an overlay to this
# shell, and it lives in a gitignored scratch tree. Before 2026-09-09 the fielded
# one existed in exactly one copy, on one workstation; a `make clean` would have
# closed that shell permanently. This stage is that copy, made by the flow
# instead of by remembering.
#
# NO DEFAULT DESTINATION. Unset MINT_HUB and this prints why it did nothing.
#
# THE .ltx SET (2026-09-23). An RM-internal ILA's probe file is as irreplaceable
# as its partial and useless without it: a re-route of the RM gives a different
# partial AND a different .ltx, and a stale pairing mislabels every probe. So
# for every config_<rm_key>.ltx (never the empty *_clear.ltx) the copy carries
# the .ltx, its .ltx.json sidecar (ILA uuids + both crc32s) and the partial/
# clearing .bin pair it was written beside. LTX_FILES_CMD is the ONE list: the
# fielding recipe (fielded/README.md) and `make fielded-files` read it too. It is
# a shell command, not $(wildcard): the .ltx files do not exist when make parses
# this file at the start of an overnight mint.
#
# Keyed on the SIDECARS, not an .ltx glob: Vivado drops extra .ltx files beside
# the one named (<f>_clear.ltx, <f>_<pblock>_partial*.ltx -- spike 2026-09-23),
# and only an .ltx the gate accepted has a sidecar.
# (mbv: the STATIC's sidecar also matches config_*.ltx.json but has no partial;
# it travels via FIELDED_EXTRA / MINT_HUB_EXTRA instead. The skip expands to
# nothing for mb, where the static .ltx cannot exist.)
LTX_SKIP_STATIC := case "$$s" in *_static.ltx.json) continue;; esac;
LTX_FILES_CMD = for s in $(PROD)/config_*.ltx.json; do \
      [ -f "$$s" ] || continue; $(if $(filter mbv,$(SHELL_CPU_EFF)),$(LTX_SKIP_STATIC) )l="$${s%.json}"; k="$${l%.ltx}"; \
      echo "$$l $$s $${k}_$(RP_PBLOCK)_partial.bin $${k}_$(RP_PBLOCK)_partial_clear.bin"; \
    done

# The fielding copy set, printed one path per line: what fielded/<static_id>/
# must hold (fielded/README.md builds MANIFEST.md5 from exactly this list). The
# stage-8 files, the two shell-project artefacts, static_stamp.json, and the
# .ltx set above -- so an .ltx can never be fielded without its partial.
.PHONY: fielded-files
# mbv adds the stage0 record + ELF and the Linux bundle (FLOW_CONTRACT.md §0),
# and REFUSES a prototype (P-mint) build: it is never fielded.
FIELDED_EXTRA = $(if $(filter mbv,$(SHELL_CPU_EFF)), stage0_bake.json stage0.elf static_canon.json linux_bundle.json linux_slot.img linux_legal_info.tar config_rm_greybox_static.ltx config_rm_greybox_static.ltx.json debug_core_rm_greybox_static.rpt)
fielded-files:
	$(if $(filter mbv,$(SHELL_CPU_EFF)),@$(PYTHON) $(DFX_DIR)/tools/stage0_bake.py fieldable --record $(PROD)/stage0_bake.json)
	@for f in mint.json static_id.txt overlay_inputs.txt static_stamp.json \
	          static_routed_locked.dcp config_rm_greybox_routed.dcp \
	          config_rm_greybox.mmi config_rm_greybox.bit $(FLASHABLE_NAME)$(FIELDED_EXTRA); do \
	   echo "$(PROD)/$$f"; done
	@echo "$(SHELL_PROJ)/shell_static_synth.dcp"
	@echo "$(SHELL_PROJ)/shell_harness.xsa"
	@$(LTX_FILES_CMD) | tr ' ' '\n'

MINT_HUB_EXTRA = $(if $(filter mbv,$(SHELL_CPU_EFF)), $(PROD)/stage0_bake.json $(PROD)/stage0.elf $(STATIC_LTX) $(STATIC_LTX).json $(PROD)/debug_core_rm_greybox_static.rpt)
# WHY A PER-STATIC SUBDIRECTORY (2026-09-23). This stage used to rsync into the
# flat MINT_HUB, so every mint overwrote the previous mint's files of the same
# name. The 0x72BB0A36 mint replaced the only off-host copy of the FIELDED
# 0x3F1A560F static_routed_locked.dcp that way (restored from the local build to
# MINT_HUB/0x3F1A560F/ the same evening). A locked static is the only thing that
# can ever add an overlay to the shell on the board; two mints must never share
# a destination. The static_id is read at run time from static_id.txt.
mint-hub-copy: $(PROD)/mint.json
	@echo "== mint [8/8] hub copy =="
	@echo "   in : $(PROD)/{mint.json,static_id.txt,overlay_inputs.txt,static_routed_locked.dcp,config_rm_greybox_routed.dcp,config_rm_greybox.mmi,$(FLASHABLE_NAME)}"
	@echo "        + every $(PROD)/config_<rm_key>.ltx, its .ltx.json sidecar and the partial/clearing .bin it describes"
	@echo "   out: $(if $(MINT_HUB),$(MINT_HUB)/<static_id>/,<nothing>)"
	$(if $(MINT_HUB),,@echo "   SKIP: MINT_HUB is not set, so there is nowhere to copy to. This stage has")
	$(if $(MINT_HUB),,@echo "         NO default destination on purpose -- a hostname baked into a Makefile")
	$(if $(MINT_HUB),,@echo "         outlives the machine. Set MINT_HUB in tools.env (see tools.env.example)")
	$(if $(MINT_HUB),,@echo "         or on the command line. UNTIL YOU DO, the locked static exists in ONE")
	$(if $(MINT_HUB),,@echo "         copy, in a gitignored directory: fpga/dfx/README.md 'Preserving a mint'.")
	$(if $(MINT_HUB),@echo "   copying to $(MINT_HUB)/<static_id>/ -- this only ever writes into ONE per-static subdirectory of MINT_HUB")
	$(if $(MINT_HUB),@if [ "$(DRYRUN)" = "1" ]; then echo "  (DRYRUN, not executed): rsync -> $(MINT_HUB)/<static_id>/  (per-static subdirectory; never the flat MINT_HUB) [+ for each $(PROD)/config_*.ltx.json sidecar: it + its .ltx + the partial pair it describes]"; exit 0; fi; \
	  sid="$$(tr -d ' \n' < $(PROD)/static_id.txt)"; \
	  echo "$$sid" | grep -qE '^0x[0-9A-Fa-f]{8}$$' || { echo "error: $(PROD)/static_id.txt does not hold a static_id: '$$sid'. Refusing to copy."; exit 1; }; \
	  ltxset="$$($(LTX_FILES_CMD))"; \
	  [ -z "$$ltxset" ] || echo "   + .ltx set (an .ltx travels with its partial): $$ltxset"; \
	  rsync -av --checksum \
	    $(PROD)/mint.json $(PROD)/static_id.txt $(PROD)/overlay_inputs.txt \
	    $(PROD)/static_routed_locked.dcp $(PROD)/config_rm_greybox_routed.dcp \
	    $(PROD)/config_rm_greybox.mmi $(FLASHABLE_BIT)$(MINT_HUB_EXTRA) \
	    $(wildcard $(PROD)/static_stamp.json $(SHELL_PROJ)/shell_static_synth.dcp $(SHELL_PROJ)/shell_harness.xsa) \
	    $$ltxset \
	    "$(MINT_HUB)/$$sid/")


# =============================================================================
# THE MICROBLAZE V (LINUX) MINT -- SHELL_CPU=mbv  (FLOW lane, 2026-09-23)
# =============================================================================
# Stages 1-5 and 7-8 are the SAME targets as above; with SHELL_CPU=mbv they run
# under 2026.1, the static carries the MBV + DDR4 CPU subsystem, and stage 6 is
# the stage0 bake below instead of the MicroBlaze firmware bake. A Linux mint
# then ends with mint-linux-image (the release bundle). Contract for the other
# lanes and the lead: docs/planning/linux_lanes/FLOW_CONTRACT.md.
#
#   make -C fpga/dfx mint SHELL_CPU=mbv VIVADO=2026.1 BUILD=<dir> SHELL_TOUCH=1
#   make -s -C fpga/dfx linux-image-env SHELL_CPU=mbv BUILD=<dir>   # -> IMAGE's build
#   make -C fpga/dfx mint-linux-image SHELL_CPU=mbv BUILD=<dir> [LINUX_ARTIFACTS=<dir>]
#   make -C fpga/dfx pmint            # the prototype (P-mint, plan §4): never fielded

# --- stage 6 (mbv): stage0, built FOR this static ------------------------------
# stage0 is compiled with the static's static_id and HARNESS_VER32 as constants
# (2026-09-23 safety review): harnessd reports THAT fabric value, so the board's
# identity travels with the bitstream (updatemem binds it) and not with whatever
# card is in the slot. The ELF is therefore a per-static artefact: it is built
# after static_id is minted and kept as $(PROD)/stage0.elf, never shared.
#
# STAGE0_CONTRACT.md §2 names the build, and the defaults below are it
# (overridable without editing this file):
#   make -C $(STAGE0_DIR) $(STAGE0_GOAL) $(STAGE0_ID_VAR)=0x... $(STAGE0_VER_VAR)=0x...
#   -> $(STAGE0_OUT), carrying the u32 symbols $(STAGE0_SYM_STATIC_ID) / $(STAGE0_SYM_VER32).
# The sub-make runs with MAKEFLAGS cleared: a command-line BUILD=/VIVADO=/SHELL_CPU=
# from this make must not reach it (the 2026-09-16 stale-ELF lesson, stage 6 above),
# and `clean` goes first because make cannot see a changed -D.
STAGE0_DIR           ?= $(REPO_ROOT)/src/linux_soc/hw/fw_stage0
STAGE0_GOAL          ?= stage0.elf
STAGE0_OUT           ?= $(STAGE0_DIR)/stage0.elf
STAGE0_ID_VAR        ?= MPS3_STATIC_ID
STAGE0_VER_VAR       ?= MPS3_HARNESS_VER32
STAGE0_SYM_STATIC_ID ?= mps3_stage0_static_id
STAGE0_SYM_VER32     ?= mps3_stage0_ver32
# plan §3 LMB map: stage0 image + stack below the status block at 0x1FE00
STAGE0_LMB_TOP       ?= 0x1FE00
# updatemem is the sibling of the vivado binary in BOTH installs; the Vitis
# path stage 6 uses for the MicroBlaze bake does not exist for 2026.1.
STAGE0_UPDATEMEM     ?= $(dir $(VIVADO))updatemem
# empty = the ONE processor in the MMI whose LMB is 128 KiB at 0x0 (the MBV MMI
# also lists the DDR4 MIG's calibration MCS -- tools/stage0_bake.py `proc`)
MBV_PROC             ?=
# mint | prototype. A prototype (P-mint) is recorded as such and never fielded.
MINT_KIND            ?= mint
STAGE0_ELF            = $(PROD)/stage0.elf
STAGE0_BIT            = $(PROD)/config_rm_greybox_stage0.bit
STAGE0_BAKE           = $(PYTHON) $(DFX_DIR)/tools/stage0_bake.py
STAGE0_SYMS           = --sym-static-id $(STAGE0_SYM_STATIC_ID) --sym-ver32 $(STAGE0_SYM_VER32)
STAGE0_SRCS           = $(wildcard $(STAGE0_DIR)/*.c $(STAGE0_DIR)/*.h $(STAGE0_DIR)/*.S $(STAGE0_DIR)/*.ld $(STAGE0_DIR)/Makefile)

.PHONY: mint-stage0 mint-linux-image linux-image-env linux-bundle-check pmint pmint-plan

$(STAGE0_ELF): $(PROD)/static_id.txt $(PROD)/config_rm_greybox.bit $$(STAGE0_SRCS)
	@echo "== mint [6/8] stage0: build it FOR this static (static_id + HARNESS_VER32 compiled in) =="
	@echo "   in : $(STAGE0_DIR) + $(PROD)/static_id.txt + the USR_ACCESS word of $(PROD)/config_rm_greybox.bit"
	@echo "   out: $@"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(STAGE0_BAKE) identity -> <static_id> <ver32>"; \
	   echo "  (DRYRUN, not executed): env -u MAKEFLAGS $(MK) -C $(STAGE0_DIR) clean $(STAGE0_GOAL) $(STAGE0_ID_VAR)=<static_id> $(STAGE0_VER_VAR)=<ver32>; cp $(STAGE0_OUT) $@"; \
	   echo "  (DRYRUN, not executed): $(STAGE0_BAKE) elf --elf $@ --expect-static-id <static_id> --expect-ver32 <ver32>"; \
	   exit 0; \
	 fi; \
	 ids=$$($(STAGE0_BAKE) identity --bit $(PROD)/config_rm_greybox.bit --static-id-file $(PROD)/static_id.txt) || { \
	   echo "error: stage0_bake gave no single static_id + HARNESS_VER32 for this static (above) -- NOT building stage0 with empty values"; exit 1; }; \
	 set -- $$ids; sid="$${1:-}"; v32="$${2:-}"; \
	 { [ $$# -eq 2 ] && echo "$$sid" | grep -qE '^0x[0-9A-F]{8}$$' && echo "$$v32" | grep -qE '^0x[0-9A-F]{8}$$'; } || { \
	   echo "error: stage0_bake identity printed '$$ids', not '<static_id> <ver32>' -- NOT building stage0"; exit 1; }; \
	 echo "   building stage0 for static_id $$sid, HARNESS_VER32 $$v32"; \
	 rm -f $@ $(STAGE0_OUT); \
	 env -u MAKEFLAGS -u MFLAGS $(MK) -C $(STAGE0_DIR) clean; \
	 env -u MAKEFLAGS -u MFLAGS $(MK) -C $(STAGE0_DIR) $(STAGE0_GOAL) $(STAGE0_ID_VAR)=$$sid $(STAGE0_VER_VAR)=$$v32; \
	 test -f $(STAGE0_OUT) || { echo "error: the stage0 build produced no $(STAGE0_OUT) (STAGE0_CONTRACT.md: STAGE0_GOAL/STAGE0_OUT)"; exit 1; }; \
	 cp -f $(STAGE0_OUT) $@; \
	 $(STAGE0_BAKE) elf --elf $@ --lmb-top $(STAGE0_LMB_TOP) $(STAGE0_SYMS) \
	   --expect-static-id $$sid --expect-ver32 $$v32 || { rm -f $@; \
	   echo "error: refusing to bake a stage0 that is not provably FOR static_id $$sid (see STAGE0 ELF REFUSED above)"; exit 1; }

# The re-keyed overlay set (stage 5) is a prerequisite here for the same reason
# it is one of the MicroBlaze firmware's: stage 6 comes after stage 5, and a
# Linux mint's swap set must be keyed to the static it ships with.
$(STAGE0_BIT): $(OVERLAY_ROOT)/mps3_shell_static_id.c $(STAGE0_ELF) $(PROD)/config_rm_greybox.mmi $(PROD)/config_rm_greybox.bit
	@echo "   [6/8] updatemem: bake stage0 into the MicroBlaze V LMB of the greybox base"
	@echo "   in : $(STAGE0_ELF) + $(PROD)/config_rm_greybox.mmi + $(PROD)/config_rm_greybox.bit"
	@echo "   out: $@ + $(PROD)/stage0_bake.json   <- THIS is the flashable base (the mcc_sd target)"
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(STAGE0_UPDATEMEM) -force -meminfo $(PROD)/config_rm_greybox.mmi -data $(STAGE0_ELF) -proc $(or $(MBV_PROC),<the 128 KiB-LMB processor in the MMI>) -bit $(PROD)/config_rm_greybox.bit -out $@"; \
	   echo "  (DRYRUN, not executed): $(STAGE0_BAKE) bit (UserID/USR_ACCESS preserved, not a no-op, static_id re-derived, stage0 built FOR this static) -> $(PROD)/stage0_bake.json"; \
	   exit 0; \
	 fi; \
	 proc="$(MBV_PROC)"; \
	 [ -n "$$proc" ] || proc=$$($(STAGE0_BAKE) proc --mmi $(PROD)/config_rm_greybox.mmi); \
	 echo "   -proc $$proc"; \
	 $(STAGE0_UPDATEMEM) -force \
	   -meminfo $(PROD)/config_rm_greybox.mmi \
	   -data    $(STAGE0_ELF) \
	   -proc    $$proc \
	   -bit     $(PROD)/config_rm_greybox.bit \
	   -out     $@; \
	 test -f $@ || { echo "error: updatemem produced nothing at $@ (it EXITS 0 after printing its usage text on a bad path)."; exit 1; }; \
	 $(STAGE0_BAKE) bit --base $(PROD)/config_rm_greybox.bit --baked $@ \
	   --elf $(STAGE0_ELF) --proc "$$proc" $(STAGE0_SYMS) --lmb-top $(STAGE0_LMB_TOP) \
	   --static-id-file $(PROD)/static_id.txt --locked-dcp $(PROD)/static_routed_locked.dcp \
	   --stamp-json $(PROD)/static_stamp.json --mint-kind $(MINT_KIND) \
	   --static-ltx-sidecar $(STATIC_LTX).json \
	   --record $(PROD)/stage0_bake.json || { rm -f $@ $(PROD)/stage0_bake.json; \
	   echo "error: the baked base was refused (STAGE0 BAKE REFUSED above); $@ removed so a re-run cannot mistake it for done."; exit 1; }; \
	 echo "   flashable base: $@ ($$(stat -c %s $@) bytes)"
mint-stage0: $(STAGE0_BIT)

# --- the static's identity, for IMAGE's build ----------------------------------
# The slot image is provisioned FOR a static (MPS3_STATIC_ID in the rootfs,
# IMAGE_CONTRACT.md §2) and records which stage0 it expects (MPS3_STAGE0_SHA256,
# §6). This prints exactly those, from the minted tree itself, for
#   eval "$$(make -s -C fpga/dfx linux-image-env SHELL_CPU=mbv BUILD=<dir>)"
#   src/linux_harness/sw/build.sh        # IMAGE's one entry point
# MPS3_IMAGE_KIND=release: a `lab` image may bake a host key and is refused by
# mint-linux-image for anything but a prototype.
#
# MPS3_GREYBOX_CLEAR (2026-09-24): the static's greybox CLEARING bitstream. The
# image carries it at /etc/mps3/greybox_clear.bin, or harnessd fails closed on the
# first swap away from the greybox (bare metal bakes the same bytes into its ELF
# via firmware/platform/gen_greybox_blob.py, from $(OVERLAY_ROOT)/greybox/
# greybox_clear.bin). The file exported is the static's own prod file; when the
# re-keyed overlay copy exists it must be byte-identical, or the plan stops.
GREYBOX_CLEAR = $(PROD)/config_rm_greybox_$(RP_PBLOCK)_partial_clear.bin
linux-image-env:
	@test -f $(PROD)/static_id.txt || { echo "error: no $(PROD)/static_id.txt -- mint first" >&2; exit 1; }
	@test -f $(STAGE0_ELF) || { echo "error: no $(STAGE0_ELF) -- run mint-stage0 first" >&2; exit 1; }
	@test -f $(GREYBOX_CLEAR) || { echo "error: no $(GREYBOX_CLEAR) -- the greybox clearing is a stage-4 output" >&2; exit 1; }
	@if [ -f $(OVERLAY_ROOT)/greybox/greybox_clear.bin ] && ! cmp -s $(GREYBOX_CLEAR) $(OVERLAY_ROOT)/greybox/greybox_clear.bin; then \
	   echo "error: $(OVERLAY_ROOT)/greybox/greybox_clear.bin differs from $(GREYBOX_CLEAR) -- the overlay set is not keyed to this static" >&2; exit 1; fi
	@ids=$$($(STAGE0_BAKE) identity --bit $(PROD)/config_rm_greybox.bit --static-id-file $(PROD)/static_id.txt) || { \
	   echo "error: no single static_id + HARNESS_VER32 for this static (above) -- no environment printed" >&2; exit 1; }; \
	 set -- $$ids; { [ $$# -eq 2 ] && echo "$$1 $$2" | grep -qE '^0x[0-9A-F]{8} 0x[0-9A-F]{8}$$'; } || { \
	   echo "error: stage0_bake identity printed '$$ids', not '<static_id> <ver32>'" >&2; exit 1; }; \
	 echo "export MPS3_STATIC_ID=$$1 MPS3_HARNESS_VER32=$$2 MPS3_IMAGE_KIND=release MPS3_STAGE0_SHA256=$$(sha256sum < $(STAGE0_ELF) | cut -c1-64) MPS3_GREYBOX_CLEAR=$(GREYBOX_CLEAR)"

# --- stage 9 (mbv): the Linux release bundle -----------------------------------
# prod/linux_bundle.json + copies beside the static: the slot image, legal-info,
# static_canon.json. Two targets, one static -- FLOW_CONTRACT.md §0 and
# tools/linux_bundle.py. Defaults = IMAGE_CONTRACT.md §1.1's artefacts dir:
# the raw 1-region FW_PAYLOAD (wrapped into the S0LB slot image here, with
# STAGE0's stage0_pack.py), its `version` record, SHA256SUMS and legal-info/.
LINUX_ARTIFACTS  ?= $(REPO_ROOT)/src/linux_harness/sw/artifacts
LINUX_BLOB       ?= $(LINUX_ARTIFACTS)/fw_payload_1region.bin
LINUX_BLOB_INFO  ?= $(LINUX_ARTIFACTS)/version
LINUX_SHA256SUMS ?= $(LINUX_ARTIFACTS)/SHA256SUMS
LINUX_LEGAL_INFO ?= $(LINUX_ARTIFACTS)/legal-info
LINUX_BUNDLE      = $(PROD)/linux_bundle.json
LINUX_BUNDLE_TOOL = $(PYTHON) $(DFX_DIR)/tools/linux_bundle.py
# POST-MINT PACKAGING DEPENDS ON NOTHING MAKE COULD REBUILD (2026-09-24, found by
# IMAGE on the P-mint). This target used to name $(STAGE0_BIT) and
# $(STATIC_CANON_JSON) as prerequisites. The first dragged in the whole mint chain
# back to the shell checkpoint, so with shell sources newer than the mint, make
# planned build_shell.tcl + RM OOC synths (14 Vivado runs; one started re-
# generating the P-mint's shell_proj). The second RECOMPUTED static_canon from the
# current tree and this invocation's flags (no SHELL_TOUCH=1) -- a canon that is
# not the mint's. Now the recipe only ASSERTS a finished mint tree and packages it,
# with the canon the mint RECORDED (prod/static_canon.json, else the build dir's
# copy the mint took before it built), cross-checked by linux_bundle.py against
# the digest in prod/mint.json. Rule for every post-mint target: read prod/, build
# nothing, recompute nothing.
LINUX_BUNDLE_NEEDS = $(STAGE0_BIT) $(PROD)/stage0_bake.json $(PROD)/static_id.txt \
                     $(PROD)/mint.json $(STATIC_LTX) $(STATIC_LTX).json
mint-linux-image:
	@echo "== mint [9] Linux release bundle: slot image + overlays + legal-info, held to ONE static =="
	@echo "   in : a FINISHED mint in $(PROD) (never rebuilt here) + $(OVERLAY_ROOT)/*/manifest.json"
	@echo "        + $(LINUX_BLOB) + $(LINUX_BLOB_INFO) + $(LINUX_LEGAL_INFO)"
	@echo "   out: $(LINUX_BUNDLE) + $(PROD)/{linux_slot.img,linux_legal_info.tar}"
	@test "$(SHELL_CPU_EFF)" = mbv || { echo "error: mint-linux-image is the SHELL_CPU=mbv flow; this make has SHELL_CPU=$(SHELL_CPU_EFF)"; exit 1; }
	@for f in $(LINUX_BUNDLE_NEEDS); do test -f $$f || { \
	   echo "error: $$f is missing -- mint-linux-image PACKAGES a finished mbv mint and never builds one:"; \
	   echo "       run the mint (make -C fpga/dfx mint SHELL_CPU=mbv BUILD=$(BUILD) ...) first."; exit 1; }; done
	@test -f "$(LINUX_BLOB)" -a -f "$(LINUX_BLOB_INFO)" || { echo "error: no $(LINUX_BLOB) + $(LINUX_BLOB_INFO) -- IMAGE's build (IMAGE_CONTRACT.md §1), provisioned FOR this static:"; \
	  echo "       eval \"\$$(make -s -C fpga/dfx linux-image-env SHELL_CPU=mbv BUILD=$(BUILD))\" && src/linux_harness/sw/build.sh"; exit 1; }
	@set -e; \
	 if [ "$(DRYRUN)" = "1" ]; then \
	   echo "  (DRYRUN, not executed): $(LINUX_BUNDLE_TOOL) pack --prod $(PROD) --static-canon <the mint's recorded canon> --mint-record $(PROD)/mint.json ... -> $(LINUX_BUNDLE) + the gate on it"; \
	   exit 0; \
	 fi; \
	 canon=$(PROD)/static_canon.json; \
	 if [ ! -f "$$canon" ]; then \
	   test -f $(STATIC_CANON_JSON) || { echo "error: no recorded static_canon ($$canon or $(STATIC_CANON_JSON)) -- it is never recomputed here"; exit 1; }; \
	   cp -p $(STATIC_CANON_JSON) "$$canon"; \
	   echo "   static_canon: the mint's record $(STATIC_CANON_JSON) -> $$canon (checked against mint.json)"; \
	 fi; \
	 $(LINUX_BUNDLE_TOOL) pack --prod $(PROD) --static-canon "$$canon" --mint-record $(PROD)/mint.json \
	   --blob $(LINUX_BLOB) --blob-info "$(LINUX_BLOB_INFO)" --sha256sums "$(LINUX_SHA256SUMS)" \
	   --legal-info "$(LINUX_LEGAL_INFO)" \
	   --overlay-root $(OVERLAY_ROOT) --mint-kind $(MINT_KIND) || { rm -f $(LINUX_BUNDLE); \
	   echo "error: the bundle was refused (LINUX BUNDLE REFUSED / FAIL linux record above)."; exit 1; }
	$(if $(MINT_HUB),@if [ "$(DRYRUN)" = "1" ] || [ "$(MINT_KIND)" != mint ]; then echo "   hub copy skipped (DRYRUN or MINT_KIND=$(MINT_KIND))"; exit 0; fi; \
	  sid="$$(tr -d ' \n' < $(PROD)/static_id.txt)"; \
	  echo "$$sid" | grep -qE '^0x[0-9A-Fa-f]{8}$$' || { echo "error: $(PROD)/static_id.txt does not hold a static_id: '$$sid'. Refusing to copy."; exit 1; }; \
	  rsync -av --checksum $(LINUX_BUNDLE) $(PROD)/linux_slot.img $(PROD)/static_canon.json \
	    $$(ls $(PROD)/linux_legal_info.tar 2>/dev/null) "$(MINT_HUB)/$$sid/")

# The one gate over a finished bundle (scripts/harness_gates/check_image_overlay_match.py
# --linux-record): image <-> static <-> overlays, from the files, board-free.
linux-bundle-check:
	@$(LINUX_BUNDLE_TOOL) check $(PROD)

# --- the prototype mint (P-mint, LINUX_HARNESS_PLAN §4 "Prototype mint + B1") ---
# Four RMs, stage0 baked, 2026.1, MINT_KIND=prototype: never fielded (fielded-files
# and the gate refuse it), no hub copy. Run it from a DETACHED worktree after the
# ILA mint and W1 are done; ~4-5 h. `make pmint-plan` prints its plan (make -n).
PMINT_RMS   ?= greybox led dbg_demo nanosoc
PMINT_BUILD ?= $(DFX_DIR)/build_pmint_mbv
PMINT_ARGS   = mint SHELL_CPU=mbv VIVADO=$(MBV_VIVADO_MIN) MINT_RMS="$(PMINT_RMS)" \
               MINT_KIND=prototype BUILD=$(PMINT_BUILD) SHELL_PROJ=$(PMINT_BUILD)/shell_proj \
               SHELL_TOUCH=1 TOUCH=1 MINT_HUB=
pmint:
	$(MK) -C $(DFX_DIR) $(PMINT_ARGS)
pmint-plan:
	$(MK) -n -C $(DFX_DIR) $(PMINT_ARGS)

# --- the entry point ----------------------------------------------------------
mint: mint-hub-copy
	@echo "=============================================================="
	@echo "MINT COMPLETE$(if $(filter 1,$(DRYRUN)),  -- DRYRUN: nothing was built)"
	@echo "  static_id  : $$(cat $(PROD)/static_id.txt 2>/dev/null || echo '<not built>')"
	@echo "  record     : $(PROD)/mint.json"
	@echo "  flashable  : $(FLASHABLE_BIT)"
	@echo "  overlays   : $(OVERLAY_ROOT)"
	@echo "NEXT (docs/BUILD_AND_MINT.md 'After the mint'):"
	@echo "  1. PRESERVE the locked static: fielded/<static_id>/ + a copy off this box."
	@echo "  2. Flash the base + re-push the re-keyed overlays."
	@echo "  3. Update docs/FIELDED_SHELL.md 'fielded' ONLY after watching the board"
	@echo "     report the new id. An sd_install timeout is not evidence."
	$(if $(filter mbv,$(SHELL_CPU_EFF)),@echo "  mbv: build the slot image FOR this static (make -s -C fpga/dfx linux-image-env SHELL_CPU=mbv BUILD=$(BUILD))")
	$(if $(filter mbv,$(SHELL_CPU_EFF)),@echo "       then make -C fpga/dfx mint-linux-image SHELL_CPU=mbv BUILD=$(BUILD) [LINUX_ARTIFACTS=<dir>]")
	@echo "=============================================================="
