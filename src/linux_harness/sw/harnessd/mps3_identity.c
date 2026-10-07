/*
 * mps3_identity.c — mps3-identity: the board identity's boot-time RESOLVER and
 * its board CLI (lane IDENT; identity_core.h is the model).
 *
 *   mps3-identity resolve     S13mps3identity, early in rcS (after /persist, before
 *                             S41mps3net): override > stage0 block > image default,
 *                             per field -> /run/mps3/identity (+ one summary line)
 *   mps3-identity get [--json]
 *                             this boot's identity, its sources, the stage0 bake,
 *                             the override in force and what the next boot changes
 *                             (--json = the 6900 `identity` reply's body)
 *   mps3-identity set label=MPS3-02 ip=192.168.10.102/24 mac=02:00:00:00:02:fe \
 *                     hostname=mps3-02
 *                             edit /persist/etc/mps3/identity (validated, atomic);
 *                             applies at the next boot. `key=` drops that key.
 *   mps3-identity clear       remove the override
 *
 * The stage0 status block is read through the LMB-tail UIO window EXACTLY as
 * harnessd reads it: hal_uio.c is linked unchanged and hal_backend_window() maps
 * 0x1FE00 (HARNESSD_CONTRACT §2). No window (QEMU, a DTS without the UIO estate)
 * = no stage0 source; the resolver still writes a complete identity.
 *
 * Test seams (host tests, tests/test_identity_tool.py): --status-file (256 raw
 * bytes instead of UIO), --uio-sysfs/--uio-devdir, --override, --run,
 * --persist-state. Exit codes: 0 ok, 2 invalid input, 3 no persistent /persist,
 * 4 I/O. `resolve` exits 0 whenever it wrote the run file: a boot never stops here.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "hal.h"
#include "harnessd.h"
#include "identity_core.h"
#include "../../../linux_soc/hw/fw_stage0/stage0_status.h"

/* hal_uio.c's two outward calls: its self-check names no blocks here (only the
 * LMB tail is wanted), and its log goes to stderr. */
const hal_block_t *hal_expected_blocks(unsigned *n)
{
    *n = 0;
    return 0;
}

static int s_quiet;

void harnessd_log(const char *fmt, ...)
{
    if (s_quiet) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}

static void id_log(void *ctx, const char *msg)
{
    (void)ctx;
    fprintf(stderr, "mps3-identity: %s\n", msg);
}

static struct {
    const char *override;
    const char *run;
    const char *persist_state;
    const char *status_file;
    const char *uio_sysfs, *uio_devdir;
    int         json;
} O = { MPS3_ID_OVERRIDE_PATH, MPS3_ID_RUN_PATH, "/run/mps3/persist.state", 0, 0, 0, 0 };

static uint32_t s_blk[S0_STATUS_BYTES / 4u];

/* The block: the test file, or the LMB-tail UIO window (a COPY, read once). */
static const volatile uint32_t *read_stage0(void)
{
    if (O.status_file) {
        if (strcmp(O.status_file, "none") == 0) {
            return 0;
        }
        FILE *f = fopen(O.status_file, "rb");
        if (!f) {
            return 0;
        }
        size_t n = fread(s_blk, 1, sizeof(s_blk), f);
        fclose(f);
        return n == sizeof(s_blk) ? s_blk : 0;
    }
    hal_opts_t ho;
    memset(&ho, 0, sizeof(ho));
    ho.uio_sysfs = O.uio_sysfs;
    ho.uio_devdir = O.uio_devdir;
    s_quiet = 1;                          /* the per-window discovery log is noise here */
    int rc = hal_backend_open(&ho);
    s_quiet = 0;
    if (rc != 0) {
        return 0;
    }
    volatile uint32_t *w = hal_backend_window(HARNESSD_S0_STATUS_ADDR, S0_STATUS_BYTES);
    if (!w) {
        return 0;
    }
    for (unsigned i = 0; i < S0_STATUS_BYTES / 4u; i++) {
        s_blk[i] = w[i];
    }
    return s_blk;
}

static int load_running(mps3_identity_t *out)
{
    char buf[MPS3_ID_FILE_MAX + 1u];
    int n = mps3_id_read_file(O.run, buf, sizeof(buf));
    return (n >= 0 && mps3_id_parse_run(buf, (size_t)n, out) == 0) ? 0 : -1;
}

