################################################################################
# fpga/hooks/pre_synth.tcl - DID THE PARAMETER YOU DECLARED ACTUALLY REACH THE
#                            DESIGN THE TOOL IS ABOUT TO SYNTHESISE?
#
# A WORKING CHECK, NOT AN ILLUSTRATION. It asserts nothing until this project
# declares RTL_ASSERT_TABLE, and until then it REFUSES THE STAGE and says how to
# fix that - every run, in the log, in words. "We did not check" and "we checked
# and it was clean" must never read alike, and an unconfigured hook that passed
# quietly would be the second one wearing the first one's face.
#
# If nanosoc_mps3 has nothing to assert here, DELETE THIS FILE. That is the honest
# way to say so and nothing else breaks.
#
# ------------------------------------------------------------------------------
# WHERE THIS RUNS
# ------------------------------------------------------------------------------
#   Sourced by `flow_hook pre_synth`, whose machinery is
#   $(FPGA_FLOW_DIR)/flow/common/flow_utils.tcl - `proc flow_hook`, line 492
#   when this template was written; line 505 sources this file inside a catch
#   that names it and re-raises, line 517 records `pre_synth(<n>s)` in the
#   stage manifest.
#
#   THE CALL SITE IS flow/vivado/4_synth.tcl AND AT THE TIME OF WRITING THAT
#   FILE DOES NOT EXIST. This toolkit is at phase 1: the contract layer is
#   built and no EDA tool is launched by anything in it, so flow/vivado/ is an
#   empty directory and `make synth` fails with "stage 'synth' has no script".
#   The seam is contracted; the stage that fires it is not written. Do not take
#   the ordering below on trust - when the stage lands, read it:
#
#       grep -n 'flow_hook pre_synth' $(FPGA_FLOW_DIR)/flow/vivado/*.tcl
#
#   WHAT HAS HAPPENED BY THEN, and what this check depends on:
#     - flow_boot has run. The part and board packs are loaded, the four run
#       directories exist, $REPORT_DIR is writable, $block_name is set.
#     - the sources are READ - the flist stage wrote $WORK_DIR/sources.tcl and
#       the synth stage has consumed it - so the fileset carries whatever
#       generics and defines the flow applied to it.
#     - IP packaging, if this design packages IP, is ALREADY DONE. It is an
#       earlier stage (package-ip) and it is where the defect below happens.
#
#   WHAT HAS NOT HAPPENED:
#     - nothing is elaborated. There is no netlist, no cell, no primitive, and
#       no question about them can be asked here. That is `post_synth`.
#     - synth_design has not run. Everything this hook reads is INPUT.
#
# ------------------------------------------------------------------------------
# WHY THIS EXISTS - A MEASURED DEFECT, WITH THE NUMBER THAT PROVED IT
# ------------------------------------------------------------------------------
#   `ipx::package_project` DROPS FILESET DEFINES, by three separate routes, and
#   none of them warn. In this codebase an `ifdef TIDELINK_USE_IDELAY` opt-in
#   was therefore false in EVERY FPGA build - the builds that opted in and the
#   builds that did not. It was found by building both and discovering the
#   "IDELAY-off" bitstream was BYTE-IDENTICAL to the "IDELAY-on" one: the
#   primitive was absent from every build that believed it had it, for months,
#   with the tool reporting nothing, ever.
#
#   Two consequences, and this file is the first of them:
#
#     RTL_PARAMS IS THE PRIMARY MECHANISM. Parameters survive packaging as
#     CONFIG.* properties on the IP; defines do not. Prefer a module parameter
#     over a macro for anything that selects behaviour.
#
#     AND THE SECOND HALF OF THE SAME LESSON: this codebase has ZERO
#     occurrences of `ifdef FPGA` and `ifdef ASIC` across 13,524 RTL files,
#     along with XILINX, VIVADO, SIMULATION and FPGA_ONLY. A flow that
#     configures the build with +define+FPGA CONFIGURES NOTHING. Selection here
#     is by FLIST FILE-SWAP (same module name, opposite directory) and by
#     MODULE PARAMETER.
#
#   So the question this hook answers is not "did I write it in design.mk" -
#   you can read that. It is "is it still there, in the tool, on the fileset
#   that is about to be synthesised, AFTER everything the flow did to get here".
#
#   ITS OTHER HALF IS fpga/hooks/post_impl.tcl, which counts the primitives the
#   opt-in was supposed to produce. This one proves the tool was TOLD. That one
#   proves it HAPPENED. Neither substitutes for the other: a define can reach
#   the fileset and be read by no file, and a primitive can appear for a reason
#   nobody declared.
#
# ------------------------------------------------------------------------------
# HOW TO CONFIGURE IT
# ------------------------------------------------------------------------------
#   Declare RTL_ASSERT_TABLE - either as an exported make variable in
#   fpga/design.mk (beside the other gates in section 12), or as a Tcl global
#   set by an earlier hook or a step override. The global wins; both spellings
#   are RTL_ASSERT_TABLE, so a message can name one variable and mean both.
#
#   ONE TABLE PER SEAM, WHEN THE SEAMS ASK DIFFERENT QUESTIONS.
#   RTL_ASSERT_TABLE_<SEAM> - RTL_ASSERT_TABLE_POST_BD, RTL_ASSERT_TABLE_PRE_SYNTH
#   - is read first and RTL_ASSERT_TABLE is the fallback for every seam that has
#   no table of its own. This exists because a row is not evaluable everywhere:
#   an `ip` row naming a block-design cell can only be answered where a block
#   design is open, and a `define` row can only be answered where the synthesis
#   fileset exists. One shared table made whichever seam ran first refuse on the
#   other seam's row. The variable actually used is printed and lands in the
#   record as `table_variable`.
#
#   ONE ROW PER EXPECTATION. Fill in the two markers below and delete this
#   commented block once the real declaration is in design.mk - `make check`
#   greps for the markers and they are how a fresh scaffold reports that this
#   decision is still outstanding.
#
#   DECLARED for this project in design.mk - see RTL_ASSERT_TABLE there. It
#   asserts IMEM_MEM_FPGA_IMG is PRESENT as a parameter and deliberately does
#   NOT assert a value, because the value is a build-specific path. That is the
#   weaker form the KEYS note below describes, and it is reported as weaker.
#
#   KEYS. An UNKNOWN KEY IS FATAL, not ignored: an expectation nothing reads is
#   worse than no expectation at all, because it looks like cover.
#
#     name    REQUIRED  the parameter or macro name, as the RTL spells it
#     kind    param | define                                  (default param)
#     value   the value it must hold. Omit to assert PRESENCE ONLY, which is a
#             weaker claim and is reported as such
#     expect  present | absent                              (default present)
#             `absent` is how RTL_DEFINES_NEVER is enforced - an ASIC-only
#             macro reaching an FPGA build swaps a memory wrapper for one that
#             instantiates a foundry macro the fabric does not have, and the
#             failure is a black box in the netlist rather than an error
#     ip      an IP instance name, or a BLOCK-DESIGN CELL name. Reads
#             CONFIG.<name> on that object instead of the fileset property - the
#             mechanism that SURVIVES packaging, so this is the strong form of
#             the assertion in a packaged flow.
#
#             IT NEEDS A SEAM WHERE THAT OBJECT EXISTS, and in a checkpoint flow
#             pre_synth is not one: measured 2026-09-08, `get_ips` returns
#             NOTHING at that seam on a block-design design, because the BD's IP
#             objects belong to the project the bd stage built and this session
#             only read the design back out of it. The lookup tries, in order,
#             `get_ips <name>`, the BD-qualified `get_ips *_<name>_*` (an IP
#             inside a block design is named <bd>_<cell>_<n>), and
#             `get_bd_cells /<name>`; when none of them answers it refuses and
#             names the seam. A row with an `ip` key on a BD cell belongs in
#             RTL_ASSERT_TABLE_POST_BD, at fpga/hooks/post_bd.tcl.
#     why     free text. Printed and recorded. Write it: a row whose reason is
#             not written down is a row the next person deletes to get a build
#
#   Two rows for one (kind, name) are fatal - the second would silently win.
#
# ------------------------------------------------------------------------------
# WHAT THIS DELIBERATELY DOES NOT CHECK
# ------------------------------------------------------------------------------
#   * THAT ANY RTL FILE READS THE MACRO. It cannot, here: nothing is elaborated
#     and a `define` is a property of the fileset, not of the design. Given
#     that this codebase has zero `ifdef` of the usual names, a green row for a
#     define means THE TOOL WAS TOLD and nothing more. The strong form of that
#     assertion is a parameter (visible in the report, in the BD, and in the
#     netlist) plus post_impl.tcl counting what it produced.
#   * THAT THE VALUE IS LEGAL for the module, or that it is the value the
#     module's own default would have been. Vivado does not normalise the
#     spelling, so `1`, `1'b1` and `32'd1` are three different strings and this
#     compares strings.
#   * PER-FILE defines, and anything applied inside a packaged IP that the row
#     did not name with `ip`.
#   * ANYTHING IN A SESSION WITH NO SOURCE FILESET. Generics and defines are
#     arguments to synth_design; the fileset properties this hook reads are a
#     second copy of them, and where there is no fileset there is nothing to
#     query. It refuses rather than reporting a clean sheet it did not read.
#     (This used to say "anything in direct mode". That was wrong, and measured
#     wrong: the synthesis stage opens `create_project -in_memory` before it
#     reads a source, because an IP resolves against the part that is set when
#     it is read - and an in-memory project HAS a sources_1 fileset carrying
#     GENERIC and VERILOG_DEFINE. The test is whether a fileset is there, never
#     which mode the run declared.)
#
# ------------------------------------------------------------------------------
# HOW TO REPRODUCE THE MEASUREMENT WITHOUT RUNNING A STAGE
# ------------------------------------------------------------------------------
#   The declared side needs no tool at all:
#
#       make env | grep -E 'RTL_(PARAMS|DEFINES)'
#
#   The tool side needs a Vivado session and the project this flow built:
#
#       vivado -mode batch -source /dev/stdin <<'EOF'
#       open_project build/<run tag>/work/<project>.xpr
#       puts "generic: [get_property GENERIC        [current_fileset]]"
#       puts "define : [get_property VERILOG_DEFINE [current_fileset]]"
#       EOF
#
#   And the record this hook leaves, which is collected with the run:
#
#       $REPORT_DIR/rtl_assert_pre_synth.txt
#
#   Copyright (C) 2026, SoC Labs (www.soclabs.org)
################################################################################

