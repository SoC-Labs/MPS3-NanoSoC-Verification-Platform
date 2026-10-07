/*
 * test_identify.c — firmware/identify/identify.c against firmware/test's
 * in-memory net backend (fake_net_if.c), built TWICE:
 *
 *   test_identify         the BARE-METAL answer: the weak providers only. The
 *                         reply must carry no impl / os_up_ms / ssh / unit key.
 *   test_identify_linux   -DTEST_LINUX_PROVIDERS: strong providers stand in for
 *                         harnessd's (impl, os_up_ms, ssh, unit, mode) and the
 *                         keys must appear, in the contract's order.
 *
 * Both: the reply goes to the SENDER's port; malformed requests are silent;
 * the rate limit holds at 10/s with a burst of 10 and refills with time; the
 * reply is under 1200 B even with every optional key at its longest.
 */
#include <stdio.h>
#include <string.h>

#include "../../../../../firmware/common/net_proto.h"
#include "../../../../../firmware/coordinator/coordinator.h"
#include "../../../../../firmware/identify/identify.h"
#include "../../../../../firmware/test/fake_net_if.h"

static int s_fail;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); s_fail++; } } while (0)

/* What identify.c reads from the rest of the shell. */
mps3_shell_state_t g_shell_state;
static uint32_t s_now_ms;
uint32_t mps3_sys_now_ms(void) { return s_now_ms; }
void mps3_platform_mac(uint8_t mac[6])
{
    static const uint8_t m[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
    memcpy(mac, m, 6);
}

#ifdef TEST_LINUX_PROVIDERS
const char *mps3_proto_impl(void) { return "linux"; }
int mps3_proto_os_up_ms(uint32_t *out) { *out = 123456u; return 1; }
const char *mps3_identify_unit(void) { return "0123456789abcdef0123456789abcdef"; }
int mps3_identify_ssh(mps3_identify_ssh_t *out)
{
    out->claimed = 1;
    snprintf(out->host_key_sha256, sizeof(out->host_key_sha256),
             "SHA256:%s", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
    snprintf(out->key_sha256, sizeof(out->key_sha256),
             "SHA256:%s", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB");
    return 1;
}
void mps3_identify_net(mps3_identify_net_t *out)
{
    mps3_platform_mac(out->mac);
    snprintf(out->ip, sizeof(out->ip), "255.255.255.255");
    out->dhcp = 1;
}
#endif

static int ask(const char *req, char *out, int cap)
{
    (void)fake_net_udp_inject(MPS3_PORT_IDENTIFY, 40000u, req, (int)strlen(req));
    identify_poll();
    uint16_t from = 0, to = 0;
    int n = fake_net_udp_take_sent(&from, &to, out, cap - 1);
    if (n >= 0) {
        out[n] = '\0';
        CHECK(from == MPS3_PORT_IDENTIFY && to == 40000u);   /* back to the SENDER */
    }
    return n;
}

int main(void)
{
    char buf[2048];
    fake_net_reset();
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    g_shell_state.static_id = 0xA1B2C3D4u;
    g_shell_state.current_rm_id = 0x0100001Eu;
    s_now_ms = 5000u;
    identify_init();

    int n = ask("{\"op\":\"identify\",\"v\":1,\"nonce\":\"0011AABBccdd\"}", buf, sizeof(buf));
    CHECK(n > 0 && n <= (int)MPS3_IDENTIFY_REPLY_MAX);
#ifndef TEST_LINUX_PROVIDERS
    CHECK(strcmp(buf,
        "{\"ok\":true,\"op\":\"identify\",\"v\":1,\"nonce\":\"0011AABBccdd\",\"board\":\"mps3\","
        "\"mac\":\"0200004d5053\",\"ip\":\"192.168.10.101\",\"dhcp\":false,"
        "\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x0100001e\",\"harness\":\"0.0.0\","
        "\"proto\":\"0.11\",\"mode\":\"run\",\"up_ms\":5000,"
        "\"ports\":{\"ctrl\":6900,\"push\":6910,\"tftp\":69,\"jtag\":6921,\"xvc\":2542,"
        "\"uart0\":6930,\"uart1\":6931,\"swo\":6932}}") == 0);
    CHECK(!strstr(buf, "impl") && !strstr(buf, "ssh") && !strstr(buf, "os_up_ms") &&
          !strstr(buf, "unit"));
#else
    CHECK(strstr(buf, "\"mode\":\"run\",\"impl\":\"linux\",\"unit\":\"0123456789abcdef0123456789abcdef\","
                      "\"up_ms\":5000,\"os_up_ms\":123456,\"ssh\":{\"claimed\":true,"
                      "\"host_key_sha256\":\"SHA256:AAAA") != 0);
    /* key_sha256 (HM_ANSWERS C1): APPENDED inside the ssh block, after the host key */
    CHECK(strstr(buf, "AAA\",\"key_sha256\":\"SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\"},"
                      "\"ports\"") != 0);
    CHECK(strstr(buf, "\"ip\":\"255.255.255.255\",\"dhcp\":true") != 0);
#endif
    if (s_fail) fprintf(stderr, "reply: %s\n", buf);

    /* malformed: silent, and counted */
    static const char *bad[] = {
        "garbage", "{\"op\":\"identify\",\"v\":1}", "{\"op\":\"identify\",\"v\":2,\"nonce\":\"00112233\"}",
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"0011223\"}",                       /* 7 hex */
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"00112233001122330011223300112233a\"}", /* 33 */
        "{\"op\":\"identify\",\"v\":1,\"nonce\":\"0011223g\"}",
        "{\"op\":\"ping\"}", "{\"op\":\"identify\",\"v\":\"1\",\"nonce\":\"00112233\"}",
    };
    uint32_t mal0 = identify_malformed();
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        CHECK(ask(bad[i], buf, sizeof(buf)) < 0);
    }
    CHECK(identify_malformed() - mal0 == sizeof(bad) / sizeof(bad[0]));

    /* the rate limit: 9 tokens left from the burst of 10; at a frozen clock the
     * 10th reply is the last, then silence; 100 ms buys exactly one more. */
    int replies = 0;
    for (int i = 0; i < 20; i++) {
        if (ask("{\"op\":\"identify\",\"v\":1,\"nonce\":\"deadbeef\"}", buf, sizeof(buf)) > 0) {
            replies++;
        }
    }
    CHECK(replies == 9);
    CHECK(identify_rate_drops() == 11u);
    s_now_ms += 100u;
    CHECK(ask("{\"op\":\"identify\",\"v\":1,\"nonce\":\"deadbeef\"}", buf, sizeof(buf)) > 0);
    CHECK(ask("{\"op\":\"identify\",\"v\":1,\"nonce\":\"deadbeef\"}", buf, sizeof(buf)) < 0);
    s_now_ms += 5000u;   /* a long gap refills to the burst, never beyond */
    replies = 0;
    for (int i = 0; i < 15; i++) {
        if (ask("{\"op\":\"identify\",\"v\":1,\"nonce\":\"deadbeef\"}", buf, sizeof(buf)) > 0) {
            replies++;
        }
    }
    CHECK(replies == 10);

    /* the pure core bounds: a reply that would not fit fails closed (-1). */
    char tiny[40];
    const char *ok_req = "{\"op\":\"identify\",\"v\":1,\"nonce\":\"00112233\"}";
    CHECK(identify_build_reply(ok_req, (int)strlen(ok_req), tiny, (int)sizeof(tiny)) == -1);

    if (s_fail) {
        fprintf(stderr, "test_identify: %d FAILED\n", s_fail);
        return 1;
    }
#ifdef TEST_LINUX_PROVIDERS
    printf("test_identify_linux: ALL PASS\n");
#else
    printf("test_identify: ALL PASS\n");
#endif
    return 0;
}
