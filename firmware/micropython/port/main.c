// main.c — NanoSoC (Cortex-M0) MicroPython entry point.
//
// Boot model: the image is preloaded into IMEM; the bootrom sets REMAP so IMEM
// appears at 0x00000000 and jumps there. Execution therefore starts at the
// vector table below (linked at 0x00000000). Reset_Handler copies .data, zeroes
// .bss, brings up the UART2 console, then runs the friendly REPL.

#include <stdint.h>
#include <string.h>

#include "py/builtin.h"
#include "py/compile.h"
#include "py/runtime.h"
#include "py/gc.h"
#include "py/mperrno.h"
#include "py/stackctrl.h"
#include "shared/runtime/gchelper.h"
#include "shared/runtime/pyexec.h"
#include "nanosoc.h"

// Hybrid-XiP build only: bring up the QSPI XiP read path before any cold
// (flash-resident) code or rodata is touched. The resident build does not
// define NANOSOC_XIP and links this out entirely.
#ifdef NANOSOC_XIP
#include "xip_bringup.h"
#endif

// Linker-provided symbols (see nanosoc.ld).
extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
extern uint32_t _heap_start, _heap_end;

int main(void) {
    // Stack limit for the runtime's overflow checks: leave a little slack below
    // the true top of stack.
    mp_stack_set_top(&_estack);
    mp_stack_set_limit((char *)&_estack - (char *)&_heap_end - 1024);

    nanosoc_uart_init();

    for (;;) {
        gc_init(&_heap_start, &_heap_end);
        mp_init();

        // Friendly REPL over UART2. Loops until Ctrl-D soft-reboot.
        for (;;) {
            if (pyexec_friendly_repl() != 0) {
                break;
            }
        }

        mp_deinit();
    }
}

// --- GC root collection -----------------------------------------------------
// Uses the shared thumb1 (armv6-m) register-spill helper for a sound scan of
// callee-saved registers plus the C stack.
void gc_collect(void) {
    gc_collect_start();
    gc_helper_collect_regs_and_stack();
    gc_collect_end();
}

// --- Filesystem stubs (no filesystem on this port) --------------------------
mp_lexer_t *mp_lexer_new_from_file(qstr filename) {
    mp_raise_OSError(MP_ENOENT);
}

mp_import_stat_t mp_import_stat(const char *path) {
    (void)path;
    return MP_IMPORT_STAT_NO_EXIST;
}

mp_obj_t mp_builtin_open(size_t n_args, const mp_obj_t *args, mp_map_t *kwargs) {
    (void)n_args;
    (void)args;
    (void)kwargs;
    mp_raise_OSError(MP_ENOENT);
}
MP_DEFINE_CONST_FUN_OBJ_KW(mp_builtin_open_obj, 1, mp_builtin_open);

// --- Fatal-error / NLR fallback ---------------------------------------------
void nlr_jump_fail(void *val) {
    (void)val;
    for (;;) {
    }
}

void NORETURN __fatal_error(const char *msg) {
    (void)msg;
    for (;;) {
    }
}

#ifndef NDEBUG
void MP_WEAK __assert_func(const char *file, int line, const char *func, const char *expr) {
    (void)file;
    (void)line;
    (void)func;
    (void)expr;
    __fatal_error("Assertion failed");
}
#endif

// ===========================================================================
// Cortex-M0 startup: vector table + reset handler.
// ===========================================================================

void Reset_Handler(void) __attribute__((naked));
void Reset_Handler(void) {
    // Set the stack pointer explicitly. On a true core reset the CPU loads SP
    // from vector[0], but the bootrom reaches us via a jump after REMAP, so we
    // must not rely on that.
    // armv6-m cannot `ldr sp, =literal` (sp is not a low register for that
    // form): load into r0 then move to sp.
    __asm volatile ("ldr r0, =_estack \n\t mov sp, r0" ::: "r0");
#ifdef NANOSOC_XIP
    // Bring up the QSPI XiP read path FIRST, before any cold (flash-resident)
    // code or rodata is fetched. nanosoc_xip_bringup() and everything it calls
    // is HOT (resident IMEM), so this is safe to run here. Cortex-M0 has no
    // VTOR, so nothing here relocates the vector table.
    nanosoc_xip_bringup();
#endif
    // Copy .data from its load image in IMEM to DMEM.
    for (uint32_t *src = &_sidata, *dst = &_sdata; dst < &_edata;) {
        *dst++ = *src++;
    }
    // Zero .bss.
    for (uint32_t *dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0;
    }
    main();
    for (;;) {
    }
}

void Default_Handler(void) {
    for (;;) {
    }
}

// Vector table. Cortex-M0 (armv6-m) has 16 system exception slots then IRQs;
// the REPL is fully polled so all handlers trap in Default_Handler.
const uint32_t isr_vector[] __attribute__((section(".isr_vector"))) = {
    (uint32_t)&_estack,          // 0: initial SP
    (uint32_t)&Reset_Handler,    // 1: reset
    (uint32_t)&Default_Handler,  // 2: NMI
    (uint32_t)&Default_Handler,  // 3: HardFault
    0, 0, 0, 0, 0, 0, 0,         // 4-10: reserved (no MemManage/BusFault/Usage on M0)
    (uint32_t)&Default_Handler,  // 11: SVCall
    0, 0,                        // 12-13: reserved
    (uint32_t)&Default_Handler,  // 14: PendSV
    (uint32_t)&Default_Handler,  // 15: SysTick
};
