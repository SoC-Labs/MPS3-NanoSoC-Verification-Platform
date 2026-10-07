/*
 * tofu_linux.c — the TOFU first-key claim: harnessd's strong
 * mps3_cfg_named_sink() (config_agent.h "the NAMED-FILE sink").
 *
 * A board ships UNCLAIMED: key-only SSH (DL5) and no authorized_keys, so nobody
 * can log in. The first `tftp put authorized_keys` to UDP 69 claims it; from then
 * on the claim is closed (TFTP ERROR 2) until someone with the serial console
 * runs IMAGE's `mps3-unclaim`. identify reports the state (ssh.claimed) and the
 * host-key fingerprint, so a client can pin the host key it is about to trust.
 *
 * The write is ATOMIC: the bytes go to <file>.claim.tmp in the same directory,
 * then fsync + chmod 0600 + rename over the target on the final block. A torn
 * transfer leaves no file (abort() unlinks the temp), so "claimed" can never
 * mean "half a key". The directory is created 0700 if missing (dropbear refuses
 * a group/world-writable ~/.ssh).
 *
 * `claimed` <=> the target exists and is non-empty — re-evaluated at begin(), so
 * a claim that raced in (or an unclaim) is seen immediately.
 *
 * AFTER a claim lands, IMAGE's /usr/sbin/mps3-keys-sync is run (IMAGE_CONTRACT
 * §4.2): it rebuilds the /root/.ssh/authorized_keys dropbear actually reads from
 * the baked keys + this claim. It is bounded (<= 100 ms by contract; harnessd
 * waits at most 2 s, then leaves it to finish on its own).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "../../../../firmware/config_agent/config_agent.h"
#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/identify/identify.h"
#ifdef MPS3_HAS_CLCD
#include "../../../../firmware/clcd/clcd.h"
#endif
#include "harnessd.h"

#define TOFU_MAX_BYTES (16u * 1024u)

static int      s_fd = -1;
static uint32_t s_len;
static char     s_tmp[600];

int harnessd_ssh_claimed(void)
{
    struct stat st;
    return (g_hd.authorized_keys && stat(g_hd.authorized_keys, &st) == 0 &&
            S_ISREG(st.st_mode) && st.st_size > 0) ? 1 : 0;
}

static int mkdir_parent(const char *path)
{
    char dir[600];
    snprintf(dir, sizeof(dir), "%s", path);
    char *slash = strrchr(dir, '/');
    if (!slash || slash == dir) {
        return 0;
    }
    *slash = '\0';
    for (char *p = dir + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            (void)mkdir(dir, 0755);
            *p = '/';
        }
    }
    return (mkdir(dir, 0700) == 0 || errno == EEXIST) ? 0 : -1;
}

static void keys_sync(void)
{
    if (!g_hd.keys_sync || access(g_hd.keys_sync, X_OK) != 0) {
        harnessd_log("tofu: %s not present -- dropbear's key file NOT re-synced\n",
                     g_hd.keys_sync ? g_hd.keys_sync : "(no keys-sync)");
        return;
    }
    pid_t pid = fork();
    if (pid == 0) {
        execl(g_hd.keys_sync, g_hd.keys_sync, (char *)0);
        _exit(127);
    }
    if (pid < 0) {
        return;
    }
    for (int i = 0; i < 200; i++) {            /* <= 2 s */
        int st;
        pid_t r = waitpid(pid, &st, WNOHANG);
        if (r == pid) {
            harnessd_log("tofu: %s exit %d\n", g_hd.keys_sync,
                         WIFEXITED(st) ? WEXITSTATUS(st) : -1);
            return;
        }
        struct timespec ts = { 0, 10000000L };
        nanosleep(&ts, 0);
    }
    harnessd_log("tofu: %s still running after 2 s -- not waited for\n", g_hd.keys_sync);
}

static void tofu_abort(void)
{
    if (s_fd >= 0) {
        close(s_fd);
        s_fd = -1;
        unlink(s_tmp);
    }
    s_len = 0;
}

