/*
 * mps3-debug -- the on-board GDB server launcher of the MPS3 Linux harness
 * (MVP, 6 Oct 2026; the contract is HM's "mps3-debug/1").
 *
 *   mps3-debug up [--rm auto|NAME] [--json]   start OpenOCD on the board (idempotent)
 *   mps3-debug down [--json]                  stop it
 *   mps3-debug status [--json]                what runs
 *   mps3-debug version [--json]               the launcher, the OpenOCD it runs, the designs
 *
 * Run as root over the claim's SSH (the board's own key-only login). OpenOCD runs as
 * the unprivileged `openocd` user, at nice 10, and binds GDB (3333, 3334 for cpu1),
 * telnet (4444) and Tcl (6666) to 127.0.0.1 ONLY: a client reaches them through the
 * same SSH (ssh -L). OpenOCD's Tcl has `exec`, so those ports are a shell on the
 * board; they never face the network.
 *
 * NO HARNESSD CHANGE. OpenOCD's adapter is remote_bitbang to 127.0.0.1:6921, the
 * harness's jtag_server, exactly as a host OpenOCD dials it; 6921 serves one client
 * at a time, which is the exclusivity: a host OpenOCD holding it makes `up` report
 * busy (exit 4), with the holder's address.
 *
 * WHICH DESIGN. `--rm auto` asks the harness's own UDP identify on 127.0.0.1:6899
 * (net-protocol.md "Identify": not single-client, answers while 6900 is held) for
 * the loaded rm_id, and maps its design number (rm_id & 0xFFFF) through the table in
 * <share>/designs.conf to the configs. `--rm NAME` names the recipe (the Harness
 * Manager knows the design). Never 6900: it is single-client and HM holds it.
 *
 * THE SESSION. `up` forks a monitor (its own session, parented to init) that holds
 * <run>/session.lock for the session's life, runs OpenOCD, and writes <run>/state.
 * It stops the session when: `down` asks (SIGTERM); OpenOCD exits (failed); no GDB
 * client for MPS3_DEBUG_IDLE_S (7200 s); or identify reports another rm_id (a swap
 * rebuilt the DUT's debug port: state "down", reason "target_lost", error.code
 * "openocd_exit" with a message starting "swap:"). One instance: <run>/lock serialises
 * up/down, and `up` with a live session answers "already":true.
 *
 * OUTPUT: ONE JSON object on stdout, always (--json is accepted and implied).
 * Exit codes: 0 ok, 2 usage, 4 busy, 6 failed, 12 no OpenOCD binary, 13 no debug
 * port in this design, 14 no recipe for this rm_id (pass --rm NAME).
 *
 * Paths and ports come from the environment only so the host tests can run it
 * (tests/test_mps3_debug.py): MPS3_DEBUG_{OPENOCD,SHARE,RUN,USER,IDENTIFY_PORT,
 * RBB_PORT,GDB_PORT,TELNET_PORT,TCL_PORT,IDLE_S,WAIT_S,START_S,SWAP_POLL_MS,LOCK_WAIT_MS}.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <time.h>
#include <unistd.h>

#define SCHEMA           "mps3-debug/1"
#ifndef MPS3_DEBUG_VERSION
#define MPS3_DEBUG_VERSION "1.0.0"
#endif

enum { EX_OK = 0, EX_USAGE = 2, EX_BUSY = 4, EX_FAILED = 6, EX_NO_OPENOCD = 12,
       EX_NO_DAP = 13, EX_NO_CFG = 14 };

#define MAX_RECIPES   32
#define MAX_CFGS      4
#define MAX_CORES     2
#define TAIL_LINES    20
#define LINE_MAX_     200

/* ==========================================================================
 * Configuration (environment; the image uses the defaults)
 * ========================================================================== */
static struct {
    const char *openocd, *share, *run, *user;
    unsigned id_port, rbb_port, gdb_port, telnet_port, tcl_port;
    unsigned idle_s, wait_s, start_s, swap_poll_ms, lock_wait_ms;
} C;

static const char *env_s(const char *k, const char *d)
{
    const char *v = getenv(k);
    return (v && *v) ? v : d;
}

static unsigned env_u(const char *k, unsigned d)
{
    const char *v = getenv(k);
    if (!v || !*v) {
        return d;
    }
    char *end = 0;
    unsigned long x = strtoul(v, &end, 0);
    return (end && *end == '\0' && x <= 0xFFFFFFFFul) ? (unsigned)x : d;
}

static void config_load(void)
{
    C.openocd      = env_s("MPS3_DEBUG_OPENOCD", "/usr/bin/openocd");
    C.share        = env_s("MPS3_DEBUG_SHARE", "/usr/share/mps3/openocd");
    C.run          = env_s("MPS3_DEBUG_RUN", "/run/mps3-debug");
    C.user         = getenv("MPS3_DEBUG_USER") ? getenv("MPS3_DEBUG_USER") : "openocd";
    C.id_port      = env_u("MPS3_DEBUG_IDENTIFY_PORT", 6899u);
    C.rbb_port     = env_u("MPS3_DEBUG_RBB_PORT", 6921u);
    C.gdb_port     = env_u("MPS3_DEBUG_GDB_PORT", 3333u);
    C.telnet_port  = env_u("MPS3_DEBUG_TELNET_PORT", 4444u);
    C.tcl_port     = env_u("MPS3_DEBUG_TCL_PORT", 6666u);
    C.idle_s       = env_u("MPS3_DEBUG_IDLE_S", 7200u);
    C.wait_s       = env_u("MPS3_DEBUG_WAIT_S", 120u);
    C.start_s      = env_u("MPS3_DEBUG_START_S", 180u);
    C.swap_poll_ms = env_u("MPS3_DEBUG_SWAP_POLL_MS", 2000u);
    C.lock_wait_ms = env_u("MPS3_DEBUG_LOCK_WAIT_MS", 10000u);
}

static void run_path(char *out, size_t cap, const char *name)
{
    snprintf(out, cap, "%s/%s", C.run, name);
}

static uint64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

static void sleep_ms(unsigned ms)
{
    struct timespec ts = { (time_t)(ms / 1000u), (long)(ms % 1000u) * 1000000L };
    while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {
    }
}

