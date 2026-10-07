#-----------------------------------------------------------------------------
# fpga/design.mk  --  THE ONE FILE THE FLOW READS FIRST
#
# Scaffolded into mps3-nanosoc-platform by `fpga-flow-init` on 2026-09-23.
#
# Everything below is YOURS. Nothing in $(FPGA_FLOW_DIR) is. If you find
# yourself editing the toolkit to make your design build, that is a toolkit bug
# -- raise it, do not fork it. The one exception is the two extension seams at
# the bottom of this file, which exist precisely so you never have to.
#
# HOW TO USE THIS FILE
#   1. Fill in every FILL-IN marker -- the ones in double angle brackets, the
#      way the assignments below spell them. `make check` names any you miss,
#      with the variable, the path it looked for, and what the file is for. A
#      fresh scaffold FAILS `make check` on purpose.
#
#      THIS SENTENCE DOES NOT SPELL THE MARKER OUT, and that is deliberate. It
#      used to, and the instruction was then counted as one of the decisions it
#      was telling you to make: a project that had made every real decision
#      still got `UNFILLED PLACEHOLDERS  1 marker(s) in 1 file(s)` pointing at
#      this comment, and the only way to clear it was to delete the line that
#      tells you what to do. The marker scan reads FILE CONTENT and has no idea
#      what a comment is -- which is exactly why it catches a marker sitting in
#      a Tcl string or an XDC argument, where no path check would ever look. The
#      scan is right; this sentence was wrong, and it is the sentence that moved.
#   2. Leave the `?=` assignments alone unless your project already has a
#      different layout. They are conventions, not requirements -- every path
#      the flow uses is overridable from here, so an existing tree does not
#      have to be moved to adopt this flow.
#   3. DELETE what you do not use. A variable set to something plausible and
#      never exercised is worse than an absent one: `make check` will assert
#      the file it names exists, and you will fix a path to a file nothing
#      reads.
#   4. `make help` lists targets. `make check` costs no licence hours and
#      should pass before you spend any.
#
# THE READING ORDER OF THIS FILE IS THE ORDER MAKE READS IT. A `:=` is expanded
# the moment make reaches the line, so anything it references must ALREADY be
# defined ABOVE it. Referencing something defined further down does not error --
# it expands to empty, and `$(MY_ROOT)/rtl/top.flist` quietly becomes
# `/rtl/top.flist`, an absolute path that looks plausible and does not exist.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
#-----------------------------------------------------------------------------


# ── 1. REQUIRED: the three the engine will not start without ────────────────
#
# These three are a hard `$(error)` at make PARSE time, before any target runs,
# because every path in this file and every path in the engine is built from
# them. An empty one produces a tree of plausible-looking wrong paths rather
# than a message.

# BLOCK is the design's short name and the STEM OF EVERY ARTEFACT the flow
# writes: $(BLOCK).bit, $(BLOCK).bin, $(BLOCK)_synth.dcp, $(BLOCK)_routed.dcp.
# It is not required to equal TOP -- see the warning under TOP -- but on most
# designs it does, and where it does not, a reader will assume it does unless
# you say why here.
BLOCK := nanosoc_mps3

# BOARD selects the board pack at $(BOARD_DIR)/board.tcl. That pack is a
# PROJECT file, not a toolkit file: which pin, which IO standard and what the
# oscillator runs at are facts about a circuit board, and this toolkit has no
# business holding them. `make board-probe` loads and validates it without a
# licence.
BOARD := mps3-hbi0309c

# FPGA_FLOW_DIR is where you cloned or submoduled this toolkit. `fpga-flow-init`
# wrote a RELATIVE path if the toolkit sits inside this project, because a
# relative path survives the tree being moved, cloned, or checked out by CI
# under a different root. An absolute path is a path that works on one machine.
#
# A PINNED SUBMODULE, like every other input this build reads (see "THE THREE
# PINNED INPUTS" further down). It was an absolute path to one machine's
# checkout. PROBED ON mk/flow.mk, NOT ON THE DIRECTORY: an uninitialised
# submodule is an EMPTY DIRECTORY that passes every `test -d`, and including
# from it fails as a make syntax error rather than saying what to run.
FPGA_FLOW_DIR ?= $(FPGA_DIR)/fpga-toolkit
ifeq ($(wildcard $(FPGA_FLOW_DIR)/mk/flow.mk),)
  $(error no toolkit engine at $(FPGA_FLOW_DIR)/mk/flow.mk - run: git submodule update --init --recursive)
endif


# ── 2. REQUIRED: reported by `make check`, not by make ──────────────────────
#
# These four are not parse-time errors because a half-filled scaffold should
# still be able to run `make check`, `make env` and `make help` and be TOLD what
# is missing. A parse error here would mean the one command that explains the
# problem is the one command that cannot run.

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: TOP IS THE BOARD-LEVEL TOP, NOT THE SoC TOP.
#
#   The module that instantiates your SoC and wires it to the BOARD: the
#   clock buffer or MMCM, the reset conditioning, the IO buffers, the debug
#   bridge, the pins that only exist because there is a PCB underneath.
#
#   GETTING THIS WRONG IS QUIET. Naming the SoC top synthesises happily. The
#   run completes. The bitstream builds. What you discover afterwards is that
#   the constraints matched nothing (the ports they name are one level up and
#   no longer exist), that Vivado inferred IO buffers for internal signals,
#   and that the design on the board is not the design you drew.
#
#   If your board-level top is a block design, this is the BD's wrapper
#   module -- typically $(DESIGN_NAME)_wrapper -- and not the BD itself.
# ═══════════════════════════════════════════════════════════════════════════
TOP := nanosoc_mps3_top

# The master flist: the flow's only route into your RTL. Absolute, or built
# from a variable defined ABOVE this line -- see the reading-order note in the
# header. A relative flist works until someone runs make from another directory.
#
# If your flist is GENERATED, do not commit a stale copy: set RTL_FLIST_GEN in
# section 4 to the make target that rebuilds it, and the flow runs that first.
# GENERATED. fpga/monolithic/filelist.tcl is the source of truth - a Tcl
# program of read_verilog calls, three of them [glob] - and gen_flist.tcl runs
# it under recording stubs to write down what it actually asked for. A committed
# copy would freeze: the README's own count (198) was already seven files short
# of what filelist.tcl asks for today, because the two nanosoc_*_ahb_interconnect
# globs point into regenerated RTL under build_soc/.
RTL_FLIST := $(FPGA_DIR)/monolithic/generated/nanosoc_mps3.flist

