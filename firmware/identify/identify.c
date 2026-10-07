/*
 * identify.c — see identify.h. The UDP 6899 identify responder: parse one
 * request, build one reply from shell state + providers, send it back to the
 * sender, rate-limited. Platform-free: common/net_if.h for the socket, the
 * coordinator's g_shell_state for identity, the weak providers below for what
 * only the platform knows.
 */
#include <stdio.h>
#include <string.h>

#include "identify.h"
#include "../common/net_if.h"
#include "../common/net_proto.h"
#include "../common/timebase.h"
#include "../coordinator/coordinator.h"   /* g_shell_state: shell_id / rm_id */
/* The harness-version SEAM header only (hand-written; <stdint.h>/<stdbool.h>) —
 * the same include coordinator.c makes for the `version` verb. */
#include "../platform/mps3_version.h"

/* The platform's MAC (net_if_lwip.c on bare metal, harnessd's platform layer
 * under Linux). Declared here rather than by including a platform header, the
 * same way clcd.c does. Only the WEAK default mps3_identify_net() calls it. */
void mps3_platform_mac(uint8_t mac[6]);

/* ==========================================================================
 * WEAK provider defaults = the bare-metal answers (identify.h).
 * ========================================================================== */
__attribute__((weak)) void mps3_identify_net(mps3_identify_net_t *out)
{
    memset(out, 0, sizeof(*out));
    mps3_platform_mac(out->mac);
    (void)snprintf(out->ip, sizeof(out->ip), "%u.%u.%u.%u",
                   (unsigned)MPS3_DEFAULT_IP_A, (unsigned)MPS3_DEFAULT_IP_B,
                   (unsigned)MPS3_DEFAULT_IP_C, (unsigned)MPS3_DEFAULT_IP_D);
    out->dhcp = 0;   /* no DHCP client in the bare-metal image */
}

__attribute__((weak)) const char *mps3_identify_unit(void)
{
    return 0;        /* no device-DNA register until D6: key omitted */
}

__attribute__((weak)) int mps3_identify_ssh(mps3_identify_ssh_t *out)
{
    (void)out;
    return 0;        /* bare metal runs no SSH server: key omitted */
}

__attribute__((weak)) const char *mps3_identify_mode(void)
{
    return "run";
}

__attribute__((weak)) const char *mps3_identify_label(void)
{
    return 0;        /* bare metal has no board-identity resolver: key omitted */
}

/* ==========================================================================
 * State
 * ========================================================================== */
static mps3_net_udp_t *s_sock;
static uint32_t s_tokens;          /* 0..MPS3_IDENTIFY_RATE_PER_S               */
static uint32_t s_refill_ms;       /* timestamp the bucket was last topped up   */
static int      s_bucket_primed;
static uint32_t s_replies, s_rate_drops, s_malformed;

void identify_init(void)
{
    s_sock = mps3_net_udp_open(MPS3_PORT_IDENTIFY);
    s_tokens = MPS3_IDENTIFY_RATE_PER_S;
    s_bucket_primed = 0;
    s_replies = 0u;
    s_rate_drops = 0u;
    s_malformed = 0u;
}

uint32_t identify_replies(void)    { return s_replies; }
uint32_t identify_rate_drops(void) { return s_rate_drops; }
uint32_t identify_malformed(void)  { return s_malformed; }

/* ==========================================================================
 * The pure core
 * ========================================================================== */

static int nonce_ok(const char *n)
{
    size_t len = strlen(n);
    if (len < 8u || len > 32u) {
        return 0;
    }
    for (size_t i = 0; i < len; i++) {
        char c = n[i];
        int hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                  (c >= 'A' && c <= 'F');
        if (!hex) {
            return 0;
        }
    }
    return 1;
}

/* A provider string is echoed into JSON; refuse anything that would need
 * escaping rather than escaping it (every legitimate value here is hex, a dotted
 * quad, a version or "SHA256:" + base64 — none contains a quote, a backslash or
 * a control byte). A value that fails is simply omitted/emptied. */
