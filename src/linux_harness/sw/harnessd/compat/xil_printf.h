/*
 * compat/xil_printf.h — the ONE Xilinx BSP symbol a service module reaches for
 * on a non-mock build: swap_fsm.c's MPS3_SWAP_LOG selects xil_printf whenever
 * MPS3_HAL_MOCK is not defined, which includes -DMPS3_HAL_UIO. This header sits
 * on harnessd's include path in place of the BSP's, and log_linux.c defines the
 * function: same console tee as bare metal (stdout + the `log` ring).
 */
#ifndef HARNESSD_COMPAT_XIL_PRINTF_H
#define HARNESSD_COMPAT_XIL_PRINTF_H
/* No format attribute: xil_printf is the BSP's printf SUBSET, and its callers were
 * written for it, not for -Wformat against glibc. */
int xil_printf(const char *fmt, ...);
#endif