# Pin and placement constraints. See section 6; this line is here because
# `make check` treats it as required and it belongs beside the other three.
# THE EXISTING, HARDWARE-CHECKED CONSTRAINTS, NOT THE SCAFFOLD'S EMPTY ONES.
# fpga/monolithic/nanosoc_mps3.xdc is one file carrying pins AND timing - 272
# PACKAGE_PIN assignments, every one checked against the real
# xcku115-flvb1760-1-c package database in Vivado 2024.1 - and it is the file
# the 2026-07-04 build used (build_results_2026-07-04/RESULT.txt: 7.87 MB
# bitstream, timing MET, WNS +4.128 ns, 0 failing).
#
# IT IS POINTED AT XDC_PINS, WHICH IS READ AT SYNTHESIS AND AT IMPLEMENTATION.
# That is deliberate and preserves the existing behaviour exactly: the July
# build added this file to the project, so Vivado read it at both. Splitting it
# into pins/timing would move the clock constraints to implementation only -
# the better shape AND a change to what gets built, so it needs its own
# before/after run, not a rename done in passing.
XDC_PINS ?= $(FPGA_DIR)/monolithic/nanosoc_mps3.xdc

# The device. NORMALLY THE BOARD PACK SAYS THIS -- a part is a consequence of
# which board you are building for -- and a project override here wins over it.
# Override when you are building for a board that ships in more than one device
# variant, or when you are speed-grade-sweeping. Otherwise delete this line and
# let board.tcl be the single place the device is named.
# SET HERE, AND THE TEMPLATE SAYS IT SHOULD NOT HAVE TO BE.
#
# templates/design.mk.in offers: "Otherwise delete this line and let board.tcl
# be the single place the device is named." Deleting it does not work.
# Measured 2026-09-23 while onboarding this board: with the line gone,
# `make check` reports
#     1. PART   looked for : (nothing configured)
# and refuses. Nothing under the toolkit's mk/ parses board.tcl - the pack is
# read by the TCL layer at stage time, not by make - so the make-side PART has
# no source but this line. Both existing projects set it explicitly
# (eth design.mk:175, compute design.mk:188), so the template's advice has
# never actually been taken by anything.
#
# The consequence is a duplicated device string: this line and
# board/mps3-hbi0309c/board.tcl must agree, and nothing checks that they do.
# Raised against the toolkit rather than worked around silently.
PART ?= xcku115-flvb1760-1-c


# ── 3. IDENTITY (optional, with defaults) ───────────────────────────────────

# The block-design name, when a BD is used. It is the name of the .bd file and
# the stem of the generated wrapper, so it is what TOP usually derives from.
DESIGN_NAME  ?= $(BLOCK)

# Used for git provenance ONLY -- the manifest records what commit built this
# bitstream. It is never used to construct an input path: the flow does not
# reach up out of fpga/ for anything it reads.
PROJECT_ROOT ?= $(FPGA_DIR)/..


# ── 4. TARGET: which board, which device, which flavour of build ────────────

# Where the board pack lives. One file, sourced as plain Tcl.
BOARD_DIR        ?= $(FPGA_DIR)/board/$(BOARD)

# A TARGET is a BUILD of this design for a board. It defaults to the board, and
# stays that way on most projects. It becomes its own thing when one board
# carries several builds that differ in collateral rather than in RTL -- a
# debug build with an ILA and a wider constraint set, a bring-up build with a
# reduced top level. Each gets a directory, and the directory is the difference.
TARGET           ?= $(BOARD)
TARGET_DIR       ?= $(FPGA_DIR)/targets/$(TARGET)

# Where the part pack lives. Part packs ship WITH THE TOOLKIT: how many LUTs a
# device has and whether it has an IDELAY primitive are facts about silicon,
# and every project on that device wants the same answer. Point this elsewhere
# for a site pack held outside the checkout.
PART_DIR         ?= $(FPGA_FLOW_DIR)/part/$(PART)

# The Vivado BOARD FILE, if you use one -- a vendor-supplied description that
# lets IP configure itself against named board interfaces. It is not the same
# thing as the board pack in section 4: that is ours and holds pins, this is
# the vendor's and holds interfaces. Using one is optional and it is a
# reasonable choice to use neither.
#
# If you set BOARD_PART you MUST also set BOARD_REPO_PATHS, or Vivado resolves
# the VLNV against whatever board files happen to be installed on THIS machine
# -- which is how a build becomes machine-dependent without anyone writing a
# path down. The board pack's schema enforces the pairing.
BOARD_PART       ?=
BOARD_REPO_PATHS ?=

# How the flow drives the tool. There is ONE value, and every other one is
# refused at make parse time:
#   direct         non-project mode: read, synth, opt, place, route by script.
#                  Reproducible, and there is no project on disk to inspect
#                  afterwards -- checkpoints are the only artefact.
#
# This used to offer `project`, `dfx` and `protocompiler` too, and defaulted to
# `project`. None of the three ran: `project` named a `launch_runs` path no
# stage implements, `protocompiler` was consumed by nothing anywhere, and `dfx`
# changed exactly one synthesis argument with none of the partition handling
# behind it. Setting one of them selected a not-covered bullet in the gate
# saying the declaration had been ignored -- the toolkit telling you, in the
# quietest place it has, that it built something other than what the manifest
# claimed. If you want the out-of-context synthesis `dfx` used to select, ask
# for it by its own knob: `make synth SYNTH_MODE=out_of_context`.
#
# The bd stage still builds a real .xpr under the work directory, and that is
# the one the GUI opens. It is a fact about that stage, not about FLOW_MODE.
FLOW_MODE        ?= direct

# What runs on the board once it is programmed:
#   bare   a bitstream and firmware you built. The default.
#   pynq   a Linux image whose overlay is this bitstream plus its .hwh.
PLATFORM         ?= bare

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: SYS_CLK_FREQ_HZ IS COMPILED INTO THE FIRMWARE *AND* CONSTRAINS THE
#       FABRIC.
#
#   It reaches the firmware as -DNANOSOC_SYS_CLK_FREQ_HZ and it reaches the
#   timing constraints as the period the design is closed at. It is ONE
#   number with TWO consumers, and nothing downstream compares them.
#
#   A build where they disagree BOOTS. It comes up, it runs, and every baud
#   rate and every timer is wrong by the ratio between them -- so the UART
#   prints garbage at the rate you expected and the symptom reads as a serial
#   wiring fault. `make check` reports the two together for this reason.
#
#   The board pack states the oscillator; this is the SYSTEM clock the design
#   actually runs at, which is usually the oscillator through an MMCM. They
#   are not the same number and only one of them is this one.
# ═══════════════════════════════════════════════════════════════════════════
# OSCCLK[1] through a BUFG, no MMCM in that path. The board pack says the
# same and carries the warning that these oscillators are MCC-programmed.
SYS_CLK_FREQ_HZ  ?= 50000000


# ── 5. RTL ──────────────────────────────────────────────────────────────────

# A make target IN THIS PROJECT that regenerates RTL_FLIST. The flow runs it
# before reading the flist, so a generated flist can never be stale. Leave it
# empty if the flist is committed.
# The flist is derived from filelist.tcl every time. See RTL_FLIST above.
RTL_FLIST_GEN      ?= mps3-flist