# The seam is THIS FILE'S OWN NAME, so copying this file to another seam's name
# runs the same assertion at that point and every message it prints names it.
# Captured at file scope: `info script` is only meaningful while the file is
# being sourced.
#
# ::RTL_ASSERT_SEAM OVERRIDES IT, and that is the OTHER way to run this check at
# a second seam - a three-line hook that sets the variable and sources this file,
# instead of a copy of it. Added 2026-09-08 because the copy is 700 lines and
# two copies of a check drift: the one that is wrong is always the one you are
# not reading. The name still has to BE a seam - flow_utils.tcl's
# flow_seam_assert refuses anything else - so this cannot invent a point that
# does not exist.
set _rtl_assert_seam ""
if {[info exists ::RTL_ASSERT_SEAM] && [string trim $::RTL_ASSERT_SEAM] ne ""} {
    set _rtl_assert_seam [string trim $::RTL_ASSERT_SEAM]
} else {
    set _rtl_assert_seam [file rootname [file tail [info script]]]
}
if {$_rtl_assert_seam eq ""} { set _rtl_assert_seam hook }

# Every proc is prefixed rtl_assert_ for the reason flow_utils.tcl gives at its
# top: a short generic name once shadowed a tool builtin and killed a stage two
# and a half hours in. Redefining these by sourcing two seam copies into one
# session is a no-op - the bodies are identical.

