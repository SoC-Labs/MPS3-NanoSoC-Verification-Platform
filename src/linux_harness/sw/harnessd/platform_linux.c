/*
 * platform_linux.c — what firmware/platform/ + firmware/smsc911x/ used to answer,
 * answered from the kernel.
 *
 * The kernel's smsc911x driver owns eth0 (HARNESSD_CONTRACT.md §3: kernel-owned),
 * so the LAN9220 registers are not ours to read. Every fact the service modules
 * asked the bare-metal driver for has an honest kernel equivalent in sysfs:
 *
 *   smsc911x_link_up()      /sys/class/net/<if>/carrier
 *   smsc911x_mii_read(5)    ANLPAR, SYNTHESISED from .../speed + .../duplex
 *                           (clcd.c decodes only the four technology bits; any
 *                           other register answers -1 = "MII error", which every
 *                           caller already handles)
 *   mps3_platform_mac()     .../address
 *   mps3_stats_net()        link/speed/duplex/MAC (the `stats` verb)
 *   eth counters            .../statistics/{rx_dropped,tx_packets,tx_errors}
 *
 * Plus the Linux-only providers: identify's mac/ip/dhcp (the board identity,
 * the lease via SIOCGIFADDR while IMAGE's /run/mps3/net.state says `dhcp=1`) and
 * os_up_ms (/proc/uptime), and the readers of
 * IMAGE's /run/mps3 state files (boot-health gates the stage0 CONFIRM).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../../../../firmware/common/net_proto.h"
#include "../../../../firmware/coordinator/coordinator.h"
#include "../../../../firmware/identify/identify.h"
#include "harnessd.h"

/* The declarations clcd.c sees (smsc911x.h) — defined here because the kernel,
 * not a bare-metal driver, owns the PHY. */
int  smsc911x_link_up(void);
int  smsc911x_mii_read(uint32_t phy_reg, uint16_t *val_out);
void mps3_platform_mac(uint8_t mac[6]);

