/*
 * test_stage0_net.c -- the RESCUE server at the Ethernet-frame level: the REAL
 * stage0_net.c + stage0_tftp.c + stage0_ident.c + stage0_rescue.c, driven by a
 * scripted client that builds its frames and checksums with its OWN code (so
 * a checksum bug on one side cannot cancel one on the other). Frames go through
 * the two platform seams s0_eth_tx / s0_eth_rx; time is the test's.
 *
 * ARP (reply, announcement, not-for-us), ICMP echo (+ bad checksum), frame
 * filtering (other MAC, IP fragment, bad UDP checksum); TFTP: normal push with
 * blksize/tsize, plain 512 push, lost DATA (server retransmit), lost ACK
 * (duplicate DATA re-ACKed, not re-written), lost OACK (repeated WRQ), blksize
 * clamp + out-of-range + garbage, oversize by tsize and by data, bad CRC (in-band
 * ERROR, still in rescue, then a good push), busy second client, unknown TID,
 * session timeout, status read, DDR not calibrated, block-number wrap, restart
 * mid-push; identify (reply shape, malformed = silent, rate limit).
 */
#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_status.h"
#include "../stage0_net.h"
#include "../stage0_tftp.h"
#include "../stage0_ident.h"
#include "../stage0_rescue.h"
#include "../stage0_flow.h"

