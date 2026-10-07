/* swap_worker.c — see swap_worker.h. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "../../drivers/icap/mps3_dfx_uapi.h"
#include "spool.h"
#include "swap_worker.h"

#define CHUNK_BYTES (64u * 1024u)   /* chunked push: write() granularity */
#define AWAIT_POLL_MS 100

/* DFXCTL.STATUS bits (drivers/icap/mps3_icap_regs.h — mirrored here so the
 * userspace build does not pull the kernel-shared header chain). */
#define DFXCTL_STATUS_DECOUPLED   (1u << 0)
#define DFXCTL_STATUS_RP_IN_RESET (1u << 1)

static long long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
    (void)nanosleep(&ts, NULL);
}

static void set_fail(swap_outcome_t *o, const char *stage, int32_t err)
{
    o->ok = 0;
    o->verified = 0;
    o->err = err;
    strncpy(o->stage, stage, sizeof(o->stage) - 1);
    o->stage[sizeof(o->stage) - 1] = '\0';
}

/* ---- staged-pair await -------------------------------------------------- */

typedef struct {
    off_t rx_tmp_sz;
    int   clearing, partial;
} await_sig_t;

static void snapshot(const char *dir, await_sig_t *s)
{
    char p[512];
    struct stat st;
    s->rx_tmp_sz = -1;
    if (stat(spool_path(dir, SPOOL_RX_TMP, NULL, p, sizeof(p)), &st) == 0)
        s->rx_tmp_sz = st.st_size;
    s->clearing = spool_staged(dir, SPOOL_CLEARING);
    s->partial  = spool_staged(dir, SPOOL_PARTIAL);
}

/* Wait until both stages are published. Idle timer re-arms on ANY receive
 * progress (rx.tmp growth or a stage appearing) — the frozen 30 s contract:
 * bounds silence, never a slow transfer. Returns 0 or -1 (timeout). */
static int await_pair(const swap_worker_cfg_t *cfg)
{
    await_sig_t last, cur;
    long long last_progress = now_ms();

    snapshot(cfg->spool_dir, &last);
    while (!(last.clearing && last.partial)) {
        if (now_ms() - last_progress >= (long long)cfg->await_ms)
            return -1;
        sleep_ms(AWAIT_POLL_MS);
        snapshot(cfg->spool_dir, &cur);
        if (memcmp(&cur, &last, sizeof(cur)) != 0) {
            last_progress = now_ms();
            last = cur;
        }
    }
    return 0;
}

/* ---- push one staged file into the driver ------------------------------- */

static int push_file(int fd, const char *dir, const char *base,
                     const spool_meta_t *m, uint32_t kind, uint32_t flags,
                     const char *tag)
{
    char p[512];
    struct mps3_dfx_push q;
    static uint8_t buf[CHUNK_BYTES];

    int bfd = open(spool_path(dir, base, ".bin", p, sizeof(p)), O_RDONLY);
    if (bfd < 0) {
        fprintf(stderr, "mps3-swap: %s open %s: %s\n", tag, p, strerror(errno));
        return -errno;
    }

    memset(&q, 0, sizeof(q));
    q.kind      = kind;
    q.rm_slot   = m->rm_slot;
    q.static_id = m->static_id;
    q.rm_id     = m->rm_id;
    q.len_words = m->len_words;
    q.crc32     = m->crc32;
    q.flags     = flags;

    if (ioctl(fd, MPS3_DFX_IOC_PUSH_BEGIN, &q) != 0) {
        int e = errno;
        fprintf(stderr, "mps3-swap: %s PUSH_BEGIN: %s\n", tag, strerror(e));
        close(bfd);
        return -e;
    }

    uint64_t left = (uint64_t)m->len_words * 4u;
    while (left > 0) {
        ssize_t r = read(bfd, buf, left < CHUNK_BYTES ? (size_t)left : CHUNK_BYTES);
        if (r <= 0) {
            fprintf(stderr, "mps3-swap: %s short spool file (%llu left)\n",
                    tag, (unsigned long long)left);
            close(bfd);
            return -EIO;
        }
        ssize_t off = 0;
        while (off < r) {
            ssize_t w = write(fd, buf + off, (size_t)(r - off));
            if (w < 0) {
                if (errno == EINTR)
                    continue;
                int e = errno;
                fprintf(stderr, "mps3-swap: %s write: %s\n", tag, strerror(e));
                close(bfd);
                return -e;
            }
            off += w;
        }
        left -= (uint64_t)r;
    }
    close(bfd);

    if (ioctl(fd, MPS3_DFX_IOC_PUSH_END) != 0) {
        int e = errno;
        fprintf(stderr, "mps3-swap: %s PUSH_END: %s\n", tag, strerror(e));
        return -e;
    }
    return 0;
}

