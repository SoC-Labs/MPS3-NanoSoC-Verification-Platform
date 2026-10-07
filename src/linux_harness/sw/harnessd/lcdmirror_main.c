/*
 * lcdmirror_main.c -- mps3-lcdmirror: the LCD mirror's server, a child of
 * mps3-harnessd at nice 10 (LCD_MIRROR_FPGA.md §6.1), serving the §6.2 wire on
 * TCP 127.0.0.1:6940 ONLY, to at most two clients. The byte layout is
 * net-protocol.md "LCD mirror (TCP 6940)" (Harness Manager's §6.1 reading plus
 * its H1/H3 rules); this header is the tour.
 *
 * SOURCE. The snooper's aperture layout (lcdmirror.h), read through src_*():
 * today the shared file harnessd's model writes (mode "sw"); after mint 4 the
 * LCDMIR UIO window (mode "hw") -- the only code that changes is src_*(). A
 * client that is not asking (before its KEY, or paused by RATE 0) and no client
 * at all cost no source access (§6.1: zero MMIO while idle).
 *
 * PER CLIENT, at most `rate` SNAPs a second (default 5 Hz; RATE clamps to
 * 1..30, 0 = pause), and only while it has room: at most LCDM_WINDOW (2)
 * UPDATEs unACKed (H1: drop-to-latest at the source -- a slow link never
 * queues stale frames; changed tiles coalesce in the client's queue):
 *   1. SNAP (sw: take the model's live dirty map with an atomic exchange), then
 *      the header words (seqlocked against a harnessd publish).
 *   2. Each dirty tile is read and compared with the local copy: dirty is not
 *      changed (the harness redraws identical glyphs), so only CHANGED tiles
 *      are queued, per client -- plus tiles that became VALID again.
 *   3. The SNAP is encoded at once (every tile in its smallest encoding,
 *      lcdmirror_enc.c) and sent as one or more UPDATEs of at most max_msg,
 *      all with the SAME t_ms/frames/resets/status/owner/valid; the last one
 *      carries snap_last. A keyframe (the first SNAP after KEY) carries every
 *      VALID tile, key/key_first/key_last, and the 256 REGS on its first part.
 *   Nothing changed (tiles, owner, valid map, status, MODE, resets) = no UPDATE:
 *   PING is the liveness probe. PONG and the RATE echo are answered at any
 *   time, between messages, whatever the window.
 *
 * REFUSALS (amendment 3): a peer that is not loopback, or a third client, gets
 * ONE line {"ok":false,"err":"lcd_mirror: <reason>"} and an immediate close.
 * A client that closed is reaped at once (its hang-up is handled before the
 * accept in the same pass, and a full house re-checks for hung-up clients
 * before it refuses), so "one tool closes, the next connects" never sees busy.
 * The port is bound to 127.0.0.1, so a real peer arrives only through an SSH
 * forward -- which exists only after a claim (S12); the peer check is the belt.
 *
 * STATUS for harnessd's `stats.lcd_mirror`: the LCDM_SV_* page, seqlocked.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "lcdmirror.h"

#define SNAP_MIN_US      2000u      /* one SNAP of the source serves every client due within 2 ms */
#define STATUS_PERIOD_US 250000u
#define FPS_WINDOW_US    2000000u
#define FPS_RING         64u
#define CTL_BUF          256u
#define RECS_MAX         (LCDM_NTILES * LCDM_REC_MAX)

static volatile sig_atomic_t s_stop;
static void on_signal(int sig) { (void)sig; s_stop = 1; }

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

static uint32_t now_ms32(void) { return (uint32_t)(now_us() / 1000u); }

/* ==========================================================================
 * The source (one struct, two backends)
 * ========================================================================== */
static struct {
    volatile uint32_t *ap;
    volatile uint16_t *fb;
    int                mode;         /* LCDM_MODE_SW (the only one before mint 4) */
} S;

#define AP(off) (S.ap[(off) / 4u])

