/*
 * stage0_tftp.c -- see stage0_tftp.h. One session, lock-step, no heap.
 */
#include "stage0_tftp.h"
#include "stage0_net.h"
#include "stage0_fmt.h"

enum { OP_RRQ = 1, OP_WRQ = 2, OP_DATA = 3, OP_ACK = 4, OP_ERROR = 5, OP_OACK = 6 };
enum { T_IDLE = 0, T_RX, T_DALLY, T_DONE };

/* SINGLE-PORT TFTP: every datagram stage0 sends comes FROM port 69, never from a
 * fresh transfer ID. A host behind a stateful firewall only accepts replies that
 * match the flow it opened (its port <-> our 69); RFC 1350's fresh-TID replies
 * were dropped on the hub (B1, 2026-09-24: ping and identify fine, no TFTP
 * reply ever arrived). One session at a time, so the peer (ip, port) alone
 * identifies it. The status read stays stateless: a lost DATA is recovered by
 * the client repeating its RRQ. */
#define REPORT_STEP     (4u * 1024u * 1024u)

static struct {
    const struct s0_tftp_cfg *cfg;
    struct s0_status *st;
    uint32_t mode;
    uint32_t rs;
    uint32_t now;
    uint32_t peer_ip;
    uint16_t peer_port;
    uint32_t blksize;
    uint32_t expected;      /* next block, counted in 32 bits (wire = low 16) */
    uint32_t off;           /* bytes staged */
    uint8_t  ctl[48];       /* the last ACK/OACK sent, for retransmission */
    uint32_t ctl_len;
    uint32_t t_last;
    uint32_t retries;
    uint32_t dally_pending;
    uint32_t t_dally;
    uint32_t next_report;
    struct s0_result res;
} T;

static uint8_t s_pkt[4u + 511u];

static uint16_t get16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }

static void tlog(struct s0_line *l)
{
    if (T.cfg->log)
        T.cfg->log(T.cfg->log_ctx, l->b);
    s0l_init(l);
}

static void set_rs(uint32_t rs)
{
    T.rs = rs;
    if (T.st)
        T.st->rescue_state = rs;
}

static uint32_t idle_rs(void)
{
    return T.cfg->ddr_ok ? S0_RS_LISTEN : S0_RS_NODDR;
}

static void note_error(uint32_t rc)
{
    if (T.st)
        T.st->last_error = S0_LAST_ERROR(S0_ES_RESCUE, rc);
}

static void send_error(uint32_t ip, uint16_t sport, uint16_t dport,
                       uint32_t code, const char *msg)
{
    uint32_t n = 0;
    s_pkt[n++] = 0u;
    s_pkt[n++] = OP_ERROR;
    s_pkt[n++] = 0u;
    s_pkt[n++] = (uint8_t)code;
    while (*msg && n < sizeof s_pkt - 1u)
        s_pkt[n++] = (uint8_t)*msg++;
    s_pkt[n++] = 0u;
    (void)s0_udp_send(ip, sport, dport, s_pkt, n);
    if (T.st)
        T.st->tftp_errors += 1u;
}

/* send (or resend) the session's last control packet */
static void send_ctl(void)
{
    (void)s0_udp_send(T.peer_ip, S0_TFTP_PORT, T.peer_port, T.ctl, T.ctl_len);
    T.t_last = T.now;
}

static void ack(uint32_t blk)
{
    T.ctl[0] = 0u;
    T.ctl[1] = OP_ACK;
    T.ctl[2] = (uint8_t)(blk >> 8);
    T.ctl[3] = (uint8_t)blk;
    T.ctl_len = 4u;
    send_ctl();
}

static void end_session(uint32_t rs)
{
    T.mode = T_IDLE;
    set_rs(rs);
}

static void reject(uint32_t rc)
{
    note_error(rc);
    if (T.st) {
        T.st->rescue_rejects += 1u;
        T.st->rescue_last_rc = rc;
    }
    end_session(S0_RS_REJECTED);
}

/* ---- parsing -------------------------------------------------------------------- */

/* Length of the NUL-terminated string at p within avail bytes, or -1. */
static int cstr(const uint8_t *p, uint32_t avail)
{
    for (uint32_t i = 0; i < avail; ++i)
        if (p[i] == 0u)
            return (int)i;
    return -1;
}

