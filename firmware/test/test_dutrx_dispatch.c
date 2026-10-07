/*
 * test_dutrx_dispatch.c — the `dutrx` verb on a build that HAS the DUT-egress
 * capture block (net-protocol.md v0.10): real net_proto.c decode/encode + real
 * coordinator.c dispatch/handler, built WITH -DMPS3_HAS_DUT_EGRESS, against a
 * BEHAVIOURAL model of DUTEGR @ 0x44B2_0000 installed as a mock_regs hook.
 *
 * A hook, not seeded register slots, because DUTEGR is not value-shaped: DATA
 * is a DESTRUCTIVE read (one byte per read, popping the FIFO) and FRAME_LEN /
 * LEVEL / STATUS move as it is drained. A slot array cannot model that, and a
 * verb whose whole job is draining a FIFO tested against one would prove
 * nothing. The model below follows fpga/shell/ip/dut_egress/dut_egress.sv's
 * read side: store-and-forward (a frame is visible only once whole), the
 * descriptor retired by the pop of the frame's LAST byte, VALID/LAST on DATA,
 * and the sticky OVF/DESYNC bits.
 *
 * The OFF-build path ("dut_egress not present" on a bitstream with no 0x44B2
 * slave -- today's fielded one) is covered inside test_coordinator_dispatch.c,
 * which is built WITHOUT the flag. Same split as display/test_display_dispatch.
 *
 * Links (see Makefile): the DISPATCH_SRCS set. -DMPS3_HAL_MOCK
 * -DMPS3_HAS_DUT_EGRESS.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "mock_regs.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* The same bit fields coordinator.c spells out of dut_egress.sv's read mux.
 * Repeated here deliberately: if the handler's copy ever disagrees with the
 * RTL, this file is the second witness, and two copies that must agree is the
 * point of a test. (When they move to platform_regs.h, both go.) */
#define DUTEGR_STATUS_FRAME_RDY  (1u << 0)
#define DUTEGR_STATUS_EMPTY      (1u << 1)
#define DUTEGR_STATUS_OVF        (1u << 4)
#define DUTEGR_STATUS_DESYNC     (1u << 6)
#define DUTEGR_DATA_VALID        (1u << 8)
#define DUTEGR_DATA_LAST         (1u << 9)
#define DUTEGR_CTRL_CLR_CNT      (1u << 2)

/* ------------------------------------------------------------------------- */
/* The DUTEGR behavioural model                                               */
/* ------------------------------------------------------------------------- */

#define MODEL_MAX_FRAMES 8
#define MODEL_MAX_LEN    1536

typedef struct {
    uint8_t  bytes[MODEL_MAX_LEN];
    uint16_t len;
} model_frame_t;

typedef struct {
    model_frame_t frames[MODEL_MAX_FRAMES];
    int      n_frames;      /* frames committed and not yet fully drained */
    int      head_off;      /* bytes of frames[0] already popped          */
    uint32_t rx_frames;
    uint32_t drop_full;
    uint32_t drop_giant;
    int      ovf;           /* sticky */
    int      desync;        /* sticky */
    /* Fault injection: after this many pops, DATA reports VALID = 0 even though
     * FRAME_LEN still says bytes remain. Models the disagreement the handler
     * must refuse to paper over. 0 = never. */
    int      valid_drops_after;
    /* Fault injection: latch DESYNC once this many pops have happened, i.e.
     * PART-WAY THROUGH a chunk. 0 = never. The RTL latches the bit on the pop
     * whose stored end-of-frame flag disagrees with the descriptor's length,
     * so "during the pops" is exactly when it can appear. */
    int      desync_at_pop;
    /* Observability: every DATA read the firmware issued, valid or not. */
    int      pops;
} dutegr_model_t;

static dutegr_model_t g_model;

static void model_reset(void)
{
    memset(&g_model, 0, sizeof(g_model));
}