static int src_open_sw(const char *path)
{
    int fd = open(path, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    struct stat sb;
    if (fstat(fd, &sb) != 0 || sb.st_size < (off_t)LCDM_APERTURE) {
        close(fd);
        return -1;
    }
    void *p = mmap(0, LCDM_APERTURE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) {
        return -1;
    }
    S.ap = (volatile uint32_t *)p;
    S.fb = (volatile uint16_t *)((volatile uint8_t *)p + LCDM_FB);
    S.mode = LCDM_MODE_SW;
    return 0;
}

/* SNAP (§2.7): the dirty tiles since the last SNAP, taken and cleared as one
 * step. Hardware: CTRL.SNAP, then DIRTY[10]. Software: an atomic exchange per
 * word of the model's live map (lcdmirror.h), written to DIRTY for the record. */
static void src_snap(uint32_t dirty[LCDM_MAP_WORDS])
{
    uint32_t minx = 0x1FF, maxx = 0, miny = 0x1FF, maxy = 0;
    for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
        dirty[w] = __atomic_exchange_n((uint32_t *)&S.ap[LCDM_SW_LIVE / 4u + w], 0u,
                                       __ATOMIC_ACQ_REL);
        AP(LCDM_DIRTY + 4u * w) = dirty[w];
        for (uint32_t b = dirty[w]; b; b &= b - 1u) {
            unsigned t = 32u * w + (unsigned)__builtin_ctz(b);
            unsigned x = (t % LCDM_TX) * LCDM_TILE, y = (t / LCDM_TX) * LCDM_TILE;
            if (x < minx) minx = x;
            if (x + 15u > maxx) maxx = x + 15u;
            if (y < miny) miny = y;
            if (y + 15u > maxy) maxy = y + 15u;
        }
    }
    AP(LCDM_SNAP_SEQ) = AP(LCDM_SEQ);
    /* tile-granular in sw mode (the hardware keeps a pixel bbox) */
    AP(LCDM_SNAP_BBOX_X) = (maxx << 16) | minx;
    AP(LCDM_SNAP_BBOX_Y) = (maxy << 16) | miny;
}

typedef struct {
    uint32_t seq, frames, resets, status, mode, blind;
    uint32_t valid[LCDM_MAP_WORDS];
    uint8_t  regs[256];
} hdr_t;

static void src_header(hdr_t *h)
{
    for (int tries = 0; tries < 16; tries++) {
        uint32_t s0 = __atomic_load_n((uint32_t *)&S.ap[LCDM_SW_PUBSEQ / 4u], __ATOMIC_ACQUIRE);
        if (s0 & 1u) {
            sched_yield();
            continue;
        }
        h->seq = AP(LCDM_SNAP_SEQ);
        h->frames = AP(LCDM_FRAMES);
        h->resets = AP(LCDM_RESETS);
        h->status = AP(LCDM_STATUS);
        h->mode = AP(LCDM_MODE);
        h->blind = AP(LCDM_SW_BLIND);
        for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
            h->valid[w] = AP(LCDM_VALID + 4u * w);
        }
        for (unsigned i = 0; i < 64u; i++) {
            uint32_t v = AP(LCDM_REGS + 4u * i);
            h->regs[4u * i] = (uint8_t)v;
            h->regs[4u * i + 1u] = (uint8_t)(v >> 8);
            h->regs[4u * i + 2u] = (uint8_t)(v >> 16);
            h->regs[4u * i + 3u] = (uint8_t)(v >> 24);
        }
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if (__atomic_load_n((uint32_t *)&S.ap[LCDM_SW_PUBSEQ / 4u], __ATOMIC_ACQUIRE) == s0) {
            return;
        }
    }
    /* a publish kept landing on us: use what we read; the next pass converges */
}

/* ==========================================================================
 * The mirror's own copy of the frame
 * ========================================================================== */
static uint16_t s_cur[LCDM_NPX];
static hdr_t    s_h;
static uint64_t s_snap_us;
static int      s_have_cur;

/* ==========================================================================
 * Clients
 * ========================================================================== */
