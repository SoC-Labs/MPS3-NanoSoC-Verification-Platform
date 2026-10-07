/*-----------------------------------------------------------------------------
 * xip_cold.h — NanoSoC (single-core Cortex-M0) hybrid-XiP hot/cold markers.
 *
 * Adapted from nanosoc-multicore-system/firmware/include/xip_cold.h for the
 * single-core m0_soc map (QSPI XiP aperture at 0x70000000, NOT 0x24000000).
 *
 * The hybrid-XiP model keeps a latency-critical *hot* set resident in IMEM and
 * executes the *cold* bulk in place (XiP) from QSPI flash through the CG092
 * read cache. Cold code / read-only data is routed to the FLASH_XIP region
 * (0x70000000) at VMA==LMA by nanosoc_xip.ld and is fetched over the cache.
 *
 * TWO ways cold placement happens on this port:
 *   1. Port-owned C that wants explicit cold placement tags a symbol with
 *      XIP_COLD / XIP_COLD_RO below (produces .text.xip / .rodata.xip input
 *      sections, matched first by nanosoc_xip.ld).
 *   2. The MicroPython *vendor* tree cannot be attribute-tagged function by
 *      function, so nanosoc_xip.ld routes whole cold object files (lexer,
 *      parser, compiler, REPL, builtin modules, qstr tables) into the XiP
 *      output sections by object-file name. Same destination, coarser grain.
 *
 * Build folding:
 *   -DXIP_DISABLE -> the macros expand to nothing, so every explicitly tagged
 *                    "cold" symbol folds back into normal .text/.rodata. Paired
 *                    with the resident linker script this reproduces the
 *                    fully-resident baseline image (characterisation control /
 *                    safe fallback).
 *
 * Residency rule for THIS port (single core, no flash writes at runtime, no
 * filesystem):
 *   MUST stay HOT  = the vector table + reset/startup, nanosoc_xip_bringup
 *                    itself, the UART console driver, the MicroPython VM
 *                    dispatch core (mp_execute_bytecode) and the GC.
 *   COLD-eligible  = the lexer / parser / compiler, REPL line handling,
 *                    builtin modules, and the large const / qstr tables.
 *
 * A joint work commissioned on behalf of SoC Labs, under Arm Academic
 * Access license.
 * Copyright (C) 2026, SoC Labs (www.soclabs.org)
 *---------------------------------------------------------------------------*/
#ifndef XIP_COLD_H
#define XIP_COLD_H

#ifdef XIP_DISABLE
/* Control / fallback build: everything resident, no XiP placement. */
#define XIP_COLD
#define XIP_COLD_RO
#define XIP_ENABLED 0
#else
/* Tagged code / data are linked to execute / be read in place from QSPI flash
 * (0x70000000) via the CG092 cache. `used` keeps them past --gc-sections even
 * when only referenced indirectly; the distinct sub-section names let the
 * linker script match them with *(.text.xip*) / *(.rodata.xip*). */
#define XIP_COLD     __attribute__((section(".text.xip"),   used, noinline))
#define XIP_COLD_RO  __attribute__((section(".rodata.xip"), used))
#define XIP_ENABLED 1
#endif

#endif /* XIP_COLD_H */
