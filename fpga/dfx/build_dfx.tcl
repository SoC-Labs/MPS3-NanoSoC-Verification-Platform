# -----------------------------------------------------------------------------
# build_dfx.tcl — DFX build driver for the MPS3 platform (xcku115-flvb1760-1-c).
#
# STATUS: RUN END-TO-END against the REAL static shell (2026-07-06,
# realshell rerun; previously proven 2026-07-06 W-DFX-PROD on the proof
# stand-in). The static_shell_dcp is A1's build/shell_proj_a1/
# shell_static_synth.dcp (fpga/shell/build_shell.tcl output, RP cell
# u_rp_dut holding a DONT_TOUCH'd synthesizable stub -- carved out below
# before RM link). Full N-config loop (greybox -> led -> nanosoc via the
# readiness filter), pr_verify COMPATIBLE on every non-reference RM,
# partial+clearing pairs (.bit + ICAP .bin) and the real-shell static_id
# emitted. Evidence: fpga/dfx/prod_results_2026-07-06-realshell/ (proof-
# static-era evidence: prod_results_2026-07-06/).
#
# Adapted from the PROVEN Z2 probe:
#   nanosoc-multicore-system/future-work/dfx-pynq/dfx_impl_probe.tcl
# extended from 2 configs (nanosoc/blank) to N RM configs (rm_list.tcl), and
# carrying the UltraScale deltas documented in fpga/dfx/README.md:
#   - clearing bitstream per RM (mandatory, not optional)
#   - no RESET_AFTER_RECONFIG (7-series-only)
#   - RP Pblock confined to one SLR (dfx_floorplan.xdc)
#   - BITSTREAM.CONFIG.PERSIST OFF
#   - .bit -> .bin for AXI HWICAP delivery (write_bitstream -bin_file; byte
#     layout analysed in fpga/dfx/README.md "I18", final say = hardware)
#
# Usage (or just `make -C fpga/dfx prod` — see fpga/dfx/Makefile):
#   vivado -mode batch -source fpga/dfx/build_dfx.tcl \
#          -journal <out>/build.jou -log <out>/build.log \
#          -tclargs <repo_root> <out_dir> <static_shell_dcp> \
#                   [<rm_subset>] [<rp_inst>]
#
# <static_shell_dcp> -- the static shell's post-synth checkpoint. The REAL
# shell is build/shell_proj_a1/shell_static_synth.dcp (regenerate: fpga/
# shell/build_shell.tcl, see fpga/shell/build_results_2026-07-06/RESULT.txt).
# Its RP cell may hold stub contents (A1's harness binds a synthesizable
# inert rp_dut_stub, DONT_TOUCH'd) -- build_config black-boxes it before RM
# link. The proof's minimal static (build/proof/static_synth.dcp from
# `make proof`) remains a usable stand-in for flow debugging.
#
# <rm_subset> (optional, 4th tclarg) -- comma- or space-separated list of
# rm_list.tcl keys to build, e.g. "rm_greybox,rm_led" for the Phase 1.1/1.2
# greybox<->LED-counter inner loop (IMPLEMENTATION_PLAN.md). Pass "" to get
# the default: every RM in rm_list.tcl's RM_ORDER that the readiness filter
# (below) deems buildable right now — with pre-staged OOC checkpoints
# (`make rm-nanosoc-dcp`, `make rm-eth-ss-dcp`) that resolves to {rm_greybox,
# rm_led, rm_nanosoc, rm_eth_ss}; without them, {rm_greybox, rm_led}.
# rm_eth_ss's wrapper (fpga/rp/eth_ss/) now exists and its OOC synth is
# proven, so it passes the readiness filter once its dcp is staged. To add
# it to an EXISTING run without re-minting static_id, use the incremental
# path instead (env DFX_ADD_RMS / `make add-rm-eth-ss` — see below).
#
# <rp_inst> (optional, 5th tclarg) -- hierarchical path of the RP cell inside
# static_shell_dcp. Default "u_rp_dut": the I4 contract's `u_top` IS the
# netlist top module (fpga/shell/shell_top.sv), so Vivado cell paths carry
# no literal "u_top/" prefix -- the RP cell in A1's real checkpoint is the
# top-level cell "u_rp_dut" (fpga/shell/README.md "Handoff", confirmed by
# the 2026-07-06 realshell dry-run). The proof stand-in uses the same name.
#
# See fpga/dfx/README.md for the full methodology writeup and the Z2 delta
# table.
# -----------------------------------------------------------------------------

if { [llength $argv] < 3 } {
    error "usage: -tclargs <repo_root> <out_dir> <static_shell_synth_dcp> \[<rm_subset>\] \[<rp_inst>\]"
}
set repo_root  [file normalize [lindex $argv 0]]
set out_dir    [file normalize [lindex $argv 1]]
# Path to the static shell's post-synth checkpoint (RP = black box OR A1's
# DONT_TOUCH'd stub -- see build_config's carve-out). A value is required
# (no default) so this script fails loudly instead of silently building
# against a stale/wrong shell.
set static_shell_dcp [file normalize [lindex $argv 2]]
set rm_subset_arg [expr { [llength $argv] >= 4 ? [lindex $argv 3] : "" }]
if { ![file exists $static_shell_dcp] } {
    error "build_dfx.tcl: static shell checkpoint not found: $static_shell_dcp (real shell: regenerate with fpga/shell/build_shell.tcl -> build/shell_proj_a1/shell_static_synth.dcp)"
}

set part xcku115-flvb1760-1-c

file mkdir $out_dir

source $repo_root/fpga/dfx/rm_list.tcl
# The one rule for how an overlay_inputs.txt row names a bitstream: paths
# RELATIVE to $out_dir, so the hand-off record travels with the artefacts it
# lists (fielded/0xA8C1C535/overlay_inputs.txt is ten rows of one
# workstation's home directory -- that is the defect this closes). Shared
# with tools/finish_partials.tcl; unit-tested under tclsh by
# tests/dfx_flow/test_mint_record.py.
source $repo_root/fpga/dfx/tools/overlay_inputs.tcl

# write_static_stamp -- static_stamp.json, the record of the two config-register
# stamps this run puts in the boot image (USERID = which IMPLEMENTATION,
# USR_ACCESS = which harness RELEASE). Kept in its own Vivado-free file so it is
# unit-testable under plain tclsh, like overlay_inputs.tcl above.
source $repo_root/fpga/dfx/tools/static_stamp.tcl

# write_rm_debug_probes -- the per-RM partial .ltx (config_<rm_key>.ltx), written
# from each routed config while it is open for its bitstreams, plus the two-way
# declared-vs-produced gate on rm_list.tcl's RM_LIB(<rm>,debug). Every call site
# below (full flow: reference + other RMs; incremental add-rm) goes through it.
source $repo_root/fpga/dfx/tools/debug_probes.tcl

# DFX_JOBS (tools/dfx_jobs.py): the phases that let the RM configs run as
# separate Vivado processes once the static is locked -- DFX_PHASE=ref|finish,
# and DFX_ROW_FILE for an RM worker (the incremental add below). Unset, nothing
# in this file changes a single command of the one-process flow. -notrace is
# LOAD-BEARING: the file prints the stage's COMPLETE marker, and an echoed proc
# body would put that literal into every log the Makefile greps (see its header).
source -notrace $repo_root/fpga/dfx/tools/dfx_jobs.tcl

# ONE Vivado per build dir (tools/vivado_version.tcl). 2024.1 builds the
# bare-metal static and 2026.1 the MicroBlaze V one; a checkpoint from the OTHER
# version is refused here by name instead of being linked -- an older one opens
# silently (the July 2026 VERSION TRAP). ensure_rm_synth_dcp guards every
# pre-built RM checkpoint the same way. DFX_SHELL_CPU=mbv (the Makefile sets it
# only for SHELL_CPU=mbv) additionally needs Vivado >= 2026.1.
source $repo_root/fpga/dfx/tools/vivado_version.tcl
dfx_cpu_version_guard
dfx_version_guard $static_shell_dcp "static shell checkpoint"