# Include directories, in addition to any `+incdir+` the flist itself carries.
RTL_INCDIRS        ?=

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: `ipx::package_project` DROPS FILESET DEFINES.
#
#   A `+define+` you set on a fileset does not survive IP packaging. Three
#   separate routes through packaging lose it, and NONE of them warn.
#
#   THIS FAILED SILENTLY, IN THIS CODEBASE, FOR MONTHS. An
#   `ifdef TIDELINK_USE_IDELAY` opt-in was false in EVERY build -- the
#   builds that opted in and the builds that did not. It was proven by
#   building both and finding the "IDELAY-off" bitstream BYTE-IDENTICAL to
#   the "IDELAY-on" one: the primitive was absent from every build that
#   believed it had it, and the tool reported nothing, ever.
#
#   So there are three variables here and they are NOT interchangeable:
#
#     RTL_PARAMS          THE PRIMARY MECHANISM. Parameters survive packaging
#                         as CONFIG.* properties on the IP. Prefer a module
#                         parameter over a macro for anything that selects
#                         behaviour -- it is visible in the BD, visible in the
#                         report, and it cannot be silently dropped.
#
#     RTL_DEFINES_INBODY  baked into the MATERIALISED COPY of each source, so
#                         the define is in the file rather than in a fileset
#                         property nothing carries. Use it when the RTL is not
#                         yours to re-parameterise.
#
#     RTL_DEFINES         reaches the tools the ordinary way. Fine for a
#                         non-packaged flow. IN A PACKAGED FLOW A PLAIN
#                         +define+ MAY REACH NOTHING.
#
#   And one more, which is the other half of the same lesson: this codebase
#   has ZERO occurrences of `ifdef FPGA` and `ifdef ASIC` across 13,524 RTL
#   files, along with XILINX, VIVADO, SIMULATION and FPGA_ONLY. A flow that
#   configures the build with +define+FPGA CONFIGURES NOTHING. Selection here
#   is by FLIST FILE-SWAP (same module name, opposite directory) and by
#   MODULE PARAMETER. Check that the macro you are about to set is read by
#   any file at all before you set it.
#
#   fpga/hooks/pre_synth.tcl is a ready-made assertion for exactly this. It
#   checks that a declared parameter is actually set and that a declared
#   define is actually present -- or actually absent -- on the fileset the
#   tool is about to synthesise.
# ═══════════════════════════════════════════════════════════════════════════
RTL_DEFINES        ?=
RTL_DEFINES_INBODY ?=

# Macros asserted ABSENT. This is not paranoia: an ASIC-only macro reaching an
# FPGA build swaps a memory wrapper for one that instantiates a foundry macro
# the fabric does not have, and the failure is a black box in the netlist
# rather than an error. Name them and the flow proves they are not set.
# Empty on purpose: this flist reads FPGA RTL and vendor IP only, and there
# is no ASIC-only define in the tree for it to forbid. If one appears, name
# it here rather than trusting the flist to keep excluding it.
RTL_DEFINES_NEVER  ?=

# NAME=VALUE, space separated. Read the box above: these are what survives.
# THE FIRMWARE IMAGE. nanosoc_mps3_top.sv:131 declares
#     parameter IMEM_MEM_FPGA_IMG = "image.hex"
# and $readmemh preloads IMEM from it at synthesis. The default names a file
# that does not exist, so a build where this override fails to arrive
# synthesises, programs, and runs whatever an unloaded memory holds.
#
# THE BARE, UNQUOTED FORM IS DELIBERATE: it is exactly what the 2026-07-04
# build passed (build_monolithic.tcl, `set_property generic
# "IMEM_MEM_FPGA_IMG=$path"`), so this build reproduces that one. That script's
# own comment flags the form as NOT VERIFIED for a Verilog string parameter -
# so the synthesis log must be read for whether the value took, not assumed.
#
# No default, for the same reason NANOSOC_BOOTROM_DIR has none: the image is
# a per-build artefact and a guessed path is a build that silently runs the
# wrong program. `make mps3-firmware` writes it; see below.
IMEM_MEM_FPGA_IMG  ?=
RTL_PARAMS         ?= $(if $(strip $(IMEM_MEM_FPGA_IMG)),IMEM_MEM_FPGA_IMG=$(IMEM_MEM_FPGA_IMG))

# The top-level HDL, read AFTER the flist. A board-level top is usually a
# hand-written wrapper that is deliberately NOT in the RTL flist -- the flist
# describes the SoC, and the board wrapper is target collateral. It lives in
# $(TARGET_DIR) with the XDCs for the same reason.
# NOT in $(TARGET_DIR): this board top predates the toolkit and lives with
# the XDC that has been built against it. Moving it would be a rename with
# no measurement behind it.
TOP_HDL            ?= $(FPGA_DIR)/monolithic/nanosoc_mps3_top.sv

# Anything else the tools must read that the flist does not name: a .coe, an
# .xci committed rather than generated, a vendor .edif.
EXTRA_SRCS         ?=

# Files whose file_type must be forced to SystemVerilog. Vivado decides by
# EXTENSION, so a .v file using SystemVerilog constructs is read as Verilog-2001
# and fails on a syntax error somewhere in the middle of a package import -- a
# message that names a line rather than a language.
SV_FILES           ?=


# ── 6. IP AND BLOCK DESIGN ──────────────────────────────────────────────────

# A LIST of directories Vivado scans for packaged IP. It is a LIST because real
# designs have more than one: your own packaged cores, a shared library, and a
# vendor drop are three different trees and three different lifetimes.
IP_REPOS        ?=

IP_VENDOR       ?= soclabs.org
IP_CORE_REV     ?= 1

# The out-of-context synthesis cache. Under BUILD_DIR by default so a clean is
# a clean; point it at a shared scratch path to share OOC runs between run tags,
# and expect the first build after a source change to be slow either way.
IP_CACHE_DIR    ?= $(BUILD_DIR)/ip_cache

# Your packaging script, if this design packages IP. See the packaging trap in
# section 5 before you write it.
PACKAGE_TCL     ?=

# The block design, and overlays applied over it IN ORDER. An overlay is how a
# variant is expressed without forking the BD: the base BD is generated by the
# vendor tooling and regenerated wholesale, and the overlay is the handful of
# connections that make it yours. Forking the BD instead means the next
# regeneration silently discards them.
BD_TCL          ?=
BD_OVERLAY_TCL  ?=

# 1 synthesises the BD as one unit rather than per-IP out-of-context. Faster to
# a first bitstream, slower on every incremental change, and it discards the
# OOC checkpoints - so a small RTL edit re-synthesises everything.
BD_GLOBAL_SYNTH ?= 0


