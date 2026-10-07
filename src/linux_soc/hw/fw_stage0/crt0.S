/*
 * crt0.S -- freestanding startup for the MicroBlaze-V (RISC-V) stage-0 loader.
 * Adapted from fw_dbg/crt0.S (same proven idioms): set gp/sp, install a direct
 * trap handler, zero .bss, call main(). main() does not return (it jumps into
 * OpenSBI or loops in rescue); if it ever did, we park the core.
 *
 * WARM RESTARTS. The watchdog restarts the MBV with the LMB intact (BRAM is
 * only initialised by FPGA configuration), so this runs again over whatever
 * the previous run left. That is why:
 *   - .bss is zeroed here on EVERY entry, and stage0.ld forbids .data (a
 *     modified initialised variable would not be re-initialised);
 *   - the status block at 0x1FE00 is NOT in .bss and is never touched here --
 *     its NOINIT attempt counters are what survive (stage0_status.h);
 *   - the diag mailbox at 0x1FF00 is above the stack and never touched.
 *
 * Parking loops SPIN rather than wfi: a hart parked in wfi could not be
 * debug-halted on the July bitstreams (memory: linux-mps3-weekend-build).
 *
 * Reset vector is the base of the LMB BRAM (0x0), SHELL_CONTRACT §7.
 */
    .option arch, +zicsr

    .section .init, "ax", @progbits
    .global _start
    .type   _start, @function
_start:
    .option push
    .option norelax
    la      gp, __global_pointer$
    .option pop

    la      sp, _estack

    la      t0, trap_entry
    csrw    mtvec, t0
    csrw    mie, zero

    /* zero .bss [__bss_start, __bss_end) -- never the status block */
    la      t0, __bss_start
    la      t1, __bss_end
1:  bgeu    t0, t1, 2f
    sw      zero, 0(t0)
    addi    t0, t0, 4
    j       1b
2:
    li      a0, 0
    li      a1, 0
    call    main

.Lhalt:
    j       .Lhalt
    .size   _start, .-_start

    .balign 4
    .global trap_entry
    .type   trap_entry, @function
trap_entry:
    la      sp, _estack          /* a trap may come from a smashed stack */
    csrr    a0, mcause
    csrr    a1, mepc
    csrr    a2, mtval
    call    trap_report
3:  j       3b
    .size   trap_entry, .-trap_entry