# --- 1. WHAT THE PROJECT DECLARED --------------------------------------------
#
# A Tcl global wins over the environment. `opt` is called either way so the
# resolved table lands in the run manifest with every other knob: the manifest
# then records WHAT THIS RUN WAS GRADED AGAINST, not merely that it was graded.
# PER-SEAM FIRST, THEN THE GENERAL TABLE. Added 2026-09-08, because one table
# read at two seams cannot be right at both: a row whose `ip` key names a block
# design cell can only be evaluated where a block design is open (post_bd), and
# a row about a synthesis define can only be evaluated where the synthesis
# fileset exists (pre_synth). Sharing one table made whichever seam ran first
# refuse on the other seam's row.
#
#     RTL_ASSERT_TABLE_<SEAM>   e.g. RTL_ASSERT_TABLE_POST_BD - this seam only
#     RTL_ASSERT_TABLE          every seam that has no table of its own
#
# The variable that was used is REPORTED and recorded, so a reader of the record
# never has to work out which of the two was in force.
proc rtl_assert_declared {seam} {
    set names [list "RTL_ASSERT_TABLE_[string toupper $seam]" RTL_ASSERT_TABLE]
    foreach n $names {
        set saved ""
        set have 0
        if {[info exists ::$n]} {
            set saved [set ::$n]
            set have 1
        }
        opt $n ""
        if {$have && [string trim $saved] ne ""} { set ::$n $saved }
        if {[string trim [set ::$n]] ne ""} { return [list $n [string trim [set ::$n]]] }
    }
    return [list [lindex $names 0] ""]
}

