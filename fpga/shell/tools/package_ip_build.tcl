###-----------------------------------------------------------------------------
### fpga/shell/tools/package_ip_build.tcl -- package the soclabs shell IP for
### the RUNNING Vivado, into a BUILD directory, never into the tree.
###
### WHY THIS EXISTS. fpga/shell/ip_packaged/package_csr_ip.tcl is the committed
### recipe and fpga/shell/ip_packaged/<block>/ is where the 2024.1 flow keeps its
### output. Two Vivado versions cannot share that directory:
###   * the MicroBlaze V static needs Vivado 2026.1 (S-mode/SSTC IP revisions),
###     the bare-metal static and every fielded overlay are 2024.1;
###   * package_csr_ip's staleness check compares FILE MTIMES only, so a
###     component.xml written by one version is "up to date" to the other, and
###     the second flow silently instantiates IP the first one packaged;
###   * a 2026.1-written component.xml in the in-tree repo would then be read by
###     the next 2024.1 mint.
### So a non-2024.1 flow packages into <build>/ip_repo_<version>/ and stamps it.
### A stamp that names a different version is wiped and repackaged -- never
### reused. The 2024.1 bare-metal flow does not call this and is unchanged.
###
### FLOW (fpga/dfx/, VIVADO=2026.1) calls soclabs_ip_repo_for_build from its
### own shell/static stages; fpga/shell/build_shell.tcl and validate_bd.tcl call
### it for any Vivado that is not the in-tree repo's 2024.1.
###
### Usage (inside Vivado):
###   source fpga/shell/tools/package_ip_build.tcl
###   set repo [soclabs_ip_repo_for_build <part> <build_dir>]
###   set_property ip_repo_paths $repo [current_project]; update_ip_catalog -rebuild
###-----------------------------------------------------------------------------

set ::SOCLABS_PKG_TOOLS_DIR [file dirname [file normalize [info script]]]

#: The Vivado whose output lives in the TRACKED fpga/shell/ip_packaged/. Any
#: other version must go through soclabs_ip_repo_for_build.
set ::SOCLABS_IN_TREE_IP_VIVADO "2024.1"

proc soclabs_vivado_is_in_tree_ip_version {} {
    return [string match "$::SOCLABS_IN_TREE_IP_VIVADO*" [version -short]]
}

proc soclabs_ip_repo_for_build {part_name build_dir} {
    set shell_dir [file normalize [file join $::SOCLABS_PKG_TOOLS_DIR ..]]
    set ver [version -short]
    set repo [file normalize [file join $build_dir "ip_repo_$ver"]]
    set stamp [file join $repo "PACKAGED_BY_VIVADO"]

    if { [file exists $stamp] } {
        set fh [open $stamp r]; set got [string trim [read $fh]]; close $fh
        if { $got ne $ver } {
            puts "WARNING: package_ip_build -- $repo was packaged by Vivado '$got', this is '$ver': wiping it (never reuse another version's IP)"
            file delete -force $repo
        }
    } elseif { [file exists $repo] } {
        # A repo with no stamp is of unknown provenance -- same treatment.
        puts "WARNING: package_ip_build -- $repo has no version stamp: wiping it"
        file delete -force $repo
    }
    file mkdir $repo

    source [file join $shell_dir "ip_packaged" "package_csr_ip.tcl"]
    # The recipe runs in its own scratch projects; restore ours afterwards (it
    # already does, but a stale current_project here would be silent).
    soclabs_package_csr_ip $part_name $repo $shell_dir [file join $repo ".pkg_build"]

    set fh [open $stamp w]; puts $fh $ver; close $fh
    puts "INFO: package_ip_build -- soclabs shell IP for Vivado $ver in $repo"
    return $repo
}
