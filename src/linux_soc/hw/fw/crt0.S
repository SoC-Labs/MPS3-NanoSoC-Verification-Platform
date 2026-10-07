/*
 * crt0.S - freestanding startup for a MicroBlaze V (RISC-V) soft core.
 *
 * Target ISA: rv32im (M-extension), ilp32 ABI, medany, no linker relaxation.
 *
 * NOTE on CSR access:
 *   binutils 2.38 (the assembler shipped with this GCC 12.2.0 toolchain) split
 *   the CSR instructions out into the "zicsr" extension, so `csrr`/`csrw` do NOT
 *   assemble under a bare -march=rv32im ("extension `zicsr' required").  We keep
 *   the -march=rv32im *multilib* selection (so no rv32ima/float multilib is
 *   needed) and enable zicsr *locally* with `.option arch, +zicsr`.  This only
 *   affects assembly of this file; it merges into the ELF arch attribute as
 *   rv32i_m_zicsr and never pulls in the atomic ('a') extension.
 *
 * Reset behaviour:
 *   The MicroBlaze V is configured to fetch its reset vector from the base of
 *   the 64 KiB LMB (0x0000_0000), which is where the linker places .init /
 *   _start.  If mbv_soc.tcl changes C_RESET_ADDRESS this must move with it.
 */

    .option arch, +zicsr        /* allow csrr/csrw below; keeps multilib = rv32im */

    .section .init, "ax", @progbits
    .global _start
    .type   _start, @function
_start:
    /* ---- global pointer (gp). Must not be relaxed away, so norelax here. ---- */
    .option push
    .option norelax
    la      gp, __global_pointer$
    .option pop

    /* ---- stack pointer: top of LMB (grows down) ---- */
    la      sp, _estack

    /* ---- install the trap handler (direct mode: mtvec low bits = 00) ---- */
    la      t0, trap_entry
    csrw    mtvec, t0

    /* ---- zero .bss : [__bss_start, __bss_end) , word-aligned by the ld ---- */
    la      t0, __bss_start
    la      t1, __bss_end
1:
    bgeu    t0, t1, 2f
    sw      zero, 0(t0)
    addi    t0, t0, 4
    j       1b
2:

    /* ---- call main(0, NULL) ; we never expect it to return ---- */
    li      a0, 0
    li      a1, 0
    call    main

    /* ---- main returned: park the core ---- */
.Lhalt:
    wfi
    j       .Lhalt
    .size   _start, .-_start

/*
 * Trap handler. A load/store access fault during the DDR test (e.g. DDR not
 * calibrated, or an address that faults on the AXI fabric) lands here.  We read
 * mcause / mepc and hand them to the C reporter so the failure is *printed*,
 * never a silent hang.  The current sp (main's stack, in LMB) is still valid, so
 * calling C is safe.  We do not attempt to return from the trap.
 *
 * mtvec is programmed in DIRECT mode, so this single entry catches every trap.
 * Must be 4-byte aligned (mtvec[1:0] are the mode field).
 */
    .balign 4
    .global trap_entry
    .type   trap_entry, @function
trap_entry:
    csrr    a0, mcause
    csrr    a1, mepc
    csrr    a2, mtval
    call    trap_report         /* void trap_report(u32 mcause, u32 mepc, u32 mtval) */
3:
    wfi
    j       3b
    .size   trap_entry, .-trap_entry