# ── 7. CONSTRAINTS ──────────────────────────────────────────────────────────
#
# EXPLICIT PATHS, NOT A GLOB. Every XDC this design uses is NAMED by a variable
# here, and `make check` ERRORS on a .xdc sitting in $(TARGET_DIR) that no
# variable names. That is deliberate and it is the correction of a measured
# defect: the flow this replaces discovered its constraints by a glob over a
# fixed filename fragment, so an XDC named anything else was silently invisible
# -- present in the directory, read by nothing, and warned about by nobody.
#
# The three files are split BY WHEN THEY ARE READ, and the split is not
# optional. See fpga/targets/README.md, which is the long version.

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: VIVADO DROPS A CONSTRAINT THAT MATCHES NOTHING, WITHOUT AN ERROR.
#
#   `set_property PACKAGE_PIN ... [get_ports clk_i]` on a design whose port
#   is called `clk` does not fail. `get_ports` returns an empty list, the
#   property is applied to nothing, and the run continues. The pin is
#   unconstrained, Vivado places it wherever it likes, and you find out on
#   the bench.
#
#   Renaming a port silently unconstrains every line that named it. So does
#   getting TOP wrong (see section 2), because then NONE of the ports exist.
#
#   That is what XDC_BASELINE and `make xdc-lint` are for, in section 11:
#   xdc-lint counts what each constraint file actually matched and compares
#   it against a committed baseline, so a constraint that stops matching is
#   a diff rather than a silence.
# ═══════════════════════════════════════════════════════════════════════════

# Read in SYNTHESIS and in IMPLEMENTATION. Declared in section 2 with the other
# required inputs.
#   XDC_PINS

# Read in IMPLEMENTATION ONLY (USED_IN_SYNTHESIS false). Clocks, IO delays,
# exceptions.
# Empty: the timing constraints are inside XDC_PINS above, for the reason
# written there. Not a missing file - a file that is somewhere else.
XDC_TIMING      ?=

# Read in IMPLEMENTATION ONLY. Configuration properties and DRC severities.
# Empty: CFGBVS/CONFIG_VOLTAGE come from the board pack (VCCO / 3.3) and
# nanosoc_mps3.xdc carries the rest. The scaffold's placeholder file is
# DELETED rather than kept empty - an empty required file is a claim that
# somebody looked at it.
XDC_DRC         ?=

# A LIST, ORDER PRESERVED, read after the three above. XDC is order-dependent
# in the same way SDC is: a later command silently overrides an earlier one on
# the same object.
XDC_EXTRA       ?=

# A LIST of "COND:path". Each is included IFF $(COND) is 1, so a build variant
# is a variable rather than an edited file:
#
#     export USE_IDELAY := 1
#     XDC_OPTIONAL += USE_IDELAY:$(TARGET_DIR)/$(BLOCK).idelay.xdc
#
# THE `export` IS NOT OPTIONAL AND IT IS NOT DECORATION. The condition is a make
# variable YOU set, and the implementation stage resolves it from ITS
# ENVIRONMENT: mk/flow.mk exports the FPGA_* set it defines, and yours is not in
# it. An assigned-but-unexported condition is not read as false - the stage
# REFUSES, because reading the file and skipping it are both guesses about a
# design somebody has to review. `make check` refuses it too, before a tool is
# launched.
#
# COND IS A NAME, NEVER A VALUE. `XDC_OPTIONAL += 0:...` and `1:...` are the
# mistake this line exists to stop: a file that should always be read belongs in
# XDC_EXTRA, and one that should never be read belongs outside TARGET_DIR.
#
# Read the packaging trap in section 5 first: if the RTL side of that variant is
# a `+define+`, the constraint may be the only half of it that actually took
# effect.
XDC_OPTIONAL    ?=

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: A DRC-WAIVER XDC CANNOT BE read_xdc'd.
#
#   Vivado REJECTS procedural Tcl inside an XDC. `create_waiver` is
#   procedural. So the file you naturally write -- a waiver per finding,
#   each with its diagnosis -- cannot be a constraint file at all, and
#   read_xdc on it errors in a way that reads like a syntax problem.
#
#   XDC_POST_ROUTE is `source`d AFTER route_design, as ordinary Tcl, which is
#   where a waiver has to go anyway: a waiver waives a finding, and the
#   findings do not exist until the design is routed.
#
#   Do not use this seam for anything that changes the design. By the time it
#   runs, place and route are done.
# ═══════════════════════════════════════════════════════════════════════════
XDC_POST_ROUTE  ?=


# ── 8. FIRMWARE ─────────────────────────────────────────────────────────────
#
# A BITSTREAM CO-DEPENDENCY, NOT A SEPARATE BUILD. On a soft-core design the
# firmware is initialised into block RAM at bitstream generation, so the .bit
# carries a copy of the program. Rebuilding the firmware and re-programming the
# OLD bitstream loads the OLD program, silently. The flow tracks the hex as an
# input for exactly this reason, and records its SHA-256 in the manifest.

# The firmware application to build, if this flow builds it.
FW_APP          ?=

# The hex image itself, if you build it elsewhere and hand it over.
FW_HEX          ?=

# byte | word. What one line of the hex file holds. Getting it wrong does not
# fail: it loads a program whose every instruction is shuffled, and the CPU
# executes it.
FW_HEX_FORMAT   ?= word

# What $readmemh in the RTL actually resolves to. It is separate from FW_HEX
# because the RTL names a path and the build produces a file, and they are only
# the same thing if somebody makes them so. This is where that is made explicit.
#
# MADE SO HERE: the image this design preloads is IMEM_MEM_FPGA_IMG (section 5),
# passed as a synthesis GENERIC, so the RTL and this variable name one file by
# construction. The bitstream stage only READS FPGA_IMAGE_HEX - it asserts the
# file exists and records its SHA-256 as fpga_image_hex_sha256 - so setting it
# cannot load the image twice. Left empty, as it was until 2026-09-25, the
# manifest recorded nothing, and ci/assert-stage.sh warned bitstream.firmware on
# every run: no record of which firmware is inside the .bit.
FPGA_IMAGE_HEX  ?= $(IMEM_MEM_FPGA_IMG)


# ── 9. EXTENSION POINTS ─────────────────────────────────────────────────────
#
# Project Tcl sourced at a named point in a stage. Absent is SILENT; present is
# ANNOUNCED with its path and runtime; an error in one STOPS THE STAGE and is
# not downgraded. The valid seam names are in
# $(FPGA_FLOW_DIR)/flow/common/seams.txt and a file here whose name is not on
# that list NEVER RUNS -- `make check` warns and names it. See hooks/README.md.
HOOKS_DIR       ?= $(FPGA_DIR)/hooks