/* ==========================================================================
 * The recipes: <share>/designs.conf, one design per line:
 *   design=0x0001 name=nanosoc [gdb=1] cfg=a.cfg,b.tcl [attach=tgt:proc,...]
 *   design=0x0002 name=eth_ss dap=no
 * ========================================================================== */
typedef struct {
    unsigned design;
    char     name[32];
    int      dap;
    unsigned ngdb;
    unsigned ncfg;
    char     cfg[MAX_CFGS][64];
    unsigned natt;
    char     att_target[MAX_CORES][48];
    char     att_proc[MAX_CORES][48];
} recipe_t;

static int word_ok(const char *s, size_t max)
{
    size_t n = strlen(s);
    if (n == 0 || n > max) {
        return 0;
    }
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (!(islower(c) || isdigit(c) || c == '_' || c == '.' || c == '-')) {
            return 0;
        }
    }
    return 1;
}

/* 0 = a recipe, 1 = blank/comment, -1 = malformed */
static int parse_recipe(char *line, recipe_t *r)
{
    char *save = 0;
    int have_design = 0;
    memset(r, 0, sizeof(*r));
    r->dap = 1;
    r->ngdb = 1u;
    char *hash = strchr(line, '#');
    if (hash) {
        *hash = '\0';
    }
    for (char *tok = strtok_r(line, " \t\r\n", &save); tok; tok = strtok_r(0, " \t\r\n", &save)) {
        char *eq = strchr(tok, '=');
        if (!eq) {
            return -1;
        }
        *eq = '\0';
        const char *k = tok;
        char *v = eq + 1;
        if (strcmp(k, "design") == 0) {
            char *end = 0;
            unsigned long d = strtoul(v, &end, 0);
            if (!end || *end || d > 0xFFFFul) {
                return -1;
            }
            r->design = (unsigned)d;
            have_design = 1;
        } else if (strcmp(k, "name") == 0) {
            if (!word_ok(v, sizeof(r->name) - 1u)) {
                return -1;
            }
            snprintf(r->name, sizeof(r->name), "%s", v);
        } else if (strcmp(k, "dap") == 0) {
            r->dap = strcmp(v, "no") != 0;
        } else if (strcmp(k, "gdb") == 0) {
            r->ngdb = (unsigned)strtoul(v, 0, 0);
            if (r->ngdb < 1u || r->ngdb > MAX_CORES) {
                return -1;
            }
        } else if (strcmp(k, "cfg") == 0) {
            char *s2 = 0;
            for (char *c = strtok_r(v, ",", &s2); c; c = strtok_r(0, ",", &s2)) {
                if (r->ncfg >= MAX_CFGS || !word_ok(c, sizeof(r->cfg[0]) - 1u)) {
                    return -1;
                }
                snprintf(r->cfg[r->ncfg++], sizeof(r->cfg[0]), "%s", c);
            }
        } else if (strcmp(k, "attach") == 0) {
            char *s2 = 0;
            for (char *c = strtok_r(v, ",", &s2); c; c = strtok_r(0, ",", &s2)) {
                char *colon = strchr(c, ':');
                if (!colon || r->natt >= MAX_CORES) {
                    return -1;
                }
                *colon = '\0';
                if (!word_ok(c, sizeof(r->att_target[0]) - 1u) ||
                    !word_ok(colon + 1, sizeof(r->att_proc[0]) - 1u)) {
                    return -1;
                }
                snprintf(r->att_target[r->natt], sizeof(r->att_target[0]), "%s", c);
                snprintf(r->att_proc[r->natt], sizeof(r->att_proc[0]), "%s", colon + 1);
                r->natt++;
            }
        } else {
            return -1;
        }
    }
    if (!have_design && !r->name[0]) {
        return 1;
    }
    if (!have_design || !r->name[0] || (r->dap && r->ncfg == 0u)) {
        return -1;
    }
    return 0;
}

/* The table; -1 when designs.conf cannot be read. Malformed lines are skipped
 * (and reported by `version`). */
static int load_recipes(recipe_t *out, int max, int *bad)
{
    char path[PATH_MAX];
    snprintf(path, sizeof(path), "%s/designs.conf", C.share);
    FILE *f = fopen(path, "r");
    if (!f) {
        return -1;
    }
    char line[512];
    int n = 0;
    if (bad) {
        *bad = 0;
    }
    while (fgets(line, sizeof(line), f) && n < max) {
        int p = parse_recipe(line, &out[n]);
        if (p == 0) {
            n++;
        } else if (p < 0 && bad) {
            (*bad)++;
        }
    }
    fclose(f);
    return n;
}

/* ==========================================================================
 * The harness's identify (UDP 6899): the loaded rm_id, read-only
 * ========================================================================== */
static int json_str_field(const char *buf, const char *key, char *out, size_t cap)
{
    char pat[48];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char *p = strstr(buf, pat);
    if (!p) {
        return -1;
    }
    p += strlen(pat);
    size_t n = 0;
    while (p[n] && p[n] != '"' && n + 1u < cap) {
        out[n] = p[n];
        n++;
    }
    if (p[n] != '"') {
        return -1;
    }
    out[n] = '\0';
    return 0;
}

/* 0 = *rm set from a reply whose nonce matched; -1 = no answer. */
static int identify_rm_id(uint32_t *rm, unsigned tries, unsigned wait_ms)
{
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        return -1;
    }
    struct sockaddr_in to;
    memset(&to, 0, sizeof(to));
    to.sin_family = AF_INET;
    to.sin_port = htons((uint16_t)C.id_port);
    to.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int rc = -1;
    for (unsigned t = 0; t < tries && rc != 0; t++) {
        char nonce[17], req[96];
        snprintf(nonce, sizeof(nonce), "%08x%08x", (unsigned)getpid(),
                 (unsigned)(now_ms() * 2654435761u + t));
        int n = snprintf(req, sizeof(req), "{\"op\":\"identify\",\"v\":1,\"nonce\":\"%s\"}", nonce);
        if (sendto(fd, req, (size_t)n, 0, (struct sockaddr *)&to, sizeof(to)) != n) {
            sleep_ms(wait_ms);
            continue;
        }
        uint64_t deadline = now_ms() + wait_ms;
        while (rc != 0) {
            uint64_t now = now_ms();
            if (now >= deadline) {
                break;
            }
            struct pollfd pfd = { fd, POLLIN, 0 };
            if (poll(&pfd, 1, (int)(deadline - now)) <= 0) {
                break;
            }
            char buf[1400];
            ssize_t got = recv(fd, buf, sizeof(buf) - 1u, 0);
            if (got <= 0) {
                continue;
            }
            buf[got] = '\0';
            char nn[40], rid[16];
            if (json_str_field(buf, "nonce", nn, sizeof(nn)) == 0 && strcmp(nn, nonce) == 0 &&
                json_str_field(buf, "rm_id", rid, sizeof(rid)) == 0) {
                char *end = 0;
                unsigned long v = strtoul(rid, &end, 16);
                if (end && *end == '\0' && strncmp(rid, "0x", 2) == 0) {
                    *rm = (uint32_t)v;
                    rc = 0;
                }
            }
        }
    }
    close(fd);
    return rc;
}

