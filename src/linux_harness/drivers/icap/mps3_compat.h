/*
 * mps3_compat.h — kernel/host type shim for the ICAP-SPIKE sources.
 *
 * The engine (mps3_icap_engine.c), the pure transition table
 * (mps3_swap_transitions.c) and the mock (mps3_icap_mock.c) are deliberately
 * kernel-independent C: everything they need from the environment comes in
 * through the mps3_icap_ops vtable. This header is the ONLY place that knows
 * whether we are compiling into the kernel module or into the host unit
 * tests — the same discipline as the firmware's MPS3_HAL_MOCK split
 * (firmware/common/platform_regs.h), which is what made swap_fsm.c's control
 * flow host-testable there.
 */
#ifndef MPS3_COMPAT_H
#define MPS3_COMPAT_H

#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/string.h>
#else
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
typedef uint8_t  u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#endif

#endif /* MPS3_COMPAT_H */