static int cmd_resolve(void)
{
    int persist = mps3_id_persist_ok(O.persist_state);
    mps3_id_fields_t ovr, s0;
    int have_ovr = mps3_id_load_override(O.override, persist, &ovr, id_log, 0);
    const volatile uint32_t *blk = read_stage0();
    int s0st = mps3_id_from_stage0(blk, &s0, id_log, 0);
    mps3_identity_t id;
    mps3_id_resolve(have_ovr ? &ovr : 0, s0st == MPS3_ID_S0_VALID ? &s0 : 0, &id);

    char text[1024];
    int len = mps3_id_render_run(&id, s0st, persist, text, sizeof(text));
    if (len < 0) {
        fprintf(stderr, "mps3-identity: render failed\n");
        return 4;
    }
    int rc = mps3_id_write_atomic(O.run, text, (size_t)len, 0644);
    if (rc) {
        fprintf(stderr, "mps3-identity: write %s: %s\n", O.run, strerror(-rc));
        return 4;
    }
    char ip[24], mac[18];
    mps3_id_fmt_cidr(id.v.ip, id.v.prefix, ip, sizeof(ip));
    mps3_id_fmt_mac_colon(id.v.mac, mac);
    printf("mps3-identity: %s (%s) %s %s -- label %s, hostname %s, ip %s, mac %s; "
           "stage0 block %s, override %s\n",
           id.v.label, id.v.hostname, ip, mac,
           mps3_id_src_name(id.src[MPS3_ID_LABEL]), mps3_id_src_name(id.src[MPS3_ID_HOSTNAME]),
           mps3_id_src_name(id.src[MPS3_ID_IP]), mps3_id_src_name(id.src[MPS3_ID_MAC]),
           mps3_id_s0_state_name(s0st),
           !persist ? "none (no persistent /persist)" : have_ovr ? "in force" : "none");
    return 0;
}

static int cmd_get(void)
{
    mps3_identity_t run;
    int have_run = load_running(&run) == 0;
    if (!have_run) {
        mps3_id_resolve(0, 0, &run);      /* what harnessd falls back to */
    }
    mps3_id_status_t st;
    mps3_id_gather(O.override, mps3_id_persist_ok(O.persist_state), read_stage0(), &run, &st,
                   id_log, 0);
    char body[1024];
    if (mps3_id_json_status(&st, body, sizeof(body)) < 0) {
        fprintf(stderr, "mps3-identity: render failed\n");
        return 4;
    }
    if (O.json) {
        printf("{%s}\n", body);
        return 0;
    }
    char ip[24], mac[18];
    mps3_id_fmt_cidr(run.v.ip, run.v.prefix, ip, sizeof(ip));
    mps3_id_fmt_mac_colon(run.v.mac, mac);
    printf("label     %-20s (%s)\nhostname  %-20s (%s)\nip        %-20s (%s)\nmac       %-20s (%s)\n",
           run.v.label, mps3_id_src_name(run.src[MPS3_ID_LABEL]),
           run.v.hostname, mps3_id_src_name(run.src[MPS3_ID_HOSTNAME]),
           ip, mps3_id_src_name(run.src[MPS3_ID_IP]), mac, mps3_id_src_name(run.src[MPS3_ID_MAC]));
    if (!have_run) {
        printf("(no %s: the boot resolver did not run -- these are the image defaults)\n", O.run);
    }
    printf("persist   %s\n", st.persist ? "yes (card)" : "no -- `set` refused");
    printf("json      {%s}\n", body);
    return 0;
}

