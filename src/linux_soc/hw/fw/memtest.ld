/*
 * memtest.ld - link the MicroBlaze V DDR4 memtest entirely inside the 64 KiB
 * LMB block RAM at 0x0000_0000.
 *
 * Rationale: the CPU boots and runs this firmware from LMB *before* DDR4 has
 * been calibrated, so NOTHING (code, stack, .data, .bss) may live in DDR.  The
 * DDR aperture at 0x8000_0000 is only ever touched by explicit volatile
 * accesses in the test itself.
 *
 * Symbols exported for crt0.S:
 *   _estack             - top of LMB (initial sp)
 *   __global_pointer$   - gp value (.sdata + 0x800 window)
 *   __bss_start/__bss_end - bounds of the region crt0 zeroes
 */

OUTPUT_ARCH("riscv")
OUTPUT_FORMAT("elf32-littleriscv")
ENTRY(_start)

/* LMB size and base are the ADDRESS_MAP.md contract (SECTION 0 of mbv_soc.tcl):
 * the boot BRAM is 128 KiB at 0x0000_0000 in linux_soc (it was 64 KiB in the
 * poc/ddr4_mbv spike). If mbv_soc.tcl's MAP_LMB_* change, this must change too. */
MEMORY
{
    LMB (rwx) : ORIGIN = 0x00000000, LENGTH = 128K
}

SECTIONS
{
    . = ORIGIN(LMB);

    /* Reset vector / startup first, then the rest of the code and read-only. */
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

    /* Initialised data. (Everything is in one memory, so no separate LMA copy
     * is required - .data's LMA == VMA and crt0 does not relocate it.) */
    .data : ALIGN(4)
    {
        __data_start = .;
        *(.data .data.*)
        . = ALIGN(8);
        /* gp points into the middle of the small-data window so a +/-2 KiB
         * gp-relative reach covers all of .sdata/.sbss. */
        PROVIDE(__global_pointer$ = . + 0x800);
        *(.sdata .sdata.* .sdata2 .sdata2.*)
        . = ALIGN(4);
        __data_end = .;
    } > LMB

    /* Zero-initialised. NOLOAD: occupies no space in the ELF/.bin image. */
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

    /* Initial stack pointer: very top of the LMB, grows downward. */
    _estack = ORIGIN(LMB) + LENGTH(LMB);

    /* NB: do NOT discard .riscv.attributes - the Makefile's build-time ISA
     * assertion reads it back with `readelf -A memtest.elf`. */
    /DISCARD/ :
    {
        *(.comment)
        *(.note .note.*)
        *(.eh_frame .eh_frame_hdr)
    }
}

/* Build-time overflow guard: image + at least 1 KiB of stack must fit the LMB. */
ASSERT(_end <= (ORIGIN(LMB) + LENGTH(LMB) - 1024),
       "ERROR: memtest image leaves < 1 KiB of stack in the 64 KiB LMB")
