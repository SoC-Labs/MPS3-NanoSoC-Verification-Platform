/*
 * nanosoc_xip.ld — hybrid-XiP GNU linker script for the SoC Labs NanoSoC
 *                  (single-core Cortex-M0) MicroPython port.
 *
 * Forked from nanosoc.ld and given the hot/cold split of the multicore
 * reference build_soc/firmware/sections_xip.ld, adapted to THIS SoC's map
 * (QSPI XiP aperture at 0x70000000, NOT 0x24000000). It keeps a small HOT set
 * resident in IMEM and places the COLD bulk in the FLASH_XIP region at
 * VMA == LMA so the CPU executes / reads it IN PLACE from QSPI flash through
 * the CG092 cache — never copied into IMEM.
 *
 * BOOT MODEL (unchanged): the stage-0 bootrom sets REMAP so IMEM appears at
 * 0x00000000 and jumps to it; the vector table is linked at 0x0. Reset_Handler
 * additionally calls nanosoc_xip_bringup() (main.c) BEFORE touching any cold
 * code/rodata, so the 0x70000000 aperture is live before mp_init() reads the
 * cold qstr tables.
 *
 * COLD SET (routed by object-file name — MicroPython vendor sources cannot be
 * attribute-tagged function by function): the lexer / parser / compiler, the
 * REPL line handling, the builtin modules, and the large const / qstr tables.
 * Port-owned code may ALSO opt in explicitly via XIP_COLD / XIP_COLD_RO
 * (xip_cold.h) which emit .text.xip / .rodata.xip input sections.
 *
 * NOTE: this script is plain GNU ld syntax (NOT C-preprocessed). The cold
 * object list is expanded inline in both .text.xip and .rodata.xip. Keep the
 * two lists in sync if you change the cold set. The leading `*` glob makes each
 * match independent of the absolute $(BUILD) directory.
 *
 * Exports (in addition to the resident symbol set):
 *   __xip_start__ : VMA/LMA base of the cold XiP segment (== flash address)
 *   __xip_end__   : end of the cold XiP segment
 *   __xip_size__  : byte size of the cold XiP segment
 *
 *   IMEM      0x00000000 (alias of 0x10000000 after REMAP) 128K  HOT code+rodata
 *   DMEM      0x18000000                                    64K  .data/.bss/stack
 *   FLASH_XIP 0x70000000 (QSPI XiP aperture)                 4M  COLD code+rodata
 */

/* FLASH_XIP origin is 0x70020000, NOT the aperture base 0x70000000: the low
 * 128 KB of flash (aperture offset 0..0x20000) is RESERVED for the boot table
 * (@0x0) + the HOT image (@0x1000) that stage-0 copies into IMEM — exactly the
 * multicore reference's sections_xip.ld low-flash reservation. The cold
 * .text.xip/.rodata.xip therefore land at VMA == LMA == 0x70020000 and execute
 * IN PLACE from flash byte offset 0x20000 through the CG092 cache. */
MEMORY
{
    IMEM      (rx)  : ORIGIN = 0x00000000, LENGTH = 128K
    DMEM      (rwx) : ORIGIN = 0x18000000, LENGTH = 64K
    FLASH_XIP (rx)  : ORIGIN = 0x70020000, LENGTH = 4M - 128K
}

/* Top of stack = top of DMEM. */
_estack = ORIGIN(DMEM) + LENGTH(DMEM);
_min_stack_size = 8K;

SECTIONS
{
    /* --- Cold XiP code: executes in place from QSPI flash --------------------
     * Listed FIRST so these object files' .text* are claimed here before the
     * greedy *(.text*) in the resident .text below (ld assigns each input
     * section to the FIRST matching output section). VMA == LMA (no AT) — the
     * bytes live in flash at 0x70000000 and are fetched through the CG092
     * cache. Long-branch veneers for hot<->cold calls are inserted next to each
     * caller automatically. Their .data/.bss still fall through to DMEM below. */
    .text.xip ORIGIN(FLASH_XIP) :
    {
        . = ALIGN(4);
        __xip_start__ = .;
        *(.text.xip*)                       /* explicit XIP_COLD port code   */
        *lexer.o(.text .text*)              /* --- cold MicroPython objects: */
        *parse.o(.text .text*)              /*     lexer / parser / compiler */
        *parsenum.o(.text .text*)
        *parsenumbase.o(.text .text*)
        *compile.o(.text .text*)
        *scope.o(.text .text*)
        *emitbc.o(.text .text*)
        *emitcommon.o(.text .text*)
        *repl.o(.text .text*)               /*     REPL line handling        */
        *readline.o(.text .text*)
        *pyexec.o(.text .text*)
        *modbuiltins.o(.text .text*)        /*     builtin modules           */
        *modmath.o(.text .text*)
        *modcmath.o(.text .text*)
        *modmicropython.o(.text .text*)
        *modgc.o(.text .text*)
        *modstruct.o(.text .text*)
        *modio.o(.text .text*)
        *modsys.o(.text .text*)
        *moderrno.o(.text .text*)
        *modarray.o(.text .text*)
        *modcollections.o(.text .text*)
        *modstring.o(.text .text*)
        *modthread.o(.text .text*)
        *modweakref.o(.text .text*)
        *builtinhelp.o(.text .text*)
        *builtinevex.o(.text .text*)
        *builtinimport.o(.text .text*)
        *qstr.o(.text .text*)               /*     qstr helper code          */
        . = ALIGN(4);
    } >FLASH_XIP

    /* --- Cold XiP read-only data: read in place from QSPI flash ------------- */
    .rodata.xip :
    {
        . = ALIGN(4);
        *(.rodata.xip*)                     /* explicit XIP_COLD_RO port data */
        *lexer.o(.rodata .rodata*)
        *parse.o(.rodata .rodata*)
        *parsenum.o(.rodata .rodata*)
        *parsenumbase.o(.rodata .rodata*)
        *compile.o(.rodata .rodata*)
        *scope.o(.rodata .rodata*)
        *emitbc.o(.rodata .rodata*)
        *emitcommon.o(.rodata .rodata*)
        *repl.o(.rodata .rodata*)
        *readline.o(.rodata .rodata*)
        *pyexec.o(.rodata .rodata*)
        *modbuiltins.o(.rodata .rodata*)
        *modmath.o(.rodata .rodata*)
        *modcmath.o(.rodata .rodata*)
        *modmicropython.o(.rodata .rodata*)
        *modgc.o(.rodata .rodata*)
        *modstruct.o(.rodata .rodata*)
        *modio.o(.rodata .rodata*)
        *modsys.o(.rodata .rodata*)
        *moderrno.o(.rodata .rodata*)
        *modarray.o(.rodata .rodata*)
        *modcollections.o(.rodata .rodata*)
        *modstring.o(.rodata .rodata*)
        *modthread.o(.rodata .rodata*)
        *modweakref.o(.rodata .rodata*)
        *builtinhelp.o(.rodata .rodata*)
        *builtinevex.o(.rodata .rodata*)
        *builtinimport.o(.rodata .rodata*)
        *qstr.o(.rodata .rodata*)           /*     large qstr string pool     */
        . = ALIGN(4);
        __xip_end__ = .;
    } >FLASH_XIP
    __xip_size__ = __xip_end__ - __xip_start__;

    /* --- Resident HOT set (IMEM) ------------------------------------------- */
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

    /* Fail the link if the HOT (resident) set overflows the IMEM bank. */
    ASSERT(_etext <= ORIGIN(IMEM) + LENGTH(IMEM),
           "hot (resident) set overflowed IMEM region")
}
