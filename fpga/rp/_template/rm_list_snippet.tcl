# -----------------------------------------------------------------------------
# rm_list_snippet.tcl -- the registration block to PASTE into
# fpga/dfx/rm_list.tcl when you add an RM built from fpga/rp/_template/.
#
# This file is never sourced. It is a copy-paste fragment; sourcing it on its
# own would define an RM the build cannot find. `fpga/dfx/rm_list.tcl` is the
# single registry the whole flow reads -- build_dfx.tcl, pin_check.py (which
# derives its target set from RM_ORDER, so a registered RM is checked
# automatically and cannot slip past the gate), check_rm_id_encoding.py and
# gen_manifest.py all start there.
#
# Substitute throughout:
#   <name>      the RM's short name        e.g. led, eth_ss, nanosoc_upy
#               -> rm_key  rm_<name>
#               -> rm_name <name>          == the overlay/<name>/ directory
#   <top>       the wrapper module         e.g. rp_<name>_wrapper
#   <NNNN>      an UNALLOCATED 16-bit design_id (see below)
#
# ALLOCATING design_id. It must be unique -- rm_list.tcl errors at source time
# if two RMs share one, because a duplicate makes RM-load verify ambiguous and
# makes the CLCD's masked name lookup print the wrong DUT. Read the currently
# allocated set straight out of the registry rather than from any document:
#
#     tclsh -c 'source fpga/dfx/rm_list.tcl; foreach r $RM_ORDER { \
#         puts "$r $RM_LIB($r,design_id) $RM_LIB($r,rm_id)" }'
#
# VERSION. `1.0.0` is the honest floor for an RM that has been pr_verified
# against a real locked static and exercised on silicon; use it once yours has,
# and note that rm_id changes when you bump major/minor (encoding v2 puts the
# version in the high half), which is exactly what check_rm_id_literals.py
# exists to catch in every consumer that pinned the old number.
#
# rm_id is DERIVED, never written: rm_list.tcl computes
#     rm_id = (major << 24) | (minor << 16) | design_id
# and check_rm_id_encoding.py holds the wrapper localparam, this entry and
# overlay/<name>/manifest.json in lockstep.
# -----------------------------------------------------------------------------

# --- 1. the RM entry: paste beside the other `set RM_LIB(...)` blocks --------
# Say what the RM IS and what proves it -- every existing entry does, and the
# per-RM STATUS line is how a reader learns whether it has ever been on silicon.
set RM_LIB(rm_<name>,wrapper_dir)  "fpga/rp/<name>"
set RM_LIB(rm_<name>,top)          "<top>"
set RM_LIB(rm_<name>,design_id)    "0x<NNNN>"
set RM_LIB(rm_<name>,version)      "1.0.0"      ;# => rm_id 0x0100<NNNN>
set RM_LIB(rm_<name>,rm_name)      "<name>"
# ip_class -- who may redistribute the partial. "arm-aaa" if ANY source reaches
# Arm-licensed IP (Cortex-M0/M0+, CMSDK, SoC-400, CMSDK_DIR, ARM_IP_LIBRARY_PATH,
# the Arm IP library); "open" only when none does. Say why in the comment. Defaulted
# to the restrictive value on purpose: rm_list.tcl refuses to source without it,
# and check_overlay_ip_class.py fails an "open" RM that names an Arm-IP root.
set RM_LIB(rm_<name>,ip_class)     "arm-aaa"    ;# <evidence: the file that pulls Arm IP, or why it is clean>
# synth_mode -- HOW the OOC checkpoint comes into being. REGISTERED, not
# guessed, because the old path-match heuristic ("anything under fpga/dfx/rms/
# is one self-contained file") does not fail on an externally-sourced RM: it
# "succeeds" with the whole design inferred as a BLACK BOX.
#   "inline"    a single dependency-free .sv at <wrapper_dir>/<top>.sv that
#               build_dfx.tcl may synthesise itself, in seconds, with no
#               external environment.
#   "prebuilt"  the RM pulls in an external source tree; its .dcp MUST arrive
#               pre-staged as <out_dir>/<rm_key>_synth.dcp.
# A template copy with a `filelist.tcl` is "prebuilt"; without one, "inline".
# tests/dfx_flow/test_mint_record.py fails if this field is missing.
set RM_LIB(rm_<name>,synth_mode)   "prebuilt"
set RM_LIB(rm_<name>,synth_recipe) "OOC synth via fpga/rp/<name>/ooc_synth.tcl (sources = fpga/rp/<name>/filelist.tcl, or the wrapper alone if there is none) -- stage via `make -C fpga/dfx rm-<name>-dcp`"

# --- 2. append the rm_key to RM_ORDER ----------------------------------------
# rm_greybox MUST stay first (it is the DFX reference config the locked static
# is extracted from) and rm_led second (the Phase 1.1/1.2 acceptance path);
# beyond those two the order is cosmetic, so append at the end:
#
#   set RM_ORDER [list rm_greybox ... rm_socscope rm_<name>]
#
# Adding it here is what puts the wrapper into `make -C fpga/dfx pin-check`.
# Register FIRST, then run pin-check -- that is the fast way to find a boundary
# mistake, and it costs a second instead of a full synth + place + route.