# The XDC gate (FLOW_CONTRACT.md §2 rule 10; SHELL's fpga/shell/tools/xdc_gate.tcl).
# An XDC is not Tcl: Vivado DROPS `if`/`catch`/`puts`/... together with the whole
# body, under CRITICAL WARNING [Designutils 20-1307], and the build closes timing
# without those constraints. RC1's own read of mps3_harness_timing.xdc below hit
# it, as did every shell before (CLCD/USD pad windows, real-PHY, the QSPI pad
# model). So: every read_xdc in this file goes through dfx_read_xdc (the subset
# check, before the read), and dfx_xdc_dropped_gate runs after the link (every
# constraint read) and after opt_design. A checkpoint's constraints come back
# from a binary archive on open_checkpoint and are not re-parsed, so an add-rm
# against an older static is judged only on what THIS run reads.
# dfx_floorplan.xdc is `source`d Tcl, not an XDC: it is never handed to these.
source -notrace $repo_root/fpga/shell/tools/xdc_gate.tcl
proc dfx_read_xdc { rm_key xdc args } {
    if { [catch { soclabs_xdc_subset_check [list $xdc] } err] } {
        puts "DFX_XDC_GATE_FAILED rm=$rm_key stage=subset file=$xdc"
        error "build_dfx.tcl: $err"
    }
    read_xdc {*}$args $xdc
}
proc dfx_xdc_dropped_gate { rm_key stage } {
    if { [catch { soclabs_xdc_dropped_check } err] } {
        puts "DFX_XDC_GATE_FAILED rm=$rm_key stage=$stage"
        error "build_dfx.tcl: $err"
    }
}

# I4 RESOLVED (partition-pins.md v0.1): "the RP is a hierarchical cell
# ALONGSIDE the shell" in one top design (u_top = u_shell + u_rp_dut).
# A1's real shell realises this with u_top == the netlist TOP MODULE
# (fpga/shell/shell_top.sv), so the RP cell path in Vivado has no literal
# "u_top/" prefix: it is the top-level cell "u_rp_dut" -- the default below
# (confirmed against build/shell_proj_a1/shell_static_synth.dcp, 2026-07-06
# realshell dry-run; matches fpga/shell/README.md "Handoff"). The proof
# stand-in static uses the same path, so no override is needed for either.
#
# The 5th tclarg still overrides this for any future static whose hierarchy
# differs (e.g. a shell that wraps the RP one level down).
set rp_inst        "u_rp_dut"
if { [llength $argv] >= 5 && [lindex $argv 4] ne "" } {
    set rp_inst [lindex $argv 4]
    puts "INFO: rp_inst overridden from tclargs: $rp_inst"
}
set rp_pblock_name "pblock_rp_dut"

# -----------------------------------------------------------------------------
# ensure_rm_synth_dcp -- resolve (building if necessary) the OOC synth
# checkpoint for one RM, per rm_list.tcl's TODO(A2) choice between:
#   (a) a PRE-BUILT .dcp from a separate synth step, $out_dir/<rm_key>_synth.dcp
#       -- the Z2-probe-parity path, and the right choice for a large,
#       multi-file, externally-sourced RM (rm_nanosoc, rm_eth_ss) once real
#       wrappers exist: keeps re-synth off this script's per-invocation
#       critical path.
#   (b) synth_design -mode out_of_context RIGHT HERE against a single-file,
#       dependency-free RM source -- the practical choice for rm_greybox/
#       rm_led (fpga/dfx/rms/<rm>/<rm>.sv, no deps, seconds to synthesize),
#       so the Phase 1.1/1.2 greybox<->led path is runnable with no separate
#       synth step at all.
# Runs its OWN create_project -in_memory/close_project pair, sequenced BEFORE
# build_config opens the static+RM link project below (Vivado only allows one
# open project at a time, so this must fully close before build_config's
# create_project -in_memory runs).
#
# Returns the resolved .dcp path, or "" if neither (a) nor (b) is available
# yet (rm_nanosoc/rm_eth_ss today) -- caller (the RM-readiness filter in the
# main sequence, below) is expected to have already excluded such RMs, so
# reaching "" here from build_config would be a caller bug, not a normal
# skip path.
# -----------------------------------------------------------------------------
proc rm_is_single_file { rm_key } {
    # REGISTERED, not guessed: rm_list.tcl's synth_mode field says whether this
    # RM is a single dependency-free .sv (inline) or instantiates a whole
    # external source tree (prebuilt). See that file's "synth_mode" block.
    #
    # This USED to be a glob match of the RM's wrapper_dir against the
    # fpga/dfx/rms/ prefix -- "anything under fpga/dfx/rms/ is self-contained".
    # It was
    # true of the five demo RMs and false for rm_socscope, whose wrapper lives
    # there and whose sources come from $SOCSCOPE_HOME. Inline-synthesizing just
    # that wrapper does not fail: it succeeds with the trace plane left as a
    # black box, and poisons every config downstream. A heuristic that is right
    # about the files that existed when it was written is a heuristic that will
    # be wrong about the next one.
    set mode [rm_field $rm_key synth_mode]
    if { $mode ni {inline prebuilt} } {
        error "rm_list.tcl: RM '$rm_key' has synth_mode '$mode' (want inline|prebuilt)"
    }
    return [expr { $mode eq "inline" }]
}

proc ensure_rm_synth_dcp { repo_root out_dir part rm_key } {
    set prebuilt_dcp [file join $out_dir "${rm_key}_synth.dcp"]
    if { [file exists $prebuilt_dcp] } {
        dfx_version_guard $prebuilt_dcp "RM '$rm_key' pre-built OOC checkpoint"
        puts "INFO: RM '$rm_key' -- using pre-built OOC checkpoint $prebuilt_dcp"
        return $prebuilt_dcp
    }

    set rm_top [rm_field $rm_key top]
    set rm_src [file join $repo_root [rm_field $rm_key wrapper_dir] "${rm_top}.sv"]
    if { [file exists $rm_src] && [rm_is_single_file $rm_key] } {
        puts "INFO: RM '$rm_key' -- synthesizing $rm_src OOC (top=$rm_top)"
        create_project -in_memory -part $part
        read_verilog -sv $rm_src
        synth_design -mode out_of_context -top $rm_top -part $part
        write_checkpoint -force $prebuilt_dcp
        close_project
        return $prebuilt_dcp
    }

    puts "WARNING: no synth checkpoint for RM '$rm_key' ($prebuilt_dcp missing; inline synth not applicable) -- [rm_field $rm_key synth_recipe]"
    return ""
}

