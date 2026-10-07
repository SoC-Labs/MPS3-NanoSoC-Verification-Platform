# -----------------------------------------------------------------------------
# fpga/rp/clcd_demo/rm_list_snippet.tcl -- the registration block to PASTE into
# fpga/dfx/rm_list.tcl.
#
# THIS FILE IS NEVER SOURCED. It is a copy-paste fragment; sourcing it alone
# would define an RM the build cannot find. `fpga/dfx/rm_list.tcl` is the single
# registry the whole flow reads -- build_dfx.tcl, pin_check.py (whose target set
# comes from RM_ORDER, so a registered RM is gated automatically),
# check_rm_id_encoding.py and gen_manifest.py all start there.
#
# WHY IT IS STILL A SNIPPET AND NOT ALREADY IN THE REGISTRY: rm_list.tcl is the
# MINT FLOW's file and has other owners. Registering an RM there commits the
# next mint to building it, and it makes check_rm_id_encoding.py demand a
# matching fpga/dfx/overlay/clcd_demo/manifest.json that does not exist yet. So
# this RM proves its boundary against a SCRATCH MIRROR of the registry instead
# -- see README.md "Proving pin-check without touching the registry", which is
# the same discipline fpga/rp/nanosoc_iice uses to gate a wrapper the registry
# does not name.
#
# design_id 0x0007 is the next free id after socscope's 0x0006. Verify before
# pasting -- rm_list.tcl errors at source time on a duplicate:
#
#     tclsh -c 'source fpga/dfx/rm_list.tcl; foreach r $RM_ORDER { \
#         puts "$r $RM_LIB($r,design_id)" }'
#
# VERSION 0.1.0, deliberately NOT 1.0.0. `1.0.0` is this repo's floor for "has
# been pr_verified against a real locked static AND exercised on silicon"
# (rm_list.tcl "VERSION POLICY"). This RM has been neither. Bump it to 1.0.0 in
# the same commit that records the Wave-C photograph.
# -----------------------------------------------------------------------------

# --- 1. the RM entry: paste beside the other `set RM_LIB(...)` blocks --------
# rm_clcd_demo -- a CPU-less test-card generator on the display tunnel. It exists
# to answer ONE question with a photograph: has any DUT ever driven the on-board
# panel? The KVM, the tunnel and the DUT-side socket are all fielded on
# 0xA8C1C535 and none of them has been observed end to end, because every other
# candidate needs a CPU to boot and firmware to be right. This one is pure RTL:
# it starts on reset and paints colour bars plus a 16-cell binary frame counter.
# STATUS: sim-proven (tests/clcd_demo), NEVER on silicon. See
# fpga/rp/clcd_demo/README.md and docs/CLCD_KVM_PLAN.md "Proving it".
set RM_LIB(rm_clcd_demo,wrapper_dir)  "fpga/rp/clcd_demo"
set RM_LIB(rm_clcd_demo,top)          "rp_clcd_demo_wrapper"
set RM_LIB(rm_clcd_demo,design_id)    "0x0007"
set RM_LIB(rm_clcd_demo,version)      "0.1.0"      ;# => rm_id 0x00010007
set RM_LIB(rm_clcd_demo,rm_name)      "clcd_demo"
# "prebuilt", NOT "inline": this RM has a filelist.tcl (it reads the SHARED
# clcd_core.sv out of fpga/shell/ip/clcd/ rather than vendoring a second copy of
# the one 8080 engine that has a proof). build_dfx.tcl's inline path reads only
# <wrapper_dir>/<top>.sv, which would silently infer clcd_core and clcd_demo_gen
# as BLACK BOXES and "succeed" with an RM that draws nothing -- the exact failure
# rm_list.tcl's synth_mode block was added to prevent.
set RM_LIB(rm_clcd_demo,synth_mode)   "prebuilt"
set RM_LIB(rm_clcd_demo,synth_recipe) "OOC synth via fpga/rp/clcd_demo/ooc_synth.tcl (sources = fpga/rp/clcd_demo/filelist.tcl: shell clcd_core.sv + clcd_demo_gen.sv + the wrapper) -- stage via `make -C fpga/dfx rm-clcd-demo-dcp`"

# --- 2. append the rm_key to RM_ORDER ----------------------------------------
# rm_greybox MUST stay first and rm_led second; beyond those the order is
# cosmetic, so append at the end:
#
#   set RM_ORDER [list rm_greybox rm_regdemo_a rm_regdemo_b rm_led rm_uart_echo \
#                      rm_nanosoc rm_eth_ss rm_nanosoc_multicore rm_nanosoc_upy \
#                      rm_socscope rm_clcd_demo]

# --- 3. the things that must land in the SAME commit -------------------------
#   * ONE line in fpga/dfx/Makefile beside the other RM_SYNTH_TCL_* entries:
#         RM_SYNTH_TCL_rm_clcd_demo := $(REPO_ROOT)/fpga/rp/clcd_demo/ooc_synth.tcl
#     Without it `make -C fpga/dfx rm-clcd-demo-dcp` (dashes) stops with
#     "RM 'rm_clcd_demo' has no registered OOC synth recipe".
#   * fpga/dfx/overlay/clcd_demo/manifest.json with rm_id 0x00010007 --
#     scripts/harness_gates/check_rm_id_encoding.py (make check stage 2) fails
#     the build if the wrapper localparam, this entry and the manifest disagree.
#   * a `clcd_demo` row wherever the resident RM is NAMED for a human:
#     firmware/clcd/clcd.c's clcd_rm_name()/clcd_rm_caps() (keyed on
#     CLCD_RM_DESIGN(rm_id) = rm_id & 0xFFFF, i.e. 0x0007) and pyverify's
#     known_rm_names. Without it the panel and the host both render "rm?..."
#     for the RM whose entire job is to be recognised on the panel.
