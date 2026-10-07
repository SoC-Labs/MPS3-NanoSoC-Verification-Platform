// nanosoc.h — SoC Labs NanoSoC (Cortex-M0) peripheral register definitions.
//
// Addresses cross-checked against
//   nanosoc_arch_tech/firmware/software/cmsis/Device/ARM/CMSDK_CM0/Include/CMSDK_CM0.h
// where CMSDK_APB_BASE = 0x40000000 and CMSDK_AHB_BASE = 0x40010000.

#ifndef NANOSOC_H
#define NANOSOC_H

#include <stdint.h>

#define REG32(addr) (*(volatile uint32_t *)(uintptr_t)(addr))

// --- Arm cmsdk_apb_uart, instance UART2 (the console) ------------------------
#define NANOSOC_UART2_BASE      0x40006000u
#define UART2_DATA              REG32(NANOSOC_UART2_BASE + 0x00)
#define UART2_STATE             REG32(NANOSOC_UART2_BASE + 0x04)
#define UART2_CTRL              REG32(NANOSOC_UART2_BASE + 0x08)
#define UART2_BAUDDIV           REG32(NANOSOC_UART2_BASE + 0x10)

#define UART_STATE_TX_FULL      (1u << 0)   // TX buffer full
#define UART_STATE_RX_FULL      (1u << 1)   // RX buffer full (a byte is waiting)
#define UART_CTRL_TX_EN         (1u << 0)
#define UART_CTRL_RX_EN         (1u << 1)

// Console baud is FIXED at 76800 by the shell's uart_axis_shim, and dut_clk is
// 50 MHz => BAUDDIV = 50_000_000 / 76800 = 651. Do not "fix" this to 115200:
// the shim's divider is hard-wired and both sides must agree.
#define NANOSOC_SYS_CLK_HZ      50000000u
#define NANOSOC_CONSOLE_BAUD    76800u
#define NANOSOC_UART2_BAUDDIV   651u

// --- Arm cmsdk_ahb_gpio -----------------------------------------------------
#define NANOSOC_GPIO0_BASE      0x40010000u   // P0[7:0]=LEDs, P0[15:8]=DIP switches
#define NANOSOC_GPIO1_BASE      0x40011000u   // P1[5]=UART2 TXD, P1[4]=UART2 RXD

#define GPIO_DATA_OFS           0x00
#define GPIO_DATAOUT_OFS        0x04
#define GPIO_OUTENSET_OFS       0x10
#define GPIO_OUTENCLR_OFS       0x14
#define GPIO_ALTFUNCSET_OFS     0x30

#define GPIO0_DATA              REG32(NANOSOC_GPIO0_BASE + GPIO_DATA_OFS)
#define GPIO0_DATAOUT           REG32(NANOSOC_GPIO0_BASE + GPIO_DATAOUT_OFS)
#define GPIO0_OUTENSET          REG32(NANOSOC_GPIO0_BASE + GPIO_OUTENSET_OFS)
#define GPIO1_ALTFUNCSET        REG32(NANOSOC_GPIO1_BASE + GPIO_ALTFUNCSET_OFS)

// UART2 TXD is muxed onto P1[5]; the pin mux selects it with ALTFUNC bit 5:
//     assign p1_out_mux[5] = (p1_altfunc[5]) ? uart2_txd : p1_out[5];
// UART2 RXD is a DIRECT tap off the pad input and is NOT altfunc-gated:
//     assign uart2_rxd = p1_in[4];
// (both from nanosoc_arch_tech/rtl/src/control/verilog/nanosoc_pin_mux.v)
// So only bit 5 must be set, and P1[4] must be left as an input, i.e. we must
// never set GPIO1 OUTENSET bit 4.
#define GPIO1_ALTFUNC_UART2_TXD (1u << 5)

void nanosoc_uart_init(void);

#endif // NANOSOC_H