# A file here REPLACES $(FPGA_FLOW_DIR)/flow/steps/<name>.tcl WHOLESALE. No
# merging, no partial override: those files set tool properties in an order
# that matters and a half-overridden one is a design nobody can reason about.
# `make check` warns whenever an override is active, and the manifest records
# which step files were the toolkit's and which were yours.
OVERRIDES_DIR   ?= $(FPGA_DIR)/overrides


# ── 10. RUN NAMESPACE ───────────────────────────────────────────────────────
#
# Everything a run produces goes to
#
#     $(BUILD_DIR)/$(RUN_TAG)/{work,logs,reports,outputs}
#
# so two runs cannot collide and an experiment is a NEW RUN TAG rather than a
# copied directory. Point BUILD_DIR at scratch if you would rather it were not
# in the repo -- but read section 11's note on evidence first: a gate that
# passed and left its evidence somewhere nothing collects is indistinguishable
# from a gate that never ran.
BUILD_DIR       ?= $(FPGA_DIR)/build
RUN_TAG         ?= default

# Which run's databases a stage READS. Both default to RUN_TAG; set them to
# resume or to branch:
#     make impl RUN_TAG=tighter_pblock IN_RUN_TAG=baseline
# The input run is read-only BY CONSTRUCTION -- paths are composed from
# WORK_DIR and a run tag containing a path separator is refused -- not by
# convention.
IN_RUN_TAG      ?= $(RUN_TAG)
SYNTH_RUN_TAG   ?= $(RUN_TAG)

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: THE DERIVED VARIABLES ARE SILENTLY OVERRIDDEN IF YOU SET THEM.
#
#     RUN_DIR  WORK_DIR  LOG_DIR  REPORT_DIR  OUT_DIR
#     IN_WORK_DIR  SYNTH_OUT_DIR
#
#   The engine assigns all seven with `:=` from BUILD_DIR and the run tags,
#   AFTER this file is read. So anything you set here is computed over and
#   discarded -- your value never reaches a recipe, and nothing says so.
#
#   The reference toolkit's own shipped example does exactly this. `make
#   check` WARNS if it finds any of them assigned here, which is the only
#   warning in the whole check that exists because the toolkit's own
#   documentation was wrong.
#
#   Move the run somewhere else with BUILD_DIR and RUN_TAG. That is what
#   they are for, and they are the only two inputs the seven are built from.
# ═══════════════════════════════════════════════════════════════════════════


# ── 11. TOOLS ───────────────────────────────────────────────────────────────

VIVADO          ?= vivado

# When set, this is ASSERTED, not assumed: the flow refuses to run under a
# different version rather than producing a bitstream from a toolchain nobody
# recorded. Leave it empty during bring-up; set it the day you have a build you
# would want to reproduce.
#
# `make doctor` reports what is ON THE FILESYSTEM, never what a modulefile
# advertises. On this host the modulefiles advertise three versions and two of
# them are not installed.
VIVADO_VER      ?=

# Local CPU count. Measure the host (`lscpu`) rather than guessing; do not
# exceed the PHYSICAL core count, because the placer and router scale on real
# cores and thrash on hyperthreads.
NUM_JOBS        ?= 8

TCLSH           ?= tclsh


# ── 12. GATES ───────────────────────────────────────────────────────────────
#
# ═══════════════════════════════════════════════════════════════════════════
# TRAP: EVERY EXPECT_* DEFAULTS TO -1, WHICH MEANS *MEASURE, DO NOT GATE*.
#
#   -1 is not "no limit" in the sense of "we do not care". It is "this run
#   reports the number and does not judge it", and the report says so in
#   those words, so a green run cannot be mistaken for a run that checked
#   something.
#
#   SET THE RATCHET AFTER A FIRST RUN, and write the measurement and the
#   margin down BESIDE IT, in a comment, on the same day you set it:
#
#       # measured -0.031 ns on 2026-09-08, run tag baseline, Vivado 2024.1.
#       # 0.100 ns of margin for temperature and for the ILA we will add.
#       EXPECT_WNS_MIN := -0.131
#
#   A budget with no measurement beside it is a number somebody guessed, and
#   the next person to see it fail cannot tell whether the design regressed
#   or the guess was always wrong. This project has a whole class of those:
#   budgets set to whatever the day's number happened to be, which pass
#   everything and discriminate nothing.
# ═══════════════════════════════════════════════════════════════════════════
# ARMED 2026-09-23, FROM A MEASUREMENT - the first routed result of this target.
#
#     run tag default:  WNS +3.98 ns   WHS +0.03 ns   0 failing either way
#                       critical_warnings 0  (Timing 38-282 did NOT fire post-route)
#
# Both floors at 0: a routed result that fails setup or hold now fails the gate,
# instead of being measured and passed. This is what makes the Timing 38-282
# allowlist safe - see "Timing 38-282 AT SYNTHESIS" below. Without these floors
# armed, that allowlist plus -1 would let a real post-route hold failure through.
#
# HOLD IS MET BY 30 ps. That is a pass, and a thin one: all 1,993 synthesis-
# stage hold violations were intra-clock and the router repaired them, but the
# margin it left is small. A design change that adds same-clock short paths is
# the thing most likely to trip this floor, and it SHOULD trip it.
EXPECT_WNS_MIN          ?= 0
EXPECT_WHS_MIN          ?= 0
EXPECT_LUT_MAX          ?= -1
EXPECT_FF_MAX           ?= -1
EXPECT_BRAM_MAX         ?= -1
EXPECT_DSP_MAX          ?= -1

# These two default to ZERO rather than -1, because unlike a resource count
# there is no design for which the right answer is "some". An unrouted net is
# not a tight budget; it is a bitstream that does not implement the schematic.
EXPECT_UNROUTED_MAX     ?= 0
EXPECT_BLACKBOX_MAX     ?= 0

ALLOW_CRITICAL_WARNINGS ?= 0

# The committed record of what each constraint file MATCHED, against which
# `make xdc-lint` compares. This is the answer to the "Vivado drops a
# constraint that matches nothing" trap in section 7: without a baseline, a
# constraint that stops matching produces no output at all, and there is
# nothing for a gate to be red about.
# Empty until this target has had a first real run. A baseline invented
# before there is anything to baseline is a file that agrees with itself.
XDC_BASELINE          ?=

# EMPTY BY DEFAULT, AND IT STAYS EMPTY UNTIL YOU DIAGNOSE SOMETHING. A default
# that tolerates message IDs hands every new project somebody else's
# undiagnosed exemptions, which is how a gate comes to pass on a defect it was
# written to catch. Every entry carries a paragraph of diagnosis and a named
# owner, here, beside it.
# Set here and nowhere else - see "Timing 38-282 AT SYNTHESIS" below for why.
MSG_GATE_ALLOWLIST    ?= {Timing 38-282}


# ── 13. DEPLOY ──────────────────────────────────────────────────────────────
#
# Declaration only at this phase: nothing here programs a board yet. Fill it in
# anyway if you know the answers, because they are answers about YOUR bench and
# nobody else can supply them later.