typedef struct {
    int      fd;
    char     peer[LCDM_SV_PEER_LEN];
    uint32_t since_ms;
    unsigned rate;                   /* SNAPs a second; 0 = paused                 */
    int      started;                /* a KEY was seen (amendment 6: KEY on start) */
    int      key_req;
    uint64_t next_snap_us;
    uint32_t seq, acked;             /* last UPDATE sent / highest ACKed (H1)      */
    uint32_t pending[LCDM_MAP_WORDS];     /* tiles changed since its last SNAP     */
    uint32_t held_valid[LCDM_MAP_WORDS];  /* the valid map its last SNAP carried   */
    uint32_t sig;                    /* header signature at its last SNAP          */
    /* the SNAP being sent */
    int      in_snap, snap_key, snap_part, snap_tiles;
    lcdm_snaphdr_t sh;
    uint8_t *recs;
    size_t   recs_len, recs_off;
    /* output: the current UPDATE, then small control messages between UPDATEs */
    uint8_t *out;
    size_t   out_len, out_off;
    uint8_t  ctl[CTL_BUF];
    size_t   ctl_len, ctl_off;
    uint8_t  in[LCDM_HDR + LCDM_CLIENT_MSG_MAX];
    size_t   in_len;
    uint32_t bytes;
    uint64_t fps_t[FPS_RING];
    unsigned fps_n;
} client_t;

static client_t s_c[LCDM_MAX_CLIENTS];
static unsigned s_max_msg = LCDM_MAX_MSG;
static uint32_t s_static_id;
static char     s_boot_id[48];
static uint32_t s_refused, s_updates, s_keys;
static const char *s_trusted;       /* test builds: the ONE peer counted as loopback */

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint64_t period_us(const client_t *c) { return 1000000u / (c->rate ? c->rate : 1u); }
static int room(const client_t *c) { return (uint32_t)(c->seq - c->acked) < LCDM_WINDOW; }

static void client_close(client_t *c)
{
    if (c->fd >= 0) {
        close(c->fd);
    }
    free(c->out);
    free(c->recs);
    memset(c, 0, sizeof(*c));
    c->fd = -1;
}

static void refuse(int fd, const char *why)
{
    char line[128];
    size_t n = lcdm_refusal_line(line, sizeof(line), why);
    (void)send(fd, line, n, MSG_NOSIGNAL | MSG_DONTWAIT);
    shutdown(fd, SHUT_WR);
    close(fd);
    s_refused++;
}

static int peer_ok(const struct sockaddr_in *a)
{
    if (s_trusted) {
        struct in_addr t;
        return inet_pton(AF_INET, s_trusted, &t) == 1 && t.s_addr == a->sin_addr.s_addr;
    }
    return (ntohl(a->sin_addr.s_addr) >> 24) == 127u;    /* 127.0.0.0/8 */
}

/* A small board->client message (PONG, the RATE echo), sent between UPDATEs. */
static void ctl_queue(client_t *c, uint8_t type, const uint8_t *body, uint32_t len)
{
    if (c->ctl_len + LCDM_HDR + len > sizeof(c->ctl)) {
        return;                                  /* a flood of PINGs: drop, never block */
    }
    lcdm_put_hdr(c->ctl + c->ctl_len, type, len);
    memcpy(c->ctl + c->ctl_len + LCDM_HDR, body, len);
    c->ctl_len += LCDM_HDR + len;
}

/* A client that already hung up (its FIN/RST queued, but the poll loop sampled
 * its revents before that arrived) must not cost the next client a "busy"
 * refusal: the order "a grab tool closes, the display connects at once" would
 * otherwise race the reap. POLLRDHUP = the peer closed its write half; a 6940
 * client that can no longer ACK is finished anyway. */
static void reap_hung_up(void)
{
    for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
        client_t *c = &s_c[i];
        if (c->fd < 0) {
            continue;
        }
        struct pollfd p = { c->fd, POLLRDHUP, 0 };
        if (poll(&p, 1, 0) == 1 && (p.revents & (POLLRDHUP | POLLHUP | POLLERR | POLLNVAL))) {
            client_close(c);
        }
    }
}

static client_t *free_slot(void)
{
    for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
        if (s_c[i].fd < 0) {
            return &s_c[i];
        }
    }
    return 0;
}

