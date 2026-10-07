# scripts/linux_board/mps3_timing.tcl -- the timed read-only probe of the on-board GDB server's measurement
# (scripts/demo_gdb_on_board.sh; OPENOCD_ON_HARNESS_SCOPE §6). Source it AFTER the
# target half (nanosoc_mps3_jtag.cfg + nanosoc_ops.tcl), then:
#
#   -c "mps3_timed /tmp/dmem.bin" -c shutdown
#
# It runs init, halts through the AP (nanosoc_halt_examine), dumps the registers, reads
# 4 KiB of DMEM (0x1800_0000) to a file, resumes the core, and prints ONE line:
#   TIMING init_ms=.. halt_ms=.. reg_ms=.. dump4k_ms=.. resume_ms=.. total_ms=..
# using OpenOCD's own millisecond clock (`ms`), so the numbers are the JTAG work only,
# not ssh or process start-up. Read-only apart from the halt/resume.
proc mps3_timed {dump} {
    set t0 [ms]
    init
    set t1 [ms]
    nanosoc_halt_examine
    set t2 [ms]
    reg
    set t3 [ms]
    dump_image $dump 0x18000000 0x1000
    set t4 [ms]
    resume
    set t5 [ms]
    echo "TIMING init_ms=[expr {$t1 - $t0}] halt_ms=[expr {$t2 - $t1}] reg_ms=[expr {$t3 - $t2}] dump4k_ms=[expr {$t4 - $t3}] resume_ms=[expr {$t5 - $t4}] total_ms=[expr {$t5 - $t0}]"
}