# ═══════════════════════════════════════════════════════════════════════════
# TRAP: THE BOARD GROUP AND THE TARGET ARE DIFFERENT NAMESPACES.
#
#   FPGAHUB_BOARD   is the board GROUP  -- the LEASE scope. Leases, queues
#                   and reservations address this.
#   FPGAHUB_TARGET  is the TARGET       -- the PROGRAM scope. Program, reset
#                   and actions address this.
#
#   They do not overlap. Passing a board group where a target is expected
#   returns 404, and passing a target where a board group is expected
#   returns 404, and neither 404 says which of the two you got wrong.
#
#   A board with one FPGA on it still has both names, and they are often
#   similar enough to look like typos of each other. Write both down.
# ═══════════════════════════════════════════════════════════════════════════
FPGAHUB_BOARD   ?=
FPGAHUB_TARGET  ?=
# DISCOVERED, not asserted - note the $(wildcard). CONTRACT.md 3.3's corollary:
# a default naming a CONVENTIONAL path must discover, or `make check` treats
# the engine's own convenience default as a file the project asked for, and a
# project with no deploy configured cannot pass until it creates an empty
# fpgahub.toml it never wanted.
FPGAHUB_TOML    ?= $(wildcard $(FPGA_DIR)/fpgahub.toml)
# ═══════════════════════════════════════════════════════════════════════════
# TRAP: BIN_STYLE IS NOT COSMETIC.
#
#   .bin conversion is board-family dependent. Zynq-7000 needs a BYTE SWAP;
#   ZynqMP needs a HEADER STRIP. They are not interchangeable and the wrong
#   one CORRUPTS THE LOAD -- the file is produced, it is the right size, it
#   is accepted by the loader, and the device does not come up.
#
#   The board pack declares this as a REQUIRED key, because it is a fact
#   about the board and not about your design. Set it here only to override
#   a pack you cannot edit.
# ═══════════════════════════════════════════════════════════════════════════
BIN_STYLE       ?=

# NO HARDWARE HANDOFF (.xsa). Nothing on this board reads one: the firmware is
# built with CMake against the generated nanosoc_memmap.h (`make mps3-firmware`),
# and the MCC loads the .bit off the config microSD. There is no block design
# and no PS, so write_hw_platform could only emit
#     CRITICAL WARNING: [Project 1-1924] Failed to write hardware handoff data
# for an archive nobody opens - which is what held the 2026-09-23 bitstream stage
# at exit 2 on an otherwise clean run. Allowlisting that id instead was
# rejected: it also fires on designs that DO need their handoff.
#
# EXPORTED, NOT JUST ASSIGNED. The stage reads it from its environment
# (flow/vivado/6_bitstream.tcl `opt`), and records it in the manifest as
# knob.BITSTREAM_WRITE_XSA - which is where mk/flow.mk and ci/assert-stage.sh
# read it. A value that stayed in make would reach none of the three graders.
export BITSTREAM_WRITE_XSA := 0


# ── 14. THE ENGINE ──────────────────────────────────────────────────────────
#
# Nothing above this line is the toolkit's. Nothing below it is yours, except
# the appended prerequisites in section 15, which must come AFTER it.
#
# This include is also in fpga/Makefile. That is deliberate and safe:
# mk/flow.mk carries an include guard, so design.mk is self-sufficient when
# something includes it directly.
#-----------------------------------------------------------------------------
# Timing 38-282 AT SYNTHESIS: ALLOWLISTED, AND WHY IT IS NOT ETH'S REASON
#
# First clean synthesis of this target (2026-09-23): HARD FAILURES none, and one
# critical warning, [Timing 38-282] "failed to meet the timing requirements".
#
#     WNS +4.715   setup MET (the 07-04 build had +4.128)
#     WHS -3.316   hold FAILS, 1993 endpoints: 1757 OSCCLK[1] -> OSCCLK[1] plus
#                  236 in async_default, also OSCCLK[1] -> OSCCLK[1]
#     clocks defined: OSCCLK[1] 50 MHz, CS_TCK 10 MHz.  NO inter-clock failure.
#
# THE ETH CHIPLET ALLOWLISTS THE SAME ID FOR A DIFFERENT REASON, and it does not
# transfer. Eth's synthesis sees NO clock, because its XDC_TIMING is
# implementation-only, so its 38-282 describes an unconstrained design. THIS
# design points XDC_PINS at the combined monolithic XDC, which carries
# create_clock and is read AT synthesis - so this 38-282 is a CONSTRAINED,
# PRE-PLACEMENT hold estimate. Real clocks, ideal clock tree, no routing.
#
# It is allowlisted because every failing endpoint is SAME-CLOCK, and
# same-clock hold before placement is the case the router repairs by padding
# routes. The failure mode worth fearing - an undeclared clock (the QSPI SCK
# precedent on HAPS-SX) - shows up BETWEEN clocks, and nothing here does.
#
# WHAT THIS ALLOWLIST DOES NOT DO. The same id fires at implementation, where
# it is real, and EXPECT_WHS_MIN is still -1 (measured, not gated). Together
# those mean a genuine POST-ROUTE hold failure would pass this flow in silence.
# That was acceptable for exactly one run - the first routed result - and it
# happened: WHS +0.03, WNS +3.98, 0 critical warnings. Both floors are now ARMED
# at 0 (see EXPECT_WHS_MIN above), which is what closes the hole.
#
# The exemption is conditional in the flow: it holds only while the ids read
# from the stage log account for the tool's whole critical-warning count, so a
# second, undiagnosed critical warning still fails the gate.
#-----------------------------------------------------------------------------
# THE ASSIGNMENT IS NOT HERE. It lives where the scaffold declares the
# variable, above. A second `MSG_GATE_ALLOWLIST ?= ...` at this point was tried
# first and measured to do NOTHING: the scaffold's earlier `?=` (empty) already
# defines the variable, and `?=` never overrides a defined one - `make` resolved
# it as empty. One definition, one place.

#-----------------------------------------------------------------------------
# THE GENERATED HALF OF THE CONTRACT
#
# gen_flist.tcl writes two files: the flist itself, and this fragment carrying
# what filelist.tcl sets on the fileset that a flist cannot express - the
# include_dirs, the RAM_PRELOAD define, and the 25 files it reads with -sv
# (this tree carries SystemVerilog in .v files, so extension inference is not
# enough and Vivado fails mid-package-import without the forcing).
#
# `-include`, not `include`: on a clean checkout the fragment does not exist
# yet, and `make check` must be able to run and SAY SO rather than die on a
# missing include. The mps3-flist target below creates both.
#
# Included HERE, above the engine, because mk/flow.mk snapshots part of the
# variable surface with `:=` - a fragment included after that line would be
# read too late to reach the stages.
#-----------------------------------------------------------------------------
-include $(FPGA_DIR)/monolithic/generated/nanosoc_mps3.vars.mk

