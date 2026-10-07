/*
 * fake_jtag_tap.c — see fake_jtag_tap.h. Moved out of
 * test_xvc_server_swdbb.c unchanged (identifiers prefixed, logic byte-for-byte
 * the same) so the unit test, the loopback-socket test and the host XVC daemon
 * all drive ONE model.
 *
 * The masks are taken from xvc_server.h deliberately — see the header's note on
 * why this TU is NOT parameterised by MPS3_XVC_SWDBB_TCK_STRETCH /
 * MPS3_XVC_SWDBB_SAMPLE_LATE.
 */
#include <string.h>

#include "fake_jtag_tap.h"
#include "../xvc_server/xvc_server.h"   /* XVC_SWDBB_TCK/TMS/TDI/TDO */
#include "../common/platform_regs.h"    /* SWDBB_DRIVE / SWDBB_SAMPLE offsets */

/* [state][tms] -> next state. The 1149.1 controller graph, transcribed once. */
static const fake_tap_state_t k_tap_next[FAKE_TAP_NSTATES][2] = {
    [FAKE_TAP_TLR]       = { FAKE_TAP_RTI,      FAKE_TAP_TLR       },
    [FAKE_TAP_RTI]       = { FAKE_TAP_RTI,      FAKE_TAP_SEL_DR    },
    [FAKE_TAP_SEL_DR]    = { FAKE_TAP_CAP_DR,   FAKE_TAP_SEL_IR    },
    [FAKE_TAP_CAP_DR]    = { FAKE_TAP_SHIFT_DR, FAKE_TAP_EXIT1_DR  },
    [FAKE_TAP_SHIFT_DR]  = { FAKE_TAP_SHIFT_DR, FAKE_TAP_EXIT1_DR  },
    [FAKE_TAP_EXIT1_DR]  = { FAKE_TAP_PAUSE_DR, FAKE_TAP_UPDATE_DR },
    [FAKE_TAP_PAUSE_DR]  = { FAKE_TAP_PAUSE_DR, FAKE_TAP_EXIT2_DR  },
    [FAKE_TAP_EXIT2_DR]  = { FAKE_TAP_SHIFT_DR, FAKE_TAP_UPDATE_DR },
    [FAKE_TAP_UPDATE_DR] = { FAKE_TAP_RTI,      FAKE_TAP_SEL_DR    },
    [FAKE_TAP_SEL_IR]    = { FAKE_TAP_CAP_IR,   FAKE_TAP_TLR       },
    [FAKE_TAP_CAP_IR]    = { FAKE_TAP_SHIFT_IR, FAKE_TAP_EXIT1_IR  },
    [FAKE_TAP_SHIFT_IR]  = { FAKE_TAP_SHIFT_IR, FAKE_TAP_EXIT1_IR  },
    [FAKE_TAP_EXIT1_IR]  = { FAKE_TAP_PAUSE_IR, FAKE_TAP_UPDATE_IR },
    [FAKE_TAP_PAUSE_IR]  = { FAKE_TAP_PAUSE_IR, FAKE_TAP_EXIT2_IR  },
    [FAKE_TAP_EXIT2_IR]  = { FAKE_TAP_SHIFT_IR, FAKE_TAP_UPDATE_IR },
    [FAKE_TAP_UPDATE_IR] = { FAKE_TAP_RTI,      FAKE_TAP_SEL_DR    },
};

void fake_tap_reset(fake_tap_t *t, uint32_t idcode)
{
    memset(t, 0, sizeof(*t));
    t->idcode = idcode;
    t->state  = FAKE_TAP_TLR;
    t->ir     = FAKE_TAP_IR_IDCODE; /* 1149.1: TLR selects IDCODE */
    t->dr_len = 32u;
}

uint32_t fake_tap_tdo(const fake_tap_t *t)
{
    if (t->state == FAKE_TAP_SHIFT_DR) {
        return t->dr & 1u;
    }
    if (t->state == FAKE_TAP_SHIFT_IR) {
        return t->ir_shift & 1u;
    }
    return 0u;
}

void fake_tap_tick(fake_tap_t *t, uint32_t tms, uint32_t tdi)
{
    uint32_t tdi_bit = tdi & 1u;

    switch (t->state) {
    case FAKE_TAP_TLR:
        t->ir = FAKE_TAP_IR_IDCODE;
        break;
    case FAKE_TAP_CAP_DR:
        if (t->ir == FAKE_TAP_IR_IDCODE) {
            t->dr_len = 32u;
            t->dr     = t->idcode;
        } else {
            /* BYPASS, and 1149.1's "any unimplemented instruction behaves as
             * BYPASS": a 1-bit register of 0. */
            t->dr_len = 1u;
            t->dr     = 0u;
        }
        break;
    case FAKE_TAP_SHIFT_DR: {
        uint32_t mask = (t->dr_len >= 32u) ? 0xFFFFFFFFu : ((1u << t->dr_len) - 1u);
        t->dr = ((tdi_bit << (t->dr_len - 1u)) | (t->dr >> 1)) & mask;
        break;
    }
    case FAKE_TAP_CAP_IR:
        t->ir_shift = 0x1u; /* 1149.1 mandates the two LSBs capture as 01 */
        break;
    case FAKE_TAP_SHIFT_IR: {
        uint32_t mask = (1u << FAKE_TAP_IR_LEN) - 1u;
        t->ir_shift = ((tdi_bit << (FAKE_TAP_IR_LEN - 1u)) | (t->ir_shift >> 1)) & mask;
        break;
    }
    case FAKE_TAP_UPDATE_IR:
        t->ir = t->ir_shift;
        break;
    default:
        break;
    }

    t->state = k_tap_next[t->state][tms ? 1 : 0];
    t->rising_edges++;
}