static void do_accept(int lfd)
{
    for (;;) {
        struct sockaddr_in a;
        socklen_t al = sizeof(a);
        int fd = accept4(lfd, (struct sockaddr *)&a, &al, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0) {
            return;
        }
        if (al < sizeof(a) || a.sin_family != AF_INET || !peer_ok(&a)) {
            refuse(fd, LCDM_REFUSE_NOT_LOOPBACK);
            continue;
        }
        client_t *c = free_slot();
        if (!c) {
            reap_hung_up();                      /* a closed client frees its slot NOW */
            c = free_slot();
        }
        if (!c) {
            refuse(fd, LCDM_REFUSE_BUSY);
            continue;
        }
        c->out = (uint8_t *)malloc(s_max_msg);
        c->recs = (uint8_t *)malloc(RECS_MAX);
        if (!c->out || !c->recs) {
            free(c->out);
            free(c->recs);
            c->out = c->recs = 0;
            refuse(fd, "out of memory");
            continue;
        }
        int one = 1;
        (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        c->fd = fd;
        char ip[INET_ADDRSTRLEN] = "?";
        inet_ntop(AF_INET, &a.sin_addr, ip, sizeof(ip));
        snprintf(c->peer, sizeof(c->peer), "%s:%u", ip, (unsigned)ntohs(a.sin_port));
        c->since_ms = now_ms32();
        c->rate = LCDM_RATE_DEFAULT;
        c->out_len = lcdm_build_hello(c->out, s_max_msg, s_static_id, (unsigned)S.mode, s_max_msg,
                                      s_boot_id, c->rate);
        c->out_off = 0;
    }
}

/* ---- the source snapshot ------------------------------------------------------ */
static void mark_all_clients(unsigned t)
{
    for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
        if (s_c[i].fd >= 0) {
            s_c[i].pending[t >> 5] |= 1u << (t & 31u);
        }
    }
}

static void snapshot(uint64_t now)
{
    if (s_have_cur && now - s_snap_us < SNAP_MIN_US) {
        return;
    }
    s_snap_us = now;
    uint32_t dirty[LCDM_MAP_WORDS];
    src_snap(dirty);
    src_header(&s_h);
    uint16_t px[LCDM_TILE_PX];
    for (unsigned t = 0; t < LCDM_NTILES; t++) {
        int take = !s_have_cur || ((dirty[t >> 5] >> (t & 31u)) & 1u);
        if (!take) {
            continue;
        }
        lcdm_tile_get(S.fb, t, px);
        unsigned x0 = (t % LCDM_TX) * LCDM_TILE, y0 = (t / LCDM_TX) * LCDM_TILE;
        int changed = 0;
        for (unsigned yy = 0; yy < LCDM_TILE; yy++) {
            uint16_t *row = &s_cur[(y0 + yy) * LCDM_W + x0];
            if (memcmp(row, &px[yy * LCDM_TILE], 2u * LCDM_TILE) != 0) {
                memcpy(row, &px[yy * LCDM_TILE], 2u * LCDM_TILE);
                changed = 1;
            }
        }
        if (changed && s_have_cur) {
            mark_all_clients(t);
        }
    }
    s_have_cur = 1;
}

static uint32_t hdr_sig(void)
{
    uint32_t h = 2166136261u;
    uint32_t v[4 + LCDM_MAP_WORDS];
    /* in_gram flips with every window set-up and frames with every glyph: not news */
    v[0] = s_h.status & ~LCDM_ST_IN_GRAM; v[1] = s_h.mode; v[2] = s_h.resets; v[3] = s_h.blind;
    memcpy(&v[4], s_h.valid, sizeof(s_h.valid));
    const uint8_t *b = (const uint8_t *)v;
    for (size_t i = 0; i < sizeof(v); i++) {
        h = (h ^ b[i]) * 16777619u;
    }
    return h;
}

/* The wire's status word for the current header (CSR bits + exactness bits). */
static uint32_t wire_status(void)
{
    uint32_t st = s_h.status & 0x7FFu;
    if (S.mode == LCDM_MODE_HW) {
        if (!(st & LCDM_ST_VIOL) && (st & LCDM_ST_FMT_OK) && !(st & LCDM_ST_APPROX)) st |= LCDM_S_EXACT;
    } else {
        st |= LCDM_S_TEXT_ONLY;
        if (s_h.blind || (st & LCDM_ST_OWNER)) st |= LCDM_S_BLIND;
    }
    return st;
}

