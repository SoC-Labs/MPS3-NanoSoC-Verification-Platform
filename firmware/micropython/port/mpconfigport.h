// mpconfigport.h — MicroPython build configuration for the NanoSoC Cortex-M0
// bare-metal port. Derived from ports/minimal, trimmed to fit 128 KB IMEM and
// with machine.mem8/16/32 enabled for register/GPIO poking from Python.

#include <stdint.h>
#include <alloca.h>

// Start from the minimal ROM level (all optional features off), then add back
// only what we want.
#define MICROPY_CONFIG_ROM_LEVEL          (MICROPY_CONFIG_ROM_LEVEL_MINIMUM)

// Keep the compiler + REPL — that is the whole point of this port.
#define MICROPY_ENABLE_COMPILER           (1)
#define MICROPY_HELPER_REPL               (1)
#define MICROPY_REPL_AUTO_INDENT          (1)
#define MICROPY_ENABLE_GC                 (1)

// No filesystem, no frozen bytecode, no external import.
#define MICROPY_MODULE_FROZEN_MPY         (0)
#define MICROPY_ENABLE_EXTERNAL_IMPORT    (0)

// No floating point — armv6-m soft-float only, and we do not need it.
#define MICROPY_FLOAT_IMPL                (MICROPY_FLOAT_IMPL_NONE)

#define MICROPY_ALLOC_PATH_MAX            (128)
#define MICROPY_ALLOC_PARSE_CHUNK_INIT    (16)

// Use a sound native GC register scan (shared gchelper_thumb1.s), not the
// setjmp fallback.
#define MICROPY_GCREGS_SETJMP             (0)

// Enable machine.mem8/mem16/mem32 for register/GPIO access from Python:
//     machine.mem32[0x40010004] = 0xFF          # drive the 8 LEDs
//     (machine.mem32[0x40010000] >> 8) & 0xFF   # read the DIP switches
//
// We use extmod/machine_mem.c (self-contained) for the real mem* objects, but
// keep MICROPY_PY_MACHINE (== the stock modmachine.c module) OFF: that file
// #errors without MICROPY_PY_SYS_EXIT and needs a port mp_machine_idle() hook.
// Instead port/modnanosoc.c registers a light `machine` module exposing
// mem8/16/32, plus a `nanosoc` convenience module (led/switches). MEMX only
// depends on itself, so machine_mem.c still compiles its objects.
#define MICROPY_PY_MACHINE                (0)
#define MICROPY_PY_MACHINE_MEMX           (1)

// Trim sys features.
#define MICROPY_PY_SYS_MODULES            (0)
#define MICROPY_PY_SYS_EXIT               (0)
#define MICROPY_PY_SYS_PATH               (0)
#define MICROPY_PY_SYS_ARGV               (0)

// Machine word / off_t types.
typedef intptr_t mp_int_t;   // must be pointer size
typedef uintptr_t mp_uint_t; // must be pointer size
typedef long mp_off_t;

#define MP_STATE_PORT MP_STATE_VM

#define MICROPY_HW_BOARD_NAME "SoC Labs NanoSoC (Cortex-M0)"
#define MICROPY_HW_MCU_NAME   "cortex-m0"

// Boot banner. Overriding MICROPY_BANNER_NAME_AND_VERSION (py/mpconfig.h only
// #defines it #ifndef) makes the first line lead with the NanoSoC identity
// rather than the generic "MicroPython ...". The REPL prints this line, then
// "; " + MICROPY_BANNER_MACHINE (= MICROPY_HW_BOARD_NAME " with " MCU). Result:
//   NanoSoC MicroPython <git> on <date>; SoC Labs NanoSoC (Cortex-M0) with cortex-m0
#define MICROPY_BANNER_NAME_AND_VERSION \
    "NanoSoC MicroPython " MICROPY_GIT_TAG " on " MICROPY_BUILD_DATE