# --- 2. THE LOUD REFUSAL ------------------------------------------------------
#
# THIS IS THE POINT OF THE WHOLE FILE. flow_refuse, not die and not warn: exit 2
# says NOTHING WAS MEASURED, which is what happened, and it is graded
# differently from exit 1 by make and by ci/lib.sh. A configuration that is
# missing and a design that is wrong need different people.
proc rtl_assert_unconfigured {seam} {
    rtl_assert_report $seam NOT-CHECKED \
        [list "# RTL_ASSERT_TABLE is not declared in this run." \
              "# Nothing was compared. This file is the record that nothing was."]
    flow_refuse \
        "NOT CHECKING: no RTL_ASSERT_TABLE_[string toupper $seam] and no RTL_ASSERT_TABLE" \
        "  is declared, so this hook asserted NOTHING." \
        "  fpga/hooks/${seam}.tcl ran and found no table, and a hook that finds no" \
        "  expectations and passes quietly reads exactly like a hook that checked" \
        "  everything and was happy. So it refuses instead." \
        "" \
        "  DECLARE IT - one row per expectation, in fpga/design.mk:" \
        "      export RTL_ASSERT_TABLE := \\" \
        "          {name <PARAM> kind param value <VALUE> why \"what breaks if wrong\"}" \
        "  Keys: name (required), kind (param|define), value, expect" \
        "  (present|absent), ip, why. An unknown key is fatal." \
        "" \
        "  OR DELETE fpga/hooks/${seam}.tcl. If this design has nothing to assert" \
        "  before synthesis, that is the honest way to say so and nothing else" \
        "  breaks. There is no third option that leaves a green run behind an" \
        "  unasked question."
}

# --- 3. THE TABLE -------------------------------------------------------------
#
# Every malformed row is diagnosed by name. An unknown key is fatal for the same
# reason flow_config rejects one: the typo is not the defect, the SILENT NO-OP
# is.
proc rtl_assert_parse {table} {
    set known {name kind value expect ip why}
    set rows {}
    set seen {}
    set n 0
    foreach row $table {
        incr n
        if {[catch {llength $row} len]} {
            die "RTL_ASSERT_TABLE row $n is not a Tcl list: $row" \
                "  Each row is key/value pairs in braces, e.g." \
                "      {name MY_PARAM kind param value 1}"
        }
        if {$len == 0} { continue }
        if {$len % 2} {
            die "RTL_ASSERT_TABLE row $n has an odd number of words: $row" \
                "  Each row is key/value pairs. Known keys: $known" \
                "  A value containing spaces needs its own braces or quotes."
        }
        array unset r
        array set r {kind param expect present value "" ip "" why ""}
        foreach {k v} $row {
            if {[lsearch -exact $known $k] < 0} {
                die "RTL_ASSERT_TABLE row $n names an unknown key '$k'." \
                    "  Known keys: $known" \
                    "  A key this hook does not read is an expectation NOTHING" \
                    "  checks, which is worse than no expectation at all: it" \
                    "  looks like cover. Fix the row."
            }
            set r($k) $v
        }
        if {[string trim $r(name)] eq ""} {
            die "RTL_ASSERT_TABLE row $n declares no 'name'." \
                "  Row: $row" \
                "  name is the parameter or macro AS THE RTL SPELLS IT - not the" \
                "  design.mk variable that carries it."
        }
        if {[lsearch -exact {param define} $r(kind)] < 0} {
            die "RTL_ASSERT_TABLE row $n: kind '$r(kind)' is not param or define." \
                "  They are read from two different tool properties and they" \
                "  survive IP packaging differently - see the header. Guessing" \
                "  between them would be guessing which defect you are testing for."
        }
        if {[lsearch -exact {present absent} $r(expect)] < 0} {
            die "RTL_ASSERT_TABLE row $n: expect '$r(expect)' is not present or absent."
        }
        if {$r(expect) eq "absent" && [string trim $r(value)] ne ""} {
            die "RTL_ASSERT_TABLE row $n asserts '$r(name)' is ABSENT and also" \
                "  declares value '$r(value)'." \
                "  Those cannot both be checked. An absent name has no value, so" \
                "  one of the two is a leftover and this hook will not pick which."
        }
        set key "$r(kind)/$r(name)"
        if {[lsearch -exact $seen $key] >= 0} {
            die "RTL_ASSERT_TABLE declares $key twice." \
                "  Two rows for one name cannot both be checked - the second would" \
                "  silently win. Merge them."
        }
        lappend seen $key
        lappend rows [array get r]
    }
    if {![llength $rows]} {
        die "RTL_ASSERT_TABLE is set and declares no row." \
            "  An empty table is not 'nothing to check' - it is a check that" \
            "  CANNOT FAIL, which passes every design including the broken one." \
            "  Write the rows, or unset it and delete this hook."
    }
    return $rows
}