/* ---- behavioral SWDBB fake ------------------------------------------------ */

static void swdbb_record(fake_swdbb_t *d, int is_write, uint32_t off, uint32_t val)
{
    if (d->n_trace < FAKE_SWDBB_TRACE_MAX) {
        d->trace[d->n_trace].is_write = is_write;
        d->trace[d->n_trace].off      = off;
        d->trace[d->n_trace].val      = val;
        d->n_trace++;
    }
}

int fake_swdbb_hook(void *ctx, int is_write, uint32_t base, uint32_t off, uint32_t *val)
{
    fake_swdbb_t *d = (fake_swdbb_t *)ctx;
    (void)base;
    d->ops++;

    if (is_write) {
        if (off != SWDBB_DRIVE) {
            /* SAMPLE (ro) and every unmapped offset: accepted, no effect. */
            d->stray++;
            swdbb_record(d, 1, off, *val);
            return 1;
        }
        uint32_t previous = d->drive;
        d->drive = *val & (XVC_SWDBB_TCK | XVC_SWDBB_TMS | XVC_SWDBB_TDI);
        d->drive_writes++;
        swdbb_record(d, 1, off, d->drive);
        /* The ONLY way a TCK edge exists: DRIVE[0] rising. */
        if ((d->drive & XVC_SWDBB_TCK) && !(previous & XVC_SWDBB_TCK)) {
            fake_tap_tick(&d->tap,
                          (d->drive & XVC_SWDBB_TMS) ? 1u : 0u,
                          (d->drive & XVC_SWDBB_TDI) ? 1u : 0u);
        }
        return 1;
    }

    switch (off) {
    case SWDBB_DRIVE:
        *val = d->drive;
        swdbb_record(d, 0, off, *val);
        return 1;
    case SWDBB_SAMPLE:
        *val = fake_tap_tdo(&d->tap) & XVC_SWDBB_TDO;
        d->sample_reads++;
        swdbb_record(d, 0, off, *val);
        return 1;
    default:
        d->stray++;
        *val = 0u;
        swdbb_record(d, 0, off, 0u);
        return 1;
    }
}

void fake_swdbb_clear_counters(fake_swdbb_t *d)
{
    d->ops = 0;
    d->sample_reads = 0;
    d->drive_writes = 0;
    d->stray = 0;
    d->n_trace = 0;
}

/* ---- XVC vector helpers --------------------------------------------------- */

void fake_xvc_bits_to_vector(const uint32_t *bits, uint32_t n, uint8_t *out)
{
    memset(out, 0, (n + 7u) / 8u);
    for (uint32_t i = 0; i < n; i++) {
        if (bits[i] & 1u) {
            out[i >> 3] |= (uint8_t)(1u << (i & 7u));
        }
    }
}

uint32_t fake_xvc_recover32(const uint8_t *vec, uint32_t first)
{
    uint32_t v = 0;
    for (uint32_t k = 0; k < 32u; k++) {
        uint32_t i = first + k;
        v |= (uint32_t)((vec[i >> 3] >> (i & 7u)) & 1u) << k;
    }
    return v;
}

void fake_xvc_put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

void fake_tap_idcode_read_sequence(uint8_t *tms_vec, uint8_t *tdi_vec)
{
    uint32_t tms[FAKE_TAP_IDCODE_SEQ_BITS];
    uint32_t tdi[FAKE_TAP_IDCODE_SEQ_BITS];
    static const uint32_t nav[FAKE_TAP_IDCODE_FIRST_BIT] = { 1, 1, 1, 1, 1, 0, 1, 0, 0 };

    for (uint32_t i = 0; i < FAKE_TAP_IDCODE_FIRST_BIT; i++) {
        tms[i] = nav[i];
    }
    for (uint32_t i = FAKE_TAP_IDCODE_FIRST_BIT; i < FAKE_TAP_IDCODE_SEQ_BITS; i++) {
        tms[i] = 0u;
    }
    tms[FAKE_TAP_IDCODE_SEQ_BITS - 1u] = 1u; /* exit on the last shift bit */
    for (uint32_t i = 0; i < FAKE_TAP_IDCODE_SEQ_BITS; i++) {
        tdi[i] = 0u;                          /* an IDCODE read shifts in don't-cares */
    }

    fake_xvc_bits_to_vector(tms, FAKE_TAP_IDCODE_SEQ_BITS, tms_vec);
    fake_xvc_bits_to_vector(tdi, FAKE_TAP_IDCODE_SEQ_BITS, tdi_vec);
}