# -----------------------------------------------------------------------------
# build_config -- link static + one RM, floorplan, implement, report, write
# checkpoint. Mirrors the Z2 probe's single-config flow (link_design ->
# markup -> opt/place/phys_opt/route -> reports -> write_checkpoint), called
# once per RM instead of being probe-inlined for exactly two configs.
# -----------------------------------------------------------------------------
proc build_config { repo_root out_dir part rp_inst rp_pblock_name rm_key first_rm static_locked_dcp } {
    set rm_name [rm_field $rm_key rm_name]
    puts "INFO: ---- building config for RM '$rm_name' ($rm_key) ----"

    # Resolve (build if needed) the RM's OOC synth checkpoint FIRST, via its
    # own create_project/close_project pair -- must fully close before the
    # static+RM link project below opens (only one Vivado project open at a
    # time). See ensure_rm_synth_dcp's header for the (a)/(b) recipe choice.
    set rm_dcp [ensure_rm_synth_dcp $repo_root $out_dir $part $rm_key]
    if { $rm_dcp eq "" } {
        error "build_config: RM '$rm_key' has no synth checkpoint and no buildable source -- caller should have filtered this RM out of the build set (see the RM-readiness filter in the main sequence)"
    }

    # Config 1 (the reference config): open the as-yet-UNlocked static shell
    # synth checkpoint and stitch this RM's OOC synth checkpoint into the RP
    # black box. This is the config whose routed result gets black-boxed +
    # lock_design'd into static_routed_locked.dcp afterwards (main sequence).
    #
    # Config N (N>1): open the ALREADY-locked static checkpoint (routing
    # locked, RP black-boxed) produced from config 1. This is what makes
    # every RM after the first independent of the others -- exactly the Z2
    # probe's static_routed_locked.dcp technique, just reused N-1 times.
    #
    # Both cases are the same two Vivado calls (proven in fpga/dfx/proof/):
    # open_checkpoint + read_checkpoint -cell. The earlier skeleton's
    # create_project/add_files/link_design draft was dropped when this script
    # first ran for real -- link_design needs a SCOPED_TO_CELLS association
    # that read_checkpoint -cell provides directly, and the -cell form is the
    # exact call the proof flow validated on this part.
    open_checkpoint $static_locked_dcp

    # --- RP carve-out (real-shell delta, 2026-07-06) ------------------------
    # A1's shell_static_synth.dcp does NOT ship the RP as an empty black box:
    # the harness build binds a synthesizable inert stub (rp_dut_stub.sv,
    # DONT_TOUCH'd, rm_id="STUB") into u_rp_dut, because a plain black box
    # trips DRC INBB-3 at opt_design in A1's own non-DFX impl (fpga/shell/
    # README.md "Topology"). read_checkpoint -cell needs a black box to fill,
    # so carve the stub out first. No-op for configs N>1 (the locked static
    # is already black-boxed by the extraction step) and for statics whose RP
    # is already empty (the proof stand-in).
    set rp_cell [get_cells -quiet $rp_inst]
    if { $rp_cell eq "" } {
        error "build_config($rm_key): RP cell '$rp_inst' not found in $static_locked_dcp (top-level cells: [get_cells -quiet])"
    }
    if { ![get_property IS_BLACKBOX $rp_cell] } {
        puts "INFO: RP cell $rp_inst carries stub contents (REF_NAME=[get_property REF_NAME $rp_cell]) -- black-boxing before RM link"
        update_design -cells $rp_cell -black_box
    }

    read_checkpoint -cell [get_cells $rp_inst] $rm_dcp

    # --- RP pin tripwire, part 1: the linked boundary IS the contract --------
    # boundary.yaml totals.bits, counted on the RP cell right after link. Part 2
    # (after opt_design, below) catches Vivado punching debug-hub ports into the
    # RP when a static-side ILA/VIO exists (spike 2026-09-23). tools/debug_probes.tcl.
    set contract_bits [boundary_contract_bits $repo_root]
    if { $contract_bits eq "" } {
        puts "WARNING: could not read totals.bits from fpga/shell/boundary.yaml -- RP pin count checked against the link only"
        set rp_link_pins [rp_pin_count $rp_inst]
    } else {
        set rp_link_pins [rp_pin_gate $rm_key $rp_inst "after link (vs boundary.yaml totals.bits)" $contract_bits]
    }

    # --- Z2 lesson #1: manual top clock at checkpoint-link time -------------
    # OOC checkpoints carry no IP-generated XDC. On Z2 this was the PS7
    # FCLKCLK[0] pin (create_clock had to be issued by hand before the
    # generated timing XDC was read, or qspi_sclk_i / set_clock_groups never
    # resolved).
    #
    # RESOLVED for the real shell (2026-07-06 realshell dry-run): NO manual
    # create_clock is needed. A1's shell_static_synth.dcp is a PROJECT-mode
    # post-synth checkpoint, and constraints/mps3_harness.xdc (used in synth,
    # so baked into the DCP) carries the board-clock primary:
    #   create_clock -period 20.000 -name dut_clk [get_ports OSCCLK1]
    # (nb: the legacy-provenance name "dut_clk" is the 50 MHz BOARD OSC, not
    # the RP's dut_clk partition pin). Vivado auto-derives the clk_wiz_shell
    # (100 MHz + 50 MHz RMII) and clk_wiz_dut MMCM output clocks from that
    # primary at link time, so the whole clock tree is constrained without
    # any Z2-style manual create_clock. The proof stand-in similarly carries
    # its osc_clk primary from proof.xdc.
    #
    # The guard below stays: a clockless link means every timing constraint
    # downstream silently resolves to nothing (the Z2 trap), so refuse to
    # continue rather than route an unconstrained design.
    set linked_clocks [get_clocks -quiet]
    if { [llength $linked_clocks] == 0 } {
        error "build_config($rm_key): NO clocks defined after linking static+RM -- the static checkpoint carries no create_clock and the manual dut_clk create_clock (Z2 lesson #1, see comment above) has not been wired for this shell. Refusing to implement an unconstrained design."
    }
    puts "INFO: clocks after link: $linked_clocks"

    # --- shell implementation-only timing XDC (real shell only) -------------
    # A1's build_shell.tcl loads constraints/mps3_harness_timing.xdc with
    # USED_IN_SYNTHESIS=false, so it is NOT baked into shell_static_synth.dcp
    # (unlike the pins XDC and its create_clock, which are). Without it every
    # pad path is timed against the 100 MHz shell clock (the WNS -29 ns class
    # of unmeetable-by-construction I/O paths A1 hit, RESULT.txt fixup #5)
    # and the shell<->DUT MMCM domains stay implicitly synchronous. Read it
    # for the reference config only: write_checkpoint saves constraints, so
    # it travels inside static_routed_locked.dcp to every config N>1.
    # Keyed on the shell BD instance (u_shell) so the proof stand-in static
    # (no u_shell, self-contained proof.xdc) keeps working unchanged.
    # NB: A1's current DCP was written from `open_run synth_1`, which loads
    # impl constraints too -- so this re-read may emit a benign
    # "[Vivado 12-5846] Redefining clock group async_shell_dutclk" warning
    # (confirmed 2026-07-06 dry-run). Kept anyway: it makes this flow correct
    # even for a shell DCP written straight from synth without impl
    # constraints loaded, at the cost of one duplicate-constraint warning.
    if { $first_rm && [llength [get_cells -quiet u_shell]] > 0 } {
        set shell_timing_xdc $repo_root/fpga/shell/constraints/mps3_harness_timing.xdc
        if { ![file exists $shell_timing_xdc] } {
            error "build_config($rm_key): real shell detected (u_shell) but $shell_timing_xdc is missing -- refusing to implement with unconstrained pad paths / undeclared shell<->DUT async clock groups"
        }
        puts "INFO: real shell detected (u_shell cell) -- reading impl-only $shell_timing_xdc"
        dfx_read_xdc $rm_key $shell_timing_xdc
        # The MicroBlaze V static's own impl-only exceptions (SHELL_CONTRACT.md:
        # the DDR calib -> TELEM synchroniser cut), re-read for the same reason
        # as the file above. Only under DFX_SHELL_CPU=mbv, which the Makefile sets
        # only for SHELL_CPU=mbv; absent there is an error, not a skip.
        if { [info exists ::env(DFX_SHELL_CPU)] && $::env(DFX_SHELL_CPU) eq "mbv" } {
            set mbv_timing_xdc $repo_root/fpga/shell/constraints/mbv/mbv_timing.xdc
            if { ![file exists $mbv_timing_xdc] } {
                error "build_config($rm_key): DFX_SHELL_CPU=mbv but $mbv_timing_xdc is missing"
            }
            puts "INFO: DFX_SHELL_CPU=mbv -- reading impl-only $mbv_timing_xdc"
            dfx_read_xdc $rm_key $mbv_timing_xdc
        }
    }

    # --- socketed RM-internal timing XDC (partition-timing.md O3) -----------
    # RESOLVED (2026-07-09) -- was the "FOLLOW-UP -- not implemented here"
    # handshake at the end of docs/contracts/partition-timing.md.
    #
    # After read_checkpoint -cell fills the RP, reapply the RM's OWN internal
    # timing exceptions (its generated clocks, its internal async clock groups)
    # SCOPED to the RP cell. These describe the RM netlist that the static
    # cannot see; without them route_design MISTIMES a genuinely-async
    # RM-internal crossing -- concretely, rm_eth_ss's WB<->MII async FIFO.
    #
    # The boundary clocks themselves still arrive by propagation from the
    # static (the clock guard above); an `_rm.xdc` carries NO boundary
    # create_clock and no reference to any static object -- see
    # partition-timing.md "2. <rm>_rm.xdc". Per that contract:
    #   * applied for EVERY RM (reference config AND configs N>1), because each
    #     RM has its own internal netlist -- it is NOT part of the locked static;
    #   * `-cell` scoped, so an RM's exceptions can never touch static logic
    #     (same scoping discipline as dfx_floorplan.xdc);
    #   * placed after read_checkpoint -cell (so `-cell` resolves the internal
    #     pins its generated clocks target) and after the boundary clocks exist
    #     (so a `create_generated_clock -source <rp>/phy_rmii_ref_clk` finds its
    #     master);
    #   * pr_verify is unaffected: an `_rm.xdc` adds only timing exceptions --
    #     no logic, no placement -- so configs stay PR-compatible.
    #
    # Naming: <wrapper_dir>/<basename(wrapper_dir)>_rm.xdc, e.g.
    #   rm_eth_ss  -> fpga/rp/eth_ss/eth_ss_rm.xdc      (exists: WB<->MII group)
    #   rm_nanosoc -> fpga/rp/nanosoc/nanosoc_rm.xdc    (absent: single-domain)
    # An RM with no internal generated clocks and no internal async crossings
    # needs no _rm.xdc at all; its absence is normal, not an error.
    set rm_wrapper_dir [rm_field $rm_key wrapper_dir]
    set rm_short       [file tail $rm_wrapper_dir]
    set rm_xdc         [file join $repo_root $rm_wrapper_dir "${rm_short}_rm.xdc"]
    if { [file exists $rm_xdc] } {
        puts "INFO: applying RM-internal timing XDC $rm_xdc scoped to $rp_inst"
        dfx_read_xdc $rm_key $rm_xdc -cell [get_cells $rp_inst]
    } else {
        puts "INFO: RM '$rm_key' ships no _rm.xdc (single-domain RM) -- nothing to reapply"
    }

    # --- Z2 lesson #2: pin-facing IOB regs are a STATIC concern, not an RM --
    # one. The Z2 probe had to `set_property IOB FALSE` on the RMII TX pads
    # inside the RP as a probe-only workaround (HDPR-29) because the
    # production fix (move the re-register stage to static) hadn't landed
    # yet. docs/contracts/partition-pins.md designs this in from day one for
    # MPS3 -- no RM here should ever own a pin-facing register, so no
    # equivalent `IOB FALSE` call should be necessary. Left as a checked
    # assumption, not asserted blindly:
    set pin_facing_ports [get_ports -quiet {phy_rmii_txd* phy_rmii_tx_en jtag_tms jtag_tdi jtag_tck}]
    if { [llength $pin_facing_ports] > 0 } {
        puts "WARNING: partition-pin-named ports found inside the RP checkpoint ($pin_facing_ports) -- these should be static-shell pads per partition-pins.md; if this fires, an RM wrapper has drifted from the contract, NOT a case for `IOB FALSE`"
    }

    # --- DFX markup + Pblock (dfx_floorplan.xdc) ----------------------------
    # This is where the I4/SSI single-SLR Pblock constraint and the "do NOT
    # set RESET_AFTER_RECONFIG" (7-series-only property) decisions actually
    # take effect -- both live in dfx_floorplan.xdc itself (sourced, not
    # read_xdc'd, so it sees rp_inst/rp_pblock_name above), not duplicated
    # here. See that file + fpga/dfx/README.md "SLR constraint" / "What's
    # dropped from the Z2 probe" for the full reasoning.
    #
    # Reference config ONLY: for configs N>1 the HD.RECONFIGURABLE property
    # and the Pblock arrive baked inside static_routed_locked.dcp (they were
    # applied before the lock), and re-applying floorplan Tcl to a routing-
    # locked design is at best a no-op -- the proof flow (steps 3 vs 5)
    # validated exactly this split.
    if { $first_rm } {
        source $repo_root/fpga/dfx/dfx_floorplan.xdc
    }

    # --- BITSTREAM.CONFIG.PERSIST OFF (UltraScale/ICAP requirement) --------
    # Mutually exclusive with ICAP-driven reconfiguration -- set explicitly
    # rather than trusting the (usually-off) default, because a silent flip
    # here breaks partial delivery in a way that's painful to root-cause on
    # real hardware.
    set_property BITSTREAM.CONFIG.PERSIST NO [current_design]  ;# UltraScale: NO/YES, not FALSE (proven in fpga/dfx/proof)

    # Every constraint this config will carry is loaded: nothing may have been dropped.
    dfx_xdc_dropped_gate $rm_key link

    # Early DFX-specific DRC, same idea as the Z2 probe: actionable output
    # even if impl fails downstream.
    report_drc -checks [get_drc_checks HDPR*] \
        -file $out_dir/drc_hdpr_${rm_key}_prelink.rpt -no_waivers
    # ...and now a GATE, not just a report (handover §4.7 item 5, U5): HDPR-16/
    # -18/-50 at any severity or any DRC Error stops the build HERE, before
    # hours of place-and-route. scripts/harness_gates/check_hdpr_reports.py.
    hdpr_report_gate $repo_root $out_dir $rm_key prelink

    opt_design
    rp_pin_gate $rm_key $rp_inst "after opt_design (vs the link)" $rp_link_pins
    dfx_xdc_dropped_gate $rm_key opt
    place_design
    phys_opt_design
    route_design

    report_utilization    -pblocks [get_pblocks $rp_pblock_name] \
                          -file $out_dir/util_${rm_key}.rpt
    report_timing_summary -file $out_dir/timing_${rm_key}.rpt
    report_drc            -file $out_dir/drc_${rm_key}.rpt
    hdpr_report_gate $repo_root $out_dir $rm_key routed
    write_checkpoint -force $out_dir/config_${rm_key}_routed.dcp
    close_project   ;# main sequence reopens checkpoints as needed (extraction, bitstreams)

    return $out_dir/config_${rm_key}_routed.dcp
}