static const uint8_t SMAC[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
static const uint8_t CMAC[6] = { 0x3C, 0x11, 0x22, 0x33, 0x44, 0x55 };
#define SIP  0xC0A80A65u   /* 192.168.10.101 */
#define CIP  0xC0A80A01u   /* 192.168.10.1   */
#define CIP2 0xC0A80A02u
#define DDR_BASE 0x80000000u
#define DDR_SPAN (2u << 20)

/* ---- frame queues (the platform seams) ------------------------------------------ */
#define QN 64
struct frame { uint8_t b[1536]; uint32_t n; };
static struct frame rxq[QN], txq[QN * 4];
static unsigned rx_h, rx_t, tx_h, tx_t;

int s0_eth_tx(const void *f, uint32_t len)
{
    struct frame *q = &txq[tx_t++ % (QN * 4)];
    memcpy(q->b, f, len);
    q->n = len;
    return 0;
}

int s0_eth_rx(void *buf, uint32_t cap)
{
    if (rx_h == rx_t)
        return 0;
    struct frame *q = &rxq[rx_h++ % QN];
    if (q->n > cap)
        return -1;
    memcpy(buf, q->b, q->n);
    return (int)q->n;
}

/* ---- the client's own frame code ------------------------------------------------ */
static void p16(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
static void p32(uint8_t *p, uint32_t v) { p16(p, v >> 16); p16(p + 2, v); }
static uint32_t g16(const uint8_t *p) { return ((uint32_t)p[0] << 8) | p[1]; }
static uint32_t g32(const uint8_t *p) { return (g16(p) << 16) | g16(p + 2); }

static uint16_t csum(const uint8_t *p, uint32_t n, uint32_t s)
{
    for (uint32_t i = 0; i < n; i++)
        s += (i & 1u) ? p[i] : ((uint32_t)p[i] << 8);
    while (s >> 16)
        s = (s & 0xFFFFu) + (s >> 16);
    return (uint16_t)~s;
}

static uint8_t *c_frame(const uint8_t *dmac, uint32_t type, const uint8_t *smac)
{
    struct frame *q = &rxq[rx_t % QN];
    memset(q->b, 0, sizeof q->b);
    memcpy(q->b, dmac, 6);
    memcpy(q->b + 6, smac ? smac : CMAC, 6);
    p16(q->b + 12, type);
    return q->b;
}

static void c_push(uint32_t n) { rxq[rx_t % QN].n = n < 60u ? 60u : n; rx_t++; }

static void c_ip(uint8_t *f, uint32_t src, uint32_t proto, uint32_t plen, uint32_t frag)
{
    uint8_t *ip = f + 14;
    ip[0] = 0x45; p16(ip + 2, 20u + plen); p16(ip + 4, 7); p16(ip + 6, frag);
    ip[8] = 64; ip[9] = (uint8_t)proto; p32(ip + 12, src); p32(ip + 16, SIP);
    p16(ip + 10, csum(ip, 20, 0));
}

static int g_bad_udp_csum;
static void c_udp_from(uint32_t src, const uint8_t *smac, uint32_t sport, uint32_t dport,
                       const void *pl, uint32_t n)
{
    uint8_t *f = c_frame(SMAC, 0x0800, smac);
    uint8_t *u = f + 34;
    c_ip(f, src, 17, 8u + n, 0);
    p16(u, sport); p16(u + 2, dport); p16(u + 4, 8u + n);
    memcpy(u + 8, pl, n);
    uint8_t ph[12];
    p32(ph, src); p32(ph + 4, SIP); ph[8] = 0; ph[9] = 17; p16(ph + 10, 8u + n);
    uint32_t s = 0;
    for (int i = 0; i < 12; i++)
        s += (i & 1) ? ph[i] : ((uint32_t)ph[i] << 8);
    uint16_t c = csum(u, 8u + n, s);
    p16(u + 6, (c ? c : 0xFFFF) ^ (g_bad_udp_csum ? 0x0101u : 0u));
    c_push(34u + 8u + n + 4u);                      /* + an FCS, as the LAN9220 gives */
}
static void c_udp(uint32_t sport, uint32_t dport, const void *pl, uint32_t n)
{
    c_udp_from(CIP, CMAC, sport, dport, pl, n);
}

/* next server frame of any kind; NULL if none */
static struct frame *s_next(void) { return tx_h == tx_t ? NULL : &txq[tx_h++ % (QN * 4)]; }
static void s_drain(void) { tx_h = tx_t; }

/* next UDP datagram from the server; checks its IP + UDP checksums */
static int s_udp(uint32_t *dst_ip, uint32_t *sport, uint32_t *dport, uint8_t *pl, uint32_t *n)
{
    struct frame *f;
    while ((f = s_next()) != NULL) {
        if (g16(f->b + 12) != 0x0800 || f->b[23] != 17)
            continue;
        const uint8_t *ip = f->b + 14, *u = ip + 20;
        CHECK(csum(ip, 20, 0) == 0, "server IP checksum");
        uint32_t ul = g16(u + 4);
        uint8_t ph[12];
        memcpy(ph, ip + 12, 8); ph[8] = 0; ph[9] = 17; p16(ph + 10, ul);
        uint32_t s = 0;
        for (int i = 0; i < 12; i++)
            s += (i & 1) ? ph[i] : ((uint32_t)ph[i] << 8);
        CHECK(csum(u, ul, s) == 0, "server UDP checksum");
        CHECK(memcmp(f->b + 6, SMAC, 6) == 0, "server source MAC");
        *dst_ip = g32(ip + 16);
        *sport = g16(u);
        *dport = g16(u + 2);
        *n = ul - 8u;
        memcpy(pl, u + 8, *n);
        return 1;
    }
    return 0;
}

/* ---- the server under test --------------------------------------------------------- */
static struct s0_status ST;
static uint8_t *g_stage, *g_ddr;
static uint32_t g_now;
static unsigned g_verifies, g_tx_at_verify;
static struct s0_result g_res;

static void *a2p(uint32_t a, uint32_t len, void *c)
{
    (void)c;
    return s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len) ? g_ddr + (a - DDR_BASE) : NULL;
}

static int verify(const uint8_t *img, uint32_t len, struct s0_result *out, void *ctx)
{
    (void)ctx;
    g_verifies++;
    g_tx_at_verify = tx_t;
    struct s0_mem_src m = { img, len };
    struct s0_backend be = { s0_mem_read, a2p, &m };
    return s0_load(&be, out);
}

static void start(int ddr_ok, uint32_t stage_max)
{
    static struct s0_rescue_cfg cfg;
    memset(&ST, 0, sizeof ST);
    const struct s0_build_ids ids = { 1u, 0x3F1A560Fu, 2u };
    s0_status_open(&ST, &ids, 0u, 2u);
    ST.rescue_reason = ddr_ok ? S0_RR_NOCARD : S0_RR_DDR;
    rx_h = rx_t = tx_h = tx_t = 0;
    memcpy(cfg.mac, SMAC, 6);
    cfg.ip = SIP;
    cfg.shell_id = 0x3F1A560Fu;
    cfg.ddr_ok = ddr_ok;
    cfg.stage = g_stage;
    cfg.stage_max = stage_max;
    cfg.verify = verify;
    g_verifies = 0;
    s0_rescue_start(&cfg, &ST);
    g_now = 1000u;
}

static int step(uint32_t ms)
{
    g_now += ms;
    return s0_rescue_poll(g_now, &g_res);
}

/* ---- a TFTP client ---------------------------------------------------------------------- */
static uint8_t g_pkt[70000];

static uint32_t wrq(uint8_t *p, const char *opts[], int nopt)
{
    uint32_t n = 0;
    p[n++] = 0; p[n++] = 2;
    n += (uint32_t)sprintf((char *)p + n, "boot.img") + 1u;
    n += (uint32_t)sprintf((char *)p + n, "octet") + 1u;
    for (int i = 0; i < nopt; i++)
        n += (uint32_t)sprintf((char *)p + n, "%s", opts[i]) + 1u;
    return n;
}

static uint32_t rd_err(const uint8_t *pl, uint32_t n, char *msg)
{
    if (n < 5 || g16(pl) != 5)
        return 0xFFFF;
    snprintf(msg, 128, "%.127s", (const char *)pl + 4);   /* bounded: clipping is intended (gcc 11 -Wformat-truncation) */
    return g16(pl + 2);
}

/* push `len` bytes with the given WRQ options. drop_data = block number whose
 * FIRST transmission is lost; dup = block sent twice. Returns: 0 = final ACK
 * received, else the TFTP error code (+100), or -1 on silence. */
struct push_opt { const char **opts; int nopt; uint32_t drop_data, dup; uint32_t *acks_seen; uint32_t cport; };
static int push(const uint8_t *img, uint32_t len, const struct push_opt *o, char *errmsg)
{
    uint8_t pl[1600];
    uint32_t dip, sp, dp, n;
    uint32_t cport = o->cport ? o->cport : 40000u + (g_now & 0xFFu);
    s_drain();
    c_udp(cport, 69, g_pkt, wrq(g_pkt, o->opts, o->nopt));
    step(1);
    if (!s_udp(&dip, &sp, &dp, pl, &n))
        return -1;
    if (g16(pl) == 5)
        return 100 + (int)rd_err(pl, n, errmsg);
    uint32_t tid = sp, blk = 512u;
    CHECK(dp == cport && dip == CIP && tid == 69u, "reply FROM port 69 to the client (single-port)");
    if (g16(pl) == 6) {                              /* OACK */
        for (uint32_t i = 2; i < n;) {
            const char *k = (const char *)pl + i; i += (uint32_t)strlen(k) + 1u;
            const char *v = (const char *)pl + i; i += (uint32_t)strlen(v) + 1u;
            if (!strcmp(k, "blksize"))
                blk = (uint32_t)atoi(v);
        }
    } else {
        CHECK(g16(pl) == 4 && g16(pl + 2) == 0, "ACK 0");
    }
    uint32_t nblocks = len / blk + 1u, off = 0;
    for (uint32_t b = 1; b <= nblocks; b++) {
        uint32_t dl = len - off < blk ? len - off : blk;
        g_pkt[0] = 0; g_pkt[1] = 3; p16(g_pkt + 2, b & 0xFFFFu);
        memcpy(g_pkt + 4, img + off, dl);
        int sends = (o->dup == b) ? 2 : 1;
        if (o->drop_data == b) {
            /* first copy lost: nothing arrives; the server times out and re-ACKs b-1 */
            CHECK(step(S0_TFTP_TIMEOUT_MS) == 0, "no hand-off");
            CHECK(s_udp(&dip, &sp, &dp, pl, &n) && g16(pl) == 4 && g16(pl + 2) == ((b - 1u) & 0xFFFFu),
                  "server retransmitted ACK %u", b - 1u);
            CHECK(sp == 69u && dp == cport, "the retransmit comes FROM 69 (single-port)");
        }
        for (int s = 0; s < sends; s++) {
            c_udp(cport, tid, g_pkt, 4u + dl);
            step(1);
            if (!s_udp(&dip, &sp, &dp, pl, &n))
                return -1;
            CHECK(sp == 69u && dp == cport && dip == CIP, "every reply FROM 69 to the client (single-port)");
            if (g16(pl) == 5)
                return 100 + (int)rd_err(pl, n, errmsg);
            CHECK(g16(pl) == 4 && g16(pl + 2) == (b & 0xFFFFu), "ACK %u (got op %u blk %u)",
                  b, g16(pl), g16(pl + 2));
            if (o->acks_seen)
                (*o->acks_seen)++;
        }
        off += dl;
    }
    return 0;
}

static uint8_t g_image[700000];
static uint8_t g_payload[600000];

static uint32_t make_image(uint32_t plen, int corrupt)
{
    tu_fill(g_payload, plen, plen);
    struct tu_region r = { g_payload, plen, DDR_BASE };
    uint32_t n = tu_build_image(g_image, sizeof g_image, &r, 1, DDR_BASE, 0, 0);
    if (corrupt)
        g_image[4096 + plen / 2] ^= 0x10;
    return n;
}

static int wait_handoff(uint32_t max_ms)
{
    for (uint32_t t = 0; t < max_ms; t += 100)
        if (step(100))
            return 1;
    return 0;
}

/* ---- tests ------------------------------------------------------------------------------- */

static void t_arp_icmp(void)
{
    printf("test: ARP announcement + reply, ICMP echo, frame filters\n");
    start(1, 1u << 20);
    struct frame *f = s_next();
    CHECK(f && g16(f->b + 12) == 0x0806 && g16(f->b + 20) == 1 && g32(f->b + 28) == SIP &&
          g32(f->b + 38) == SIP && f->b[0] == 0xFF, "gratuitous ARP at start");

    uint8_t *a = c_frame((const uint8_t *)"\xff\xff\xff\xff\xff\xff", 0x0806, NULL) + 14;
    p16(a, 1); p16(a + 2, 0x0800); a[4] = 6; a[5] = 4; p16(a + 6, 1);
    memcpy(a + 8, CMAC, 6); p32(a + 14, CIP); p32(a + 24, SIP);
    c_push(42);
    step(1);
    f = s_next();
    CHECK(f && g16(f->b + 12) == 0x0806 && g16(f->b + 20) == 2 && memcmp(f->b + 22, SMAC, 6) == 0 &&
          memcmp(f->b, CMAC, 6) == 0 && g32(f->b + 38) == CIP, "ARP reply");
    CHECK(f && f->n >= 60, "padded to the Ethernet minimum");
    a = c_frame((const uint8_t *)"\xff\xff\xff\xff\xff\xff", 0x0806, NULL) + 14;
    p16(a, 1); p16(a + 2, 0x0800); a[4] = 6; a[5] = 4; p16(a + 6, 1);
    memcpy(a + 8, CMAC, 6); p32(a + 14, CIP); p32(a + 24, CIP2);
    c_push(42);
    step(1);
    CHECK(s_next() == NULL, "no reply for another address");

    uint8_t *e = c_frame(SMAC, 0x0800, NULL);
    c_ip(e, CIP, 1, 8 + 56, 0);
    uint8_t *m = e + 34;
    m[0] = 8; p16(m + 4, 0x77); p16(m + 6, 3);
    for (int i = 0; i < 56; i++) m[8 + i] = (uint8_t)i;
    p16(m + 2, csum(m, 64, 0));
    c_push(34 + 64);
    step(1);
    f = s_next();
    CHECK(f && f->b[23] == 1 && f->b[34] == 0 && g16(f->b + 38) == 0x77 &&
          memcmp(f->b + 42, e + 42, 56) == 0 && csum(f->b + 34, 64, 0) == 0 &&
          csum(f->b + 14, 20, 0) == 0, "echo reply");
    CHECK(ST.pings == 1u, "pings %u", ST.pings);

    e = c_frame(SMAC, 0x0800, NULL);
    c_ip(e, CIP, 1, 16, 0);
    e[34] = 8; p16(e + 36, 0x1234);                 /* wrong checksum */
    c_push(50);
    e = c_frame((const uint8_t *)"\x02\x00\x00\x4d\x50\x54", 0x0800, NULL);   /* other MAC */
    c_ip(e, CIP, 1, 8, 0); e[34] = 8; p16(e + 36, csum(e + 34, 8, 0));
    c_push(42);
    e = c_frame(SMAC, 0x0800, NULL);                 /* a fragment */
    c_ip(e, CIP, 1, 8, 0x2000); e[34] = 8; p16(e + 36, csum(e + 34, 8, 0));
    c_push(42);
    g_bad_udp_csum = 1;
    const char *rq = "\0\1stage0.status\0octet";
    c_udp(5000, 69, rq, 21);
    g_bad_udp_csum = 0;
    step(1);
    CHECK(s_next() == NULL, "bad ICMP csum / other MAC / fragment / bad UDP csum: all dropped");
    CHECK(ST.pings == 1u, "still one ping");
}

static void t_push_ok(void)
{
    char msg[128] = "";
    uint32_t len = make_image(300000, 0);
    const char *opts[] = { "blksize", "1468", "tsize", "" };
    char ts[16];
    sprintf(ts, "%u", len);
    opts[3] = ts;
    struct push_opt o = { opts, 4, 0, 0, NULL, 0 };

    printf("test: push with blksize 1468 + tsize; final ACK only after verify; dally; hand-off\n");
    start(1, 1u << 20);
    memset(g_ddr, 0, DDR_SPAN);
    int rc = push(g_image, len, &o, msg);
    CHECK(rc == 0, "push rc %d %s", rc, msg);
    CHECK(g_verifies == 1u, "verified once");
    CHECK(g_tx_at_verify < tx_t, "final ACK sent after the verify");
    CHECK(ST.rescue_state == S0_RS_ACCEPTED && ST.rescue_bytes == len && ST.rescue_last_rc == 0u,
          "status: accepted %u B", ST.rescue_bytes);
    CHECK(step(S0_TFTP_DALLY_MS / 2) == 0, "dallying");
    CHECK(wait_handoff(S0_TFTP_DALLY_MS), "hand-off after the dally");
    CHECK(g_res.entry_pc == DDR_BASE && memcmp(g_ddr, g_payload, 300000) == 0, "image placed");

    printf("test: no options -> ACK 0, 512-byte blocks\n");
    start(1, 1u << 20);
    struct push_opt o2 = { NULL, 0, 0, 0, NULL, 0 };
    len = make_image(20000, 0);
    CHECK(push(g_image, len, &o2, msg) == 0 && wait_handoff(3000), "plain push");

    printf("test: lost DATA -> server retransmits its ACK after %u ms\n", S0_TFTP_TIMEOUT_MS);
    start(1, 1u << 20);
    struct push_opt o3 = { NULL, 0, 3, 0, NULL, 0 };
    CHECK(push(g_image, len, &o3, msg) == 0 && wait_handoff(3000), "recovered");

    printf("test: lost ACK -> duplicate DATA re-ACKed and not re-written\n");
    start(1, 1u << 20);
    uint32_t acks = 0;
    struct push_opt o4 = { NULL, 0, 0, 5, &acks, 0 };
    CHECK(push(g_image, len, &o4, msg) == 0, "push");
    CHECK(acks == len / 512u + 2u, "one extra ACK (%u)", acks);
    CHECK(ST.rescue_bytes == len, "bytes %u == %u (no double write)", ST.rescue_bytes, len);
    CHECK(wait_handoff(3000), "hand-off");

    printf("test: lost OACK -> the repeated WRQ gets the same OACK from the same TID\n");
    start(1, 1u << 20);
    s_drain();
    const char *ob[] = { "blksize", "1024" };
    uint32_t n = wrq(g_pkt, ob, 2);
    uint8_t pl[1600]; uint32_t dip, sp, dp, pn, sp2;
    c_udp(41000, 69, g_pkt, n); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &pn) && g16(pl) == 6, "OACK");
    c_udp(41000, 69, g_pkt, n); step(1);
    CHECK(s_udp(&dip, &sp2, &dp, pl, &pn) && g16(pl) == 6 && sp2 == sp, "same OACK, same TID");
    CHECK(ST.rescue_sessions == 1u, "still one session");

    printf("test: block numbers wrap past 65535 (blksize 8)\n");
    start(1, 1u << 20);
    len = make_image(530000, 0);
    const char *o8[] = { "blksize", "8" };
    struct push_opt o5 = { o8, 2, 0, 0, NULL, 0 };
    CHECK(push(g_image, len, &o5, msg) == 0 && wait_handoff(3000), "%u blocks", len / 8u + 1u);
    CHECK(memcmp(g_ddr, g_payload, 530000) == 0, "wrapped image intact");
}

