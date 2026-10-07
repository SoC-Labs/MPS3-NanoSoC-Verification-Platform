/*
 * swd_server.h — TCP 6920 OpenOCD remote_bitbang server -> SWD pins.
 * See swd_server/README.md and docs/contracts/net-protocol.md.
 */
#ifndef MPS3_SWD_SERVER_H
#define MPS3_SWD_SERVER_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Current pin state as this module understands it -- mirrors what would be
 * written to/read from MPS3_SWDBB_BASE once that block is real (see
 * platform_regs.h AMBIGUITY #1). Kept as a struct here so the byte-dispatch
 * logic below is exercised even without real registers to poke yet. */
typedef struct {
    uint8_t swclk;      /* 0/1 */
    uint8_t swdio_o;    /* output value when driving */
    uint8_t swdio_oe;   /* 1 = firmware drives SWDIO, 0 = released/input */
    uint8_t swdio_i;     /* last-sampled input value */
    uint8_t srst;         /* dbg_resetn sense: 1 = asserted (held in reset) --
                           * TODO(A3): confirm active-sense vs CLKRST's
                           * "1=released" convention; likely needs inversion
                           * when driving CLKRST_RESET_CTRL_DBG_RESETN. */
} swd_pin_state_t;

extern swd_pin_state_t g_swd_pins;

void swd_server_init(void);

/* Non-blocking poll -- accept a connection if none is open, drain and
 * dispatch available bytes one remote_bitbang command at a time, write any
 * required reply byte ('c' sample -> '0'/'1'). No-ops entirely while
 * g_shell_state.swd_gated is set (see swd_server/README.md "Gating during a
 * swap"). */
void swd_server_poll(void);

/* Dispatches a single remote_bitbang protocol byte (see common/net_proto.h
 * mps3_bitbang_char_t). *reply_out receives the reply byte for 'c'
 * (sample: '0'/'1'), else 0 (no reply expected per the protocol). Returns
 * 0 = handled, 1 = 'Q' quit (caller closes the connection), -1 =
 * unrecognized byte (caller drops the connection, fail closed). Exposed as
 * its own function so the bit-order mapping (CONFIRMED vs OpenOCD
 * remote_bitbang.c; swd_server/README.md,
 * I22) is concentrated in one place, pending host-side bitbang.c
 * confirmation. */
int swd_server_handle_byte(uint8_t c, uint8_t *reply_out);

#ifdef __cplusplus
}
#endif

#endif /* MPS3_SWD_SERVER_H */
