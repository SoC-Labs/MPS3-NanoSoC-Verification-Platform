/* status_linux.c — see status_linux.h. */
#define _POSIX_C_SOURCE 200809L
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <ifaddrs.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "status_linux.h"

/* ---- tiny file readers ----------------------------------------------------*/
static int read_line(const char *root, const char *rel, char *out, size_t cap)
{
    char path[512];
    snprintf(path, sizeof(path), "%s%s", root, rel);
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    if (!fgets(out, (int)cap, f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    out[strcspn(out, "\n")] = '\0';
    return 0;
}

static int read_u64(const char *root, const char *rel, uint64_t *out)
{
    char b[64];
    if (read_line(root, rel, b, sizeof(b)) != 0)
        return -1;
    *out = strtoull(b, NULL, 0);
    return 0;
}

static int read_u32_hex(const char *root, const char *rel, uint32_t *out)
{
    char b[64];
    if (read_line(root, rel, b, sizeof(b)) != 0)
        return -1;
    *out = (uint32_t)strtoul(b, NULL, 0);
    return 0;
}

void mps3_status_cfg_default(mps3_status_cfg_t *cfg)
{
    cfg->root = "";
    cfg->netdev = "eth0";
    cfg->static_id_file = "/etc/mps3/static_id";
}

/* ---- IP address ------------------------------------------------------------*/
static void get_ip(const mps3_status_cfg_t *cfg, clcd_status_t *st)
{
    st->ip[0] = '\0';

    /* File override first (and the ONLY path under a faked root). */
    char rel[128];
    snprintf(rel, sizeof(rel), "/run/mps3/ip");
    (void)rel;
    char b[64];
    if (read_line(cfg->root, "/run/mps3/ip", b, sizeof(b)) == 0) {
        snprintf(st->ip, sizeof(st->ip), "%.19s", b);
        return;
    }
    if (cfg->root[0] != '\0')
        return;   /* faked sysroot: never query the live system */

    struct ifaddrs *ifa0 = NULL;
    if (getifaddrs(&ifa0) != 0)
        return;
    for (struct ifaddrs *ifa = ifa0; ifa; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET)
            continue;
        if (strcmp(ifa->ifa_name, cfg->netdev) != 0)
            continue;
        struct sockaddr_in *sin = (struct sockaddr_in *)ifa->ifa_addr;
        if (!inet_ntop(AF_INET, &sin->sin_addr, st->ip, sizeof(st->ip)))
            st->ip[0] = '\0';
        break;
    }
    freeifaddrs(ifa0);
}

/* ---- collector ---------------------------------------------------------------*/
int mps3_status_collect(const mps3_status_cfg_t *cfg, clcd_status_t *st)
{
    char b[128];
    char rel[256];

    memset(st, 0, sizeof(*st));
    st->link_up = -1;
    st->speed_mbps = -1;
    st->full_duplex = -1;
    st->swap_state = -1;

    /* uptime: "12345.67 8901.23" seconds */
    if (read_line(cfg->root, "/proc/uptime", b, sizeof(b)) == 0) {
        double up = strtod(b, NULL);
        if (up > 0)
            st->uptime_ms = (uint64_t)(up * 1000.0);
    }

    /* kernel identity */
    if (read_line(cfg->root, "/proc/sys/kernel/osrelease", b, sizeof(b)) == 0)
        snprintf(st->kernel, sizeof(st->kernel), "%.31s", b);
    else
        snprintf(st->kernel, sizeof(st->kernel), "?");

    /* static_id (same provisioning file mps3-ctrld reads) */
    (void)read_u32_hex(cfg->root, cfg->static_id_file, &st->static_id);

    /* network */
    snprintf(rel, sizeof(rel), "/sys/class/net/%s/operstate", cfg->netdev);
    if (read_line(cfg->root, rel, b, sizeof(b)) == 0) {
        st->link_up = (strcmp(b, "up") == 0) ? 1 : 0;
        if (st->link_up == 1) {
            snprintf(rel, sizeof(rel), "/sys/class/net/%s/speed", cfg->netdev);
            if (read_line(cfg->root, rel, b, sizeof(b)) == 0) {
                long sp = strtol(b, NULL, 10);
                st->speed_mbps = (sp > 0) ? (int)sp : -1;
            }
            snprintf(rel, sizeof(rel), "/sys/class/net/%s/duplex", cfg->netdev);
            if (read_line(cfg->root, rel, b, sizeof(b)) == 0)
                st->full_duplex = (strcmp(b, "full") == 0) ? 1 :
                                  (strcmp(b, "half") == 0) ? 0 : -1;
        }
        snprintf(rel, sizeof(rel), "/sys/class/net/%s/address", cfg->netdev);
        if (read_line(cfg->root, rel, b, sizeof(b)) == 0) {
            unsigned m[6];
            if (sscanf(b, "%x:%x:%x:%x:%x:%x",
                       &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) == 6) {
                for (int i = 0; i < 6; i++)
                    st->mac[i] = (uint8_t)m[i];
                st->mac_valid = 1;
            }
        }
        snprintf(rel, sizeof(rel), "/sys/class/net/%s/statistics/rx_dropped",
                 cfg->netdev);
        (void)read_u64(cfg->root, rel, &st->rx_dropped);
        snprintf(rel, sizeof(rel), "/sys/class/net/%s/statistics/tx_errors",
                 cfg->netdev);
        (void)read_u64(cfg->root, rel, &st->tx_errors);
    }

    get_ip(cfg, st);

    /* DFX driver sysfs (drivers/icap/mps3_dfx_drv.c: misc device "mps3dfx",
     * attrs state / rm_id / icap_bytes / eos_status / target_rm_id). rm_id is
     * the LAST-VERIFIED id, deliberately (the wire contract's `ping` uses the
     * same source); its validity licence here is simply "driver present and
     * engine not mid-swap/failed" — the driver zeroes/holds it accordingly. */
    {
        uint64_t v;
        if (read_u64(cfg->root, "/sys/class/misc/mps3dfx/state", &v) == 0) {
            st->dfx_present = 1;
            st->swap_state = (int)v;
            if (read_u32_hex(cfg->root, "/sys/class/misc/mps3dfx/rm_id",
                             &st->rm_id) == 0) {
                /* Interpret the id only in settled states: IDLE (post-boot or
                 * post-swap) and DONE. Anything else is transient/parked. */
                st->rm_id_valid = (st->swap_state == CLCD_SWAP_IDLE ||
                                   st->swap_state == CLCD_SWAP_DONE);
            }
            (void)read_u64(cfg->root, "/sys/class/misc/mps3dfx/icap_bytes",
                           &st->icap_bytes);
        }
    }

    return 0;
}

/* ---- JSON surface --------------------------------------------------------------*/
static unsigned jstr(char *p, unsigned cap, unsigned n, const char *fmt, ...)
{
    if (n >= cap)
        return n;
    va_list ap;
    va_start(ap, fmt);
    int r = vsnprintf(p + n, cap - n, fmt, ap);
    va_end(ap);
    if (r < 0)
        return n;
    unsigned nn = n + (unsigned)r;
    return (nn >= cap) ? cap - 1 : nn;
}

unsigned mps3_status_json(const clcd_status_t *st, char *buf, unsigned cap)
{
    unsigned n = 0;
    if (cap == 0)
        return 0;

    n = jstr(buf, cap, n, "{\"board\":\"%s\",\"static_id\":\"0x%08x\","
                          "\"uptime_ms\":%llu,\"kernel\":\"%s\",",
             st->board_name, st->static_id,
             (unsigned long long)st->uptime_ms, st->kernel);

    n = jstr(buf, cap, n, "\"net\":{\"ip\":\"%s\",\"link\":%s,"
                          "\"speed_mbps\":%d,\"full_duplex\":%s,",
             st->ip,
             st->link_up == 1 ? "true" : st->link_up == 0 ? "false" : "null",
             st->speed_mbps,
             st->full_duplex == 1 ? "true" :
             st->full_duplex == 0 ? "false" : "null");
    if (st->mac_valid)
        n = jstr(buf, cap, n, "\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\",",
                 st->mac[0], st->mac[1], st->mac[2],
                 st->mac[3], st->mac[4], st->mac[5]);
    else
        n = jstr(buf, cap, n, "\"mac\":null,");
    n = jstr(buf, cap, n, "\"rx_dropped\":%llu,\"tx_errors\":%llu},",
             (unsigned long long)st->rx_dropped,
             (unsigned long long)st->tx_errors);

    if (st->dfx_present) {
        n = jstr(buf, cap, n, "\"dfx\":{\"present\":true,\"swap_state\":%d,"
                              "\"swap_state_name\":\"%s\",",
                 st->swap_state, clcd_swap_state_name(st->swap_state));
        if (st->rm_id_valid)
            n = jstr(buf, cap, n, "\"rm_id\":\"0x%08x\",\"rm_name\":\"%s\",",
                     st->rm_id, clcd_rm_name(st->rm_id));
        else
            n = jstr(buf, cap, n, "\"rm_id\":null,\"rm_name\":null,");
        n = jstr(buf, cap, n, "\"icap_bytes\":%llu},",
                 (unsigned long long)st->icap_bytes);
    } else {
        n = jstr(buf, cap, n, "\"dfx\":{\"present\":false},");
    }

    /* What bare-metal TELEM showed, honestly: there is no power sensor, by
     * construction (SERVICE_DISPOSITION §3.10 — four independent dead-ends).
     * The frozen WIRE shape (ok:false/err/lockup) belongs to mps3-ctrld. */
    n = jstr(buf, cap, n, "\"power_sensor\":false}");

    buf[n] = '\0';
    return n;
}