static void t_push_refused(void)
{
    char msg[128];
    uint8_t pl[1600]; uint32_t dip, sp, dp, n;
    uint32_t len = make_image(20000, 0);

    printf("test: blksize negotiation: clamp, too small, too big, garbage\n");
    start(1, 1u << 20);
    const char *big[] = { "BLKSIZE", "65464" };
    s_drain(); c_udp(42000, 69, g_pkt, wrq(g_pkt, big, 2)); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && g16(pl) == 6 && !strcmp((char *)pl + 2, "blksize") &&
          !strcmp((char *)pl + 10, "1468"), "65464 -> OACK 1468");
    static const char *bad[][2] = { { "blksize", "4" }, { "blksize", "65465" }, { "blksize", "12a" },
                                    { "blksize", "" } };
    for (int i = 0; i < 4; i++) {
        start(1, 1u << 20);
        s_drain(); c_udp(42001, 69, g_pkt, wrq(g_pkt, bad[i], 2)); step(1);
        CHECK(s_udp(&dip, &sp, &dp, pl, &n) && rd_err(pl, n, msg) == 8, "blksize '%s' -> ERROR 8",
              bad[i][1]);
    }

    printf("test: oversize image: by tsize (at once) and by data (at the block)\n");
    start(1, 64u * 1024u);
    const char *ts[] = { "tsize", "67109000" };
    s_drain(); c_udp(42002, 69, g_pkt, wrq(g_pkt, ts, 2)); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && rd_err(pl, n, msg) == 3, "tsize > max -> ERROR 3");
    CHECK(ST.rescue_rejects == 1u && ST.rescue_last_rc == S0_ETOOBIG, "recorded");
    len = make_image(100000, 0);
    struct push_opt o = { NULL, 0, 0, 0, NULL, 0 };
    CHECK(push(g_image, len, &o, msg) == 103, "data past 64 KiB -> ERROR 3 (%s)", msg);
    CHECK(ST.rescue_state == S0_RS_REJECTED, "state rejected");

    printf("test: bad CRC -> in-band ERROR, still in rescue; then a good push boots\n");
    start(1, 1u << 20);
    len = make_image(50000, 1);
    int rc = push(g_image, len, &o, msg);
    CHECK(rc == 100 && strstr(msg, "payload CRC"), "rc %d '%s'", rc, msg);
    CHECK(ST.rescue_state == S0_RS_REJECTED && ST.rescue_rejects == 1u &&
          ST.rescue_last_rc == S0_ECRC && ST.last_error == S0_LAST_ERROR(S0_ES_RESCUE, S0_ECRC),
          "recorded");
    CHECK(!wait_handoff(5000), "no hand-off after a rejected image");
    len = make_image(50000, 0);
    CHECK(push(g_image, len, &o, msg) == 0 && wait_handoff(3000), "the next good push boots");

    printf("test: one session: a second client is busy, a stranger on the TID gets ERROR 5\n");
    start(1, 1u << 20);
    s_drain(); c_udp(43000, 69, g_pkt, wrq(g_pkt, NULL, 0)); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && g16(pl) == 4, "session 1 ACK 0");
    uint32_t tid = sp;
    c_udp_from(CIP2, (const uint8_t *)"\x3c\x11\x22\x33\x44\x66", 43001, 69, g_pkt, wrq(g_pkt, NULL, 0));
    step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && rd_err(pl, n, msg) == 0 && strstr(msg, "busy") && dip == CIP2 &&
          sp == 69u, "busy, FROM 69");
    g_pkt[0] = 0; g_pkt[1] = 3; p16(g_pkt + 2, 1);
    c_udp_from(CIP2, (const uint8_t *)"\x3c\x11\x22\x33\x44\x66", 43001, tid, g_pkt, 100); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && rd_err(pl, n, msg) == 5 && dip == CIP2 && sp == 69u,
          "unknown TID, FROM 69");
    CHECK(ST.rescue_bytes == 0u, "the stranger's data was not taken");

    printf("test: silent client -> %u retransmits, then the session is dropped\n", S0_TFTP_RETRIES);
    unsigned re = 0;
    for (unsigned i = 0; i < S0_TFTP_RETRIES + 2u; i++) {
        step(S0_TFTP_TIMEOUT_MS);
        while (s_udp(&dip, &sp, &dp, pl, &n))
            re += g16(pl) == 4;
    }
    CHECK(re == S0_TFTP_RETRIES, "retransmits %u", re);
    CHECK(ST.rescue_state == S0_RS_LISTEN && ST.last_error == S0_LAST_ERROR(S0_ES_RESCUE, S0_ETIMEOUT),
          "dropped, listening");

    printf("test: a restart mid-push from the same client starts a new session\n");
    start(1, 1u << 20);
    s_drain(); c_udp(44000, 69, g_pkt, wrq(g_pkt, NULL, 0)); step(1);
    s_udp(&dip, &sp, &dp, pl, &n);
    g_pkt[0] = 0; g_pkt[1] = 3; p16(g_pkt + 2, 1); memset(g_pkt + 4, 0xAB, 512);
    c_udp(44000, sp, g_pkt, 516); step(1); s_drain();
    len = make_image(10000, 0);
    struct push_opt o6 = { NULL, 0, 0, 0, NULL, 44000u };   /* the same client port */
    CHECK(push(g_image, len, &o6, msg) == 0 && wait_handoff(3000), "restart works");
    CHECK(ST.rescue_sessions == 2u, "two sessions");
}