/* ==========================================================================
 * Who holds 6921? (/proc/net/tcp: an ESTABLISHED socket whose LOCAL port is
 * 6921 is the harness's end of a client's connection; its remote is the client.)
 * ========================================================================== */
static int rbb_holder(char *peer, size_t cap)
{
    FILE *f = fopen("/proc/net/tcp", "r");
    if (!f) {
        return 0;
    }
    char line[256];
    int found = 0;
    if (!fgets(line, sizeof(line), f)) {
        fclose(f);
        return 0;
    }
    while (!found && fgets(line, sizeof(line), f)) {
        unsigned la, lp, ra, rp, st;
        if (sscanf(line, " %*d: %8X:%4X %8X:%4X %2X", &la, &lp, &ra, &rp, &st) == 5 &&
            lp == C.rbb_port && st == 0x01u) {
            struct in_addr a;
            a.s_addr = (in_addr_t)ra;       /* /proc prints the raw be32 as a host word */
            snprintf(peer, cap, "%s:%u", inet_ntoa(a), rp);
            found = 1;
        }
    }
    fclose(f);
    return found;
}

/* ==========================================================================
 * The state file (<run>/state): key=value lines, written by tmp + rename
 * ========================================================================== */
typedef struct {
    char state[16];            /* down | starting | up | failed */
    char reason[24];           /* closed | idle | target_lost | openocd_exit | ... */
    char design[32];
    char rm_id[16];            /* "0x01000001" or "" (unknown) */
    char cfg[MAX_CFGS * 64 + 8];
    char started_at[32];
    char idcode[16];
    long pid, mpid;
    unsigned ngdb;
    int  exitcode;
    char err_code[24], err_msg[200], err_hint[200];
    char busy_by[32], busy_peer[48];
} st_t;

static void st_clear(st_t *s)
{
    memset(s, 0, sizeof(*s));
    snprintf(s->state, sizeof(s->state), "down");
}

static int st_read(st_t *s)
{
    char p[PATH_MAX];
    st_clear(s);
    run_path(p, sizeof(p), "state");
    FILE *f = fopen(p, "r");
    if (!f) {
        return -1;
    }
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\n")] = '\0';
        char *eq = strchr(line, '=');
        if (!eq) {
            continue;
        }
        *eq = '\0';
        const char *k = line, *v = eq + 1;
#define S(key, field) else if (strcmp(k, key) == 0) snprintf(s->field, sizeof(s->field), "%s", v)
        if (0) {
        }
        S("state", state); S("reason", reason); S("design", design); S("rm_id", rm_id);
        S("cfg", cfg); S("started_at", started_at); S("idcode", idcode);
        S("err_code", err_code); S("err_msg", err_msg); S("err_hint", err_hint);
        S("busy_by", busy_by); S("busy_peer", busy_peer);
        else if (strcmp(k, "pid") == 0) s->pid = strtol(v, 0, 10);
        else if (strcmp(k, "mpid") == 0) s->mpid = strtol(v, 0, 10);
        else if (strcmp(k, "ngdb") == 0) s->ngdb = (unsigned)strtoul(v, 0, 10);
        else if (strcmp(k, "exit") == 0) s->exitcode = (int)strtol(v, 0, 10);
#undef S
    }
    fclose(f);
    return 0;
}

static void st_write(const st_t *s)
{
    char p[PATH_MAX], t[PATH_MAX + 32];
    run_path(p, sizeof(p), "state");
    snprintf(t, sizeof(t), "%s.tmp.%d", p, (int)getpid());
    FILE *f = fopen(t, "w");
    if (!f) {
        return;
    }
    fprintf(f, "state=%s\nreason=%s\ndesign=%s\nrm_id=%s\ncfg=%s\nstarted_at=%s\nidcode=%s\n"
               "pid=%ld\nmpid=%ld\nngdb=%u\nexit=%d\nerr_code=%s\nerr_msg=%s\nerr_hint=%s\n"
               "busy_by=%s\nbusy_peer=%s\n",
            s->state, s->reason, s->design, s->rm_id, s->cfg, s->started_at, s->idcode,
            s->pid, s->mpid, s->ngdb, s->exitcode, s->err_code, s->err_msg, s->err_hint,
            s->busy_by, s->busy_peer);
    fflush(f);
    (void)fsync(fileno(f));
    fclose(f);
    (void)rename(t, p);
}

static void st_error(st_t *s, const char *code, const char *hint, const char *fmt, ...)
    __attribute__((format(printf, 4, 5)));
static void st_error(st_t *s, const char *code, const char *hint, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    snprintf(s->err_code, sizeof(s->err_code), "%s", code);
    vsnprintf(s->err_msg, sizeof(s->err_msg), fmt, ap);
    snprintf(s->err_hint, sizeof(s->err_hint), "%s", hint ? hint : "");
    va_end(ap);
    for (char *c = s->err_msg; *c; c++) {
        if (*c == '\n') {
            *c = ' ';
        }
    }
}

/* ==========================================================================
 * Locks
 * ========================================================================== */
static int lock_file(const char *name, int nonblock_only, unsigned wait_ms)
{
    char p[PATH_MAX];
    run_path(p, sizeof(p), name);
    int fd = open(p, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0) {
        return -1;
    }
    uint64_t deadline = now_ms() + wait_ms;
    for (;;) {
        if (flock(fd, LOCK_EX | LOCK_NB) == 0) {
            return fd;
        }
        if (errno != EWOULDBLOCK || nonblock_only || now_ms() >= deadline) {
            close(fd);
            return -2;
        }
        sleep_ms(50);
    }
}