/* Commit a whole frame into the capture FIFO (store-and-forward: a frame
 * becomes visible only as a whole, which is what the RTL's commit pointer
 * buys). Also advances RX_FRAMES, exactly as the block does. */
static void model_push_frame(const uint8_t *bytes, uint16_t len)
{
    assert(g_model.n_frames < MODEL_MAX_FRAMES);
    assert(len <= MODEL_MAX_LEN);
    memcpy(g_model.frames[g_model.n_frames].bytes, bytes, len);
    g_model.frames[g_model.n_frames].len = len;
    g_model.n_frames++;
    g_model.rx_frames++;
}

static uint32_t model_bytes_waiting(void)
{
    uint32_t total = 0;
    for (int i = 0; i < g_model.n_frames; i++) {
        total += g_model.frames[i].len;
    }
    return total - (uint32_t)g_model.head_off;
}

static int dutegr_hook(void *ctx, int is_write, uint32_t base, uint32_t off,
                       uint32_t *val)
{
    (void)ctx;
    (void)base;

    if (is_write) {
        if (off == DUTEGR_CTRL && (*val & DUTEGR_CTRL_CLR_CNT)) {
            g_model.rx_frames = g_model.drop_full = g_model.drop_giant = 0;
            g_model.ovf = g_model.desync = 0;
        }
        return 1; /* every write to this page is the model's */
    }

    switch (off) {
    case DUTEGR_STATUS: {
        uint32_t s = 0;
        if (g_model.n_frames > 0)        s |= DUTEGR_STATUS_FRAME_RDY;
        if (model_bytes_waiting() == 0u) s |= DUTEGR_STATUS_EMPTY;
        if (g_model.ovf)                 s |= DUTEGR_STATUS_OVF;
        if (g_model.desync)              s |= DUTEGR_STATUS_DESYNC;
        *val = s;
        return 1;
    }
    case DUTEGR_LEVEL:
        *val = (model_bytes_waiting() & 0xFFFFu)
             | ((uint32_t)g_model.n_frames << 16);
        return 1;
    case DUTEGR_FRAME_LEN:
        if (g_model.n_frames == 0) {
            *val = 0;
        } else {
            uint32_t total = g_model.frames[0].len;
            uint32_t rem   = total - (uint32_t)g_model.head_off;
            *val = (rem & 0xFFFFu) | (total << 16);
        }
        return 1;
    case DUTEGR_DATA: {
        g_model.pops++;
        if (g_model.desync_at_pop > 0 && g_model.pops >= g_model.desync_at_pop) {
            g_model.desync = 1;   /* latched mid-chunk, like the RTL's desync_q */
        }
        int starved = (g_model.valid_drops_after > 0
                       && g_model.pops > g_model.valid_drops_after);
        if (g_model.n_frames == 0 || starved) {
            *val = 0; /* VALID = 0, and it pops NOTHING */
            return 1;
        }
        model_frame_t *f = &g_model.frames[0];
        uint8_t b = f->bytes[g_model.head_off];
        int last  = (g_model.head_off == f->len - 1);
        *val = (uint32_t)b | DUTEGR_DATA_VALID | (last ? DUTEGR_DATA_LAST : 0u);
        g_model.head_off++;
        if (last) {
            /* The descriptor retires with the frame's last byte: memmove the
             * queue down, exactly as desc_pop does in the RTL. */
            for (int i = 1; i < g_model.n_frames; i++) {
                g_model.frames[i - 1] = g_model.frames[i];
            }
            g_model.n_frames--;
            g_model.head_off = 0;
        }
        return 1;
    }
    case DUTEGR_RX_FRAMES:  *val = g_model.rx_frames;  return 1;
    case DUTEGR_DROP_FULL:  *val = g_model.drop_full;  return 1;
    case DUTEGR_DROP_GIANT: *val = g_model.drop_giant; return 1;
    default:
        *val = 0; /* unmapped in the page: reads 0 and pops nothing */
        return 1;
    }
}

/* ------------------------------------------------------------------------- */
/* Harness                                                                    */
/* ------------------------------------------------------------------------- */