static void t_status_ddr(void)
{
    char msg[128];
    uint8_t pl[1600]; uint32_t dip, sp, dp, n;
    printf("test: RRQ stage0.status = the 256-byte block; other files ERROR 1\n");
    start(1, 1u << 20);
    s_drain();
    c_udp(45000, 69, "\0\1stage0.status\0octet\0", 22); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && g16(pl) == 3 && g16(pl + 2) == 1 && n == 4u + 256u &&
          memcmp(pl + 4, "S0ST", 4) == 0 && sp == 69u && dp == 45000, "status DATA FROM 69 (%u B)", n);
    CHECK(memcmp(pl + 4, &ST, 0xC8) == 0 && memcmp(pl + 4 + 0xCC, (uint8_t *)&ST + 0xCC, 256 - 0xCC) == 0,
          "the live block (tx_frames moves as it is sent)");
    c_udp(45000, 69, "\0\1vmlinux\0octet\0", 17); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && rd_err(pl, n, msg) == 1 && sp == 69u, "ERROR 1, FROM 69");

    printf("test: DDR not calibrated -> WRQ refused, ping/status/identify still answer\n");
    start(0, 1u << 20);
    CHECK(ST.rescue_state == S0_RS_NODDR, "state NODDR");
    s_drain();
    c_udp(45001, 69, g_pkt, wrq(g_pkt, NULL, 0)); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && rd_err(pl, n, msg) == 2 && strstr(msg, "ddr calib fail"),
          "ERROR 2 '%s'", msg);
    const char *req = "{\"op\":\"identify\",\"v\":1,\"nonce\":\"0123456789abcdef\"}";
    c_udp(45002, 6899, req, (uint32_t)strlen(req)); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && sp == 6899 && dp == 45002, "identify answers");
    pl[n] = 0;
    CHECK(strstr((char *)pl, "\"reason\":\"ddr calib fail\"") != NULL, "%s", (char *)pl);
}