static int ieq(const uint8_t *a, const char *b)
{
    for (;; ++a, ++b) {
        uint8_t c = *a;
        if (c >= 'A' && c <= 'Z')
            c = (uint8_t)(c - 'A' + 'a');
        if (c != (uint8_t)*b)
            return 0;
        if (c == 0u)
            return 1;
    }
}

/* Decimal, 1..10 digits, no sign, no overflow. 0 ok / -1. */
static int parse_u32(const uint8_t *s, uint32_t *out)
{
    uint64_t v = 0;
    uint32_t n = 0;
    for (; *s; ++s, ++n) {
        if (*s < '0' || *s > '9' || n >= 10u)
            return -1;
        v = v * 10u + (uint32_t)(*s - '0');
    }
    if (n == 0u || v > 0xFFFFFFFFull)
        return -1;
    *out = (uint32_t)v;
    return 0;
}

static uint32_t put_str(uint8_t *d, uint32_t n, const char *s)
{
    while (*s)
        d[n++] = (uint8_t)*s++;
    d[n++] = 0u;
    return n;
}

static uint32_t put_dec(uint8_t *d, uint32_t n, uint32_t v)
{
    struct s0_line l;
    s0l_init(&l);
    s0l_dec(&l, v);
    return put_str(d, n, l.b);
}

/* ---- requests on port 69 ---------------------------------------------------------- */

static void on_rrq(uint32_t ip, uint16_t port, const uint8_t *p, uint32_t len)
{
    int fl = cstr(p, len);
    if (fl < 0 || !ieq(p, S0_TFTP_STATUS_FILE)) {
        send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_NOTFOUND,
                   "stage0 serves only " S0_TFTP_STATUS_FILE);
        return;
    }
    uint32_t n = T.cfg->status_len < 511u ? T.cfg->status_len : 511u;
    const uint8_t *src = (const uint8_t *)T.cfg->status;
    s_pkt[0] = 0u;
    s_pkt[1] = OP_DATA;
    s_pkt[2] = 0u;
    s_pkt[3] = 1u;
    for (uint32_t i = 0; i < n; ++i)
        s_pkt[4u + i] = src[i];
    (void)s0_udp_send(ip, S0_TFTP_PORT, port, s_pkt, 4u + n);
}