static int cmd_set(int argc, char **argv, int clear)
{
    const char *keys[16], *vals[16];
    char kv[16][96];
    int n = 0;
    if (clear && argc > 0) {
        fprintf(stderr, "mps3-identity: clear takes no arguments\n");
        return 2;
    }
    if (!clear && argc == 0) {
        fprintf(stderr, "mps3-identity: set needs key=value (label, hostname, ip, mac)\n");
        return 2;
    }
    for (int i = 0; i < argc; i++) {
        if (n >= 16) {
            fprintf(stderr, "mps3-identity: too many arguments\n");
            return 2;
        }
        snprintf(kv[n], sizeof(kv[n]), "%s", argv[i]);
        char *eq = strchr(kv[n], '=');
        if (!eq || strlen(argv[i]) >= sizeof(kv[n])) {
            fprintf(stderr, "mps3-identity: '%s' is not key=value\n", argv[i]);
            return 2;
        }
        *eq = '\0';
        keys[n] = kv[n];
        vals[n] = eq + 1;
        n++;
    }
    const char *field = 0, *why = 0;
    int rc = mps3_id_do_set(O.override, mps3_id_persist_ok(O.persist_state), clear, keys, vals,
                            n, &field, &why);
    switch (rc) {
    case MPS3_ID_OK:
        break;
    case MPS3_ID_EINVALID:
        fprintf(stderr, "mps3-identity: invalid %s: %s\n", field, why);
        return 2;
    case MPS3_ID_ENOPERSIST:
        fprintf(stderr, "mps3-identity: %s\n", why);
        return 3;
    default:
        fprintf(stderr, "mps3-identity: %s\n", why ? why : "I/O error");
        return 4;
    }
    /* what now differs from this boot's identity */
    mps3_identity_t run;
    if (load_running(&run) != 0) {
        mps3_id_resolve(0, 0, &run);
    }
    mps3_id_status_t st;
    mps3_id_gather(O.override, 1, read_stage0(), &run, &st, id_log, 0);
    char pend[256];
    if (mps3_id_json_pending(&st.next, &run, pend, sizeof(pend)) < 0) {
        snprintf(pend, sizeof(pend), "?");
    }
    printf("mps3-identity: %s %s; pending %s (applies at the next boot)\n",
           clear ? "override removed:" : "override written:", O.override, pend);
    return 0;
}

static void usage(FILE *f)
{
    fprintf(f,
        "usage: mps3-identity [options] resolve | get [--json] | set key=value... | clear\n"
        "  keys: label (1-19 of A-Z 0-9 -), hostname (RFC 1123), ip (a.b.c.d/nn, nn 8-30;\n"
        "        no /nn = /24), mac (12 hex, bare or :-separated; unicast); key= drops it\n"
        "  precedence per field: %s > the stage0 bake > the image default\n"
        "options: --override P [%s]  --run P [%s]\n"
        "         --persist-state P [/run/mps3/persist.state]  --status-file P|none\n"
        "         --uio-sysfs D  --uio-devdir D  --json\n",
        MPS3_ID_OVERRIDE_PATH, MPS3_ID_OVERRIDE_PATH, MPS3_ID_RUN_PATH);
}

int main(int argc, char **argv)
{
    enum { O_OVR = 1000, O_RUN, O_PST, O_SF, O_SYSFS, O_DEVDIR, O_JSON, O_HELP };
    static const struct option lo[] = {
        { "override", 1, 0, O_OVR }, { "run", 1, 0, O_RUN }, { "persist-state", 1, 0, O_PST },
        { "status-file", 1, 0, O_SF }, { "uio-sysfs", 1, 0, O_SYSFS },
        { "uio-devdir", 1, 0, O_DEVDIR }, { "json", 0, 0, O_JSON }, { "help", 0, 0, O_HELP },
        { 0, 0, 0, 0 },
    };
    int c;
    while ((c = getopt_long(argc, argv, "+h", lo, 0)) != -1) {
        switch (c) {
        case O_OVR:    O.override = optarg; break;
        case O_RUN:    O.run = optarg; break;
        case O_PST:    O.persist_state = optarg; break;
        case O_SF:     O.status_file = optarg; break;
        case O_SYSFS:  O.uio_sysfs = optarg; break;
        case O_DEVDIR: O.uio_devdir = optarg; break;
        case O_JSON:   O.json = 1; break;
        case 'h': case O_HELP: usage(stdout); return 0;
        default:       usage(stderr); return 2;
        }
    }
    if (optind >= argc) {
        usage(stderr);
        return 2;
    }
    const char *cmd = argv[optind++];
    if (strcmp(cmd, "get") == 0 && optind < argc && strcmp(argv[optind], "--json") == 0) {
        O.json = 1;
        optind++;
    }
    if (strcmp(cmd, "resolve") == 0 && optind == argc) return cmd_resolve();
    if (strcmp(cmd, "get") == 0 && optind == argc)     return cmd_get();
    if (strcmp(cmd, "set") == 0)   return cmd_set(argc - optind, argv + optind, 0);
    if (strcmp(cmd, "clear") == 0) return cmd_set(argc - optind, argv + optind, 1);
    usage(stderr);
    return 2;
}