static void t_identify(void)
{
    uint8_t pl[1600]; uint32_t dip, sp, dp, n;
    printf("test: identify reply shape\n");
    start(1, 1u << 20);
    s_drain();
    const char *req = " { \"nonce\" : \"DEADbeef01\", \"v\":1, \"op\":\"identify\", \"x\":null } ";
    c_udp(46000, 6899, req, (uint32_t)strlen(req)); step(1);
    CHECK(s_udp(&dip, &sp, &dp, pl, &n) && dip == CIP && sp == 6899 && dp == 46000, "to the sender");
    pl[n] = 0;
    const char *want = "{\"ok\":true,\"op\":\"identify\",\"v\":1,\"nonce\":\"DEADbeef01\",\"board\":\"mps3\","
                       "\"mode\":\"rescue\",\"shell_id\":\"0x3f1a560f\",\"ip\":\"192.168.10.101\","
                       "\"mac\":\"0200004d5053\",\"reason\":\"no card\",\"ports\":{\"tftp\":69}}";
    CHECK(strcmp((char *)pl, want) == 0, "\n      got  %s\n      want %s", (char *)pl, want);
    CHECK(n <= S0_IDENT_REPLY_MAX && ST.identifies == 1u, "size + counter");

    printf("test: identify malformed -> silent\n");
    static const char *bad[] = {
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"0123456\"}",            /* 7 hex */
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"0123456789abcdef0123456789abcdef0\"}", /* 33 */
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"0123456g\"}",           /* not hex */
        "{\"op\":\"identify\",\"v\":2,\"nonce\":\"01234567\"}",
        "{\"op\":\"ping\",\"v\":1,\"nonce\":\"01234567\"}",
        "{\"v\":1,\"nonce\":\"01234567\"}",
        "{\"op\":\"identify\",\"v\":1}",
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"01234567\",\"nonce\":\"01234567\"}",
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"01234567\",\"n\":{\"a\":1}}",
        "{\"op\":\"iden\\u0074ify\",\"v\":1,\"nonce\":\"01234567\"}",
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"01234567\"} x",
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"01234567\"",
        "[\"op\",\"identify\"]",
        "",
    };
    for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        c_udp(46001, 6899, bad[i], (uint32_t)strlen(bad[i]));
        step(200);
        CHECK(!s_udp(&dip, &sp, &dp, pl, &n), "silent for #%u %s", i, bad[i]);
    }
    char nonce[33];
    CHECK(s0_ident_parse((const uint8_t *)"{\"op\":\"identify\",\"v\":1,\"nonce\":\"01234567\"}\0", 44,
                         nonce) == 0 && !strcmp(nonce, "01234567"), "trailing NUL ok");

    printf("test: identify rate limit (%u/s)\n", S0_IDENT_RATE);
    start(1, 1u << 20);
    step(1);
    s_drain();
    const char *ok = "{\"op\":\"identify\",\"v\":1,\"nonce\":\"a1b2c3d4\"}";
    for (int i = 0; i < 15; i++)
        c_udp(46002, 6899, ok, (uint32_t)strlen(ok));
    step(1); step(1);
    unsigned got = 0;
    while (s_udp(&dip, &sp, &dp, pl, &n))
        got++;
    CHECK(got == S0_IDENT_RATE, "burst: %u replies", got);
    step(1000);
    for (int i = 0; i < 15; i++)
        c_udp(46002, 6899, ok, (uint32_t)strlen(ok));
    step(1); step(1);
    got = 0;
    while (s_udp(&dip, &sp, &dp, pl, &n))
        got++;
    CHECK(got == S0_IDENT_RATE, "refilled after 1 s: %u", got);
}

int main(void)
{
    g_stage = malloc(1u << 20);
    g_ddr = calloc(1, DDR_SPAN);
    t_arp_icmp();
    t_push_ok();
    t_push_refused();
    t_status_ddr();
    t_identify();
    printf("\n%d checks, %d failed\n", g_checks, g_fails);
    printf("RESULT: stage0 rescue network %s\n", g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