static void on_wrq(uint32_t ip, uint16_t port, const uint8_t *p, uint32_t len)
{
    struct s0_line l;
    int same = (T.mode == T_RX && ip == T.peer_ip && port == T.peer_port);

    if (T.mode != T_IDLE) {
        if (same && T.expected == 1u) {
            send_ctl();           /* our OACK/ACK 0 was lost: the client repeated its WRQ */
            return;
        }
        if (!same) {
            send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_UNDEF, "busy: one push at a time");
            return;
        }
        /* the same client restarted mid-push: drop the old session, start over */
    }
    if (!T.cfg->ddr_ok) {
        send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_ACCESS,
                   "ddr calib fail: stage0 cannot stage an image");
        return;
    }

    int fl = cstr(p, len);
    int ml = fl < 0 ? -1 : cstr(p + fl + 1, len - (uint32_t)fl - 1u);
    if (fl < 0 || ml < 0) {
        send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_ILLEGAL, "malformed WRQ");
        return;
    }
    if (!ieq(p + fl + 1, "octet")) {
        send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_UNDEF, "octet mode only");
        return;
    }

    uint32_t blk = S0_TFTP_BLKSIZE_DEF, tsize = 0u;
    int has_blk = 0, has_tsize = 0;
    uint32_t o = (uint32_t)fl + 1u + (uint32_t)ml + 1u;
    while (o < len) {
        int nl = cstr(p + o, len - o);
        int vl = nl < 0 ? -1 : cstr(p + o + nl + 1, len - o - (uint32_t)nl - 1u);
        if (nl < 0 || vl < 0) {
            send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_OPTION, "malformed option");
            return;
        }
        const uint8_t *name = p + o, *val = p + o + nl + 1;
        uint32_t v;
        if (ieq(name, "blksize")) {
            if (parse_u32(val, &v) != 0 || v < S0_TFTP_BLKSIZE_MIN || v > S0_TFTP_BLKSIZE_CEIL) {
                send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_OPTION, "blksize must be 8..65464");
                return;
            }
            blk = v < S0_TFTP_BLKSIZE_MAX ? v : S0_TFTP_BLKSIZE_MAX;
            has_blk = 1;
        } else if (ieq(name, "tsize")) {
            if (parse_u32(val, &v) != 0) {
                send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_OPTION, "bad tsize");
                return;
            }
            if (v > T.cfg->stage_max) {
                send_error(ip, S0_TFTP_PORT, port, S0_TFTP_E_FULL, "image too large for stage0");
                reject(S0_ETOOBIG);
                return;
            }
            tsize = v;
            has_tsize = 1;
        }   /* any other option (timeout, windowsize, ...) is not acknowledged */
        o += (uint32_t)nl + 1u + (uint32_t)vl + 1u;
    }

    T.peer_ip = ip;
    T.peer_port = port;
    T.blksize = blk;
    T.expected = 1u;
    T.off = 0u;
    T.retries = 0u;
    T.next_report = REPORT_STEP;
    T.mode = T_RX;
    if (T.st) {
        T.st->rescue_sessions += 1u;
        T.st->rescue_bytes = 0u;
    }
    set_rs(S0_RS_RECEIVING);

    if (has_blk || has_tsize) {
        uint32_t n = 0;
        T.ctl[n++] = 0u;
        T.ctl[n++] = OP_OACK;
        if (has_blk) {
            n = put_str(T.ctl, n, "blksize");
            n = put_dec(T.ctl, n, blk);
        }
        if (has_tsize) {
            n = put_str(T.ctl, n, "tsize");
            n = put_dec(T.ctl, n, tsize);
        }
        T.ctl_len = n;
        send_ctl();
    } else {
        ack(0u);
    }

    s0l_init(&l);
    s0l_str(&l, "stage0: rescue: push from ");
    s0l_ip(&l, ip);
    s0l_str(&l, " blksize=");
    s0l_dec(&l, blk);
    if (has_tsize) {
        s0l_str(&l, " tsize=");
        s0l_dec(&l, tsize);
    }
    tlog(&l);
}

/* ---- the session ------------------------------------------------------------------ */

static void on_data(const uint8_t *p, uint32_t len)
{
    struct s0_line l;
    uint32_t blk = get16(p + 2);
    const uint8_t *d = p + 4;
    uint32_t dlen = len - 4u;

    if (T.mode == T_DALLY || T.mode == T_DONE) {
        if (blk == ((T.expected - 1u) & 0xFFFFu))
            send_ctl();           /* the final ACK was lost */
        return;
    }
    if (blk != (T.expected & 0xFFFFu)) {
        if (T.expected > 1u && blk == ((T.expected - 1u) & 0xFFFFu))
            send_ctl();           /* duplicate: its ACK was lost; re-ACK, never re-write */
        return;                   /* anything else is stale: ignore (RFC 1350) */
    }
    if (dlen > T.blksize) {
        send_error(T.peer_ip, S0_TFTP_PORT, T.peer_port, S0_TFTP_E_ILLEGAL,
                   "DATA larger than the negotiated blksize");
        note_error(S0_EPROTO);
        end_session(idle_rs());
        return;
    }
    if (dlen > T.cfg->stage_max - T.off) {
        send_error(T.peer_ip, S0_TFTP_PORT, T.peer_port, S0_TFTP_E_FULL, "image too large for stage0");
        reject(S0_ETOOBIG);
        return;
    }

    uint8_t *dst = T.cfg->stage + T.off;
    for (uint32_t i = 0; i < dlen; ++i)
        dst[i] = d[i];
    T.off += dlen;
    T.retries = 0u;
    T.expected += 1u;
    if (T.st)
        T.st->rescue_bytes = T.off;

    if (dlen == T.blksize) {
        ack(blk);
        if (T.off >= T.next_report) {
            T.next_report += REPORT_STEP;
            s0l_init(&l);
            s0l_str(&l, "stage0: rescue: ");
            s0l_dec(&l, T.off >> 20);
            s0l_str(&l, " MiB");
            tlog(&l);
        }
        return;
    }

    /* The final block. Verify BEFORE acknowledging it, so the ACK itself says
     * "accepted" and a bad image is answered with an ERROR the client sees. */
    set_rs(S0_RS_VERIFYING);
    s0l_init(&l);
    s0l_str(&l, "stage0: rescue: received ");
    s0l_dec(&l, T.off);
    s0l_str(&l, " B, verifying");
    tlog(&l);

    int rc = T.cfg->verify(T.cfg->stage, T.off, &T.res, T.cfg->verify_ctx);
    if (T.st)
        T.st->rescue_last_rc = (uint32_t)rc;
    if (rc == S0_OK) {
        ack(blk);
        T.mode = T_DALLY;
        T.dally_pending = 1u;
        set_rs(S0_RS_ACCEPTED);
        s0l_str(&l, "stage0: rescue: image OK, hdr_crc=");
        s0l_hex(&l, T.res.header_crc32);
        tlog(&l);
        return;
    }
    s0l_str(&l, "image rejected: ");
    s0l_str(&l, s0_strerror(rc));
    send_error(T.peer_ip, S0_TFTP_PORT, T.peer_port, S0_TFTP_E_UNDEF, l.b);
    reject((uint32_t)rc);
    s0l_init(&l);
    s0l_str(&l, "stage0: rescue: image REJECTED (");
    s0l_str(&l, s0_strerror(rc));
    s0l_str(&l, "), still in rescue");
    tlog(&l);
}