# -----------------------------------------------------------------------------
# file_crc32 -- zlib/IEEE CRC-32 of a file's raw bytes, chunked (1 MiB) so a
# multi-MB checkpoint never has to sit in Tcl memory whole. Same polynomial /
# semantics as Python's zlib.crc32 (gen_manifest.py, pyverify, and the wire
# header's crc32 all agree on this -- contract I13).
# -----------------------------------------------------------------------------
proc file_crc32 { path } {
    set fh [open $path rb]
    set crc 0
    while { 1 } {
        set chunk [read $fh 1048576]
        if { [string length $chunk] == 0 } { break }
        set crc [zlib crc32 $chunk $crc]
    }
    close $fh
    return [expr { $crc & 0xFFFFFFFF }]
}

# -----------------------------------------------------------------------------
# harness_version -- HARNESS_VER32 + git provenance, from the ONE generator
# (scripts/gen_version.py, fed by the repo-root VERSION file).
#
# Returns a dict {ver32 sha version}. Shelling out to the generator instead of
# re-implementing the major<<24|minor<<16|patch<<8|flags packing in Tcl is
# deliberate: this repo has been bitten repeatedly by a value computed in two
# places drifting apart (the wrapper-vs-rm_list-vs-manifest rm_id class of bug).
# One encoder, three consumers (firmware, bitstream, manifests).
#
# *** DO NOT "FIX" THE ENVIRONMENT AROUND THIS exec. *** Measured under Vivado
# 2024.1 on this host (the obvious hardening is the exact thing that breaks it):
#   - `python3` on Vivado's PATH is Vivado's OWN bundled interpreter,
#     tps/lnx64/python-3.8.3/bin/python3. With Vivado's environment INTACT it
#     runs gen_version.py correctly.
#   - Scrubbing LD_LIBRARY_PATH first (the usual "Vivado poisons the loader"
#     workaround) makes that interpreter die: `libssl.so.3: cannot open shared
#     object file` -- it needs Vivado's own lib dir on the path.
#   - Forcing /usr/bin/python3 instead dies too, with or without the scrub
#     (`Py_Initialize: Unable to get the locale encoding`) -- Vivado's
#     PYTHONHOME/PYTHONPATH point the system interpreter at the wrong stdlib.
#   - System `git` (which gen_version.py shells out to for sha/dirty) works fine
#     under Vivado's environment as-is.
# So: leave the environment ALONE and let PATH resolve. The one constraint this
# imposes is that gen_version.py must stay Python-3.8-compatible (it is).
# -----------------------------------------------------------------------------
proc harness_version { repo_root } {
    set gen [file join $repo_root scripts gen_version.py]
    if { ![file exists $gen] } {
        error "build_dfx.tcl: $gen not found -- the harness version generator is\
               required to stamp BITSTREAM.CONFIG.USR_ACCESS. Set\
               DFX_NO_VERSION_STAMP=1 to build an UNSTAMPED bitstream on purpose."
    }

    set result [dict create]
    foreach field {ver32 sha version} {
        if { [catch { exec python3 $gen --print $field } val] } {
            error "build_dfx.tcl: gen_version.py --print $field failed: $val\n \
                   (python3 must be able to run $gen; set DFX_NO_VERSION_STAMP=1\
                   to build an UNSTAMPED bitstream on purpose)"
        }
        dict set result $field [string trim $val]
    }
    return $result
}