/* ---- the swap ------------------------------------------------------------ */

int mps3_swap_worker_run(const swap_worker_cfg_t *cfg, swap_outcome_t *out)
{
    spool_meta_t clr_meta, part_meta;
    struct mps3_dfx_status st_pre, st_post;
    struct mps3_dfx_result res;
    const char *clr_base;
    int fd, rc, begun = 0;

    memset(out, 0, sizeof(*out));

    /* 1. probe the device FIRST: a host/dev-less run fails fast and honest
     * (the parent still delivers the reply as a held response). */
    fd = open(cfg->dev_path, O_RDWR);
    if (fd < 0) {
        set_fail(out, "open", -errno);
        fprintf(stderr, "mps3-swap: open %s: %s\n", cfg->dev_path,
                strerror(errno));
        return -1;
    }

    /* 2. staged pair (immediate if pre-staged; else await, progress-armed). */
    if (await_pair(cfg) != 0) {
        set_fail(out, "await", -ETIMEDOUT);
        fprintf(stderr, "mps3-swap: no staged pair within %u ms (idle)\n",
                cfg->await_ms);
        goto out_close;
    }
    if (spool_meta_read(cfg->spool_dir, SPOOL_CLEARING, &clr_meta) != 0 ||
        spool_meta_read(cfg->spool_dir, SPOOL_PARTIAL, &part_meta) != 0) {
        set_fail(out, "meta", -EIO);
        goto out_close;
    }
    /* pushd validated static_id on the wire; re-check as belt-and-braces. */
    if (part_meta.static_id != cfg->static_id ||
        clr_meta.static_id != cfg->static_id) {
        set_fail(out, "staticid", -EINVAL);
        goto out_consume;
    }

    /* Outgoing clearing: resident cache if promoted, else first-swap
     * fallback to the incoming pair's clearing. */
    if (spool_staged(cfg->spool_dir, SPOOL_RESIDENT_CLR) &&
        spool_meta_read(cfg->spool_dir, SPOOL_RESIDENT_CLR, &clr_meta) == 0) {
        clr_base = SPOOL_RESIDENT_CLR;
    } else {
        (void)spool_meta_read(cfg->spool_dir, SPOOL_CLEARING, &clr_meta);
        clr_base = SPOOL_CLEARING;
    }

    /* 3. mock plumbing (test rigs only; ENOTTY on real hardware is fatal
     * only if -M was requested, which it must not be there). */
    if (cfg->mock) {
        struct mps3_dfx_mock_cfg mc;
        memset(&mc, 0, sizeof(mc));
        mc.next_rm_id = cfg->mock_force ? cfg->mock_force_rm : part_meta.rm_id;
        mc.status_lat = 2;
        mc.valid_lat  = 3;
        mc.cr_lat     = 1;
        mc.fifo_depth = 1024;
        if (ioctl(fd, MPS3_DFX_IOC_MOCK_SET, &mc) != 0 ||
            ioctl(fd, MPS3_DFX_IOC_MOCK_ARM_CAP) != 0) {
            set_fail(out, "mockcfg", -errno);
            goto out_consume;
        }
    }

    memset(&st_pre, 0, sizeof(st_pre));
    (void)ioctl(fd, MPS3_DFX_IOC_GET_STATUS, &st_pre);

    /* 4. SWAP_BEGIN: decouple + shutdown + rp reset, confirm-polled. */
    if (ioctl(fd, MPS3_DFX_IOC_SWAP_BEGIN) != 0) {
        set_fail(out, "begin", -errno);
        goto out_consume;
    }
    begun = 1;

    /* 5. outgoing clearing (staged protocol), then incoming partial
     * (STREAM_DIRECT: decouple containment substitutes for pre-validation;
     * the frame CRC still gates at PUSH_END). */
    rc = push_file(fd, cfg->spool_dir, clr_base, &clr_meta,
                   MPS3_DFX_KIND_CLEARING, 0, "clearing");
    if (rc != 0) {
        set_fail(out, "clearing", rc);
        goto out_abort;
    }
    rc = push_file(fd, cfg->spool_dir, SPOOL_PARTIAL, &part_meta,
                   MPS3_DFX_KIND_PARTIAL, MPS3_DFX_PUSH_F_STREAM_DIRECT,
                   "partial");
    if (rc != 0) {
        set_fail(out, "partial", rc);
        goto out_abort;
    }

    /* 6. finish: hostio4 hook -> release -> RM_ID verify (re-isolate on
     * mismatch) — all in-driver, bounded, fail-closed to parked. */
    memset(&res, 0, sizeof(res));
    if (ioctl(fd, MPS3_DFX_IOC_SWAP_FINISH, &res) != 0) {
        set_fail(out, "finish", -errno);
        goto out_abort;
    }
    out->ok       = res.ok;
    out->verified = res.verified;
    out->rm_id    = res.rm_id;
    out->err      = res.err;
    if (!res.ok)
        strncpy(out->stage, "verify", sizeof(out->stage) - 1);

capture:
    /* 7. capture — EOS/SR/progress are observable post-swap by design. */
    memset(&st_post, 0, sizeof(st_post));
    if (ioctl(fd, MPS3_DFX_IOC_GET_STATUS, &st_post) == 0) {
        out->eos_status    = st_post.eos_status;
        out->sr_last       = st_post.sr_last;
        out->hostio4_calls = st_post.hostio4_calls;
        out->dfxctl_status = st_post.dfxctl_status;
        out->icap_delta =
            (((uint64_t)st_post.icap_bytes_hi << 32) | st_post.icap_bytes_lo) -
            (((uint64_t)st_pre.icap_bytes_hi << 32) | st_pre.icap_bytes_lo);
    }
    if (cfg->mock) {
        struct mps3_dfx_mock_state ms;
        if (ioctl(fd, MPS3_DFX_IOC_MOCK_GET, &ms) == 0) {
            out->mock_valid      = 1;
            out->mock_words      = ms.words_total;
            out->mock_stream_crc = ms.stream_crc;
            out->mock_saw_sync   = ms.saw_sync;
            out->mock_saw_desync = ms.saw_desync;
        }
    }

    /* 8. one swap consumes one pair (v0.7); success also promotes the
     * incoming clearing to the resident cache (§4 step 9). */
    if (out->ok && out->verified) {
        spool_unlink_pair(cfg->spool_dir, SPOOL_RESIDENT_CLR);
        if (spool_rename_pair(cfg->spool_dir, SPOOL_CLEARING,
                              SPOOL_RESIDENT_CLR) != 0)
            fprintf(stderr, "mps3-swap: WARNING: clearing promote failed\n");
        spool_unlink_pair(cfg->spool_dir, SPOOL_PARTIAL);
    } else {
        spool_unlink_pair(cfg->spool_dir, SPOOL_CLEARING);
        spool_unlink_pair(cfg->spool_dir, SPOOL_PARTIAL);
    }

    close(fd);
    fprintf(stderr,
            "mps3-swap: settled ok=%u verified=%u rm_id=0x%08x err=%d stage=%s "
            "eos=%u sr=0x%08x hostio4=%u icap_delta=%llu dfxctl=0x%x parked=%d\n",
            out->ok, out->verified, out->rm_id, out->err,
            out->ok ? "done" : out->stage,
            out->eos_status, out->sr_last, out->hostio4_calls,
            (unsigned long long)out->icap_delta, out->dfxctl_status,
            (out->dfxctl_status &
             (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET)) ==
                (DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET));
    if (out->mock_valid)
        fprintf(stderr,
                "mps3-swap: mock words=%u stream_crc=0x%08x sync=%u desync=%u\n",
                out->mock_words, out->mock_stream_crc,
                out->mock_saw_sync, out->mock_saw_desync);
    return (out->ok && out->verified) ? 0 : -1;

out_abort:
    /* Park explicitly; harmless if the driver already parked on the error
     * (ABORT from a parked/idle engine is a no-op-shaped repark). */
    (void)ioctl(fd, MPS3_DFX_IOC_ABORT);
    goto capture;

out_consume:
    if (begun)
        (void)ioctl(fd, MPS3_DFX_IOC_ABORT);
    /* pre-BEGIN validation failures consume nothing streamed, but a
     * static_id/mock-cfg reject still burns the pair (it can never load). */
    spool_unlink_pair(cfg->spool_dir, SPOOL_CLEARING);
    spool_unlink_pair(cfg->spool_dir, SPOOL_PARTIAL);
    goto capture_min;

out_close:
capture_min:
    memset(&st_post, 0, sizeof(st_post));
    if (ioctl(fd, MPS3_DFX_IOC_GET_STATUS, &st_post) == 0)
        out->dfxctl_status = st_post.dfxctl_status;
    close(fd);
    fprintf(stderr, "mps3-swap: settled ok=0 err=%d stage=%s (no stream)\n",
            out->err, out->stage);
    return -1;
}
