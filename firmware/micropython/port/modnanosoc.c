// modnanosoc.c — register the `machine` and `nanosoc` modules for the port.
//
//   machine.mem8/mem16/mem32   raw memory access (from extmod/machine_mem.c)
//   nanosoc.led(mask)          drive the 8 user LEDs (P0[7:0])
//   nanosoc.switches()         read the 8 DIP switches (P0[15:8])
//   nanosoc.mem32(addr[,val])  32-bit peek/poke convenience wrapper
//
// The heavy stock extmod/modmachine.c is intentionally not used (see
// mpconfigport.h); this provides just what the demo needs.

#include "py/runtime.h"
#include "py/obj.h"
#include "extmod/modmachine.h"     // machine_mem8/16/32_obj externs
#include "nanosoc.h"

// -------------------------------------------------------------------------
// `machine` module: expose the raw memory-access objects.
// -------------------------------------------------------------------------
#if MICROPY_PY_MACHINE_MEMX
static const mp_rom_map_elem_t machine_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_machine) },
    { MP_ROM_QSTR(MP_QSTR_mem8),  MP_ROM_PTR(&machine_mem8_obj) },
    { MP_ROM_QSTR(MP_QSTR_mem16), MP_ROM_PTR(&machine_mem16_obj) },
    { MP_ROM_QSTR(MP_QSTR_mem32), MP_ROM_PTR(&machine_mem32_obj) },
};
static MP_DEFINE_CONST_DICT(machine_module_globals, machine_module_globals_table);

const mp_obj_module_t mp_module_machine = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&machine_module_globals,
};
MP_REGISTER_MODULE(MP_QSTR_machine, mp_module_machine);
#endif

// -------------------------------------------------------------------------
// `nanosoc` module: board-specific convenience helpers.
// -------------------------------------------------------------------------

// nanosoc.led(mask) — write the low 8 bits to the user LEDs on P0[7:0].
static mp_obj_t nanosoc_led(mp_obj_t mask_in) {
    uint32_t mask = (uint32_t)mp_obj_get_int_truncated(mask_in) & 0xffu;
    GPIO0_OUTENSET = 0xffu;            // ensure P0[7:0] are outputs
    uint32_t v = GPIO0_DATAOUT;
    GPIO0_DATAOUT = (v & ~0xffu) | mask;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(nanosoc_led_obj, nanosoc_led);

// nanosoc.switches() — read the 8 DIP switches on P0[15:8].
static mp_obj_t nanosoc_switches(void) {
    return mp_obj_new_int((GPIO0_DATA >> 8) & 0xffu);
}
static MP_DEFINE_CONST_FUN_OBJ_0(nanosoc_switches_obj, nanosoc_switches);

// nanosoc.mem32(addr) -> read; nanosoc.mem32(addr, val) -> write.
static mp_obj_t nanosoc_mem32(size_t n_args, const mp_obj_t *args) {
    uintptr_t addr = (uintptr_t)mp_obj_get_int_truncated(args[0]);
    if (n_args == 1) {
        return mp_obj_new_int_from_uint(REG32(addr));
    }
    REG32(addr) = (uint32_t)mp_obj_get_int_truncated(args[1]);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(nanosoc_mem32_obj, 1, 2, nanosoc_mem32);

static const mp_rom_map_elem_t nanosoc_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_nanosoc) },
    { MP_ROM_QSTR(MP_QSTR_led),      MP_ROM_PTR(&nanosoc_led_obj) },
    { MP_ROM_QSTR(MP_QSTR_switches), MP_ROM_PTR(&nanosoc_switches_obj) },
    { MP_ROM_QSTR(MP_QSTR_mem32),    MP_ROM_PTR(&nanosoc_mem32_obj) },
};
static MP_DEFINE_CONST_DICT(nanosoc_module_globals, nanosoc_module_globals_table);

const mp_obj_module_t mp_module_nanosoc = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&nanosoc_module_globals,
};
MP_REGISTER_MODULE(MP_QSTR_nanosoc, mp_module_nanosoc);