void s0_tftp_udp(uint32_t src_ip, uint16_t sport, uint16_t dport,
                 const uint8_t *p, uint32_t len)
{
    if (len < 2u || dport != S0_TFTP_PORT)
        return;                   /* single-port: nothing of ours lives anywhere else */
    uint32_t op = get16(p);

    if (op == OP_WRQ) {
        on_wrq(src_ip, sport, p + 2, len - 2u);
        return;
    }
    if (op == OP_RRQ) {
        on_rrq(src_ip, sport, p + 2, len - 2u);
        return;
    }
    if (T.mode == T_IDLE || op == OP_ACK || op == OP_OACK)
        return;                   /* stale, or the ACK of a status read: ignore */
    if (src_ip != T.peer_ip || sport != T.peer_port) {
        if (op == OP_DATA)        /* never answer another peer's ERROR (RFC 1350) */
            send_error(src_ip, S0_TFTP_PORT, sport, S0_TFTP_E_TID, "unknown transfer ID");
        return;
    }
    if (op == OP_ERROR) {
        if (T.mode == T_RX) {
            struct s0_line l;
            s0l_init(&l);
            s0l_str(&l, "stage0: rescue: client aborted the push");
            tlog(&l);
            end_session(idle_rs());
        }
        return;
    }
    if (op == OP_DATA && len >= 4u)
        on_data(p, len);
}

/* ---- API ---------------------------------------------------------------------------- */

void s0_tftp_init(const struct s0_tftp_cfg *cfg, struct s0_status *st)
{
    T.cfg = cfg;
    T.st = st;
    T.mode = T_IDLE;
    T.retries = 0u;
    T.dally_pending = 0u;
    T.now = 0u;
    set_rs(idle_rs());
}

void s0_tftp_poll(uint32_t now_ms)
{
    T.now = now_ms;
    if (T.mode == T_RX) {
        if ((uint32_t)(now_ms - T.t_last) >= S0_TFTP_TIMEOUT_MS) {
            if (T.retries >= S0_TFTP_RETRIES) {
                struct s0_line l;
                s0l_init(&l);
                s0l_str(&l, "stage0: rescue: push timed out at ");
                s0l_dec(&l, T.off);
                s0l_str(&l, " B");
                tlog(&l);
                note_error(S0_ETIMEOUT);
                end_session(idle_rs());
            } else {
                T.retries += 1u;
                send_ctl();
            }
        }
    } else if (T.mode == T_DALLY) {
        if (T.dally_pending) {
            T.dally_pending = 0u;   /* start timing after the (long) verify */
            T.t_dally = now_ms;
        } else if ((uint32_t)(now_ms - T.t_dally) >= S0_TFTP_DALLY_MS) {
            T.mode = T_DONE;
        }
    }
}

int s0_tftp_ready(struct s0_result *out)
{
    if (T.mode != T_DONE)
        return 0;
    *out = T.res;
    return 1;
}

uint32_t s0_tftp_state(void)
{
    return T.rs;
}