static void boot(void)
{
    mock_regs_reset();
    model_reset();
    mock_regs_set_hook(MPS3_DUTEGR_BASE, dutegr_hook, 0);
}

static int dispatch(const char *line, char *out, int out_len)
{
    return coordinator_dispatch_line(line, (int)strlen(line), out, out_len);
}

static int dutrx(char *out, int out_len)
{
    return dispatch("{\"op\":\"dutrx\"}", out, out_len);
}

/* Extract the "data" hex value into bytes. Returns the byte count, or -1 if
 * the key is missing / the hex is not an even run of hex digits -- a torn
 * chunk must fail this, not be half-decoded. */
static int parse_data_hex(const char *line, uint8_t *out, int out_max)
{
    const char *p = strstr(line, "\"data\":\"");
    if (p == NULL) {
        return -1;
    }
    p += strlen("\"data\":\"");
    int n = 0;
    while (*p != '"' && *p != '\0') {
        if (p[0] == '\0' || p[1] == '\0' || p[1] == '"') {
            return -1; /* odd number of hex digits */
        }
        char pair[3] = { p[0], p[1], '\0' };
        for (int i = 0; i < 2; i++) {
            if (strchr("0123456789abcdef", pair[i]) == NULL) {
                return -1; /* lowercase hex only, per the contract */
            }
        }
        if (n >= out_max) {
            return -1;
        }
        out[n++] = (uint8_t)strtoul(pair, NULL, 16);
        p += 2;
    }
    return (*p == '"') ? n : -1;
}

static int has(const char *line, const char *needle)
{
    return strstr(line, needle) != NULL;
}

/* Deterministic filler that is NOT a constant: a chunk boundary that dropped or
 * duplicated bytes would still pass against a run of 0xAA. */
static void fill_pattern(uint8_t *b, int len, uint8_t seed)
{
    for (int i = 0; i < len; i++) {
        b[i] = (uint8_t)(seed + (uint8_t)(i * 31u) + (uint8_t)(i >> 3));
    }
}

/* ------------------------------------------------------------------------- */
/* Cases                                                                      */
/* ------------------------------------------------------------------------- */

/* Nothing captured yet: the reply has the SAME key set as a full one, with
 * len/off/n zero and an empty data string -- so a polling client branches on
 * `n`, never on shape. And the counters are there even with no frame, which is
 * the whole reason they are on every reply. */
static void test_empty_fifo_is_a_full_shaped_reply(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(strcmp(out,
        "{\"ok\":true,\"len\":0,\"off\":0,\"n\":0,\"more\":false,\"last\":false,"
        "\"frames\":0,\"rx\":0,\"drop_full\":0,\"drop_giant\":0,"
        "\"ovf\":false,\"desync\":false,\"data\":\"\"}\n") == 0);

    /* An empty read pops NOTHING that a later frame would miss: the block's
     * DATA read on an empty FIFO returns VALID=0 and moves no pointer. */
    CHECK(g_model.n_frames == 0);
}

/* One short frame, read whole in a single request, byte for byte. */
static void test_one_short_frame_round_trips_byte_for_byte(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    uint8_t frame[60];
    fill_pattern(frame, (int)sizeof(frame), 0x11);
    model_push_frame(frame, (uint16_t)sizeof(frame));

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"ok\":true"));
    CHECK(has(out, "\"len\":60,\"off\":0,\"n\":60,\"more\":false,\"last\":true,"));
    /* The frame is gone from the FIFO and the queue is empty behind it. */
    CHECK(has(out, "\"frames\":0,"));
    CHECK(has(out, "\"rx\":1,"));

    uint8_t got[MPS3_DUTRX_CHUNK_MAX];
    CHECK(parse_data_hex(out, got, (int)sizeof(got)) == (int)sizeof(frame));
    CHECK(memcmp(got, frame, sizeof(frame)) == 0);

    /* EXACTLY one pop per byte -- no over-read past the frame's end, which on a
     * shared FIFO would eat the NEXT frame's first bytes. */
    CHECK(g_model.pops == (int)sizeof(frame));
}

