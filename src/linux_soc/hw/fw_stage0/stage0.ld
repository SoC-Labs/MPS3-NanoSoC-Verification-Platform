/*
 * stage0.ld -- link the first-stage loader inside the LMB BELOW the stage0
 * status block (plan §3 LMB map; SHELL_CONTRACT §3):
 *
 *   0x00000-0x1FDFF  image + stack      <- the only MEMORY region here
 *   0x1FE00-0x1FEFF  status block       (absolute symbol, never loaded)
 *   0x1FF00-0x1FFFF  diag mailbox       (HARNESSD; not described at all)
 *
 * Nothing (code, stack, .data, .bss) may live in DDR: the loader runs BEFORE it
 * has staged anything there, and DDR stalls forever until calibration. The DDR
 * aperture is touched only by explicit volatile accesses.
 *
 * Budget gate: check_budget.py reads stage0.map and requires >= 48 KiB free
 * between the end of the image and the bottom of the stack
 * (`make size-gate`, with a negative control).
 */
OUTPUT_ARCH("riscv")
OUTPUT_FORMAT("elf32-littleriscv")
ENTRY(_start)

S0_STACK_BYTES = 8K;

MEMORY
{
    LMB (rwx) : ORIGIN = 0x00000000, LENGTH = 0x1FE00
}

SECTIONS
{
    . = ORIGIN(LMB);

    .text : ALIGN(4)
    {
        KEEP(*(.init))
        KEEP(*(.vectors))
        . = ALIGN(4);
        KEEP(*(.s0_ids))            /* mps3_stage0_{static_id,ver32,build_id} */
        *(.text.startup .text.startup.*)
        *(.text .text.*)
        . = ALIGN(4);
        *(.rodata .rodata.* .srodata .srodata.*)
        . = ALIGN(4);
    } > LMB

    /* MUST STAY EMPTY: a watchdog warm restart does not reload BRAM, so an
     * initialised variable stage0 modifies would start the next run with the
     * previous run's value. Mutable state lives in .bss (zeroed by crt0) or,
     * deliberately, in the status block. */
    .data : ALIGN(8)
    {
        __data_start = .;
        *(.data .data.*)
        *(.sdata .sdata.* .sdata2 .sdata2.*)
        __data_end = .;       /* no ALIGN in between: empty must mean equal */
        PROVIDE(__global_pointer$ = . + 0x800);
    } > LMB

    .bss (NOLOAD) : ALIGN(4)
    {
        __bss_start = .;
        *(.sbss .sbss.* .scommon)
        *(.bss .bss.*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > LMB

    . = ALIGN(4);
    _end = .;
    PROVIDE(end = .);

    _estack = ORIGIN(LMB) + LENGTH(LMB);          /* 0x1FE00: the status block */
    __stack_bottom = _estack - S0_STACK_BYTES;

    /DISCARD/ :
    {
        *(.comment)
        *(.note .note.*)
        *(.eh_frame .eh_frame_hdr)
    }
}

/* the status block (stage0_status.h S0_STATUS_ADDR) and the mailbox, for the map */
s0_status_block = 0x1FE00;
s0_diag_mailbox = 0x1FF00;

ASSERT(_estack == 0x1FE00, "ERROR: stage0 stack top must be the status block base 0x1FE00")
ASSERT(_end <= __stack_bottom, "ERROR: stage0 image overlaps its 8 KiB stack")
ASSERT(__data_end == __data_start, "ERROR: stage0 has .data -- a warm restart would not re-initialise it")