static int json_safe(const char *s)
{
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c < 0x20u || c == '"' || c == '\\' || c >= 0x7Fu) {
            return 0;
        }
    }
    return 1;
}

/* Bounded append: the ONE writer, so every step is checked the same way and a
 * short buffer fails the whole reply closed (never a torn datagram). */
#define ID_APPEND(...)                                                       \
    do {                                                                     \
        int k_ = snprintf(out + used, (size_t)(cap - used), __VA_ARGS__);    \
        if (k_ < 0 || k_ >= cap - used) {                                    \
            return -1;                                                       \
        }                                                                    \
        used += k_;                                                          \
    } while (0)

int identify_build_reply(const char *req, int req_len, char *out, int cap)
{
    mps3_json_obj_t obj;
    char op[16];
    char nonce[40];
    int32_t v = 0;
    int used = 0;

    if (out == 0 || cap <= 0) {
        return -1;
    }
    out[0] = '\0';
    if (req == 0 || req_len <= 0 ||
        mps3_json_parse(req, req_len, &obj) != MPS3_JSON_OK ||
        mps3_json_get_string(&obj, "op", op, (int)sizeof(op)) != MPS3_JSON_OK ||
        strcmp(op, "identify") != 0 ||
        mps3_json_get_int(&obj, "v", &v) != MPS3_JSON_OK || v != 1 ||
        mps3_json_get_string(&obj, "nonce", nonce, (int)sizeof(nonce)) != MPS3_JSON_OK ||
        !nonce_ok(nonce)) {
        return 0;   /* malformed: SILENT, by contract */
    }

    mps3_identify_net_t net;
    memset(&net, 0, sizeof(net));
    mps3_identify_net(&net);
    net.ip[sizeof(net.ip) - 1u] = '\0';
    if (!json_safe(net.ip)) {
        net.ip[0] = '\0';
    }
    const char *harness = mps3_harness_version_str();
    if (harness == 0 || !json_safe(harness)) {
        harness = "";
    }
    const char *mode = mps3_identify_mode();
    if (mode == 0 || !json_safe(mode)) {
        mode = "run";
    }

    ID_APPEND("{\"ok\":true,\"op\":\"identify\",\"v\":1,\"nonce\":\"%s\","
              "\"board\":\"mps3\","
              "\"mac\":\"%02x%02x%02x%02x%02x%02x\",\"ip\":\"%s\",\"dhcp\":%s,"
              "\"shell_id\":\"0x%08lx\",\"rm_id\":\"0x%08lx\","
              "\"harness\":\"%s\",\"proto\":\"" MPS3_IDENTIFY_PROTO "\","
              "\"mode\":\"%s\"",
              nonce,
              (unsigned)net.mac[0], (unsigned)net.mac[1], (unsigned)net.mac[2],
              (unsigned)net.mac[3], (unsigned)net.mac[4], (unsigned)net.mac[5],
              net.ip, net.dhcp ? "true" : "false",
              (unsigned long)g_shell_state.static_id,
              (unsigned long)g_shell_state.current_rm_id,
              harness, mode);

    /* The same `impl` rule as version.impl: emitted only when the engine sets
     * it (net_proto.h mps3_proto_impl), so a bare-metal reply has no key. */
    const char *impl = mps3_proto_impl();
    if (impl != 0 && impl[0] != '\0' && json_safe(impl) && strlen(impl) <= 12u) {
        ID_APPEND(",\"impl\":\"%s\"", impl);
    }
    const char *unit = mps3_identify_unit();
    if (unit != 0 && unit[0] != '\0' && json_safe(unit) && strlen(unit) <= 32u) {
        ID_APPEND(",\"unit\":\"%s\"", unit);
    }
    ID_APPEND(",\"up_ms\":%lu", (unsigned long)mps3_sys_now_ms());
    {
        uint32_t os_up = 0u;
        if (mps3_proto_os_up_ms(&os_up)) {
            ID_APPEND(",\"os_up_ms\":%lu", (unsigned long)os_up);
        }
    }
    {
        mps3_identify_ssh_t ssh;
        memset(&ssh, 0, sizeof(ssh));
        if (mps3_identify_ssh(&ssh)) {
            ssh.host_key_sha256[sizeof(ssh.host_key_sha256) - 1u] = '\0';
            if (!json_safe(ssh.host_key_sha256)) {
                ssh.host_key_sha256[0] = '\0';
            }
            ssh.key_sha256[sizeof(ssh.key_sha256) - 1u] = '\0';
            if (!ssh.claimed || !json_safe(ssh.key_sha256)) {
                ssh.key_sha256[0] = '\0';   /* unclaimed: no key to name */
            }
            /* key_sha256 (2026-09-26, HM_ANSWERS C1): APPENDED inside the block. */
            ID_APPEND(",\"ssh\":{\"claimed\":%s,\"host_key_sha256\":\"%s\",\"key_sha256\":\"%s\"}",
                      ssh.claimed ? "true" : "false", ssh.host_key_sha256, ssh.key_sha256);
        }
    }
    /* v0.16 (lane IDENT): the board's resolved LABEL, after ssh, before ports
     * (which stays last) -- only when the engine resolves one. A label is 1..19
     * of [A-Z0-9-] (identity_core.h); anything else is omitted, never escaped. */
    {
        const char *label = mps3_identify_label();
        if (label != 0 && label[0] != '\0' && json_safe(label) && strlen(label) <= 19u) {
            ID_APPEND(",\"label\":\"%s\"", label);
        }
    }
    ID_APPEND(",\"ports\":{\"ctrl\":%u,\"push\":%u,\"tftp\":%u,\"jtag\":%u,"
              "\"xvc\":%u,\"uart0\":%u,\"uart1\":%u,\"swo\":%u}}",
              (unsigned)MPS3_PORT_CONTROL, (unsigned)MPS3_PORT_RAW_PUSH,
              (unsigned)MPS3_PORT_TFTP, (unsigned)MPS3_PORT_JTAG,
              (unsigned)MPS3_PORT_XVC, (unsigned)MPS3_PORT_UART0,
              (unsigned)MPS3_PORT_UART1, (unsigned)MPS3_PORT_SWO);
    return used;
}