/* A frame longer than one chunk: it must arrive in ceil(len/256) replies whose
 * `off`/`n`/`more` describe the run exactly, and reassemble byte for byte. This
 * is the case the 256-byte chunk exists for. */
static void test_long_frame_arrives_in_chunks_and_reassembles(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    uint8_t frame[700];
    uint8_t got[sizeof(frame)];
    fill_pattern(frame, (int)sizeof(frame), 0xC3);
    model_push_frame(frame, (uint16_t)sizeof(frame));

    int total = 0;
    int replies = 0;
    for (;;) {
        CHECK(dutrx(out, sizeof(out)) == 1);
        replies++;
        uint8_t chunk[MPS3_DUTRX_CHUNK_MAX];
        int n = parse_data_hex(out, chunk, (int)sizeof(chunk));
        CHECK(n >= 0);
        CHECK(n <= MPS3_DUTRX_CHUNK_MAX);
        CHECK(has(out, "\"len\":700,"));
        char off_key[32];
        (void)snprintf(off_key, sizeof(off_key), "\"off\":%d,", total);
        CHECK(has(out, off_key));
        memcpy(got + total, chunk, (size_t)n);
        total += n;
        if (has(out, "\"more\":false,")) {
            CHECK(total == (int)sizeof(frame));
            CHECK(has(out, "\"last\":true,"));  /* DATA[9] agreed with the length */
            break;
        }
        CHECK(has(out, "\"last\":false,"));     /* mid-frame: not the last byte */
        CHECK(n == MPS3_DUTRX_CHUNK_MAX);       /* a full chunk while more remains */
        CHECK(replies < 10);                    /* never loop forever */
    }
    CHECK(replies == 3);                        /* 256 + 256 + 188 */
    CHECK(memcmp(got, frame, sizeof(frame)) == 0);
    CHECK(g_model.pops == (int)sizeof(frame));
}

/* Two frames queued: `frames` counts what is still waiting AFTER the chunk just
 * read (the head's descriptor retires with its last byte), and the second frame
 * comes out intact behind the first -- the boundary is not smeared. */
static void test_frames_queue_behind_each_other(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    uint8_t a[64], b[128];
    uint8_t got[256];
    fill_pattern(a, (int)sizeof(a), 0x01);
    fill_pattern(b, (int)sizeof(b), 0x80);
    model_push_frame(a, (uint16_t)sizeof(a));
    model_push_frame(b, (uint16_t)sizeof(b));

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"len\":64,\"off\":0,\"n\":64,\"more\":false,\"last\":true,"));
    CHECK(has(out, "\"frames\":1,"));   /* b is still waiting */
    CHECK(has(out, "\"rx\":2,"));
    CHECK(parse_data_hex(out, got, (int)sizeof(got)) == (int)sizeof(a));
    CHECK(memcmp(got, a, sizeof(a)) == 0);

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"len\":128,\"off\":0,\"n\":128,\"more\":false,\"last\":true,"));
    CHECK(has(out, "\"frames\":0,"));
    CHECK(parse_data_hex(out, got, (int)sizeof(got)) == (int)sizeof(b));
    CHECK(memcmp(got, b, sizeof(b)) == 0);

    /* Drained: the next read is the empty shape again. */
    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"n\":0,\"more\":false,"));
}

/* THE DROP COUNTERS ARE NOT OPTIONAL. The block cannot backpressure the bridge
 * (that parks the whole bridge), so it drops -- and the invariant
 * rx + drop_full + drop_giant == frames presented is only useful if a host sees
 * all three on the same reply it reads frames from. A reply that reported
 * frames without them would make a lossy capture look lossless. */