/* Is a monitor holding the session? (probe: try the lock, give it back) */
static int session_alive(void)
{
    int fd = lock_file("session.lock", 1, 0);
    if (fd >= 0) {
        close(fd);
        return 0;
    }
    return fd == -2;
}

/* ==========================================================================
 * JSON out
 * ========================================================================== */
static void jstr(FILE *o, const char *s)
{
    fputc('"', o);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') {
            fputc('\\', o);
            fputc(c, o);
        } else if (c < 0x20u || c >= 0x7Fu) {
            fprintf(o, "\\u%04x", c);
        } else {
            fputc(c, o);
        }
    }
    fputc('"', o);
}

static void jstr_or_null(FILE *o, const char *s)
{
    if (s && *s) {
        jstr(o, s);
    } else {
        fputs("null", o);
    }
}

static int read_tail(char lines[TAIL_LINES][LINE_MAX_])
{
    char p[PATH_MAX];
    run_path(p, sizeof(p), "openocd.log");
    FILE *f = fopen(p, "r");
    if (!f) {
        return 0;
    }
    if (fseek(f, 0, SEEK_END) == 0) {
        long sz = ftell(f);
        (void)fseek(f, sz > 65536 ? sz - 65536 : 0, SEEK_SET);
        if (sz > 65536) {
            char skip[LINE_MAX_ * 4];
            if (!fgets(skip, sizeof(skip), f)) {
                fclose(f);
                return 0;
            }
        }
    }
    int n = 0;
    char buf[1024];
    while (fgets(buf, sizeof(buf), f)) {
        buf[strcspn(buf, "\r\n")] = '\0';
        if (!buf[0]) {
            continue;
        }
        if (n == TAIL_LINES) {
            memmove(lines[0], lines[1], sizeof(lines[0]) * (TAIL_LINES - 1));
            n--;
        }
        size_t len = strnlen(buf, LINE_MAX_ - 1u);   /* a long line is cut, never overrun */
        memcpy(lines[n], buf, len);
        lines[n++][len] = '\0';
    }
    fclose(f);
    return n;
}

static void openocd_version(char *out, size_t cap)
{
    char p[PATH_MAX];
    snprintf(p, sizeof(p), "%s/VERSION", C.share);
    FILE *f = fopen(p, "r");
    out[0] = '\0';
    if (f) {
        if (fgets(out, (int)cap, f)) {
            out[strcspn(out, "\r\n")] = '\0';
        }
        fclose(f);
    }
    if (!out[0]) {
        snprintf(out, cap, "unknown");
    }
}

static void emit(const st_t *s, int already)
{
    FILE *o = stdout;
    char ver[128];
    openocd_version(ver, sizeof(ver));
    int live = strcmp(s->state, "up") == 0 || strcmp(s->state, "starting") == 0;
    fprintf(o, "{\"schema\":\"%s\",\"state\":", SCHEMA);
    jstr(o, s->state);
    fprintf(o, ",\"already\":%s,\"rm_id\":", already ? "true" : "false");
    jstr_or_null(o, s->rm_id);
    fputs(",\"design\":", o);
    jstr_or_null(o, s->design);
    fputs(",\"cfg\":[", o);
    {
        char tmp[sizeof(s->cfg)], *save = 0;
        int first = 1;
        snprintf(tmp, sizeof(tmp), "%s", s->cfg);
        for (char *c = strtok_r(tmp, ",", &save); c; c = strtok_r(0, ",", &save)) {
            if (!first) {
                fputc(',', o);
            }
            jstr(o, c);
            first = 0;
        }
    }
    fputs("],\"pid\":", o);
    if (live && s->pid > 0) {
        fprintf(o, "%ld", s->pid);
    } else {
        fputs("null", o);
    }
    fputs(",\"started_at\":", o);
    jstr_or_null(o, live ? s->started_at : "");
    fputs(",\"bind\":\"127.0.0.1\",\"openocd\":{\"version\":", o);
    jstr(o, ver);
    fputs(",\"adapter\":\"remote_bitbang\"},\"tap\":", o);
    if (s->idcode[0]) {
        fputs("{\"idcode\":", o);
        jstr(o, s->idcode);
        fputc('}', o);
    } else {
        fputs("null", o);
    }
    fputs(",\"cores\":[", o);
    for (unsigned i = 0; live && i < s->ngdb && i < MAX_CORES; i++) {
        fprintf(o, "%s{\"name\":\"cpu%u\",\"gdb_port\":%u}", i ? "," : "", i, C.gdb_port + i);
    }
    fprintf(o, "],\"telnet_port\":%u,\"tcl_port\":%u,\"reason\":", C.telnet_port, C.tcl_port);
    jstr_or_null(o, s->reason);
    fputs(",\"busy\":", o);
    if (s->busy_by[0]) {
        fputs("{\"by\":", o);
        jstr(o, s->busy_by);
        fputs(",\"peer\":", o);
        jstr_or_null(o, s->busy_peer);
        fputc('}', o);
    } else {
        fputs("null", o);
    }
    fputs(",\"error\":", o);
    if (s->err_code[0]) {
        fputs("{\"code\":", o);
        jstr(o, s->err_code);
        fputs(",\"message\":", o);
        jstr(o, s->err_msg);
        fputs(",\"hint\":", o);
        jstr_or_null(o, s->err_hint);
        fputc('}', o);
    } else {
        fputs("null", o);
    }
    fputs(",\"log_tail\":[", o);
    if (s->err_code[0] || strcmp(s->state, "failed") == 0) {
        char lines[TAIL_LINES][LINE_MAX_];
        int n = read_tail(lines);
        for (int i = 0; i < n; i++) {
            if (i) {
                fputc(',', o);
            }
            jstr(o, lines[i]);
        }
    }
    fputs("]}\n", o);
    fflush(o);
}

/* A reply that never touched the state file (usage-level refusals). */
static int refuse(int code, const char *ecode, const char *hint, const char *msg,
                  const char *design, const char *rm_id)
{
    st_t s;
    st_clear(&s);
    snprintf(s.design, sizeof(s.design), "%s", design ? design : "");
    snprintf(s.rm_id, sizeof(s.rm_id), "%s", rm_id ? rm_id : "");
    st_error(&s, ecode, hint, "%s", msg);
    emit(&s, 0);
    return code;
}