# --- 4. THE CONTROL -----------------------------------------------------------
#
# WHY THIS EXISTS. `get_property FOO $obj` returns the empty string for a
# property that is unset AND for one this Vivado does not have on that object.
# Read as an answer, those are the same "" - and one of them is the defect this
# hook hunts while the other is a hook that measured nothing and would report a
# clean sheet on a design that had lost every define it declared.
#
# So nothing is compared until the query mechanism has answered a question whose
# answer is KNOWN: a fileset's NAME property, which is never empty on a fileset
# that exists.
proc rtl_assert_fileset {} {
    foreach c {get_property get_filesets} {
        if {![flow_have $c]} {
            flow_refuse "this tool has no '$c', so this hook cannot see the design at all." \
                "  It belongs at a Vivado stage seam. Nothing was checked." \
                "  (Sourcing it under a bare tclsh reaches exactly this message.)"
        }
    }
    set fs ""
    catch { set fs [current_fileset] }
    if {$fs eq ""} { catch { set fs [get_filesets -quiet sources_1] } }
    if {$fs eq ""} {
        flow_refuse "no source fileset in this session." \
            "  Generics and defines are arguments to synth_design; the fileset" \
            "  properties this hook reads are the second copy of them, and with" \
            "  no fileset open NOTHING HERE CAN READ THEM. A clean sheet would" \
            "  be a sheet this hook never read, so it refuses." \
            "  The toolkit's own synthesis stage opens an in-memory project" \
            "  before it reads a source, so the usual cause is a stage seam" \
            "  earlier than that one, or a project step override that replaced" \
            "  it. Either assert this at post_synth on the elaborated design," \
            "  or delete this hook file."
    }
    set nm ""
    catch { set nm [get_property -quiet NAME $fs] }
    if {[string trim $nm] eq ""} {
        flow_refuse "CONTROL FAILED: get_property NAME on the source fileset returned" \
            "  nothing, on an object this session just handed back. Every property" \
            "  this hook could read would then be empty, and an empty property" \
            "  reads exactly like a dropped define." \
            "  The run is stopped rather than reporting it."
    }
    return [list $fs $nm]
}

# NAME=VALUE entries from a fileset property, as a flat {name value ...} dict.
# A bare entry (a define with no value) maps to the empty string, which is
# DIFFERENT from being absent - the two are distinguished by the caller.
proc rtl_assert_pairs {fs prop} {
    set raw ""
    catch { set raw [get_property -quiet $prop $fs] }
    set out {}
    foreach e $raw {
        set e [string trim $e]
        if {$e eq ""} { continue }
        set i [string first "=" $e]
        if {$i < 0} {
            lappend out $e ""
        } else {
            lappend out [string range $e 0 [expr {$i - 1}]] \
                        [string range $e [expr {$i + 1}] end]
        }
    }
    return [list $raw $out]
}

# CONFIG.<name> on one object. The property is the same on an IP instance and on
# a block-design cell, and that is the point: CONFIG.* is what survives
# ipx::package_project, whichever object carries it.
proc rtl_assert_config_of {obj name where} {
    set v ""
    if {[catch { set v [get_property -quiet CONFIG.$name $obj] }]} { return [list 0 "" $where] }
    if {[string trim $v] eq ""} { return [list 0 "" $where] }
    return [list 1 $v $where]
}

