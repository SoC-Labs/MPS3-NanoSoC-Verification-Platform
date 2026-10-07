/*
 * version_linux.c — the `version` verb's seams, and the diag/stats OMIT masks.
 *
 * `version` is answered by coordinator.c's unmodified handler; what differs on
 * Linux is where its inputs come from:
 *
 *   impl        "linux" (net_proto.h mps3_proto_impl) — the ONE additive key that
 *               tells a client which engine it is talking to.
 *   harness / ver32 / sha / dirty
 *               the IMAGE MANIFEST, /etc/mps3/version (IMAGE writes it at build:
 *               one `key=value` per line; harnessd reads harness=, ver32=, sha=,
 *               dirty=, date=). On bare metal these are generated into the ELF by
 *               scripts/gen_version.py; under Linux the image is built separately
 *               from the bitstream, so the manifest that ships WITH the image is
 *               the honest source. Absent manifest => the weak TU's "not
 *               provisioned" answers (0.0.0 / 0 / unknown), never an invented one.
 *   lmb_kb      -DMPS3_LMB_KB=128 (Makefile): the MBV LMB.
 *   usr_access  firmware/platform/mps3_usr_access.c, unchanged, via UIO.
 *
 * The OMIT masks (net_proto.h): the diag keys that only an lwIP stack or the
 * bare-metal LAN9220 driver can fill are left OFF the `diag` line instead of
 * being reported as 0 (the list is HARNESSD_CONTRACT.md §5.4). Every `stats` key
 * has an honest Linux source, so the stats mask is empty.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../../../firmware/common/diag.h"
#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/platform/mps3_version.h"
#include "harnessd.h"

static struct {
    int      loaded;
    char     harness[16];
    char     sha[16];
    char     date[32];
    uint32_t ver32;
    int      dirty;
} s_m;

static void copy_val(char *dst, size_t cap, const char *v)
{
    size_t n = strcspn(v, "\r\n");
    if (n >= cap) {
        n = cap - 1u;
    }
    memcpy(dst, v, n);
    dst[n] = '\0';
}

void harnessd_version_load(void)
{
    memset(&s_m, 0, sizeof(s_m));
    FILE *f = g_hd.version_file ? fopen(g_hd.version_file, "r") : 0;
    if (!f) {
        harnessd_log("version: no manifest at %s -- reporting \"not provisioned\"\n",
                     g_hd.version_file ? g_hd.version_file : "(none)");
        return;
    }
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        char *eq = strchr(line, '=');
        if (!eq || line[0] == '#') {
            continue;
        }
        *eq = '\0';
        const char *k = line, *v = eq + 1;
        if (strcmp(k, "harness") == 0)      copy_val(s_m.harness, sizeof(s_m.harness), v);
        else if (strcmp(k, "sha") == 0)     copy_val(s_m.sha, sizeof(s_m.sha), v);
        else if (strcmp(k, "date") == 0)    copy_val(s_m.date, sizeof(s_m.date), v);
        else if (strcmp(k, "ver32") == 0)   s_m.ver32 = (uint32_t)strtoul(v, 0, 0);
        else if (strcmp(k, "dirty") == 0)   s_m.dirty = (int)strtol(v, 0, 0) != 0;
    }
    fclose(f);
    s_m.loaded = 1;
    harnessd_log("version: manifest %s: harness %s ver32 0x%08x sha %s%s\n",
                 g_hd.version_file, s_m.harness[0] ? s_m.harness : "?",
                 (unsigned)s_m.ver32, s_m.sha[0] ? s_m.sha : "?", s_m.dirty ? " (dirty)" : "");
}

uint32_t harnessd_manifest_ver32(void) { return s_m.loaded ? s_m.ver32 : 0u; }

/* ---- strong overrides of firmware/platform/mps3_version_weak.c ------------- */
uint32_t mps3_harness_version(void)
{
    return s_m.loaded ? s_m.ver32 : 0u;
}

const char *mps3_harness_version_str(void)
{
    return (s_m.loaded && s_m.harness[0]) ? s_m.harness : "0.0.0";
}

const char *mps3_harness_git_sha(void)
{
    return (s_m.loaded && s_m.sha[0]) ? s_m.sha : "unknown";
}

bool mps3_harness_dirty(void)
{
    return s_m.loaded && s_m.dirty;
}

const char *mps3_harness_build_date(void)
{
    return (s_m.loaded && s_m.date[0]) ? s_m.date : "unknown";
}

/* ---- net_proto.h seams ------------------------------------------------------ */
const char *mps3_proto_impl(void)
{
    return "linux";
}

#define OMIT(name) ((uint64_t)1u << (unsigned)MPS3_DIAG_IX_##name)

uint64_t mps3_proto_diag_omit(void)
{
    return
        /* the bare-metal LAN9220 driver's own counters (smsc911x.c) */
        OMIT(rx_recover_events) | OMIT(rx_recover_dumps) |
        OMIT(tx_status_drained) | OMIT(tx_fifo_full_drops) |
        OMIT(tx_space_stalls)   | OMIT(tx_iface_errors)  | OMIT(tx_last_status) |
        /* lwIP pcb / pbuf visibility (net_if_lwip.c) */
        OMIT(tcp_rcv_wnd) | OMIT(tcp_rcv_ann_wnd) | OMIT(rx_queued) |
        OMIT(pbuf_free)   | OMIT(tcp_sndbuf)      | OMIT(tcp_snd_wnd) |
        /* the QSPI overlay store (not linked: D13's store is L2 STORE's) */
        OMIT(ovlstore_phase) | OMIT(ovlstore_detail);
}

uint32_t mps3_proto_stats_omit(void)
{
    return 0u;   /* every stats key has an honest Linux source (§5.4) */
}