/* ==========================================================================
 * The monitor (the session's life)
 * ========================================================================== */
static volatile sig_atomic_t g_term;

static void on_term(int sig)
{
    (void)sig;
    g_term = 1;
}

static void iso_now(char *out, size_t cap)
{
    time_t t = time(0);
    struct tm tm;
    gmtime_r(&t, &tm);
    strftime(out, cap, "%Y-%m-%dT%H:%M:%SZ", &tm);
}

static void stop_openocd(pid_t pid)
{
    if (pid <= 0) {
        return;
    }
    kill(-pid, SIGTERM);
    kill(pid, SIGTERM);
    for (int i = 0; i < 60; i++) {
        if (waitpid(pid, 0, WNOHANG) == pid) {
            return;
        }
        sleep_ms(50);
    }
    kill(-pid, SIGKILL);
    kill(pid, SIGKILL);
    (void)waitpid(pid, 0, 0);
}

/* The log says why OpenOCD stopped: busy (6921 held), or a plain exit. */
static void classify_exit(st_t *s, int status)
{
    char lines[TAIL_LINES][LINE_MAX_];
    int n = read_tail(lines);
    int busy = 0, port = 0;
    for (int i = 0; i < n; i++) {
        if (strstr(lines[i], "remote_bitbang_fill_buf") || strstr(lines[i], "remote_bitbang_putc") ||
            strstr(lines[i], "invalid read response")) {
            busy = 1;
        }
        if (strstr(lines[i], "couldn't bind")) {
            port = 1;
        }
    }
    s->exitcode = WIFSIGNALED(status) ? -WTERMSIG(status) : WEXITSTATUS(status);
    snprintf(s->state, sizeof(s->state), "failed");
    if (busy) {
        char peer[48] = "";
        (void)rbb_holder(peer, sizeof(peer));
        snprintf(s->reason, sizeof(s->reason), "busy");
        snprintf(s->busy_by, sizeof(s->busy_by), "6921 remote_bitbang");
        snprintf(s->busy_peer, sizeof(s->busy_peer), "%s", peer);
        st_error(s, "busy", "close the other OpenOCD on 6921 (e.g. Harness Manager's host "
                 "session: debug down), then mps3-debug up",
                 "127.0.0.1:%u refused OpenOCD: another remote_bitbang client holds it%s%s",
                 C.rbb_port, peer[0] ? " from " : "", peer);
    } else if (port) {
        snprintf(s->reason, sizeof(s->reason), "openocd_exit");
        st_error(s, "busy", "another program listens on the GDB/telnet/Tcl ports",
                 "OpenOCD could not bind 127.0.0.1:%u/%u/%u", C.gdb_port, C.telnet_port,
                 C.tcl_port);
        snprintf(s->busy_by, sizeof(s->busy_by), "port %u", C.gdb_port);
    } else {
        snprintf(s->reason, sizeof(s->reason), "openocd_exit");
        st_error(s, "openocd_exit", "see log_tail; mps3-debug up retries",
                 "OpenOCD exited (%s %d)", s->exitcode < 0 ? "signal" : "code",
                 s->exitcode < 0 ? -s->exitcode : s->exitcode);
    }
}

typedef struct {
    int      fd;
    off_t    off;
    char     part[LINE_MAX_];
    size_t   np;
    unsigned listening;
    int      gdb_clients;
    uint64_t idle_mark;
} logscan_t;

static void scan_line(logscan_t *L, st_t *s, const char *line)
{
    const char *p;
    unsigned port = 0;
    char svc[16];
    if ((p = strstr(line, "Listening on port ")) != 0 &&
        sscanf(p, "Listening on port %u for %15s connections", &port, svc) == 2 &&
        strcmp(svc, "gdb") == 0) {
        L->listening++;
    } else if ((p = strstr(line, "tap/device found: 0x")) != 0 && !s->idcode[0]) {
        snprintf(s->idcode, sizeof(s->idcode), "0x%.8s", p + strlen("tap/device found: 0x"));
        for (char *c = s->idcode; *c; c++) {
            *c = (char)tolower((unsigned char)*c);
        }
    } else if (strstr(line, "accepting 'gdb' connection")) {
        L->gdb_clients++;
    } else if (strstr(line, "dropped 'gdb' connection")) {
        if (L->gdb_clients > 0) {
            L->gdb_clients--;
        }
        if (L->gdb_clients == 0) {
            L->idle_mark = now_ms();
        }
    }
}

static int scan_log(logscan_t *L, st_t *s)
{
    int changed = 0;
    char buf[2048];
    for (int rounds = 0; rounds < 8; rounds++) {
        ssize_t n = pread(L->fd, buf, sizeof(buf), L->off);
        if (n <= 0) {
            break;
        }
        L->off += n;
        for (ssize_t i = 0; i < n; i++) {
            if (buf[i] == '\n' || L->np + 1u >= sizeof(L->part)) {
                L->part[L->np] = '\0';
                char idc[16];
                snprintf(idc, sizeof(idc), "%s", s->idcode);
                unsigned li = L->listening;
                scan_line(L, s, L->part);
                changed |= (li != L->listening) || strcmp(idc, s->idcode) != 0;
                L->np = 0;
                if (buf[i] == '\n') {
                    continue;
                }
            }
            L->part[L->np++] = buf[i];
        }
    }
    return changed;
}