# THE `ip` KEY, AND THE THREE PLACES AN ANSWER CAN LIVE. Rewritten 2026-09-08
# after it was measured unusable on every block-design design.
#
# WHAT WENT WRONG. The row said `ip nanosoc_eth_chiplet_0` - the name the block
# design gives the cell - and the hook refused: "row names ip
# 'nanosoc_eth_chiplet_0', which is not in this project". Correcting it to the
# Vivado IP object's real name, tidelink_design_nanosoc_eth_chiplet_0_0, refused
# identically, because at the pre_synth seam of a checkpoint flow `get_ips`
# returns NOTHING AT ALL: the block design's IP objects live in the separate
# project the bd stage built, and this session read the design back from it.
#
# So the key is not "unusable for BD designs" - it is answerable at a DIFFERENT
# SEAM, and it now looks in all three places an answer can be, in the order that
# makes the strongest claim first:
#
#   1. get_ips <name>          an IP instance named exactly as the row spells it
#   2. get_ips *_<name>_*      the BD-QUALIFIED spelling. An IP instantiated
#                              inside a block design is named <bd>_<cell>_<n>,
#                              so the cell name the project author knows is a
#                              substring of the object name and never equal to
#                              it. Ambiguity here is FATAL, not a guess.
#   3. get_bd_cells /<name>    the block-design cell itself, which is where the
#                              value is SET (`set_property CONFIG.<x> {v}` in
#                              the BD Tcl) and is answerable at post_bd.
#
# WHEN NONE OF THEM ANSWERS the refusal names the seam and says which seam can:
# a row about an object that is not there cannot pass or fail honestly, and a
# message that only says "not in this project" sends the reader to check a name
# that was never the problem.
proc rtl_assert_ip_config {ip name seam} {
    set tried {}

    if {[flow_have get_ips]} {
        set obj ""
        catch { set obj [get_ips -quiet $ip] }
        if {[llength $obj] == 1} {
            return [rtl_assert_config_of [lindex $obj 0] $name "CONFIG.$name on ip $ip"]
        }
        lappend tried "get_ips $ip"

        set obj ""
        catch { set obj [get_ips -quiet "*_${ip}_*"] }
        if {[llength $obj] > 1} {
            die "row names ip '$ip' and [llength $obj] IP objects match the" \
                "  block-design spelling *_${ip}_*:" \
                "    [join $obj {, }]" \
                "  Picking one would be a guess about which instance the row" \
                "  meant, and the two can hold different CONFIG values. Name the" \
                "  object exactly."
        }
        if {[llength $obj] == 1} {
            set o [lindex $obj 0]
            say "  ip '$ip' resolved to the block-design IP object '$o'"
            return [rtl_assert_config_of $o $name "CONFIG.$name on ip $o (BD-qualified from '$ip')"]
        }
        lappend tried "get_ips *_${ip}_*"
    } else {
        lappend tried "get_ips (this tool has no get_ips)"
    }

    if {[flow_have get_bd_cells]} {
        set cur ""
        catch { set cur [current_bd_design -quiet] }
        if {$cur ne ""} {
            set obj ""
            catch { set obj [get_bd_cells -quiet "/$ip"] }
            if {[llength $obj] == 1} {
                say "  ip '$ip' resolved to the block-design CELL '/$ip' in '$cur'"
                return [rtl_assert_config_of [lindex $obj 0] $name \
                            "CONFIG.$name on bd_cell /$ip in block design $cur"]
            }
            lappend tried "get_bd_cells /$ip (block design '$cur' is open)"
        } else {
            lappend tried "get_bd_cells /$ip (NO block design is open in this session)"
        }
    } else {
        lappend tried "get_bd_cells (this tool has no get_bd_cells)"
    }

    # NO lmap: it is Tcl 8.6 and the tool's interpreter is 8.5 - see the note
    # above rtl_assert_run's failure formatter. A hook that only works on the
    # newest Vivado is a hook that stops working on the machine with the licence.
    set tried_lines {}
    foreach t $tried { lappend tried_lines "    $t" }
    die "row names ip '$ip', and nothing in this session answers to it at seam '$seam'." \
        "  Tried, in order:" \
        {*}$tried_lines \
        "" \
        "  A row about an object that is not there cannot pass or fail honestly," \
        "  so it stops the run rather than reporting a clean sheet it never read." \
        "" \
        "  WHERE THIS KEY CAN BE ANSWERED. `ip` reads CONFIG.<name>, which lives" \
        "  on an IP instance or on a block-design cell. In a CHECKPOINT flow the" \
        "  synthesis session has neither: the block design's IP objects belong to" \
        "  the project the bd stage built, and this session read the design back" \
        "  out of it. Move the row to fpga/hooks/post_bd.tcl - the bd stage owns" \
        "  that project and the block design is open there - by declaring it in" \
        "  RTL_ASSERT_TABLE_POST_BD." \
        "" \
        "  Or drop the `ip` key from the row. Without it the same name is looked" \
        "  up as a GENERIC or a VERILOG_DEFINE on the fileset, which is a weaker" \
        "  claim (CONTRACT.md section 9.2: defines do not survive packaging) and" \
        "  is reported as one."
}