/* ---- one SNAP, then its parts ---------------------------------------------------- */
static void start_snap(client_t *c, uint64_t now, int key)
{
    uint32_t tiles[LCDM_MAP_WORDS];
    for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
        tiles[w] = key ? s_h.valid[w]
                       : ((c->pending[w] & s_h.valid[w]) | (s_h.valid[w] & ~c->held_valid[w]));
        c->pending[w] = 0u;
        c->held_valid[w] = s_h.valid[w];
    }
    unsigned n = 0;
    c->recs_len = lcdm_encode_records(s_cur, tiles, c->recs, &n);
    c->recs_off = 0;
    c->snap_tiles = (int)n;
    lcdm_snaphdr_t *h = &c->sh;
    h->t_ms = (uint32_t)(now / 1000u);
    h->frames = s_h.frames;
    h->resets = s_h.resets;
    h->status = wire_status();
    h->owner = (s_h.status & LCDM_ST_OWNER) ? LCDM_OWNER_DUT : LCDM_OWNER_HARNESS;
    for (unsigned i = 0; i < LCDM_MAP_BYTES; i++) {
        h->valid[i] = (uint8_t)(s_h.valid[i >> 2] >> (8u * (i & 3u)));
    }
    memcpy(h->regs, s_h.regs, 256);
    h->mode = s_h.mode;
    c->in_snap = 1;
    c->snap_key = key;
    c->snap_part = 0;
    c->sig = hdr_sig();
    if (key) {
        c->key_req = 0;
        s_keys++;
    }
}

static void next_part(client_t *c, uint64_t now)
{
    unsigned n = 0;
    size_t end = lcdm_next_part(c->recs, c->recs_len, c->recs_off,
                                lcdm_part_budget(s_max_msg, c->snap_key), &n);
    int last = end >= c->recs_len;
    uint32_t flags = last ? LCDM_S_SNAP_LAST : 0u;
    if (c->snap_key) {
        flags |= LCDM_S_KEY;
        if (c->snap_part == 0) flags |= LCDM_S_KEY_FIRST;
        if (last) flags |= LCDM_S_KEY_LAST;
    }
    c->seq++;
    c->out_len = lcdm_build_update(c->out, c->seq, &c->sh, flags, c->recs + c->recs_off,
                                   end - c->recs_off, n);
    c->out_off = 0;
    c->recs_off = end;
    c->snap_part++;
    s_updates++;
    if (last) {
        c->in_snap = 0;
        if (c->snap_tiles) {
            c->fps_t[c->fps_n++ % FPS_RING] = now;
        }
    }
}

/* Out buffer free: the next part, or a new SNAP if one is due and there is room. */
static void maybe_update(client_t *c, uint64_t now)
{
    if (c->out_len || !room(c)) {
        return;
    }
    if (c->in_snap) {
        next_part(c, now);
        return;
    }
    if (!c->started || c->rate == 0u) {
        return;                                  /* not asking, or paused: no source access */
    }
    if (!c->key_req && now < c->next_snap_us) {
        return;
    }
    c->next_snap_us = now + period_us(c);
    snapshot(now);
    if (!c->key_req) {
        int any = 0;
        for (unsigned w = 0; w < LCDM_MAP_WORDS; w++) {
            any |= (c->pending[w] & s_h.valid[w]) || (s_h.valid[w] & ~c->held_valid[w]);
        }
        if (!any && hdr_sig() == c->sig) {
            return;                              /* nothing changed: no UPDATE */
        }
    }
    start_snap(c, now, c->key_req);
    next_part(c, now);
}