static void monitor(const recipe_t *r, const char *rm_text, uint32_t rm, int rm_known)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_term;
    sigaction(SIGTERM, &sa, 0);
    sigaction(SIGINT, &sa, 0);
    signal(SIGHUP, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);

    /* held for the session's life; a short wait, because a status probe
     * (session_alive) may be holding it for an instant */
    int lk = lock_file("session.lock", 0, 2000u);
    st_t s;
    st_clear(&s);
    snprintf(s.state, sizeof(s.state), "starting");
    snprintf(s.design, sizeof(s.design), "%s", r->name);
    snprintf(s.rm_id, sizeof(s.rm_id), "%s", rm_text);
    s.ngdb = r->ngdb;
    s.mpid = (long)getpid();
    iso_now(s.started_at, sizeof(s.started_at));
    for (unsigned i = 0; i < r->ncfg; i++) {
        size_t o = strlen(s.cfg);
        snprintf(s.cfg + o, sizeof(s.cfg) - o, "%s%s", i ? "," : "", r->cfg[i]);
    }
    if (lk < 0) {
        snprintf(s.state, sizeof(s.state), "failed");
        st_error(&s, "busy", "mps3-debug down, then up", "another session holds %s/session.lock",
                 C.run);
        snprintf(s.busy_by, sizeof(s.busy_by), "mps3-debug");
        st_write(&s);
        _exit(0);
    }

    char logp[PATH_MAX];
    run_path(logp, sizeof(logp), "openocd.log");
    int logw = open(logp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0640);

    /* who OpenOCD runs as: resolved here, before its fork */
    int drop = 0;
    uid_t uid = 0;
    gid_t gid = 0;
    if (geteuid() == 0 && C.user[0]) {
        struct passwd *pw = getpwnam(C.user);
        if (!pw) {
            snprintf(s.state, sizeof(s.state), "failed");
            snprintf(s.reason, sizeof(s.reason), "openocd_exit");
            st_error(&s, "openocd_exit", "the image must create the openocd user",
                     "no user '%s': OpenOCD never runs as root", C.user);
            st_write(&s);
            _exit(0);
        }
        uid = pw->pw_uid;
        gid = pw->pw_gid;
        drop = 1;
    }

    char a_host[] = "set RBB_HOST 127.0.0.1", a_mode[] = "set TRANSPORT_MODE rbb";
    char a_port[48], a_ports[192], a_att[MAX_CORES][128];
    const char *argv[16 + 2 * MAX_CFGS + 2 * MAX_CORES];
    int a = 0;
    snprintf(a_port, sizeof(a_port), "set RBB_PORT %u", C.rbb_port);
    snprintf(a_ports, sizeof(a_ports), "gdb_port %u; telnet_port %u; tcl_port %u; bindto 127.0.0.1",
             C.gdb_port, C.telnet_port, C.tcl_port);
    argv[a++] = C.openocd;
    argv[a++] = "-s";
    argv[a++] = C.share;
    argv[a++] = "-c"; argv[a++] = a_host;
    argv[a++] = "-c"; argv[a++] = a_port;
    argv[a++] = "-c"; argv[a++] = a_mode;
    for (unsigned i = 0; i < r->ncfg; i++) {
        argv[a++] = "-f";
        argv[a++] = r->cfg[i];
    }
    for (unsigned i = 0; i < r->natt; i++) {
        snprintf(a_att[i], sizeof(a_att[i]), "%s configure -event gdb-attach %s",
                 r->att_target[i], r->att_proc[i]);
        argv[a++] = "-c";
        argv[a++] = a_att[i];
    }
    argv[a++] = "-c";
    argv[a++] = a_ports;
    argv[a] = 0;

    pid_t me = getpid();
    pid_t pid = fork();
    if (pid == 0) {
        int nul = open("/dev/null", O_RDWR);
        if (nul >= 0) {
            dup2(nul, 0);
        }
        if (logw >= 0) {
            dup2(logw, 1);
            dup2(logw, 2);
        }
        for (int fd = 3; fd < 1024; fd++) {
            close(fd);
        }
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        signal(SIGPIPE, SIG_DFL);
        (void)setpgid(0, 0);
        (void)!nice(10);                              /* never harnessd's CPU (it is nice 0) */
        if (drop && (setgroups(0, 0) != 0 || setgid(gid) != 0 || setuid(uid) != 0)) {
            _exit(126);
        }
        /* AFTER the setuid: a credential change clears the parent-death signal */
        (void)prctl(PR_SET_PDEATHSIG, SIGTERM);       /* the monitor gone => OpenOCD goes */
        if (getppid() != me) {
            _exit(0);
        }
        if (chdir(C.share) != 0) {
            _exit(125);
        }
        execv(C.openocd, (char *const *)argv);
        _exit(127);
    }
    if (logw >= 0) {
        close(logw);
    }
    if (pid < 0) {
        snprintf(s.state, sizeof(s.state), "failed");
        st_error(&s, "openocd_exit", 0, "fork failed: %s", strerror(errno));
        st_write(&s);
        _exit(0);
    }
    s.pid = (long)pid;
    st_write(&s);

    logscan_t L;
    memset(&L, 0, sizeof(L));
    L.fd = open(logp, O_RDONLY | O_CLOEXEC);
    uint64_t t0 = now_ms(), next_id = t0 + C.swap_poll_ms;
    for (;;) {
        if (g_term) {
            stop_openocd(pid);
            st_t cur;
            (void)st_read(&cur);
            snprintf(s.state, sizeof(s.state), "down");
            snprintf(s.reason, sizeof(s.reason), "%s", cur.reason[0] && strcmp(cur.state, "down") == 0
                     ? cur.reason : "closed");
            s.pid = 0;
            st_write(&s);
            _exit(0);
        }
        int status = 0;
        if (waitpid(pid, &status, WNOHANG) == pid) {
            if (L.fd >= 0) {
                (void)scan_log(&L, &s);
            }
            classify_exit(&s, status);
            s.pid = 0;
            st_write(&s);
            _exit(0);
        }
        if (L.fd >= 0 && scan_log(&L, &s)) {
            if (strcmp(s.state, "starting") == 0 && L.listening >= r->ngdb) {
                snprintf(s.state, sizeof(s.state), "up");
                L.idle_mark = now_ms();
            }
            st_write(&s);
        }
        uint64_t now = now_ms();
        if (strcmp(s.state, "starting") == 0 && now - t0 > (uint64_t)C.start_s * 1000u) {
            stop_openocd(pid);
            snprintf(s.state, sizeof(s.state), "failed");
            snprintf(s.reason, sizeof(s.reason), "openocd_exit");
            st_error(&s, "openocd_exit", "see log_tail (is the DUT's debug port alive?)",
                     "OpenOCD did not come up within %u s", C.start_s);
            s.pid = 0;
            st_write(&s);
            _exit(0);
        }
        if (strcmp(s.state, "up") == 0 && C.idle_s > 0u && L.gdb_clients == 0 &&
            now - L.idle_mark >= (uint64_t)C.idle_s * 1000u) {
            stop_openocd(pid);
            snprintf(s.state, sizeof(s.state), "down");
            snprintf(s.reason, sizeof(s.reason), "idle");
            s.pid = 0;
            st_write(&s);
            _exit(0);
        }
        if (rm_known && now >= next_id) {
            next_id = now + C.swap_poll_ms;
            uint32_t cur = 0;
            if (identify_rm_id(&cur, 1, 500u) == 0 && cur != rm) {
                stop_openocd(pid);
                snprintf(s.state, sizeof(s.state), "down");
                snprintf(s.reason, sizeof(s.reason), "target_lost");
                /* the contract (HM, 1 Oct): code openocd_exit, "swap:" first, reason
                 * target_lost */
                st_error(&s, "openocd_exit", "the DUT's debug port was rebuilt: mps3-debug up again",
                         "swap: the partition was swapped (rm_id 0x%08" PRIx32 " -> 0x%08" PRIx32 ")",
                         rm, cur);
                s.pid = 0;
                st_write(&s);
                _exit(0);
            }
        }
        sleep_ms(250);
    }
}