# -----------------------------------------------------------------------------
# extract_locked_static -- black-box the RP of the REFERENCE config's routed
# checkpoint, lock its routing, write static_routed_locked.dcp, and MINT
# static_id from it. Written to <static_id_path>: out_dir/static_id.txt in the
# one-process flow; the DFX_JOBS ref phase writes it to its state dir instead
# (tools/dfx_jobs.tcl), because out_dir/static_id.txt is make's stage-4 target.
# One body for both, so the static identity has one derivation.
# -----------------------------------------------------------------------------
proc extract_locked_static { out_dir rp_inst routed static_id_path } {
    # --- static extraction (once, from the reference RM's routed result) ---
    open_checkpoint $routed
    update_design -cell [get_cells $rp_inst] -black_box
    lock_design -level routing
    write_checkpoint -force $out_dir/static_routed_locked.dcp
    close_project

    # --- static_id: the shell identity every overlay is keyed to --------
    # SCHEME (fpga/dfx/README.md "static_id scheme"; flagged for A6 to
    # fold into overlay-manifest.md's wording): zlib/IEEE CRC-32 over the
    # raw bytes of static_routed_locked.dcp -- the routed, routing-locked,
    # RP-black-boxed static checkpoint that every RM in this run was (or
    # will be) implemented against -- formatted "0x%08X" (10 chars, upper-
    # case hex). Properties that make it the right key:
    #   - deterministic for a given locked-static file: gen_manifest.py,
    #     pyverify and the shell can all re-derive/compare it with plain
    #     zlib.crc32, no Vivado needed;
    #   - changes whenever the static changes -- including on a mere
    #     rebuild of an unchanged shell (a .dcp is a zip with embedded
    #     timestamps). That is conservative in exactly the direction
    #     overlay-manifest.md demands: "a shell rebuild changes static_id
    #     and invalidates every stored partial";
    #   - 32 bits, fits the wire header's u32 static_id field
    #     (net-protocol.md "Bitstream framing") as-is.
    set static_id [format "0x%08X" [file_crc32 $out_dir/static_routed_locked.dcp]]
    set static_id_fh [open $static_id_path w]
    puts $static_id_fh $static_id
    close $static_id_fh
    puts "INFO: static_id = $static_id (CRC32 of static_routed_locked.dcp; written to $static_id_path)"
    return $static_id
}