/* ---- client input --------------------------------------------------------------- */
static int handle_input(client_t *c)
{
    for (;;) {
        ssize_t r = recv(c->fd, c->in + c->in_len, sizeof(c->in) - c->in_len, 0);
        if (r == 0) {
            return -1;
        }
        if (r < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            if (errno == EINTR) continue;
            return -1;
        }
        c->in_len += (size_t)r;
        while (c->in_len >= LCDM_HDR) {
            if (c->in[0] != 'L' || c->in[1] != 'M') {
                return -1;                       /* not this protocol: drop it */
            }
            uint32_t len = get32(c->in + 4);
            if (len > LCDM_CLIENT_MSG_MAX) {
                return -1;
            }
            if (c->in_len < LCDM_HDR + len) {
                break;
            }
            uint8_t type = c->in[2];
            const uint8_t *body = c->in + LCDM_HDR;
            if (type == LCDM_MSG_KEY) {
                c->key_req = 1;
                c->started = 1;
            } else if (type == LCDM_MSG_RATE && len >= 1u) {
                unsigned hz = body[0];
                if (hz > LCDM_RATE_MAX) hz = LCDM_RATE_MAX;
                c->rate = hz;                    /* 0 = pause */
                c->next_snap_us = 0;
                uint8_t echo = (uint8_t)hz;
                ctl_queue(c, LCDM_MSG_RATE, &echo, 1u);
            } else if (type == LCDM_MSG_PING && len >= 4u) {
                ctl_queue(c, LCDM_MSG_PONG, body, 4u);
            } else if (type == LCDM_MSG_ACK && len >= 4u) {
                uint32_t a = get32(body);
                /* newer than what we hold, and never beyond what we sent */
                if ((int32_t)(a - c->acked) > 0 && (int32_t)(a - c->seq) <= 0) {
                    c->acked = a;
                }
            }                                    /* unknown types: ignored */
            memmove(c->in, c->in + LCDM_HDR + len, c->in_len - LCDM_HDR - len);
            c->in_len -= LCDM_HDR + len;
        }
    }
    return 0;
}

/* ---- output ------------------------------------------------------------------------ */
static int send_buf(client_t *c, const uint8_t *buf, size_t len, size_t *off)
{
    while (*off < len) {
        ssize_t n = send(c->fd, buf + *off, len - *off, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;
            if (errno == EINTR) continue;
            return -1;
        }
        *off += (size_t)n;
        c->bytes += (uint32_t)n;
    }
    return 1;
}

/* Control messages go out BETWEEN UPDATEs (never inside one), whatever the window. */
static int flush_out(client_t *c)
{
    for (;;) {
        if (c->out_len && c->out_off > 0) {          /* finish the UPDATE in flight */
            int r = send_buf(c, c->out, c->out_len, &c->out_off);
            if (r <= 0) return r;
            c->out_len = c->out_off = 0;
            continue;
        }
        if (c->ctl_len) {
            int r = send_buf(c, c->ctl, c->ctl_len, &c->ctl_off);
            if (r < 0) return -1;
            if (r == 0) {
                /* keep what is unsent at the front */
                memmove(c->ctl, c->ctl + c->ctl_off, c->ctl_len - c->ctl_off);
                c->ctl_len -= c->ctl_off;
                c->ctl_off = 0;
                return 0;
            }
            c->ctl_len = c->ctl_off = 0;
            continue;
        }
        if (c->out_len) {
            int r = send_buf(c, c->out, c->out_len, &c->out_off);
            if (r <= 0) return r;
            c->out_len = c->out_off = 0;
            continue;
        }
        return 0;
    }
}

/* ---- the status page ---------------------------------------------------------------- */
static void write_status(void)
{
    const client_t *old = 0;
    unsigned n = 0;
    for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
        if (s_c[i].fd < 0) continue;
        n++;
        if (!old || (int32_t)(s_c[i].since_ms - old->since_ms) < 0) old = &s_c[i];
    }
    uint32_t fps = 0;
    if (old) {
        uint64_t now = now_us();
        unsigned cnt = 0, have = old->fps_n < FPS_RING ? old->fps_n : FPS_RING;
        for (unsigned k = 0; k < have; k++) {
            if (now - old->fps_t[k] <= FPS_WINDOW_US) cnt++;
        }
        fps = cnt * 10u * 1000000u / FPS_WINDOW_US;
    }
    uint32_t s = AP(LCDM_SV_SEQ);
    __atomic_store_n((uint32_t *)&S.ap[LCDM_SV_SEQ / 4u], s | 1u, __ATOMIC_RELEASE);
    __atomic_thread_fence(__ATOMIC_RELEASE);
    AP(LCDM_SV_PID) = (uint32_t)getpid();
    AP(LCDM_SV_BEAT_MS) = now_ms32();
    AP(LCDM_SV_CLIENTS) = n;
    AP(LCDM_SV_SINCE_MS) = old ? old->since_ms : 0u;
    AP(LCDM_SV_FPS_X10) = fps;
    AP(LCDM_SV_BYTES) = old ? old->bytes : 0u;
    AP(LCDM_SV_REFUSED) = s_refused;
    AP(LCDM_SV_UPDATES) = s_updates;
    AP(LCDM_SV_KEYS) = s_keys;
    volatile uint8_t *peer = (volatile uint8_t *)S.ap + LCDM_SV_PEER;
    const char *src = old ? old->peer : "";
    size_t sl = strlen(src);
    for (unsigned i = 0; i < LCDM_SV_PEER_LEN; i++) {
        peer[i] = (uint8_t)(i < sl ? src[i] : '\0');
    }
    AP(LCDM_SV_MAGIC) = LCDM_SV_MAGIC_VALUE;
    __atomic_thread_fence(__ATOMIC_RELEASE);
    __atomic_store_n((uint32_t *)&S.ap[LCDM_SV_SEQ / 4u], (s | 1u) + 1u, __ATOMIC_RELEASE);
}