/* ==========================================================================
 * The commands
 * ========================================================================== */
static int ensure_run_dir(void)
{
    if (mkdir(C.run, 0755) != 0 && errno != EEXIST) {
        return -1;
    }
    return 0;
}

/* The live session's state, corrected for a monitor that is gone. */
static void current(st_t *s)
{
    (void)st_read(s);
    int live = strcmp(s->state, "up") == 0 || strcmp(s->state, "starting") == 0;
    if (live && !session_alive()) {
        snprintf(s->state, sizeof(s->state), "failed");
        snprintf(s->reason, sizeof(s->reason), "monitor_gone");
        st_error(s, "openocd_exit", "mps3-debug down, then up", "the session's monitor is gone");
    }
}

static int exit_for(const st_t *s)
{
    if (strcmp(s->err_code, "busy") == 0) {
        return EX_BUSY;
    }
    if (strcmp(s->state, "failed") == 0) {
        return EX_FAILED;
    }
    return EX_OK;
}

static int cmd_up(const char *want)
{
    if (ensure_run_dir() != 0) {
        return refuse(EX_FAILED, "openocd_exit", 0, "cannot create the run directory", 0, 0);
    }
    int op = lock_file("lock", 0, C.lock_wait_ms);
    if (op < 0) {
        st_t s;
        st_clear(&s);
        snprintf(s.busy_by, sizeof(s.busy_by), "mps3-debug");
        st_error(&s, "busy", "another mps3-debug up/down is running; retry",
                 "%s", "another mps3-debug instance holds the lock");
        emit(&s, 0);
        return EX_BUSY;
    }
    st_t s;
    current(&s);
    if ((strcmp(s.state, "up") == 0 || strcmp(s.state, "starting") == 0)) {
        emit(&s, 1);
        close(op);
        return EX_OK;
    }
    if (access(C.openocd, X_OK) != 0) {
        close(op);
        return refuse(EX_NO_OPENOCD, "no_openocd", "this image has no on-board OpenOCD: use the "
                      "host OpenOCD on 6921", "no OpenOCD binary at the launcher's path", 0, 0);
    }
    recipe_t rs[MAX_RECIPES];
    int n = load_recipes(rs, MAX_RECIPES, 0);
    if (n < 0) {
        close(op);
        return refuse(EX_NO_CFG, "no_cfg", "the image's designs.conf is missing",
                      "no design table on the board", 0, 0);
    }
    /* the loaded design, from identify (read-only; never 6900) */
    uint32_t rm = 0;
    int rm_known = identify_rm_id(&rm, 3, 700u) == 0;
    char rm_text[16] = "";
    if (rm_known) {
        snprintf(rm_text, sizeof(rm_text), "0x%08" PRIx32, rm);
    }
    const recipe_t *r = 0;
    if (strcmp(want, "auto") == 0) {
        if (!rm_known) {
            close(op);
            return refuse(EX_NO_CFG, "no_cfg", "pass --rm NAME (e.g. --rm nanosoc)",
                          "the harness's identify (127.0.0.1:6899) did not answer", 0, 0);
        }
        for (int i = 0; i < n && !r; i++) {
            if (rs[i].design == (rm & 0xFFFFu)) {
                r = &rs[i];
            }
        }
        if (!r) {
            char msg[96];
            snprintf(msg, sizeof(msg), "no debug recipe for rm_id %s (design 0x%04x)", rm_text,
                     (unsigned)(rm & 0xFFFFu));
            close(op);
            return refuse(EX_NO_CFG, "no_cfg", "pass --rm NAME (see mps3-debug version)", msg, 0,
                          rm_text);
        }
    } else {
        for (int i = 0; i < n && !r; i++) {
            if (strcmp(rs[i].name, want) == 0) {
                r = &rs[i];
            }
        }
        if (!r) {
            char msg[96];
            snprintf(msg, sizeof(msg), "no debug recipe named %.40s", want);
            close(op);
            return refuse(EX_NO_CFG, "no_cfg", "see mps3-debug version for the names", msg, 0,
                          rm_text);
        }
    }
    if (!r->dap) {
        char msg[96];
        snprintf(msg, sizeof(msg), "the design %s has no debug port (no DAP)", r->name);
        close(op);
        return refuse(EX_NO_DAP, "no_dap", "load a design with a CPU (nanosoc, nanosoc_upy, ...)",
                      msg, r->name, rm_text);
    }
    char peer[48] = "";
    if (rbb_holder(peer, sizeof(peer))) {
        st_t b;
        st_clear(&b);
        snprintf(b.design, sizeof(b.design), "%s", r->name);
        snprintf(b.rm_id, sizeof(b.rm_id), "%s", rm_text);
        snprintf(b.busy_by, sizeof(b.busy_by), "6921 remote_bitbang");
        snprintf(b.busy_peer, sizeof(b.busy_peer), "%s", peer);
        st_error(&b, "busy", "close the other OpenOCD on 6921 (e.g. Harness Manager's host "
                 "session: debug down), then mps3-debug up",
                 "127.0.0.1:%u is held by another remote_bitbang client (%s)", C.rbb_port, peer);
        emit(&b, 0);
        close(op);
        return EX_BUSY;
    }

    /* a fresh state, then the monitor */
    st_t fresh;
    st_clear(&fresh);
    snprintf(fresh.state, sizeof(fresh.state), "starting");
    snprintf(fresh.design, sizeof(fresh.design), "%s", r->name);
    snprintf(fresh.rm_id, sizeof(fresh.rm_id), "%s", rm_text);
    fresh.ngdb = r->ngdb;
    st_write(&fresh);
    fflush(stdout);
    pid_t m = fork();
    if (m < 0) {
        close(op);
        return refuse(EX_FAILED, "openocd_exit", 0, "fork failed", r->name, rm_text);
    }
    if (m == 0) {
        close(op);                        /* the op lock stays with `up` */
        (void)setsid();
        int nul = open("/dev/null", O_RDWR);
        if (nul >= 0) {
            dup2(nul, 0);
            dup2(nul, 1);
            dup2(nul, 2);
            if (nul > 2) {
                close(nul);
            }
        }
        monitor(r, rm_text, rm, rm_known);
        _exit(0);
    }
    /* wait until the monitor holds the session (so a racing `up` sees it) */
    for (int i = 0; i < 100 && !session_alive(); i++) {
        st_t t;
        (void)st_read(&t);
        if (strcmp(t.state, "failed") == 0) {
            break;
        }
        sleep_ms(20);
    }
    close(op);
    (void)waitpid(m, 0, WNOHANG);

    uint64_t deadline = now_ms() + (uint64_t)C.wait_s * 1000u;
    for (;;) {
        current(&s);
        if (strcmp(s.state, "starting") != 0 || now_ms() >= deadline) {
            break;
        }
        sleep_ms(200);
    }
    emit(&s, 0);
    return exit_for(&s);
}