# -----------------------------------------------------------------------------
# write_config_bitstreams -- one routed config -> its bitstreams + .ltx, the
# pair check, and its overlay_inputs.txt row (returned). rm_key == reference_rm is
# the reference RM: the FULL-device write (the boot image) with the USR_ACCESS /
# USERID stamps, static_stamp.json and the static .ltx; every other RM gets a
# -cell partial pair. Called by the one-process flow for every RM in RM order,
# and by the DFX_JOBS ref phase for the reference (tools/dfx_jobs.tcl).
# -----------------------------------------------------------------------------
# --- write_bitstream: full (reference RM only, as the boot image) + -------
#     partial+clearing pair for every RM -------------------------------------
#
# Naming is normalised so downstream (Makefile `overlays` -> gen_manifest.py)
# has ONE rule for every RM:
#   partial : $out_dir/config_<rm_key>_<pblock>_partial.bin       (+ .bit)
#   clearing: $out_dir/config_<rm_key>_<pblock>_partial_clear.bin (+ .bit)
# For the reference RM those names fall out of the FULL-device write (Vivado
# suffixes the partial with the pblock name itself); for every other RM the
# -cell write is given that same root explicitly (Vivado's -cell form names
# the partial exactly as told + `_clear` for the clearing -- proven in
# fpga/dfx/proof, where `-cell u_rp_dut .../config_led` yielded
# config_led.bit + config_led_clear.bit at partial size).
proc write_config_bitstreams { out_dir rp_inst rp_pblock_name rm_key routed_dcp reference_rm harness_ver static_id } {
    set pair_root  $out_dir/config_${rm_key}_${rp_pblock_name}_partial

    open_checkpoint $routed_dcp

    if { $rm_key eq $reference_rm } {
        # ---- HARNESS VERSION STAMP (USR_ACCESS / USERID) --------------------
        # *** PLACEMENT IS LOAD-BEARING. DO NOT HOIST THIS EARLIER. ***
        #
        # These are DESIGN properties: they are captured into any checkpoint
        # written from the design that carries them. static_id is the CRC-32
        # (file_crc32) of static_routed_locked.dcp, so ANY design property set
        # before that checkpoint is written perturbs the very bytes being
        # hashed and SILENTLY RE-MINTS static_id (0xE4B1C44A) -- which
        # invalidates every fielded partial and every shell already in the
        # field. That failure would be invisible until a board refused a swap.
        #
        # The trap is real and adjacent: build_config() sets
        # BITSTREAM.CONFIG.PERSIST at :345, BEFORE its write_checkpoint of
        # config_<rm>_routed.dcp at :361 -- and the REFERENCE RM's routed dcp is
        # exactly what gets black-boxed + lock_design'd into
        # static_routed_locked.dcp. Putting USR_ACCESS next to PERSIST would
        # therefore have flowed straight into the hashed bytes. It must NOT go
        # there.
        #
        # HERE is safe, for two INDEPENDENT reasons -- either alone suffices:
        #   1. ORDER. static_routed_locked.dcp was written and closed long ago
        #      (extract_locked_static, which every flow calls before this
        #      proc -- the DFX_JOBS ref phase included), and $static_id was
        #      already computed from it with file_crc32 and written down. The
        #      number is minted. Nothing set now can reach backwards into a CRC
        #      that has already been taken.
        #   2. SCOPE. The design being modified here is a FRESH open_checkpoint
        #      of config_<rm>_routed.dcp, used only to write bitstreams and then
        #      close_project'd. It is never write_checkpoint'd, so these
        #      properties are never persisted to ANY .dcp on disk -- least of
        #      all to static_routed_locked.dcp, which this loop does not even
        #      open.
        # => Bumping VERSION rebuilds firmware and re-stamps the bitstream while
        #    leaving static_id BIT-IDENTICAL. That orthogonality is the whole
        #    point of the scheme (docs/VERSIONING_PLAN.md §3.0).
        #
        # BOTH SLRs: the KU115 is a 2-SLR SSI device (get_slrs -> SLR0 SLR1) and
        # USR_ACCESS/AXSS is a per-SLR configuration register -- the hardware
        # readback exposes REGISTER.USR_ACCESS.SLR0 *and* .SLR1. On the SET side
        # there is exactly ONE knob: `list_property [current_design]` on this
        # part yields BITSTREAM.CONFIG.USR_ACCESS and NO per-SLR variant (no
        # *.SLR0/*.SLR1 design property exists). Vivado's full-device writer
        # replicates the AXSS write into each SLR's configuration stream, so the
        # single set_property below stamps both SLRs with the same value by
        # construction -- they cannot be set independently and cannot diverge.
        #
        # ONLY the FULL bitstream carries this. A PARTIAL cannot: the sole
        # USR_ACCESS BEL (CONFIG_SITE_X0Y0/USR_ACCESS, clock region X5Y1) is
        # OUTSIDE the RP pblock (X2Y0/X3Y0/X2Y1/X3Y1), and AXSS is device-global
        # -- a partial writing it would clobber the harness's own stamp. Hence
        # no set_property in the `-cell` branch below, and none in the
        # INCREMENTAL add-RM path (which writes partials only). RM/design
        # identity travels via rm_id, not USR_ACCESS (VERSIONING_PLAN.md §2.4).
        #
        # Verify after the next shell build, over JTAG, with no firmware running:
        #   open_hw_manager; connect_hw_server; open_hw_target
        #   refresh_hw_device [current_hw_device]
        #   get_property REGISTER.USR_ACCESS.SLR0 [current_hw_device]  ;# expect HARNESS_VER32
        #   get_property REGISTER.USR_ACCESS.SLR1 [current_hw_device]  ;# same value
        # Both read 0x00000000 today (nothing stamps them), so a non-zero
        # readback is itself the proof this landed.
        if { $harness_ver ne "" } {
            set_property BITSTREAM.CONFIG.USR_ACCESS [dict get $harness_ver ver32] [current_design]
            puts "INFO: USR_ACCESS = [dict get $harness_ver ver32] (harness\
                  v[dict get $harness_ver version]) -- stamped into the FULL bitstream,\
                  both SLRs; static_id ($static_id) already minted and UNAFFECTED"
            # USERID = the 8-hex build commit, the second 32-bit config slot
            # (VERSIONING_PLAN.md §3.1). Skipped outside a git checkout, where
            # gen_version.py reports sha "unknown" (not valid hex).
            set sha [dict get $harness_ver sha]
            if { [regexp {^[0-9a-fA-F]{8}$} $sha] } {
                set_property BITSTREAM.CONFIG.USERID 0x$sha [current_design]
                puts "INFO: USERID     = 0x$sha (git provenance)"
            } else {
                puts "INFO: USERID not set (git sha unavailable: '$sha')"
            }
        }

        # Only the reference RM (rm_greybox) needs a FULL bitstream -- it is
        # the one-time boot image the MCC loads from SD (spec §6.1/§8A.3).
        # Every other RM only ever gets delivered as a partial+clearing pair
        # over ICAP; a full bitstream for them is unused in normal operation.
        #
        # The UltraScale-mandatory pairing happens here too: a full-device
        # write against an RP-enabled routed checkpoint emits the full image
        # AND the RM's partial AND its matching clearing bitstream (not a
        # flag -- how UltraScale DFX bitstreams are structured, see
        # fpga/dfx/README.md "clearing-bitstream rule"). `-bin_file` emits
        # the ICAP-ready `.bin` alongside each `.bit` (byte-layout analysis:
        # README "I18"). No separate -cell write for this RM: it would
        # clobber $out_dir/config_${rm_key}.bit (the boot image) with a
        # partial-only bitstream of the same name.
        write_bitstream -force -bin_file $out_dir/config_${rm_key}

        # ---- static_stamp.json: what this run PUT in the boot image ---------
        # The two config-register stamps, written down beside the artefact that
        # carries them, so a later stage can read them WITHOUT Vivado.
        #
        # NOT the authority: tools/stamp_usercode.py reads USERID out of the
        # .bit's own ASCII header and uses this file as a cross-check -- two
        # independent derivations of one stamp that must agree, or the .bit
        # beside the record is not the one the record describes. A value only
        # one path knows is a claim; a value two paths agree on is evidence.
        #
        # REFERENCE RM ONLY: this is the only FULL-device write, and neither
        # stamp can live in a partial (the USR_ACCESS BEL is outside the RP
        # pblock and AXSS is device-global -- VERSIONING_PLAN.md §2.4).
        #
        # get_property, not the values set above: what the DESIGN reports is
        # what write_bitstream just used. Under DFX_NO_VERSION_STAMP nothing was
        # set and both read back as unset, which static_stamp.tcl records as
        # null -- never as a plausible-looking zero.
        set stamp_usercode ""
        set stamp_usr_access ""
        catch { set stamp_usercode   [get_property BITSTREAM.CONFIG.USERID     [current_design]] }
        catch { set stamp_usr_access [get_property BITSTREAM.CONFIG.USR_ACCESS [current_design]] }
        set stamp_path [write_static_stamp $out_dir "config_${rm_key}.bit" \
            $static_id [expr { $harness_ver ne "" ? [dict get $harness_ver version] : "" }] \
            $stamp_usercode $stamp_usr_access]
        puts "INFO: $stamp_path written (USERID=$stamp_usercode USR_ACCESS=$stamp_usr_access)"

        # ---- the STATIC's probes file (tools/debug_probes.tcl) -------------
        # Written here because this is the one config whose full-device image
        # IS the static, and the static is final (locked, id minted). Exists iff
        # the static carries debug cores: the MicroBlaze V static does (SEAM-8
        # MIG hub + the DDR4 calibration slave), the bare-metal one must not.
        # tools/ltx_sidecar.py `static` gates it, both ways, from the Makefile.
        write_static_debug_probes $out_dir $rp_inst $rm_key
    } else {
        # Non-reference RM: partial + clearing only, written -cell against
        # the RP so the emitted names match the reference RM's pblock-
        # suffixed pattern above.
        write_bitstream -force -bin_file -cell [get_cells $rp_inst] $pair_root
    }

    # ---- the RM's partial .ltx: same open routed config as its partial ------
    # Both branches above (reference = full-device write, others = -cell write)
    # land here with the routed config still open. -cell $rp_inst makes this
    # the PARTIAL probe file even for the reference config's full design, and
    # the RP-scoped core count means a future static-side ILA can never make a
    # debug-0 RM grow an .ltx. Gate + marker: tools/debug_probes.tcl.
    write_rm_debug_probes $out_dir $rp_inst $rm_key

    close_project

    # Fail loudly (with what IS on disk) if the {clearing, partial} pair is
    # incomplete -- a lone partial is not a valid overlay per
    # overlay-manifest.md, so don't let it survive to gen_manifest.
    foreach f [list ${pair_root}.bin ${pair_root}_clear.bin ${pair_root}.bit ${pair_root}_clear.bit] {
        if { ![file exists $f] } {
            error "write_bitstream for '$rm_key' did not emit expected artefact: $f (directory holds: [glob -nocomplain -tails -directory $out_dir config_${rm_key}*])"
        }
    }
    puts [format "INFO: %s pair: partial %d bytes (crc32 0x%08x), clearing %d bytes (crc32 0x%08x)" \
        $rm_key [file size ${pair_root}.bin] [file_crc32 ${pair_root}.bin] \
        [file size ${pair_root}_clear.bin] [file_crc32 ${pair_root}_clear.bin]]

    return [overlay_row $out_dir $rm_key [rm_field $rm_key rm_name] \
        [rm_field $rm_key rm_id] ${pair_root}.bin ${pair_root}_clear.bin]
}

# -----------------------------------------------------------------------------
# write_overlay_inputs -- the one writer of overlay_inputs.txt for a full
# stage 4 (the one-process flow, and the DFX_JOBS finish phase).
# -----------------------------------------------------------------------------
# One line per built RM: rm_key rm_name rm_id <partial.bin> <clearing.bin>
# (space-separated; consumed by the Makefile's `overlays` target together
# with static_id.txt). This replaces the old TODO about renaming artefacts
# in-Tcl: renaming into the overlay/<rm_name>/ contract layout is
# gen_manifest.py --copy's job, driven off this file.
proc write_overlay_inputs { out_dir static_id overlay_rows } {
    set inputs_fh [open $out_dir/overlay_inputs.txt w]
    puts $inputs_fh "# rm_key rm_name rm_id partial_bin clearing_bin (build_dfx.tcl, static_id=$static_id)"
    foreach row $overlay_rows {
        puts $inputs_fh [join $row " "]
    }
    close $inputs_fh
}

# -----------------------------------------------------------------------------
# Main sequence
# -----------------------------------------------------------------------------

# --- HARNESS VERSION: resolved EARLY, applied LATE --------------------------
# Resolved here, in the first seconds of the run, so a broken VERSION file or an
# unusable python3 fails the build IMMEDIATELY rather than after hours of
# place-and-route -- but it is not APPLIED to any design until the write_bitstream
# loop far below (see the USR_ACCESS block there for why the placement is
# load-bearing). Escape hatch for a deliberately unstamped build:
#   DFX_NO_VERSION_STAMP=1
set harness_ver ""
if { [info exists ::env(DFX_NO_VERSION_STAMP)] && $::env(DFX_NO_VERSION_STAMP) ne "0" } {
    puts "WARNING: DFX_NO_VERSION_STAMP set -- bitstream will carry NO USR_ACCESS harness stamp"
} else {
    set harness_ver [harness_version $repo_root]
    puts "INFO: harness version v[dict get $harness_ver version]\
          HARNESS_VER32=[dict get $harness_ver ver32] git=[dict get $harness_ver sha]\
          (VERSION + scripts/gen_version.py; stamped into USR_ACCESS at write_bitstream)"
}