/* ==========================================================================
 * main
 * ========================================================================== */
static void usage(void)
{
    fprintf(stderr,
            "usage: mps3-lcdmirror --shm PATH [--port N] [--parent PID] [--max-msg N]\n"
            "  serves the LCD mirror (net-protocol.md \"LCD mirror (TCP 6940)\") on\n"
            "  127.0.0.1:N (default %u) from the aperture file harnessd's model writes\n",
            (unsigned)LCDM_PORT);
}

static void read_boot_id(void)
{
    FILE *f = fopen("/proc/sys/kernel/random/boot_id", "r");
    s_boot_id[0] = '\0';
    if (f) {
        if (fgets(s_boot_id, sizeof(s_boot_id), f)) {
            s_boot_id[strcspn(s_boot_id, "\r\n")] = '\0';
        }
        fclose(f);
    }
}

int main(int argc, char **argv)
{
    const char *shm = 0;
    unsigned port = LCDM_PORT;
    long parent = 0;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : 0;
        if (strcmp(a, "--shm") == 0 && v) { shm = v; i++; }
        else if (strcmp(a, "--port") == 0 && v) { port = (unsigned)strtoul(v, 0, 0); i++; }
        else if (strcmp(a, "--parent") == 0 && v) { parent = strtol(v, 0, 0); i++; }
        else if (strcmp(a, "--max-msg") == 0 && v) { s_max_msg = (unsigned)strtoul(v, 0, 0); i++; }
#ifdef LCDM_TEST_HOOKS
        else if (strcmp(a, "--trusted-peer") == 0 && v) { s_trusted = v; i++; }
#endif
        else { usage(); return 2; }
    }
    if (!shm) {
        usage();
        return 2;
    }
    if (s_max_msg < LCDM_MIN_MSG) s_max_msg = LCDM_MIN_MSG;
    if (s_max_msg > LCDM_MAX_MSG) s_max_msg = LCDM_MAX_MSG;
#ifdef LCDM_TEST_HOOKS
    {
        const char *m = getenv("MPS3_LCDMIRROR_MUTATE");
        if (m) {
            lcdm_mutation = strcmp(m, "tile_idx") == 0 ? LCDM_MUT_TILE_IDX :
                            strcmp(m, "pal_bits") == 0 ? LCDM_MUT_PAL_BITS :
                            strcmp(m, "rle_len") == 0 ? LCDM_MUT_RLE_LEN :
                            strcmp(m, "px_endian") == 0 ? LCDM_MUT_PX_ENDIAN : LCDM_MUT_NONE;
            if (lcdm_mutation) fprintf(stderr, "mps3-lcdmirror: TEST MUTATION %s\n", m);
        }
    }