/* ==========================================================================
 * The service
 * ========================================================================== */

/* Top the bucket up by one token per (1000 / RATE) ms elapsed. Pure 32-bit,
 * wrap-safe (timebase.h rule: subtract, never compare with `<`). */
static void bucket_refill(uint32_t now)
{
    const uint32_t period = 1000u / MPS3_IDENTIFY_RATE_PER_S;
    if (!s_bucket_primed) {
        s_bucket_primed = 1;
        s_refill_ms = now;
        return;
    }
    uint32_t elapsed = now - s_refill_ms;
    if (elapsed >= period) {
        uint32_t add = elapsed / period;
        s_refill_ms += add * period;
        s_tokens = (s_tokens + add > MPS3_IDENTIFY_RATE_PER_S)
                       ? MPS3_IDENTIFY_RATE_PER_S : s_tokens + add;
    }
}

void identify_poll(void)
{
    char req[256];
    char reply[MPS3_IDENTIFY_REPLY_MAX];
    mps3_net_addr_t from;

    if (s_sock == 0) {
        return;
    }
    for (int i = 0; i < MPS3_IDENTIFY_DGRAMS_PER_POLL; i++) {
        int n = mps3_net_udp_recvfrom(s_sock, req, (uint32_t)(sizeof(req) - 1u), &from);
        if (n <= 0) {
            return;   /* nothing pending (or a transport error: try next poll) */
        }
        req[n] = '\0';
        int len = identify_build_reply(req, n, reply, (int)sizeof(reply));
        if (len <= 0) {
            s_malformed++;
            continue;   /* silent */
        }
        bucket_refill(mps3_sys_now_ms());
        if (s_tokens == 0u) {
            s_rate_drops++;
            continue;   /* over the rate: silent, the client retries */
        }
        s_tokens--;
        /* To the SENDER's addr:port — never a fixed port — which is also what
         * keeps the reply on the interface the request came in on. */
        if (mps3_net_udp_sendto(s_sock, reply, (uint32_t)len, &from) == len) {
            s_replies++;
        }
    }
}