# --- 5. THE RECORD ------------------------------------------------------------
#
# A verdict that exists only in a stage log is a verdict nobody collects. Written
# under $REPORT_DIR, which the run already gathers - and written on the
# NOT-CHECKED path too, so the absence of a check is an artefact rather than an
# absence of artefacts. NEVER into the source tree: a hook that writes into
# fpga/ makes the next run's inputs depend on the last run's outputs.
proc rtl_assert_report {seam verdict lines} {
    global REPORT_DIR
    if {![info exists REPORT_DIR] || ![file isdirectory $REPORT_DIR]} {
        warn "no REPORT_DIR: this census is in the log only, and a log is not a"
        warn "  collected artefact."
        return ""
    }
    set p [file join $REPORT_DIR rtl_assert_${seam}.txt]
    if {[catch {set fh [open $p w]} msg]} {
        warn "could not write $p: $msg"
        return ""
    }
    puts $fh "# RTL parameter/define assertion - seam $seam"
    puts $fh [format "%-24s %s" verdict $verdict]
    foreach l $lines { puts $fh $l }
    puts $fh ""
    puts $fh "# NOT COVERED BY THIS CHECK, AT ANY SETTING:"
    puts $fh "#   - that any RTL file READS a macro asserted here. Nothing is"
    puts $fh "#     elaborated at this seam and this codebase has zero occurrences"
    puts $fh "#     of the usual ifdef names across 13,524 files."
    puts $fh "#   - that a value is legal for the module. Strings are compared as"
    puts $fh "#     written: 1, 1'b1 and 32'd1 are three different strings."
    puts $fh "#   - per-file defines, and anything inside a packaged IP that no row"
    puts $fh "#     named with the ip key."
    puts $fh "#   - what the primitives actually became. That is post_impl.tcl."
    close $fh
    say "record written to $p"
    return $p
}

