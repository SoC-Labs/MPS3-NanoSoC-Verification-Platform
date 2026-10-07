/*
 * nanosoc.ld — GNU linker script for the SoC Labs NanoSoC (Cortex-M0)
 *              MicroPython port.
 *
 * BOOT MODEL: the stage-0 bootrom runs first, sets the sysctrl REMAP bit so
 * IMEM appears at 0x00000000, then jumps to it. Our image is preloaded into
 * IMEM and executes from 0x00000000, so the vector table is linked there.
 *
 * Memory map — this is the "nanosoc_upy" SCAFFOLD config (fat BRAM), NOT the
 * product. Values match fpga/rp/nanosoc_upy/rp_nanosoc_upy_wrapper.sv
 * (IMEM_RAM_ADDR_W=17 => 128 KB, DMEM_RAM_ADDR_W=16 => 64 KB).
 *
 *   IMEM  0x00000000 (alias of 0x10000000 after REMAP)  128 KB  code + rodata
 *   DMEM  0x18000000                                     64 KB  .data/.bss/stack + heap
 */

MEMORY
{
    IMEM (rx)  : ORIGIN = 0x00000000, LENGTH = 128K
    DMEM (rwx) : ORIGIN = 0x18000000, LENGTH = 64K
}

/* Top of stack = top of DMEM. */
_estack = ORIGIN(DMEM) + LENGTH(DMEM);

/* Reserve a stack below _estack; the GC heap gets everything from _ebss up to
 * the stack guard. 8 KB of stack is generous for the REPL/compiler. */
_min_stack_size = 8K;

SECTIONS
{
    .text :
    {
        . = ALIGN(4);
        KEEP(*(.isr_vector))   /* vector table must be first, at 0x00000000 */
        *(.text)
        *(.text*)
        *(.rodata)
        *(.rodata*)
        *(.glue_7)             /* ARM/thumb interworking veneers (libgcc) */
        *(.glue_7t)
        . = ALIGN(4);
        _etext = .;
        _sidata = _etext;      /* .data initialiser image lives here in IMEM */
    } >IMEM

    .data : AT ( _sidata )
    {
        . = ALIGN(4);
        _sdata = .;
        *(.data)
        *(.data*)
        . = ALIGN(4);
        _edata = .;
    } >DMEM

    .bss :
    {
        . = ALIGN(4);
        _sbss = .;
        *(.bss)
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } >DMEM

    /* GC heap spans from end of .bss to the bottom of the reserved stack. */
    _heap_start = _ebss;
    _heap_end   = _estack - _min_stack_size;

    /* Fail the link if .data+.bss+stack overflow DMEM. */
    .stack_guard (NOLOAD) :
    {
        . = ALIGN(8);
        . = . + _min_stack_size;
    } >DMEM

    .ARM.attributes 0 : { *(.ARM.attributes) }

    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) }
}