# --- INCREMENTAL mode: add RM(s) to an EXISTING locked static ---------------
# Adds one or more RMs as config-N builds against an ALREADY-extracted
# static_routed_locked.dcp from a previous full `prod` run, WITHOUT
# re-routing the reference/other RMs and WITHOUT changing static_id. This is
# the correct way to make a newly-ready RM (e.g. rm_eth_ss) a first-class
# swappable overlay against a shell the fleet is already keyed to: the
# locked static's CRC-32 (= static_id) is a function of a routed .dcp with
# embedded timestamps, so a full re-run would mint a NEW static_id and
# invalidate every already-shipped partial for that shell. Reusing the
# locked static keeps the existing greybox/led/nanosoc triples valid and
# just appends the new one.
#
# Driven by env vars (set by the Makefile `add-rm-eth-ss` target), so the
# normal tclargs signature and full-flow path below are untouched:
#   DFX_ADD_RMS        space/comma list of rm_list.tcl keys to add
#   DFX_REUSE_LOCKED   existing static_routed_locked.dcp to build against
#   DFX_STATIC_ID_FILE existing static_id.txt (read, NOT recomputed)
#   DFX_REF_ROUTED     reference routed dcp (rm_greybox) for pr_verify
# Reuses build_config (first_rm=0 path) + file_crc32 verbatim.
if { [info exists ::env(DFX_ADD_RMS)] && $::env(DFX_ADD_RMS) ne "" } {
    set add_rms [split [string map {"," " "} $::env(DFX_ADD_RMS)]]
    foreach v {DFX_REUSE_LOCKED DFX_STATIC_ID_FILE DFX_REF_ROUTED} {
        if { ![info exists ::env($v)] || $::env($v) eq "" } {
            error "build_dfx.tcl incremental mode (DFX_ADD_RMS set) requires env $v"
        }
    }
    set locked_dcp     [file normalize $::env(DFX_REUSE_LOCKED)]
    set static_id_path  $::env(DFX_STATIC_ID_FILE)
    set ref_routed     [file normalize $::env(DFX_REF_ROUTED)]
    foreach f [list $locked_dcp $static_id_path $ref_routed] {
        if { ![file exists $f] } { error "build_dfx.tcl incremental mode: input not found: $f" }
    }
    dfx_version_guard $locked_dcp "locked static"
    dfx_version_guard $ref_routed "pr_verify reference"
    set idfh [open $static_id_path r]
    set static_id [string trim [read $idfh]]
    close $idfh
    puts "INFO: INCREMENTAL mode -- adding {$add_rms} against locked static $locked_dcp"
    puts "INFO:   static_id=$static_id (read from $static_id_path, NOT recomputed); pr_verify reference=$ref_routed"

    # DFX_ROW_FILE: this process is ONE RM worker of a DFX_JOBS stage 4
    # (tools/dfx_jobs.py; tools/dfx_jobs.tcl). Everything below runs exactly as
    # for `make add-rm-<rm>`, except the two SHARED files: out_dir/static_id.txt
    # is make's stage-4 target (the finish phase writes it, last) and
    # overlay_inputs.txt is merged once, by the finish phase, in RM order. The
    # worker's row goes to its own file instead.
    set row_file [expr { [info exists ::env(DFX_ROW_FILE)] ? $::env(DFX_ROW_FILE) : "" }]
    if { $row_file ne "" } {
        if { [llength $add_rms] != 1 } {
            error "build_dfx.tcl: DFX_ROW_FILE is one RM worker's record, but DFX_ADD_RMS names {$add_rms}"
        }
        file delete -force $row_file
        puts "INFO:   DFX_JOBS worker: row -> $row_file (static_id.txt and overlay_inputs.txt are the finish phase's)"
    } else {
        # keep out_dir/static_id.txt at the SAME id so `make overlays` re-keys the
        # added triple(s) to the shell everything else is already keyed to
        set sfh [open $out_dir/static_id.txt w]
        puts $sfh $static_id
        close $sfh
    }

    set added_rows {}
    foreach rm_key $add_rms {
        # config N (first_rm=0): reuse the locked static, no re-floorplan, no
        # shell-XDC re-read (both baked into the locked checkpoint), no static
        # re-extraction, no static_id recompute -- exactly the led/nanosoc
        # path from the full run, just seeded from a persisted locked static.
        set routed [build_config $repo_root $out_dir $part $rp_inst $rp_pblock_name \
                        $rm_key 0 $locked_dcp]
        pr_verify $ref_routed $routed -file $out_dir/pr_verify_${rm_key}.rpt

        set pair_root $out_dir/config_${rm_key}_${rp_pblock_name}_partial
        open_checkpoint $routed
        write_bitstream -force -bin_file -cell [get_cells $rp_inst] $pair_root
        # the .ltx from the SAME open routed config as the partial (F8, §4.7)
        write_rm_debug_probes $out_dir $rp_inst $rm_key
        close_project
        foreach f [list ${pair_root}.bin ${pair_root}_clear.bin ${pair_root}.bit ${pair_root}_clear.bit] {
            if { ![file exists $f] } {
                error "write_bitstream for '$rm_key' did not emit expected artefact: $f (directory holds: [glob -nocomplain -tails -directory $out_dir config_${rm_key}*])"
            }
        }
        puts [format "INFO: %s pair: partial %d bytes (crc32 0x%08x), clearing %d bytes (crc32 0x%08x)" \
            $rm_key [file size ${pair_root}.bin] [file_crc32 ${pair_root}.bin] \
            [file size ${pair_root}_clear.bin] [file_crc32 ${pair_root}_clear.bin]]
        lappend added_rows [overlay_row $out_dir $rm_key [rm_field $rm_key rm_name] \
            [rm_field $rm_key rm_id] ${pair_root}.bin ${pair_root}_clear.bin]
    }

    if { $row_file ne "" } {
        # Written LAST, after the pair check above: a row exists only for a
        # config whose pr_verify, partial pair and .ltx gate all passed.
        dfx_jobs_write_row $row_file $static_id [lindex $added_rows 0]
        puts "DFX_ADD_COMPLETE static_id=$static_id added={$add_rms} (DFX_JOBS worker; row -> $row_file)"
        return
    }

    # Merge into overlay_inputs.txt: preserve existing rows (so `make overlays`
    # still rebuilds every triple), drop any stale row for a re-added RM, and
    # append the freshly-built one(s). Header carries the (unchanged) static_id.
    set inputs_path $out_dir/overlay_inputs.txt
    set existing {}
    if { [file exists $inputs_path] } {
        set ifh [open $inputs_path r]
        foreach line [split [read $ifh] "\n"] {
            if { $line eq "" || [string index $line 0] eq "#" } { continue }
            if { [lsearch -exact $add_rms [lindex $line 0]] >= 0 } { continue }
            # Rows written before 2026-09-10 are ABSOLUTE. Relativize them on
            # the way through rather than leaving one file half-portable:
            # overlay_row_rel passes an already-relative path back unchanged, so
            # this is a no-op on rows this version wrote.
            set cells [split $line " "]
            if { [llength $cells] == 5 } {
                set line [join [overlay_row $out_dir [lindex $cells 0] \
                                    [lindex $cells 1] [lindex $cells 2] \
                                    [lindex $cells 3] [lindex $cells 4]] " "]
            }
            lappend existing $line
        }
        close $ifh
    }
    set ofh [open $inputs_path w]
    puts $ofh "# rm_key rm_name rm_id partial_bin clearing_bin (build_dfx.tcl incremental add, static_id=$static_id)"
    foreach line $existing { puts $ofh $line }
    foreach row $added_rows { puts $ofh [join $row " "] }
    close $ofh

    puts "DFX_ADD_COMPLETE static_id=$static_id added={$add_rms} (pr_verify + pairs in $out_dir; next: make -C fpga/dfx overlays)"
    return
}

