# -----------------------------------------------------------------------------
# dfx_jobs.tcl -- the Tcl half of DFX_JOBS (MINT-SPEED lane, 2026-09-24).
#
# With DFX_JOBS=N (N >= 2) the Makefile hands stage 4 to tools/dfx_jobs.py,
# which runs build_dfx.tcl as SEVERAL Vivado processes instead of one:
#
#   1. DFX_PHASE=ref     ONE process: the reference config (rm_greybox), the
#                        black-box + lock -> static_routed_locked.dcp, static_id,
#                        and the reference's bitstreams (the full boot image
#                        with its USR_ACCESS/USERID stamps, static_stamp.json,
#                        the static .ltx, its partial pair). Writes
#                        dfx_jobs/ref.rec LAST: its existence = phase done.
#   2. RM workers        up to N processes at once, one per non-reference RM.
#                        Each IS the incremental add path that already folds an
#                        RM into a fielded locked static (DFX_ADD_RMS=<one rm>,
#                        DFX_REUSE_LOCKED / DFX_STATIC_ID_FILE / DFX_REF_ROUTED),
#                        plus DFX_ROW_FILE: the worker writes its overlay row to
#                        dfx_jobs/<rm>.row instead of merging overlay_inputs.txt
#                        and leaves static_id.txt alone. Same build_config, same
#                        pr_verify against the ONE reference, same -cell pair
#                        write, same .ltx gate as the one-process flow.
#   3. DFX_PHASE=finish  ONE short process: every RM has a row keyed to THIS
#                        static_id, then overlay_inputs.txt (the same writer as
#                        the one-process flow), then static_id.txt LAST -- it is
#                        make's target for the stage, so it exists only when the
#                        whole stage does. A failed RM therefore leaves no
#                        static_id.txt, and re-running make RESUMES (dfx_jobs.py):
#                        only the missing configs run again, against the SAME
#                        locked static.
#
# DFX_PHASE unset (DFX_JOBS unset or 1) = the one-process flow, unchanged.
#
# SOURCED WITH -notrace (build_dfx.tcl). This file prints DFX_BUILD_COMPLETE,
# and Vivado echoes the text of every top-level command it sources -- a proc
# body included -- into the log. Echoed, the literal would satisfy the
# Makefile's (unanchored) `grep -q DFX_BUILD_COMPLETE build.log` in EVERY run,
# including one that failed. tests: fpga/dfx/tools/tests/test_dfx_jobs.py.
#
# Pure Tcl apart from the procs it calls from build_dfx.tcl; the record
# readers/writers are tclsh-testable.
# -----------------------------------------------------------------------------

# dfx_jobs_phase -> "" (the one-process flow), "ref" or "finish".
proc dfx_jobs_phase {} {
    if { ![info exists ::env(DFX_PHASE)] || $::env(DFX_PHASE) eq "" } { return "" }
    set p $::env(DFX_PHASE)
    if { $p ni {ref finish} } {
        error "build_dfx.tcl: DFX_PHASE=$p -- want ref or finish (an RM worker is the incremental add: DFX_ADD_RMS + DFX_ROW_FILE)"
    }
    return $p
}

proc dfx_jobs_state_dir { out_dir } {
    return [file join $out_dir dfx_jobs]
}

# Write-then-rename, so a reader (the finish phase, dfx_jobs.py's resume) sees
# either the whole record or none of it -- never a half-written one from a
# worker that was killed mid-write.
proc dfx_jobs_write_atomic { path text } {
    set tmp "${path}.tmp[pid]"
    set fh [open $tmp w]
    puts -nonewline $fh $text
    close $fh
    file rename -force $tmp $path
}

# A key/value record: one "key value..." line per key. dfx_jobs.py parses it
# with str.split(" ", 1); nothing in it contains a newline.
proc dfx_jobs_write_record { path kv } {
    set text ""
    foreach {k v} $kv { append text "$k [join $v { }]\n" }
    dfx_jobs_write_atomic $path $text
}

proc dfx_jobs_read_record { path } {
    set fh [open $path r]
    set text [read $fh]
    close $fh
    set rec [dict create]
    foreach line [split $text "\n"] {
        if { [string trim $line] eq "" } { continue }
        set k [lindex [split $line " "] 0]
        dict set rec $k [string range $line [expr {[string length $k] + 1}] end]
    }
    return $rec
}

# The per-RM row record: the static_id the partial was built against, and the
# overlay_inputs.txt row exactly as overlay_row returned it.
proc dfx_jobs_write_row { path static_id row } {
    dfx_jobs_write_record $path [list static_id $static_id row $row]
}

# -> the row as a list, or "" when the file is absent, unreadable, or keyed to
# ANOTHER static (a stale row from an earlier lock can never be merged).
proc dfx_jobs_read_row { path static_id } {
    if { ![file exists $path] } { return "" }
    if { [catch { dfx_jobs_read_record $path } rec] } { return "" }
    if { ![dict exists $rec static_id] || ![dict exists $rec row] } { return "" }
    if { [dict get $rec static_id] ne $static_id } { return "" }
    return [split [dict get $rec row] " "]
}

# DFX_ADOPT_LOCKED=1 (dfx_jobs.py run --adopt-locked): the static is ALREADY
# routed and locked in out_dir -- e.g. a one-process stage 4 that died after the
# lock. The ref phase then re-derives static_id from the locked checkpoint
# instead of re-routing, so every partial still fits the static it was keyed to.
proc dfx_jobs_adopt {} {
    return [expr { [info exists ::env(DFX_ADOPT_LOCKED)] && $::env(DFX_ADOPT_LOCKED) ni {"" 0} }]
}