static int tofu_begin(const char *name)
{
    (void)name;
    tofu_abort();
    if (!g_hd.authorized_keys || harnessd_ssh_claimed()) {
        harnessd_log("tofu: claim REFUSED (%s)\n",
                     g_hd.authorized_keys ? "already claimed" : "no target configured");
        return -1;
    }
    if (mkdir_parent(g_hd.authorized_keys) != 0) {
        harnessd_log("tofu: cannot create the directory of %s: %s\n",
                     g_hd.authorized_keys, strerror(errno));
        return -1;
    }
    snprintf(s_tmp, sizeof(s_tmp), "%s.claim.tmp", g_hd.authorized_keys);
    s_fd = open(s_tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (s_fd < 0) {
        harnessd_log("tofu: cannot open %s: %s\n", s_tmp, strerror(errno));
        return -1;
    }
    s_len = 0;
    return 0;
}

static int tofu_write(const uint8_t *buf, uint32_t len)
{
    if (s_fd < 0 || s_len + len > TOFU_MAX_BYTES) {
        return -1;   /* -> ERROR 3; config_agent then calls abort() */
    }
    uint32_t done = 0;
    while (done < len) {
        ssize_t w = write(s_fd, buf + done, len - done);
        if (w <= 0) {
            if (w < 0 && errno == EINTR) continue;
            return -1;
        }
        done += (uint32_t)w;
    }
    s_len += len;
    return 0;
}

static int tofu_finish(void)
{
    int rc = -1;
    if (s_fd < 0) {
        return -1;
    }
    if (s_len == 0) {
        harnessd_log("tofu: claim REFUSED (empty authorized_keys)\n");
    } else if (harnessd_ssh_claimed()) {
        harnessd_log("tofu: claim REFUSED (claimed while this one was in flight)\n");
    } else if (fsync(s_fd) == 0 && fchmod(s_fd, 0600) == 0 && close(s_fd) == 0) {
        s_fd = -1;
        if (rename(s_tmp, g_hd.authorized_keys) == 0) {
            harnessd_log("tofu: board CLAIMED (%u B -> %s)\n", (unsigned)s_len,
                         g_hd.authorized_keys);
            s_len = 0;
            keys_sync();
            return 0;
        }
        harnessd_log("tofu: rename to %s: %s\n", g_hd.authorized_keys, strerror(errno));
    }
    tofu_abort();   /* also unlinks the temp */
    return rc;
}

static const mps3_cfg_named_sink_t s_sink = {
    .begin = tofu_begin, .write = tofu_write, .finish = tofu_finish, .abort = tofu_abort,
};

const mps3_cfg_named_sink_t *mps3_cfg_named_sink(const char *name)
{
    return (name && strcmp(name, MPS3_CFG_TOFU_FILENAME) == 0) ? &s_sink : 0;
}

/* The claim's FIRST key's fingerprint ("SHA256:<b64>"), or "" when unclaimed or
 * unreadable. Cached against the claim file's (inode, size, mtime): one stat()
 * per call, a hash only when the claim changed. Shared by identify's
 * ssh.key_sha256 (HM_ANSWERS C1) and the CLCD engine row. */
static const char *claim_key_fp(void)
{
    static struct stat seen;
    static int have_seen;
    static char fpr[64];
    struct stat st;
    if (!harnessd_ssh_claimed() || !g_hd.authorized_keys || stat(g_hd.authorized_keys, &st) != 0) {
        have_seen = 0;
        fpr[0] = '\0';
    } else if (!have_seen || st.st_ino != seen.st_ino || st.st_size != seen.st_size ||
               st.st_mtime != seen.st_mtime) {
        seen = st;
        have_seen = 1;
        if (harnessd_authorized_key_fingerprint(g_hd.authorized_keys, fpr, sizeof(fpr)) != 0) {
            fpr[0] = '\0';
        }
    }
    return fpr;
}

/* identify.h provider: harnessd runs dropbear, so the ssh block is present.
 * The fingerprint: IMAGE's published one-liner first (/run/mps3/ssh/
 * host_key_sha256, IMAGE_CONTRACT §4.1, written every boot by S12mps3persist),
 * else computed from the key itself (sshfp.c) — cached against the key's mtime,
 * so a regenerated key is picked up and a steady one costs one stat(). */
int mps3_identify_ssh(mps3_identify_ssh_t *out)
{
    static char     fp[64];
    static time_t   fp_mtime;
    static int      fp_have;
    struct stat st;

    out->claimed = harnessd_ssh_claimed();
    out->host_key_sha256[0] = '\0';
    snprintf(out->key_sha256, sizeof(out->key_sha256), "%s", out->claimed ? claim_key_fp() : "");
    if (g_hd.host_key_fp) {
        FILE *f = fopen(g_hd.host_key_fp, "r");
        if (f) {
            char line[80] = "";
            if (fgets(line, sizeof(line), f)) {
                line[strcspn(line, "\r\n")] = '\0';
                /* "SHA256:" + 43 base64 = 50 chars; anything longer is not one */
                if (strncmp(line, "SHA256:", 7) == 0 && strlen(line) < sizeof(out->host_key_sha256)) {
                    memcpy(out->host_key_sha256, line, strlen(line) + 1u);
                }
            }
            fclose(f);
            if (out->host_key_sha256[0]) {
                return 1;
            }
        }
    }
    if (g_hd.host_key && stat(g_hd.host_key, &st) == 0) {
        if (!fp_have || st.st_mtime != fp_mtime) {
            fp_have = (harnessd_host_key_fingerprint(g_hd.host_key, fp, sizeof(fp)) == 0);
            fp_mtime = st.st_mtime;
        }
    } else {
        fp_have = 0;
    }
    snprintf(out->host_key_sha256, sizeof(out->host_key_sha256), "%s", fp_have ? fp : "");
    return 1;
}

#ifdef MPS3_HAS_CLCD
/* The CLCD ENGINE ROW provider (clcd.h "THE ENGINE ROW", status page row 12):
 * "SYS : linux  ssh claimed SHA256:<8>" / "... ssh unclaimed". The panel refreshes
 * every 250 ms; the claim is re-stat()ed at most once a second and its key is
 * fingerprinted only when the file changed (inode, size, mtime), so the row costs
 * one stat a second and a hash per claim. */
int mps3_clcd_engine(mps3_clcd_engine_t *out)
{
    static uint64_t last_us;
    static int claimed;
    static char fpr[64];
    uint64_t now = harnessd_now_us64();
    if (!last_us || now - last_us >= 1000000u) {
        last_us = now ? now : 1u;
        claimed = harnessd_ssh_claimed();
        snprintf(fpr, sizeof(fpr), "%s", claimed ? claim_key_fp() : "");
    }
    out->impl = mps3_proto_impl();
    out->claimed = claimed;
    /* "SHA256:" + base64: the row shows the first 8 characters after the prefix */
    snprintf(out->fpr, sizeof(out->fpr), "%.8s", strncmp(fpr, "SHA256:", 7) == 0 ? fpr + 7 : "");
    return 1;
}
#endif