static void test_drop_counters_and_sticky_flags_ride_every_reply(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    uint8_t frame[64];
    fill_pattern(frame, (int)sizeof(frame), 0x5A);

    g_model.drop_full  = 3;
    g_model.drop_giant = 1;
    g_model.ovf        = 1;      /* sticky: something WAS dropped */
    model_push_frame(frame, (uint16_t)sizeof(frame));

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"rx\":1,\"drop_full\":3,\"drop_giant\":1,"));
    CHECK(has(out, "\"ovf\":true,\"desync\":false,"));

    /* ... and on the EMPTY reply that follows, where a client polling an idle
     * board would otherwise never learn it had lost four frames. */
    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"n\":0,"));
    CHECK(has(out, "\"rx\":1,\"drop_full\":3,\"drop_giant\":1,"));
    CHECK(has(out, "\"ovf\":true,"));

    /* DESYNC is the block's own cross-check of its two end-of-frame records and
     * is reported, never interpreted, by the verb. */
    g_model.desync = 1;
    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"desync\":true,"));
}

/* DESYNC is read AFTER the pops, not before. The bit latches on the pop whose
 * stored end-of-frame record disagrees with the descriptor's length, so a
 * handler that sampled STATUS once up front would report this chunk's
 * disagreement one whole request late -- i.e. hung on the NEXT frame, or never
 * at all if the client stopped reading. The model latches it mid-chunk; a
 * before-the-pops sample reads false, an after-the-pops sample reads true, and
 * only one of those can produce the line below.
 *
 * Same argument for `frames`, which is LEVEL read after the pops: the head
 * frame's descriptor retires with its last byte, so a before sample would count
 * a frame the reply just consumed. test_frames_queue_behind_each_other pins
 * that half ("frames":1 with two frames pushed and one drained). */
static void test_sticky_desync_is_sampled_after_the_pops(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    uint8_t frame[16];
    fill_pattern(frame, (int)sizeof(frame), 0x20);
    model_push_frame(frame, (uint16_t)sizeof(frame));

    g_model.desync       = 0;     /* clean before the request... */
    g_model.desync_at_pop = 8;    /* ...latched half-way through its pops */

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"n\":16,"));
    CHECK(has(out, "\"desync\":true,"));
    CHECK(g_model.desync == 1);
}

/* FRAME_LEN says bytes remain but DATA reports VALID = 0. The handler must stop
 * at once and report the SHORT chunk (n < what the length implied), never pad
 * the reply out with the zeroes an empty DATA read returns: a short chunk is
 * visible to the client, invented bytes are not. */
static void test_valid_low_mid_chunk_truncates_rather_than_inventing_bytes(void)
{
    boot();
    char out[MPS3_CTRL_RESP_MAX];
    uint8_t frame[64];
    uint8_t got[MPS3_DUTRX_CHUNK_MAX];
    fill_pattern(frame, (int)sizeof(frame), 0x7E);
    model_push_frame(frame, (uint16_t)sizeof(frame));
    g_model.valid_drops_after = 10;   /* the 11th pop returns VALID = 0 */

    CHECK(dutrx(out, sizeof(out)) == 1);
    CHECK(has(out, "\"len\":64,\"off\":0,\"n\":10,"));
    CHECK(parse_data_hex(out, got, (int)sizeof(got)) == 10);
    CHECK(memcmp(got, frame, 10) == 0);
    /* `more` still says the frame is unfinished -- the client keeps asking. */
    CHECK(has(out, "\"more\":true,"));
    CHECK(has(out, "\"last\":false,"));
}

int main(void)
{
    test_empty_fifo_is_a_full_shaped_reply();
    test_one_short_frame_round_trips_byte_for_byte();
    test_long_frame_arrives_in_chunks_and_reassembles();
    test_frames_queue_behind_each_other();
    test_drop_counters_and_sticky_flags_ride_every_reply();
    test_sticky_desync_is_sampled_after_the_pops();
    test_valid_low_mid_chunk_truncates_rather_than_inventing_bytes();

    printf("test_dutrx_dispatch: %d checks passed\n", s_checks);
    return 0;
}