static int read_line(const char *path, char *out, size_t cap)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        return -1;
    }
    if (!fgets(out, (int)cap, f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    out[strcspn(out, "\r\n")] = '\0';
    return 0;
}

int harnessd_read_u32_file(const char *path, uint32_t *out)
{
    char buf[64];
    if (!path || read_line(path, buf, sizeof(buf)) != 0) {
        return -1;
    }
    char *end = 0;
    errno = 0;
    unsigned long v = strtoul(buf, &end, 0);
    if (errno != 0 || end == buf) {
        return -1;
    }
    *out = (uint32_t)v;
    return 0;
}

static int net_file(const char *leaf, char *out, size_t cap)
{
    char path[256];
    snprintf(path, sizeof(path), "/sys/class/net/%s/%s", g_hd.netif, leaf);
    return read_line(path, out, cap);
}

static long net_long(const char *leaf, long dflt)
{
    char buf[64];
    if (net_file(leaf, buf, sizeof(buf)) != 0) {
        return dflt;
    }
    char *end = 0;
    long v = strtol(buf, &end, 10);
    return (end == buf) ? dflt : v;
}

int smsc911x_link_up(void)
{
    long c = net_long("carrier", -1);
    return (c < 0) ? -1 : (c ? 1 : 0);
}

int smsc911x_mii_read(uint32_t phy_reg, uint16_t *val_out)
{
    if (phy_reg != 0x05u || val_out == 0) {
        return -1;   /* only ANLPAR is ever asked for; nothing else is honest */
    }
    char dup[16] = "";
    long spd = net_long("speed", -1);
    (void)net_file("duplex", dup, sizeof(dup));
    int full = (strcmp(dup, "full") == 0);
    /* ANLPAR [8]100TX-FD [7]100TX [6]10T-FD [5]10T */
    if (spd >= 100)     *val_out = full ? (1u << 8) : (1u << 7);
    else if (spd == 10) *val_out = full ? (1u << 6) : (1u << 5);
    else                return -1;
    return 0;
}

void mps3_platform_mac(uint8_t mac[6])
{
    /* The bare-metal default (net_if_lwip.c) if sysfs has no answer. */
    static const uint8_t dflt[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
    char buf[32];
    unsigned m[6];
    if (net_file("address", buf, sizeof(buf)) == 0 &&
        sscanf(buf, "%x:%x:%x:%x:%x:%x", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) == 6) {
        for (int i = 0; i < 6; i++) {
            mac[i] = (uint8_t)m[i];
        }
        return;
    }
    memcpy(mac, dflt, 6);
}

/* Strong override of coordinator.c's weak "no link, MAC 0" `stats` seam. */
void mps3_stats_net(mps3_stats_net_t *out)
{
    memset(out, 0, sizeof(*out));
    out->link = (smsc911x_link_up() == 1);
    if (out->link) {
        char dup[16] = "";
        long spd = net_long("speed", 0);
        (void)net_file("duplex", dup, sizeof(dup));
        out->spd = (spd == 10 || spd == 100) ? (uint32_t)spd : (spd > 100 ? 100u : 0u);
        out->fdx = (strcmp(dup, "full") == 0);
    }
    mps3_platform_mac(out->mac);
}

void harnessd_eth_counters(harnessd_eth_counters_t *out)
{
    memset(out, 0, sizeof(*out));
    long a = net_long("statistics/rx_dropped", -1);
    long b = net_long("statistics/tx_packets", -1);
    long c = net_long("statistics/tx_errors", -1);
    if (a < 0 || b < 0 || c < 0) {
        return;
    }
    out->valid = 1;
    out->rx_dropped = (uint32_t)a;
    out->tx_packets = (uint32_t)b;
    out->tx_errors  = (uint32_t)c;
}

/* ---- identify providers ---------------------------------------------------- */

/* identify's mac / ip / dhcp (v0.16, lane IDENT): the BOARD identity this boot
 * resolved (identity_linux.c, /run/mps3/identity) -- the MAC S41mps3net set on
 * eth0 before link up, and its static IP. One exception, so `dhcp` keeps meaning
 * "ip came from DHCP": while a lease is held (IMAGE's net.state dhcp=1) `ip` is
 * the interface's primary address, the lease, as before. */
void mps3_identify_net(mps3_identify_net_t *out)
{
    memset(out, 0, sizeof(*out));
    harnessd_board_mac(out->mac);
    char v[8];
    out->dhcp = (harnessd_kv_file(g_hd.net_state, "dhcp", v, sizeof(v)) == 0 &&
                 strcmp(v, "1") == 0) ? 1 : 0;
    if (!out->dhcp) {
        uint32_t ip = harnessd_board_ip();
        snprintf(out->ip, sizeof(out->ip), "%u.%u.%u.%u", (unsigned)(ip >> 24),
                 (unsigned)(ip >> 16) & 255u, (unsigned)(ip >> 8) & 255u, (unsigned)ip & 255u);
        return;
    }
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd >= 0) {
        struct ifreq ifr;
        memset(&ifr, 0, sizeof(ifr));
        strncpy(ifr.ifr_name, g_hd.netif, IFNAMSIZ - 1);
        if (ioctl(fd, SIOCGIFADDR, &ifr) == 0) {
            struct sockaddr_in *sa = (struct sockaddr_in *)&ifr.ifr_addr;
            (void)inet_ntop(AF_INET, &sa->sin_addr, out->ip, sizeof(out->ip));
        }
        close(fd);
    }
}

/* `os_up_ms` (net_proto.h): Linux has an OS under the shell. */
int mps3_proto_os_up_ms(uint32_t *out)
{
    char buf[64];
    if (read_line("/proc/uptime", buf, sizeof(buf)) != 0) {
        return 0;
    }
    double s = strtod(buf, 0);
    if (s < 0.0) {
        return 0;
    }
    *out = (uint32_t)((uint64_t)(s * 1000.0) & 0xFFFFFFFFu);
    return 1;
}

/* ---- IMAGE's /run/mps3 state files (IMAGE_CONTRACT.md §4-§5) --------------- */

/* `key=value` tokens, space- or newline-separated (IMAGE writes both shapes:
 * net.state is one line, boot-health one per line). Returns 0 and the value. */
int harnessd_kv_file(const char *path, const char *key, char *out, size_t cap)
{
    char buf[1024];
    FILE *f = path ? fopen(path, "r") : 0;
    if (!f) {
        return -1;
    }
    size_t n = fread(buf, 1, sizeof(buf) - 1u, f);
    fclose(f);
    buf[n] = '\0';
    size_t kl = strlen(key);
    for (char *tok = strtok(buf, " \t\r\n"); tok; tok = strtok(0, " \t\r\n")) {
        if (strncmp(tok, key, kl) == 0 && tok[kl] == '=') {
            snprintf(out, cap, "%s", tok + kl + 1);
            return 0;
        }
    }
    return -1;
}

int harnessd_boot_healthy(void)
{
    char v[16];
    return harnessd_kv_file(g_hd.boot_health, "healthy", v, sizeof(v)) == 0 &&
           strcmp(v, "1") == 0;
}