# --- 6. THE COMPARISON --------------------------------------------------------
#
# Every row is evaluated before anything is refused, so one run tells the reader
# about every wrong expectation rather than about the first one.
proc rtl_assert_run {seam} {
    step "RTL parameter/define assertion ($seam)"

    foreach {tvar table} [rtl_assert_declared $seam] break
    if {$table eq ""} { rtl_assert_unconfigured $seam ; return 0 }
    set rows [rtl_assert_parse $table]
    say "table: $tvar ([llength $rows] row(s))"

    lassign [rtl_assert_fileset] fs fsname
    lassign [rtl_assert_pairs $fs GENERIC]        generic_raw generic
    lassign [rtl_assert_pairs $fs VERILOG_DEFINE] define_raw  defines

    say "basis: fileset '$fsname'"
    say "  GENERIC        = $generic_raw"
    say "  VERILOG_DEFINE = $define_raw"

    set lines [list \
        [format "%-24s %s" fileset $fsname] \
        [format "%-24s %s" generic $generic_raw] \
        [format "%-24s %s" verilog_define $define_raw] \
        [format "%-24s %s" declared_rtl_params [flow_env FPGA_RTL_PARAMS "(none)"]] \
        [format "%-24s %s" declared_rtl_defines [flow_env FPGA_RTL_DEFINES "(none)"]] \
        [format "%-24s %s" table_variable $tvar]]

    # THE DECLARED-VERSUS-TOOL CENSUS. design.mk said one thing; the fileset
    # holds another. This is the shape the packaging defect takes, so it is
    # always reported - but it WARNS rather than failing, because a flow may
    # legitimately deliver a define by another route (per file, or as a
    # synth_design argument), and only the TABLE says what must be true.
    foreach {var pairs what} [list FPGA_RTL_PARAMS $generic parameter \
                                   FPGA_RTL_DEFINES $defines define] {
        foreach e [flow_env $var] {
            set nm [lindex [split $e =] 0]
            if {$nm eq ""} { continue }
            if {![dict exists $pairs $nm]} {
                warn "DECLARED BUT NOT ON THE FILESET: $what '$nm' is in $var and is"
                warn "  not in the tool's property. This is the exact shape of the"
                warn "  ipx::package_project define-drop: declared, applied to"
                warn "  nothing, reported by no one. It is a warning here and not a"
                warn "  failure only because RTL_ASSERT_TABLE is where this project"
                warn "  states what MUST be true. If it must be, add a row."
                lappend lines [format "%-24s %s %s" declared_not_present $what $nm]
            }
        }
    }

    set fails {}
    foreach row $rows {
        array unset r
        array set r $row

        if {$r(ip) ne ""} {
            lassign [rtl_assert_ip_config $r(ip) $r(name) $seam] found value where
        } elseif {$r(kind) eq "param"} {
            set found [dict exists $generic $r(name)]
            set value ""
            if {$found} { set value [dict get $generic $r(name)] }
            set where "GENERIC on $fsname"
        } else {
            set found [dict exists $defines $r(name)]
            set value ""
            if {$found} { set value [dict get $defines $r(name)] }
            set where "VERILOG_DEFINE on $fsname"
        }

        set verdict ok
        if {$r(expect) eq "absent"} {
            if {$found} {
                set verdict FAIL
                lappend fails "$r(kind) '$r(name)' is PRESENT and was declared absent\
                               (value '$value', in $where)."
                if {$r(why) ne ""} { lappend fails "  why it matters: $r(why)" }
            }
        } elseif {!$found} {
            set verdict FAIL
            lappend fails "$r(kind) '$r(name)' IS NOT THERE. Expected it in $where."
            if {$r(kind) eq "define"} {
                lappend fails "  Defines do not survive ipx::package_project - three\
                               routes through packaging drop them and none warn. If\
                               this selects behaviour, make it a PARAMETER\
                               (RTL_PARAMS) or bake it into the materialised copy\
                               (RTL_DEFINES_INBODY). A plain +define+ in a packaged\
                               flow may reach nothing."
            } else {
                lappend fails "  Check RTL_PARAMS in design.mk, and that the flow\
                               applied it to this fileset rather than to another."
            }
            if {$r(why) ne ""} { lappend fails "  why it matters: $r(why)" }
        } elseif {[string trim $r(value)] ne "" && $value ne $r(value)} {
            set verdict FAIL
            lappend fails "$r(kind) '$r(name)' is '$value', expected '$r(value)'\
                           (in $where)."
            lappend fails "  Strings are compared as written and Vivado does not\
                           normalise them: 1, 1'b1 and 32'd1 differ here. If the\
                           two are the same number spelled twice, fix the\
                           declaration to match the spelling the tool holds."
            if {$r(why) ne ""} { lappend fails "  why it matters: $r(why)" }
        } elseif {[string trim $r(value)] eq "" && $r(expect) eq "present"} {
            set verdict "ok (presence only)"
        }

        set shown "(absent)"
        if {$found} { set shown "= '$value'" }
        set want "(any)"
        if {[string trim $r(value)] ne ""} { set want $r(value) }
        set got "(absent)"
        if {$found} { set got $value }
        say [format "  %-8s %-28s %-16s %s" $r(kind) $r(name) $verdict $shown]
        lappend lines [format "%-24s kind=%s name=%s expect=%s want=%s got=%s where=%s verdict=%s" \
                              row $r(kind) $r(name) $r(expect) $want $got $where $verdict]
        if {$r(why) ne ""} { lappend lines [format "%-24s %s" row_why $r(why)] }
    }

    if {[llength $fails]} {
        # No lmap and no `string cat`: both are Tcl 8.6 and the tool's
        # interpreter is 8.5. A hook that only works on the newest Vivado is a
        # hook that stops working on the machine that has the licence.
        set flines [list "" "# failures:"]
        foreach f $fails { lappend flines "# $f" }
        rtl_assert_report $seam FAIL [concat $lines $flines]
        die "RTL ASSERTION FAILED at $seam - [llength $rows] row(s) checked:" \
            {*}$fails \
            "Synthesis is stopped here rather than producing a bitstream that\
             believes it was configured. See the record beside this run's reports."
    }

    rtl_assert_report $seam PASS $lines
    say "all [llength $rows] declared expectation(s) hold on fileset '$fsname'."
    return 1
}

rtl_assert_run $_rtl_assert_seam
unset _rtl_assert_seam

# Copyright (C) 2026, SoC Labs (www.soclabs.org)
