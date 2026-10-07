/*
 * fw_memtest/memtest.ld -- link the DDR4 memtest entirely inside the 128 KiB LMB
 * block RAM at 0x0000_0000 (mbv_soc.tcl: MAP_LMB_BASE / MAP_LMB_RANGE = 128K).
 *
 * NOTHING (code, stack, .data, .bss) may live in DDR: the whole point of the
 * test is that DDR is the thing under test. The DDR aperture at 0x8000_0000 is
 * touched only by the explicit volatile accesses in memtest.c.
 *
 * If mbv_soc.tcl's MAP_LMB_RANGE changes, LENGTH below must change with it --
 * the ASSERT at the bottom is the only thing standing between an oversized image
 * and a firmware that silently runs off the end of the BRAM.
 */

OUTPUT_ARCH("riscv")
OUTPUT_FORMAT("elf32-littleriscv")
ENTRY(_start)

MEMORY
{
    LMB (rwx) : ORIGIN = 0x00000000, LENGTH = 128K
}

SECTIONS
{
    . = ORIGIN(LMB);

    .text : ALIGN(4)
    {
        KEEP(*(.init))
        KEEP(*(.vectors))
        *(.text.startup .text.startup.*)
        *(.text .text.*)
        . = ALIGN(4);
        *(.rodata .rodata.* .srodata .srodata.*)
        . = ALIGN(4);
    } > LMB

    .data : ALIGN(4)
    {
        __data_start = .;
        *(.data .data.*)
        . = ALIGN(8);
        PROVIDE(__global_pointer$ = . + 0x800);
        *(.sdata .sdata.* .sdata2 .sdata2.*)
        . = ALIGN(4);
        __data_end = .;
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

    _estack = ORIGIN(LMB) + LENGTH(LMB);

    /* Keep .riscv.attributes: the Makefile's ISA assertion reads it back. */
    /DISCARD/ :
    {
        *(.comment)
        *(.note .note.*)
        *(.eh_frame .eh_frame_hdr)
    }
}

ASSERT(_end <= (ORIGIN(LMB) + LENGTH(LMB) - 4096),
       "ERROR: memtest image leaves < 4 KiB of stack in the 128 KiB LMB")