#-----------------------------------------------------------------------------
# REGENERATING THE FLIST
#
# NANOSOC_BOOTROM_DIR has no default, here or in filelist.tcl, and that is
# deliberate: the bootrom carries a UART divisor compiled against a specific
# firmware clock constant, so the PYNQ-Z2 build's bootrom on MPS3 gives a board
# that comes up with a garbled console and nothing saying why.
#-----------------------------------------------------------------------------
#-----------------------------------------------------------------------------
# WHAT pre_synth ASSERTS ABOUT THE RTL IT IS ABOUT TO SYNTHESISE
#
# IMEM_MEM_FPGA_IMG is the top-level parameter naming the hex image preloaded
# into IMEM at synthesis (nanosoc_mps3_top.sv:131, default "image.hex"). The
# build overrides it through the project GENERIC property. If that override
# ever stops arriving, synthesis succeeds, the bitstream is written, the board
# programs, and the CPU executes whatever an absent image leaves behind - a
# failure with no error anywhere in the chain.
#
# PRESENCE ONLY, NO VALUE, AND THAT IS DELIBERATE. The value is a path that
# differs per build, so asserting one would either be wrong for everybody else
# or would have to be a wildcard that asserts nothing. The hook reports a
# presence-only claim AS weaker, which is the honest record of what is known.
#
# WHAT IS NOT ASSERTED HERE, and why: that the parameter is not still at its
# default. That is the check worth having and the table has no form for it
# (value X is an equality, not an inequality). Raised against the toolkit.
export RTL_ASSERT_TABLE := \
    {name IMEM_MEM_FPGA_IMG \
     kind param \
     why "the IMEM preload image. Absent, the CPU runs whatever an unloaded memory holds, and nothing reports it"}

MPS3_FLIST_DIR := $(FPGA_DIR)/monolithic/generated

#-----------------------------------------------------------------------------
# THE THREE PINNED INPUTS - submodules, not paths into one machine's home
#
#   fpga/fpga-toolkit          the engine (FPGA_FLOW_DIR, above)
#   fpga/deps/nanosoc_m0_soc   the SoC: RTL list, generated build_soc/, firmware
#   fpga/deps/ahb_qspi         the XiP flash controller the SoC instantiates
#
# All three used to be $(HOME)/SoCLabs/<repo>, and on 2026-09-24 that was
# measured to be wrong in two different ways:
#
#   - nanosoc_m0_soc's checkout carried another session's uncommitted
#     generator and arch_tech state, so the 2026-09-23 bitstream was built from
#     a WORKING TREE no commit reproduces. Its published main (6a6f059) builds
#     from a clean clone and is what is pinned.
#   - ahb_qspi's checkout was at 0ab9f42, which DOES NOT EXIST on the remote -
#     its branch was deleted upstream. Its logical/ tree, the only part this
#     build reads, is byte-identical to ad10478 on the remote's master, so
#     pinning ad10478 is the same RTL at a commit anyone can fetch.
#
# `?=` so a deliberate override still works - but an override is a build of
# something other than these pins, and the manifest's git provenance is the
# only record of which.
#-----------------------------------------------------------------------------
MPS3_AHB_QSPI_DIR ?= $(FPGA_DIR)/deps/ahb_qspi

#-----------------------------------------------------------------------------
# BUILDING THE MPS3 FIRMWARE - the README recipe, with its two defects fixed
#
# fpga/monolithic/README.md section 4 is the source of this recipe, and run as
# written it does not work, in two independent ways (measured 2026-09-23):
#
#   1. It passes CMAKE_TOOLCHAIN_FILE as a RELATIVE path. CMake resolves a
#      relative toolchain file against the BUILD directory, not the cwd, and
#      the recipe itself puts the build directory outside the source tree - so
#      configure fails "Could not find toolchain file". Absolute here.
#
#   2. stage0_bootloader.c has included qspi_flash.h since nanosoc_m0_soc
#      b065ed8 (2026-07-15, the XiP flash integration), and pynq/firmware's
#      superproject never put that header's directory on stage0's include path.
#      NOTHING has built stage0 through this superproject since - the PYNQ-Z2
#      bootrom under imp/fpga/firmware/ is from 2026-07-06, nine days earlier.
#      Fixed on OUR side with -I, because nanosoc_m0_soc is read-only by its
#      own README's rule. The proper fix has since landed there (460d8e6, in
#      the pinned 6a6f059); the -I is now redundant and harmless, and stays
#      until a build without it has been run.
#
# THE CLOCK PATCH IS THE POINT OF ALL THIS. MPS3 runs OSCCLK[1] at 50 MHz and
# the generated nanosoc_memmap.h's NANOSOC_SYS_CLK_FREQ_HZ is unguarded (a -D
# is silently ineffective), so the patch is a sed on a COPY. PROVEN by a
# controlled rebuild differing ONLY in that constant: bootrom.sv changes in
# exactly one ROM word, the UART divisor, 0x28b (651) -> 0x516 (1302). Both
# give 38,402 baud at their own clock. The PYNQ-Z2 bootrom on this board would
# divide 50 MHz by 651 and put the console at 76,800 - double, garbled, and
# nothing in the build says so.
#-----------------------------------------------------------------------------
NANOSOC_M0_SOC_SRC ?= $(FPGA_DIR)/deps/nanosoc_m0_soc
MPS3_FW_BUILD      := $(FPGA_DIR)/monolithic/build
MPS3_FW_CONFIG     := $(MPS3_FW_BUILD)/fw_config_mps3
MPS3_FW_OUT        := $(MPS3_FW_BUILD)/firmware_mps3