#endif
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, 0);
    sigaction(SIGINT, &sa, 0);
    signal(SIGPIPE, SIG_IGN);
    read_boot_id();

    for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) s_c[i].fd = -1;

    /* the source: harnessd created and initialised it before spawning us */
    for (int tries = 0; ; tries++) {
        if (src_open_sw(shm) == 0 && AP(LCDM_SW_MAGIC) == LCDM_SW_MAGIC_VALUE) break;
        if (s_stop || tries >= 50) {
            fprintf(stderr, "mps3-lcdmirror: no source at %s\n", shm);
            return 1;
        }
        struct timespec ts = { 0, 100000000L };
        nanosleep(&ts, 0);
    }
    s_static_id = AP(LCDM_SW_STATIC_ID);

    int lfd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    int one = 1;
    (void)setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in la;
    memset(&la, 0, sizeof(la));
    la.sin_family = AF_INET;
    la.sin_port = htons((uint16_t)port);
    la.sin_addr.s_addr = htonl(INADDR_LOOPBACK);        /* 127.0.0.1 ONLY, always */
    if (lfd < 0 || bind(lfd, (struct sockaddr *)&la, sizeof(la)) != 0 || listen(lfd, 4) != 0) {
        fprintf(stderr, "mps3-lcdmirror: cannot listen on 127.0.0.1:%u: %s\n", port, strerror(errno));
        return 1;
    }
    write_status();

    uint64_t status_us = 0, parent_us = 0;
    while (!s_stop) {
        uint64_t now = now_us();
        struct pollfd pf[1 + LCDM_MAX_CLIENTS];
        client_t *who[1 + LCDM_MAX_CLIENTS];
        unsigned np = 0;
        pf[np].fd = lfd; pf[np].events = POLLIN; who[np] = 0; np++;
        int timeout = 1000;
        unsigned nclients = 0;
        for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
            client_t *c = &s_c[i];
            if (c->fd < 0) continue;
            nclients++;
            short ev = POLLIN;
            if (c->out_len || c->ctl_len) {
                ev |= POLLOUT;
            } else if (room(c) && (c->in_snap || (c->started && c->rate))) {
                int64_t dt = (c->in_snap || c->key_req) ? 0 : (int64_t)(c->next_snap_us - now);
                int ms = dt <= 0 ? 0 : (int)((dt + 999) / 1000);
                if (ms < timeout) timeout = ms;
            }
            pf[np].fd = c->fd; pf[np].events = ev; who[np] = c; np++;
        }
        if (nclients) {
            uint64_t due = status_us + STATUS_PERIOD_US;
            int ms = (int)((due > now ? due - now : 0) / 1000u);
            if (ms < timeout) timeout = ms;
        }
        int r = poll(pf, np, timeout);
        if (r < 0 && errno != EINTR) break;
        now = now_us();
        for (unsigned k = 1; k < np; k++) {
            client_t *c = who[k];
            if (c->fd < 0) continue;
            if (pf[k].revents & (POLLERR | POLLNVAL)) { client_close(c); continue; }
            if (pf[k].revents & (POLLIN | POLLHUP)) {
                if (handle_input(c) != 0) { client_close(c); continue; }
            }
        }
        if (pf[0].revents & POLLIN) {
            do_accept(lfd);
        }
        for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
            client_t *c = &s_c[i];
            if (c->fd < 0) continue;
            for (int guard = 0; guard < 4; guard++) {       /* parts back to back while room */
                if (flush_out(c) != 0) { client_close(c); break; }
                if (c->out_len || c->ctl_len) break;
                size_t before = c->out_len;
                maybe_update(c, now);
                if (c->out_len == before) break;
            }
        }
        unsigned nc = 0;
        for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) nc += s_c[i].fd >= 0;
        if (!nc && s_have_cur) {
            s_have_cur = 0;          /* idle: the next client re-reads the whole frame */
        }
        if ((nc && now - status_us >= STATUS_PERIOD_US) || (!nc && nclients)) {
            status_us = now;
            write_status();
        }
        if (parent && now - parent_us >= 1000000u) {
            parent_us = now;
            if (getppid() != (pid_t)parent) break;       /* orphaned: harnessd is gone */
        }
    }
    for (unsigned i = 0; i < LCDM_MAX_CLIENTS; i++) {
        if (s_c[i].fd >= 0) client_close(&s_c[i]);
    }
    close(lfd);
    AP(LCDM_SV_CLIENTS) = 0u;
    AP(LCDM_SV_MAGIC) = 0u;
    return 0;
}