# --- RM build-set selection ("RM readiness filter") -------------------------
# rm_list.tcl's RM_ORDER is the full RM LIBRARY (every RM anyone has ever
# registered), not necessarily every RM that is buildable RIGHT NOW. Two ways
# to pick the actual build set for this invocation:
if { $rm_subset_arg ne "" } {
    # Explicit override (4th tclarg): e.g. "rm_greybox,rm_led" for the
    # Phase 1.1/1.2 greybox<->LED-counter inner loop specifically.
    set rm_names [split [string map {"," " "} $rm_subset_arg]]
    puts "INFO: RM build set from explicit -tclargs override: $rm_names"
} else {
    # Default: filter RM_ORDER down to RMs that ensure_rm_synth_dcp can
    # actually resolve today (pre-built .dcp OR a single-file source under
    # wrapper_dir/top.sv) -- see that proc's header. This is what keeps
    # `build_dfx.tcl <repo_root> <out_dir> <static_shell_dcp>` (no 4th arg)
    # runnable-shaped without erroring out deep inside link_design on an
    # external-source RM (rm_nanosoc/rm_eth_ss) whose OOC dcp hasn't been
    # pre-staged this invocation (`make rm-nanosoc-dcp` / `rm-eth-ss-dcp`
    # stage them; both wrappers now exist and pr_verify against the 256 KiB
    # shell).
    set rm_names {}
    foreach rm_key [rm_all_names] {
        set prebuilt [file join $out_dir "${rm_key}_synth.dcp"]
        set src      [file join $repo_root [rm_field $rm_key wrapper_dir] "[rm_field $rm_key top].sv"]

        if { [file exists $prebuilt] } {
            lappend rm_names $rm_key
            continue
        }
        if { ![rm_is_single_file $rm_key] } {
            # External-source RM (rm_nanosoc, rm_eth_ss): only buildable via a
            # pre-staged OOC checkpoint (e.g. `make rm-nanosoc-dcp`); its
            # wrapper .sv alone is NOT a synthesizable unit (see
            # rm_is_single_file). Skip rather than fail later in build_config.
            puts "INFO: RM '$rm_key' not buildable in this invocation (no pre-staged $prebuilt; not single-file synthesizable) -- skipping. [rm_field $rm_key synth_recipe]"
            continue
        }
        if { ![file exists $src] } {
            puts "INFO: RM '$rm_key' not buildable yet (no $src, no $prebuilt) -- skipping. [rm_field $rm_key synth_recipe]"
            continue
        }

        # Heuristic port-list freshness check -- a lightweight stand-in for
        # rm_list.tcl's own TODO(A2) "pin_check" (a real mechanical diff of
        # each wrapper's ports against partition-pins.md, still not written).
        # partition-pins.md's I4 board-GPIO passthrough group
        # (dut_gpio_o/dut_gpio_oe/dut_gpio_i) was added in v0.1 AFTER some
        # wrappers were first stubbed (rm_nanosoc's, specifically -- see the
        # MISMATCH note on that RM_LIB entry above). Feeding such a stale
        # source into link_design/pr_verify would fail there instead --
        # later, and much less legibly than this grep. Not a substitute for
        # a real port-list diff, just enough to catch the ONE known drift
        # class today without silently building a doomed config.
        set fh [open $src r]
        set src_text [read $fh]
        close $fh
        if { [string first "dut_gpio_o" $src_text] < 0 } {
            puts "INFO: RM '$rm_key' source exists ($src) but looks STALE against partition-pins.md v0.1 (missing dut_gpio_o/dut_gpio_oe/dut_gpio_i -- the I4 board-GPIO passthrough group) -- skipping until its wrapper is updated. See rm_list.tcl's MISMATCH note; not a bug in this filter."
            continue
        }

        lappend rm_names $rm_key
    }
    puts "INFO: RM build set (readiness-filtered, default): $rm_names"
}

if { [llength $rm_names] < 2 } {
    error "build_dfx.tcl: need at least 2 buildable RMs to run pr_verify (got: $rm_names) -- either the readiness filter excluded everything but the reference RM, or an explicit -tclargs rm_subset was too short"
}
if { [lindex $rm_names 0] ne "rm_greybox" } {
    error "build_dfx.tcl: rm_greybox must be first in the build set (it is the DFX reference config static_routed_locked.dcp is extracted from) -- got '[lindex $rm_names 0]' first. Check rm_list.tcl's RM_ORDER / the -tclargs rm_subset override."
}

set reference_rm [lindex $rm_names 0]
;# rm_greybox is first -- it is both the DFX reference config AND the
;# boot-time greybox default (spec D14), so it doubles as the RM whose routed
;# checkpoint gets black-boxed+locked into static_routed_locked.dcp, same
;# role the Z2 probe's config_nanosoc played before extraction.

set static_locked_dcp $static_shell_dcp
;# First pass through the loop below, static_locked_dcp is the RAW
;# (not-yet-locked) shell checkpoint A1 hands off (stub carved out inside
;# build_config); after the reference RM's config is routed, we black-box +
;# lock it (mirrors the Z2 probe's `update_design -cell $rp_cell -black_box`
;# + `lock_design -level routing` step) and REPLACE static_locked_dcp with
;# that result for every subsequent RM. Left explicit here rather than
;# folded into build_config for clarity on first read.

# --- DFX_JOBS phases (tools/dfx_jobs.py; tools/dfx_jobs.tcl) -----------------
# DFX_PHASE is set only by dfx_jobs.py, i.e. only when the Makefile's DFX_JOBS is
# 2 or more. The RM build set above is resolved and checked exactly as for the
# one-process flow; then this process does ONE phase of it and stops:
#   ref    -- the reference config, the lock, static_id (to dfx_jobs/), and the
#             reference's bitstreams;
#   finish -- every other RM's row (written by its own worker process: the
#             incremental add above with DFX_ROW_FILE) -> overlay_inputs.txt ->
#             static_id.txt, last.
set dfx_phase [dfx_jobs_phase]
if { $dfx_phase eq "ref" } {
    dfx_phase_ref $repo_root $out_dir $part $rp_inst $rp_pblock_name $rm_names $static_shell_dcp $harness_ver
    return
}
if { $dfx_phase eq "finish" } {
    dfx_phase_finish $out_dir $rm_names
    return
}

set routed_dcps {}
set first 1
foreach rm_key $rm_names {
    set routed [build_config $repo_root $out_dir $part $rp_inst $rp_pblock_name \
                    $rm_key $first $static_locked_dcp]
    lappend routed_dcps $routed

    if { $first } {
        set static_id [extract_locked_static $out_dir $rp_inst $routed $out_dir/static_id.txt]
        set static_locked_dcp $out_dir/static_routed_locked.dcp
    }
    set first 0
}

# --- pr_verify: reference RM's routed config against every other RM ---------
set ref_dcp [lindex $routed_dcps 0]
for {set i 1} {$i < [llength $routed_dcps]} {incr i} {
    set other_dcp  [lindex $routed_dcps $i]
    set other_rm   [lindex $rm_names $i]
    pr_verify $ref_dcp $other_dcp -file $out_dir/pr_verify_${other_rm}.rpt
}

# --- write_bitstream: full (reference RM only, as the boot image) + -------
#     partial+clearing pair for every RM, in RM order (write_config_bitstreams)
set overlay_rows {}
foreach rm_key $rm_names {
    set idx        [lsearch -exact $rm_names $rm_key]
    set routed_dcp [lindex $routed_dcps $idx]
    lappend overlay_rows [write_config_bitstreams $out_dir $rp_inst $rp_pblock_name $rm_key \
        $routed_dcp $reference_rm $harness_ver $static_id]
}

# --- overlay_inputs.txt: the hand-off to gen_manifest.py (write_overlay_inputs)
write_overlay_inputs $out_dir $static_id $overlay_rows

puts "DFX_BUILD_COMPLETE static_id=$static_id rms={$rm_names} (pr_verify + bitstream pairs in $out_dir; next: make -C fpga/dfx overlays)"
