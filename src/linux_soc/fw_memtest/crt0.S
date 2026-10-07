/*
 * fw_memtest/crt0.S -- freestanding startup for the linux_soc MicroBlaze V.
 *
 * ISA: rv32imac_zicsr_zifencei / ilp32 / medany / no-relax.
 *   The BD core (mbv_soc.tcl) is C_USE_MULDIV=1, C_USE_ATOMIC=1,
 *   C_USE_COMPRESSION=1 (+ Zba/Zbb/Zbs), so I, M, A and C are all legal here.
 *   Unlike the poc/ddr4_mbv spike -- which had to build rv32im because the
 *   2024.1 toolchain shipped no rv32ima multilib -- the 2026.1 bare-metal GCC
 *   13.4.0 DOES ship an rv32imac_zicsr_zifencei/ilp32 multilib, so no
 *   `.option arch,+zicsr` hack is needed: zicsr is in -march and csrr/csrw
 *   assemble directly.
 *
 * Reset: microblaze_riscv C_BASE_VECTORS = 0x0 and the 128 KiB LMB BRAM is at
 * 0x0, so _start (in .init, placed first by memtest.ld) is the reset vector.
 * The core is held in reset by proc_sys_reset_0 until c0_init_calib_complete,
 * so by the time this executes the DDR4 controller has calibrated.
 */

    .section .init, "ax", @progbits
    .global _start
    .type   _start, @function
_start:
    /* global pointer -- must not be relaxed away */
    .option push
    .option norelax
    la      gp, __global_pointer$
    .option pop

    /* stack: top of the LMB, grows down */
    la      sp, _estack

    /* trap handler, direct mode (mtvec[1:0] = 00) */
    la      t0, trap_entry
    csrw    mtvec, t0

    /* zero .bss */
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
    wfi
    j       .Lhalt
    .size   _start, .-_start

/*
 * Trap handler. A load/store access fault on the DDR aperture (uncalibrated
 * memory, or an address that does not decode on the fabric) lands here. We hand
 * mcause/mepc/mtval to the C reporter so the failure is PRINTED on the console
 * -- including the machine-readable MEMTEST_RESULT: FAIL line -- rather than
 * hanging silently and looking like a slow test. sp still points at main's stack
 * in LMB, so calling C is safe. We never return from the trap.
 */
    .balign 4
    .global trap_entry
    .type   trap_entry, @function
trap_entry:
    csrr    a0, mcause
    csrr    a1, mepc
    csrr    a2, mtval
    call    trap_report          /* void trap_report(u32 mcause, u32 mepc, u32 mtval) */
3:  wfi
    j       3b
    .size   trap_entry, .-trap_entry