.PHONY: mps3-firmware
mps3-firmware:
	@test -d "$(NANOSOC_M0_SOC_SRC)/build_soc/firmware" || { \
	    echo "FAIL: no build_soc/firmware under $(NANOSOC_M0_SOC_SRC)."; \
	    echo "      It is the pinned submodule: git submodule update --init --recursive"; \
	    exit 1; }
	@mkdir -p "$(MPS3_FW_CONFIG)"
	@cp -f "$(NANOSOC_M0_SOC_SRC)"/build_soc/firmware/* "$(MPS3_FW_CONFIG)/"
	@sed -i 's/#define NANOSOC_SYS_CLK_FREQ_HZ.*/#define NANOSOC_SYS_CLK_FREQ_HZ                  (50000000UL)  \/* FPGA override: MPS3 OSCCLK[1] *\//' \
	    "$(MPS3_FW_CONFIG)/nanosoc_memmap.h"
	@grep -q '(50000000UL)' "$(MPS3_FW_CONFIG)/nanosoc_memmap.h" || { \
	    echo "FAIL: the 50 MHz clock patch did not land in nanosoc_memmap.h."; \
	    echo "      A bootrom built without it runs the console at double baud."; \
	    exit 1; }
	@cmake -S "$(NANOSOC_M0_SOC_SRC)/pynq/firmware" -B "$(MPS3_FW_OUT)" \
	    -DCMAKE_TOOLCHAIN_FILE="$(NANOSOC_M0_SOC_SRC)/nanosoc_arch_tech/firmware/cmake/toolchains/arm-gcc.cmake" \
	    -DNanoSoC_FIRMWARE_CONFIG_DIR="$(MPS3_FW_CONFIG)" \
	    -DCMAKE_C_FLAGS="-I$(NANOSOC_M0_SOC_SRC)/nanosoc_arch_tech/firmware/software/drivers" \
	    -DNANOSOC_BUILD_TESTS=ON
	@cmake --build "$(MPS3_FW_OUT)" --target nanosoc_bootrom hello -j 4
	@python3 "$(NANOSOC_M0_SOC_SRC)/pynq/scripts/hex_byte_to_word.py" \
	    "$(MPS3_FW_OUT)/fw/testcodes/hello/hello.hex" "$(MPS3_FW_BUILD)/hello_word.hex"
	@echo "OK: firmware for MPS3 (50 MHz)"
	@echo "    NANOSOC_BOOTROM_DIR=$(MPS3_FW_OUT)/stage0"
	@echo "    IMEM_MEM_FPGA_IMG=$(MPS3_FW_BUILD)/hello_word.hex"

# The read-only Arm IP library (CMSDK etc.). A site mount, so NO default: taken
# from the command line / environment, else from the ONE key of the same name in
# the repo-root tools.env (see tools.env.example). Only that key is read here.
ARM_IP_LIBRARY_PATH ?= $(strip $(shell sed -n 's/^[[:space:]]*ARM_IP_LIBRARY_PATH[[:space:]]*[:?]\{0,1\}=[[:space:]]*//p' "$(FPGA_DIR)/../tools.env" 2>/dev/null | tail -n 1))

.PHONY: mps3-flist
mps3-flist:
	@test -n "$(ARM_IP_LIBRARY_PATH)" || { \
	    echo "FAIL: ARM_IP_LIBRARY_PATH is not set (the read-only Arm IP library)."; \
	    echo "      It has no default. Set it in tools.env at the repo root"; \
	    echo "      (see tools.env.example), the environment, or on the command line."; \
	    exit 1; }
	@test -n "$(NANOSOC_BOOTROM_DIR)" || { \
	    echo "FAIL: NANOSOC_BOOTROM_DIR is not set."; \
	    echo "      It must name a directory holding nanosoc_region_bootrom.v"; \
	    echo "      and bootrom.sv from an MPS3-clock-patched bootrom build."; \
	    echo "      There is no default: the PYNQ-Z2 bootrom bakes in a UART"; \
	    echo "      divisor for a 25 MHz firmware clock, and MPS3 runs at 50."; \
	    exit 1; }
	@test -d "$(MPS3_AHB_QSPI_DIR)/logical" || { \
	    echo "FAIL: no ahb_qspi checkout at $(MPS3_AHB_QSPI_DIR)."; \
	    echo "      The SoC instantiates the QSPI flash controller unconditionally"; \
	    echo "      (nanosoc.sv, module qspi_flash_ahb). It is the pinned"; \
	    echo "      submodule: git submodule update --init --recursive"; \
	    exit 1; }
	@mkdir -p "$(MPS3_FLIST_DIR)"
	@cd "$(FPGA_DIR)/monolithic" && \
	    SOCLABS_NANOSOC_SOC_DIR="$(NANOSOC_M0_SOC_SRC)" \
	    SOCLABS_NANOSOC_ARCH_TECH_DIR="$(NANOSOC_M0_SOC_SRC)/nanosoc_arch_tech" \
	    SOCLABS_NANOSOC_GEN_DIR="$(NANOSOC_M0_SOC_SRC)/nanosoc_arch_tech/nanosoc_gen" \
	    SOCLABS_AHB_QSPI_DIR="$(MPS3_AHB_QSPI_DIR)" \
	    FPGA_BOOTROM_DIR="$(NANOSOC_BOOTROM_DIR)" \
	    ARM_IP_LIBRARY_PATH="$(ARM_IP_LIBRARY_PATH)" \
	    tclsh gen_flist.tcl \
	    "$(MPS3_FLIST_DIR)/nanosoc_mps3.flist" \
	    "$(MPS3_FLIST_DIR)/nanosoc_mps3.vars.mk" \
	    "$(NANOSOC_M0_SOC_SRC)/pynq/filelist.tcl" < /dev/null

include $(FPGA_FLOW_DIR)/mk/flow.mk


# ── 15. YOUR OWN WORK, ATTACHED TO A STAGE ──────────────────────────────────
#
# To make one of your targets run as part of a stage, add a RECIPE-LESS rule:
#
#     synth: my-project-lint
#     impl:  my-project-pblock-report
#
#     my-project-lint:
#             $(MY_LINTER) --flist $(RTL_FLIST)
#
# THE RULE MUST BE HERE, AFTER THE INCLUDE ABOVE. Before it, `synth` does not
# exist yet, so `synth: my-project-lint` DEFINES it -- as a target with a
# prerequisite and no recipe -- and the engine's later definition then collides
# with yours.
#
# Four hazards, all of them quiet:
#
#   * A SECOND RECIPE SILENTLY REPLACES OURS. Writing `synth:` with a TAB line
#     under it does not extend the stage; it replaces it. GNU make says so as
#     a WARNING ("overriding recipe for target"), which scrolls past in a build
#     log, and then runs yours instead. If you want work attached to a stage,
#     write NO recipe under the stage's name.
#
#   * AN INCLUDE GUARD CANNOT SEE A TARGET COLLISION. The guard on mk/flow.mk
#     stops the file being PARSED twice; it has nothing to say about a target
#     name defined in two files. The only protection is a manual name check --
#     `make help-all` lists every target the engine defines -- redone every
#     time you add one. Prefix yours (`my-project-`, or the block's name) and
#     the check becomes unnecessary.
#
#   * PREREQUISITES CARRY NO ORDERING. Under `-j`, the prerequisites of one
#     target may run in any order and at the same time. If two of yours must
#     be sequenced, sequence them inside one recipe, not by listing them.
#
#   * A PREREQUISITE RUNS *BEFORE* THE STAGE. If what you want is a check on
#     the stage's OUTPUT, a prerequisite runs at the wrong time: use
#     <STAGE>_POST_TARGETS, which runs after the stage's verdict artefact
#     exists.
#
#     BITSTREAM_POST_TARGETS += my-project-deploy
#
#     Failure there is non-fatal to the BUILD and fatal to the CLAIM: a
#     90-minute implementation must not die because a board was busy, but the
#     run is not "deployed" and the message says so loudly.