static int cmd_down(void)
{
    if (ensure_run_dir() != 0) {
        return refuse(EX_FAILED, "openocd_exit", 0, "cannot create the run directory", 0, 0);
    }
    int op = lock_file("lock", 0, C.lock_wait_ms);
    if (op < 0) {
        st_t s;
        st_clear(&s);
        snprintf(s.busy_by, sizeof(s.busy_by), "mps3-debug");
        st_error(&s, "busy", "another mps3-debug up/down is running; retry", "%s",
                 "another mps3-debug instance holds the lock");
        emit(&s, 0);
        return EX_BUSY;
    }
    st_t s;
    (void)st_read(&s);
    if (session_alive() && s.mpid > 0) {
        kill((pid_t)s.mpid, SIGTERM);
        for (int i = 0; i < 120 && session_alive(); i++) {
            sleep_ms(50);
        }
        if (session_alive()) {
            kill((pid_t)s.mpid, SIGKILL);
            sleep_ms(100);
        }
    }
    /* a leftover OpenOCD (a monitor that was killed) goes too */
    if (s.pid > 0 && kill((pid_t)s.pid, 0) == 0) {
        char exe[PATH_MAX], link[64];
        snprintf(link, sizeof(link), "/proc/%ld/exe", s.pid);
        ssize_t k = readlink(link, exe, sizeof(exe) - 1u);
        if (k > 0) {
            exe[k] = '\0';
            if (strstr(exe, "openocd")) {
                kill((pid_t)s.pid, SIGKILL);
            }
        }
    }
    (void)st_read(&s);
    st_t d;
    st_clear(&d);
    snprintf(d.reason, sizeof(d.reason), "%s",
             strcmp(s.state, "down") == 0 && s.reason[0] ? s.reason : "closed");
    snprintf(d.design, sizeof(d.design), "%s", s.design);
    snprintf(d.rm_id, sizeof(d.rm_id), "%s", s.rm_id);
    st_write(&d);
    close(op);
    emit(&d, 0);
    return EX_OK;
}

static int cmd_status(void)
{
    st_t s;
    current(&s);
    emit(&s, 0);
    return EX_OK;
}

static int cmd_version(void)
{
    char ver[128];
    openocd_version(ver, sizeof(ver));
    recipe_t rs[MAX_RECIPES];
    int bad = 0;
    int n = load_recipes(rs, MAX_RECIPES, &bad);
    FILE *o = stdout;
    fprintf(o, "{\"schema\":\"%s\",\"launcher\":\"%s\",\"openocd\":{\"version\":", SCHEMA,
            MPS3_DEBUG_VERSION);
    jstr(o, ver);
    fputs(",\"adapter\":\"remote_bitbang\",\"binary\":", o);
    jstr(o, C.openocd);
    fprintf(o, ",\"present\":%s},\"bind\":\"127.0.0.1\",\"ports\":{\"gdb\":[%u,%u],\"telnet\":%u,"
               "\"tcl\":%u,\"rbb\":%u},\"idle_s\":%u,\"designs\":[",
            access(C.openocd, X_OK) == 0 ? "true" : "false", C.gdb_port, C.gdb_port + 1u,
            C.telnet_port, C.tcl_port, C.rbb_port, C.idle_s);
    for (int i = 0; i < n; i++) {
        fprintf(o, "%s{\"design\":\"0x%04x\",\"name\":", i ? "," : "", rs[i].design);
        jstr(o, rs[i].name);
        fprintf(o, ",\"dap\":%s,\"cores\":%u}", rs[i].dap ? "true" : "false",
                rs[i].dap ? rs[i].ngdb : 0u);
    }
    fprintf(o, "],\"designs_bad_lines\":%d}\n", n < 0 ? -1 : bad);
    return EX_OK;
}

static int usage(void)
{
    fputs("usage: mps3-debug up [--rm auto|NAME] [--json] | down [--json] | status [--json] |"
          " version [--json]\n", stderr);
    return EX_USAGE;
}

int main(int argc, char **argv)
{
    config_load();
    signal(SIGPIPE, SIG_IGN);
    if (argc < 2) {
        return usage();
    }
    const char *cmd = argv[1];
    const char *rm = "auto";
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--json") == 0) {
            continue;                      /* JSON is the only output */
        }
        if (strcmp(argv[i], "--rm") == 0 && i + 1 < argc && strcmp(cmd, "up") == 0) {
            rm = argv[++i];
            if (strcmp(rm, "auto") != 0 && !word_ok(rm, 31u)) {
                return usage();
            }
            continue;
        }
        return usage();
    }
    if (strcmp(cmd, "up") == 0) {
        return cmd_up(rm);
    }
    if (strcmp(cmd, "down") == 0) {
        return cmd_down();
    }
    if (strcmp(cmd, "status") == 0) {
        return cmd_status();
    }
    if (strcmp(cmd, "version") == 0) {
        return cmd_version();
    }
    return usage();
}
