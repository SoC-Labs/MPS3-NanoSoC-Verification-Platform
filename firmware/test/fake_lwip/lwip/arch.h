/*
 * fake_lwip/lwip/arch.h — the lwIP width typedefs, host edition.
 *
 * Part of the SHIM lwIP that lets firmware/platform/src/net_if_lwip.c — the
 * one target-only translation unit in the tree — be compiled by host gcc and
 * tested. The names and widths are the ones lwip220's arch.h publishes
 * (checked against the BSP copy under build/vitis_fw/.../include/lwip), so
 * the file under test sees exactly the types it sees on target.
 */
#ifndef FAKE_LWIP_ARCH_H
#define FAKE_LWIP_ARCH_H

#include <stdint.h>
#include <stddef.h>

typedef uint8_t  u8_t;
typedef int8_t   s8_t;
typedef uint16_t u16_t;
typedef int16_t  s16_t;
typedef uint32_t u32_t;
typedef int32_t  s32_t;
typedef uintptr_t mem_ptr_t;

/* lwIP's window/typedef aliases used by struct tcp_pcb below. */
typedef u16_t tcpwnd_size_t;
typedef u16_t tcpflags_t;

#define LWIP_CONST_CAST(target_type, val) ((target_type)((ptrdiff_t)val))

#endif /* FAKE_LWIP_ARCH_H */