# -----------------------------------------------------------------------------
# DFX_PHASE=ref
# -----------------------------------------------------------------------------
proc dfx_phase_ref { repo_root out_dir part rp_inst rp_pblock_name rm_names static_shell_dcp harness_ver } {
    set state [dfx_jobs_state_dir $out_dir]
    file mkdir $state
    set ref [lindex $rm_names 0]
    # Nothing from an earlier ref phase may survive this one.
    file delete -force $state/ref.rec $state/${ref}.row $state/static_id.txt

    set locked $out_dir/static_routed_locked.dcp
    if { [dfx_jobs_adopt] } {
        set routed $out_dir/config_${ref}_routed.dcp
        foreach f [list $locked $routed] {
            if { ![file exists $f] } { error "build_dfx.tcl DFX_ADOPT_LOCKED: $f not found -- there is no locked static to adopt" }
        }
        dfx_version_guard $locked "adopted locked static"
        dfx_version_guard $routed "adopted reference config"
        set static_id [format "0x%08X" [file_crc32 $locked]]
        if { [file exists $out_dir/static_id.txt] } {
            set fh [open $out_dir/static_id.txt r]; set rec_id [string trim [read $fh]]; close $fh
            if { $rec_id ne $static_id } {
                error "build_dfx.tcl DFX_ADOPT_LOCKED: $out_dir/static_id.txt says $rec_id but CRC-32 of $locked is $static_id -- refusing to adopt a static whose id the tree does not agree on"
            }
        }
        set fh [open $state/static_id.txt w]; puts $fh $static_id; close $fh
        puts "INFO: DFX_JOBS ref phase ADOPTED the locked static $locked: static_id = $static_id (CRC-32 re-derived; NOT re-routed, NOT re-locked)"
    } else {
        set routed [build_config $repo_root $out_dir $part $rp_inst $rp_pblock_name $ref 1 $static_shell_dcp]
        # static_id goes to the state dir, NOT out_dir/static_id.txt: that file
        # is make's target for stage 4 and is written by the finish phase only.
        set static_id [extract_locked_static $out_dir $rp_inst $routed $state/static_id.txt]
    }

    # The reference's bitstreams, here rather than after the other configs:
    # static_id is minted and the locked checkpoint is written, which is the
    # whole placement rule for the USR_ACCESS/USERID stamps (see
    # write_config_bitstreams), and harness_ver was resolved at the start of
    # THIS process -- the same moment the one-process flow resolves it.
    set row [write_config_bitstreams $out_dir $rp_inst $rp_pblock_name $ref $routed $ref $harness_ver $static_id]
    dfx_jobs_write_row $state/${ref}.row $static_id $row
    dfx_jobs_write_record $state/ref.rec [list static_id $static_id reference_rm $ref rm_names $rm_names]
    puts "DFX_PHASE_REF_COMPLETE static_id=$static_id reference=$ref rms={$rm_names}"
    return $static_id
}

# -----------------------------------------------------------------------------
# DFX_PHASE=finish
# -----------------------------------------------------------------------------
proc dfx_phase_finish { out_dir rm_names } {
    set state [dfx_jobs_state_dir $out_dir]
    if { ![file exists $state/ref.rec] } {
        error "build_dfx.tcl finish: no $state/ref.rec -- the ref phase never completed"
    }
    set rec [dfx_jobs_read_record $state/ref.rec]
    set static_id [dict get $rec static_id]
    set ref [dict get $rec reference_rm]
    if { [lindex $rm_names 0] ne $ref } {
        error "build_dfx.tcl finish: the build set starts with '[lindex $rm_names 0]' but the static was locked from '$ref'"
    }
    # The ONE static every partial was built against must still be the one on
    # disk: a changed locked checkpoint here means some process rewrote it.
    set crc [format "0x%08X" [file_crc32 $out_dir/static_routed_locked.dcp]]
    if { $crc ne $static_id } {
        error "build_dfx.tcl finish: CRC-32 of static_routed_locked.dcp is $crc, but the ref phase minted $static_id -- the locked static changed under the RM workers"
    }

    set rows {}
    set missing {}
    foreach rm_key $rm_names {
        set row [dfx_jobs_read_row $state/${rm_key}.row $static_id]
        if { [llength $row] != 5 } { lappend missing $rm_key; continue }
        set ok 1
        foreach rel [lrange $row 3 4] {
            set bin [file join $out_dir $rel]
            foreach f [list $bin "[file rootname $bin].bit"] {
                if { ![file exists $f] } { set ok 0 }
            }
        }
        if { !$ok } { lappend missing $rm_key; continue }
        lappend rows $row
    }
    if { [llength $missing] > 0 } {
        puts "DFX_JOBS_INCOMPLETE static_id=$static_id missing={$missing}"
        error "build_dfx.tcl finish: no complete row for {$missing} against static $static_id -- overlay_inputs.txt and static_id.txt NOT written"
    }

    write_overlay_inputs $out_dir $static_id $rows
    # static_id.txt LAST (see the header).
    dfx_jobs_write_atomic $out_dir/static_id.txt "$static_id\n"
    puts "INFO: static_id = $static_id (minted by the ref phase; written to $out_dir/static_id.txt)"
    puts "DFX_BUILD_COMPLETE static_id=$static_id rms={$rm_names} (pr_verify + bitstream pairs in $out_dir; next: make -C fpga/dfx overlays)"
    return $static_id
}
