/*
 * test_identity_core.c — the BOARD identity resolver (identity_core.c; lane IDENT).
 *
 *   1. precedence, each source ALONE: default only; stage0 only; override only
 *   2. MIXED per field: override label, stage0 ip, default mac -- each field keeps
 *      its own source; the hostname follows the label ("label") unless overridden
 *   3. a CORRUPT override: bad lines are ignored and logged, good lines still apply;
 *      binary garbage applies nothing (the other sources win, nothing crashes)
 *   4. the stage0 block INVALID (bad magic / torn magic_end / wrong size) or its
 *      identity fields ZERO (a stage0 built before 2026-09-28): the default wins;
 *      a nonzero field the rules refuse (multicast MAC, 127.x IP, lowercase label,
 *      not NUL-padded) is ignored with a log line; a NEWER version is still read
 *   5. validation tables (label, hostname, ip, mac) -- every refusal named
 *   6. round trips: run file render -> parse; override render -> parse; edits
 *      (set, drop with "", unknown key) and pending (diff)
 * Negative control: the precedence test is re-run with the resolver's inputs
 * swapped, and must then FAIL to see the override win (a test that passes either
 * way proves nothing).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../identity_core.h"
#include "../../../../linux_soc/hw/fw_stage0/stage0_status.h"

static int g_fail, g_n;
#define CHECK(c, ...) do { g_n++; if (!(c)) { g_fail++; printf("  FAIL %s:%d: %s -- ", \
    __FILE__, __LINE__, #c); printf(__VA_ARGS__); printf("\n"); } } while (0)

static char g_log[4096];
static int g_logs;
static void cap_log(void *ctx, const char *m)
{
    (void)ctx;
    g_logs++;
    strncat(g_log, m, sizeof(g_log) - strlen(g_log) - 2);
    strcat(g_log, "\n");
}
static void log_reset(void) { g_log[0] = 0; g_logs = 0; }

#define W(field) (__builtin_offsetof(struct s0_status, field) / 4u)

static void s0_block(uint32_t *b, uint32_t ip, const uint8_t *mac, const char *label)
{
    memset(b, 0, 256);
    b[W(magic)] = S0_STATUS_MAGIC;
    b[W(version)] = S0_STATUS_VERSION;
    b[W(size)] = S0_STATUS_BYTES;
    b[W(magic_end)] = S0_STATUS_MAGIC;
    b[W(ip_addr)] = ip;
    if (mac) {
        b[W(mac_lo)] = S0_LABEL_WORD(mac[0], mac[1], mac[2], mac[3]);
        b[W(mac_hi)] = (uint32_t)mac[4] | ((uint32_t)mac[5] << 8);
    }
    if (label) {
        char l[8] = { 0 };
        memcpy(l, label, strlen(label) < 8 ? strlen(label) : 8);
        b[W(label_lo)] = S0_LABEL_WORD(l[0], l[1], l[2], l[3]);
        b[W(label_hi)] = S0_LABEL_WORD(l[4], l[5], l[6], l[7]);
    }
}

static const uint8_t MAC_B2[6] = { 0x02, 0x00, 0x00, 0x00, 0x02, 0xFE };
static const uint8_t MAC_DEF[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };

static void resolve_text(const char *ovr_text, const uint32_t *blk, mps3_identity_t *id,
                         int *s0st)
{
    mps3_id_fields_t o, s;
    int have_o = ovr_text != 0;
    if (have_o) {
        mps3_id_parse_override(ovr_text, strlen(ovr_text), &o, cap_log, 0);
    }
    int st = mps3_id_from_stage0(blk, &s, cap_log, 0);
    if (s0st) *s0st = st;
    mps3_id_resolve(have_o ? &o : 0, st == MPS3_ID_S0_VALID ? &s : 0, id);
}

static void t_each_source_alone(void)
{
    uint32_t blk[64];
    mps3_identity_t id;
    int st;
    printf("test: each source alone\n");
    resolve_text(0, 0, &id, &st);
    CHECK(st == MPS3_ID_S0_NOWINDOW && strcmp(id.v.label, "MPS3") == 0 &&
          strcmp(id.v.hostname, "mps3") == 0 && id.v.ip == 0xC0A80A65u && id.v.prefix == 24u &&
          memcmp(id.v.mac, MAC_DEF, 6) == 0, "defaults: %s %s", id.v.label, id.v.hostname);
    CHECK(id.src[MPS3_ID_LABEL] == MPS3_ID_SRC_DEFAULT && id.src[MPS3_ID_IP] == MPS3_ID_SRC_DEFAULT &&
          id.src[MPS3_ID_MAC] == MPS3_ID_SRC_DEFAULT && id.src[MPS3_ID_HOSTNAME] == MPS3_ID_SRC_LABEL,
          "sources default/label");

    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    resolve_text(0, blk, &id, &st);
    CHECK(st == MPS3_ID_S0_VALID && strcmp(id.v.label, "MPS3-02") == 0 &&
          strcmp(id.v.hostname, "mps3-02") == 0 && id.v.ip == 0xC0A80B65u && id.v.prefix == 24u &&
          memcmp(id.v.mac, MAC_B2, 6) == 0, "stage0 alone: board 2 (%s)", id.v.label);
    CHECK(id.src[MPS3_ID_LABEL] == MPS3_ID_SRC_STAGE0 && id.src[MPS3_ID_IP] == MPS3_ID_SRC_STAGE0 &&
          id.src[MPS3_ID_MAC] == MPS3_ID_SRC_STAGE0 && id.src[MPS3_ID_HOSTNAME] == MPS3_ID_SRC_LABEL,
          "sources stage0 + hostname from the label");

    resolve_text("MPS3_LABEL=BENCH-7\nMPS3_HOSTNAME=bench7.lab\nMPS3_IP=10.1.2.3/16\n"
                 "MPS3_MAC=02-aa-bb-cc-dd-ee\n", 0, &id, &st);
    CHECK(strcmp(id.v.label, "BENCH-7") == 0 && strcmp(id.v.hostname, "bench7.lab") == 0 &&
          id.v.ip == 0x0A010203u && id.v.prefix == 16u && id.v.mac[1] == 0xAA && id.v.mac[5] == 0xEE,
          "override alone (%s %s)", id.v.label, id.v.hostname);
    for (int f = 0; f < MPS3_ID_NFIELDS; f++) {
        CHECK(id.src[f] == MPS3_ID_SRC_OVERRIDE, "field %d source %d", f, id.src[f]);
    }
}

static int override_wins(int swapped)
{
    uint32_t blk[64];
    mps3_id_fields_t o, s;
    mps3_identity_t id;
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    const char *t = "MPS3_LABEL=OVR\n";
    mps3_id_parse_override(t, strlen(t), &o, 0, 0);
    mps3_id_from_stage0(blk, &s, 0, 0);
    if (swapped) mps3_id_resolve(&s, &o, &id);   /* the wrong precedence */
    else         mps3_id_resolve(&o, &s, &id);
    return strcmp(id.v.label, "OVR") == 0 && id.src[MPS3_ID_LABEL] == MPS3_ID_SRC_OVERRIDE;
}

static void t_mixed(void)
{
    uint32_t blk[64];
    mps3_identity_t id;
    printf("test: mixed per field (override label, stage0 ip, default mac)\n");
    s0_block(blk, 0xC0A80B65u, 0, 0);            /* stage0: ip only */
    resolve_text("MPS3_LABEL=LAB-A\n", blk, &id, 0);
    CHECK(strcmp(id.v.label, "LAB-A") == 0 && id.src[MPS3_ID_LABEL] == MPS3_ID_SRC_OVERRIDE,
          "label override");
    CHECK(id.v.ip == 0xC0A80B65u && id.src[MPS3_ID_IP] == MPS3_ID_SRC_STAGE0, "ip stage0");
    CHECK(memcmp(id.v.mac, MAC_DEF, 6) == 0 && id.src[MPS3_ID_MAC] == MPS3_ID_SRC_DEFAULT,
          "mac default");
    CHECK(strcmp(id.v.hostname, "lab-a") == 0 && id.src[MPS3_ID_HOSTNAME] == MPS3_ID_SRC_LABEL,
          "hostname = the (overridden) label lowercased: %s", id.v.hostname);

    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    resolve_text("MPS3_HOSTNAME=bench-two\n", blk, &id, 0);
    CHECK(strcmp(id.v.label, "MPS3-02") == 0 && strcmp(id.v.hostname, "bench-two") == 0 &&
          id.src[MPS3_ID_HOSTNAME] == MPS3_ID_SRC_OVERRIDE, "hostname override, stage0 label");

    CHECK(override_wins(0), "the override beats stage0");
    CHECK(!override_wins(1), "NEGATIVE CONTROL: with the inputs swapped the override must NOT win");

    resolve_text("MPS3_LABEL=-X-\n", 0, &id, 0);
    CHECK(strcmp(id.v.label, "-X-") == 0 && strcmp(id.v.hostname, "mps3") == 0 &&
          id.src[MPS3_ID_HOSTNAME] == MPS3_ID_SRC_DEFAULT,
          "a label that makes no host name -> default hostname (%s)", id.v.hostname);
}

static void t_corrupt_override(void)
{
    mps3_identity_t id;
    uint32_t blk[64];
    printf("test: a corrupt override is ignored line by line, with a log\n");
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    log_reset();
    resolve_text("garbage line\nMPS3_MAC=01:00:00:00:00:00\nMPS3_IP=300.1.1.1/24\n"
                 "MPS3_LABEL=lower\nFOO=bar\nMPS3_HOSTNAME='quoted-ok'\n", blk, &id, 0);
    CHECK(g_logs == 5, "five lines refused, five logged (%d):\n%s", g_logs, g_log);
    CHECK(strstr(g_log, "line 1") && strstr(g_log, "line 2") && strstr(g_log, "unknown key 'FOO'"),
          "the log names the lines");
    CHECK(strcmp(id.v.label, "MPS3-02") == 0 && id.src[MPS3_ID_MAC] == MPS3_ID_SRC_STAGE0 &&
          id.src[MPS3_ID_IP] == MPS3_ID_SRC_STAGE0, "refused fields fall through to stage0");
    CHECK(strcmp(id.v.hostname, "quoted-ok") == 0 && id.src[MPS3_ID_HOSTNAME] == MPS3_ID_SRC_OVERRIDE,
          "a good (quoted) line still applies");

    static const char bin[] = "\x7f" "ELF\x01\x02\x00\x00\xff\xfe=\x80\x81\n\x00\x00MPS3_LABEL=X";
    mps3_id_fields_t o;
    log_reset();
    int bad = mps3_id_parse_override(bin, sizeof(bin) - 1u, &o, cap_log, 0);
    CHECK(o.have == 0u && bad >= 2, "binary garbage applies nothing (have 0x%x, %d ignored)",
          o.have, bad);

    const char *dup = "MPS3_LABEL=AAA\nMPS3_LABEL=BBB\n";
    mps3_id_parse_override(dup, strlen(dup), &o, 0, 0);
    CHECK(strcmp(o.label, "BBB") == 0, "the later line wins, as in the shell");
}

static void t_stage0_invalid_or_zero(void)
{
    uint32_t blk[64];
    mps3_identity_t id;
    mps3_id_fields_t s;
    int st;
    printf("test: an invalid block, or a valid one with the identity words 0\n");
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    blk[W(magic)] ^= 1u;
    resolve_text(0, blk, &id, &st);
    CHECK(st == MPS3_ID_S0_INVALID && strcmp(id.v.label, "MPS3") == 0 &&
          id.src[MPS3_ID_IP] == MPS3_ID_SRC_DEFAULT, "bad magic -> invalid -> defaults");
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    blk[W(magic_end)] = 0u;
    CHECK(mps3_id_from_stage0(blk, &s, 0, 0) == MPS3_ID_S0_INVALID, "torn magic_end -> invalid");
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    blk[W(size)] = 0x80u;
    CHECK(mps3_id_from_stage0(blk, &s, 0, 0) == MPS3_ID_S0_INVALID, "wrong size -> invalid");
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    blk[W(version)] = 2u;
    CHECK(mps3_id_from_stage0(blk, &s, 0, 0) == MPS3_ID_S0_VALID && s.ip == 0xC0A80B65u,
          "a NEWER version is still read (identity.c's >= rule)");

    s0_block(blk, 0, 0, 0);                     /* a pre-IDENT stage0: words 0 */
    resolve_text(0, blk, &id, &st);
    CHECK(st == MPS3_ID_S0_VALID && id.src[MPS3_ID_LABEL] == MPS3_ID_SRC_DEFAULT &&
          id.src[MPS3_ID_IP] == MPS3_ID_SRC_DEFAULT && id.src[MPS3_ID_MAC] == MPS3_ID_SRC_DEFAULT,
          "zero identity words = absent: defaults");

    static const uint8_t mcast[6] = { 0x01, 0x00, 0x5E, 0, 0, 1 };
    s0_block(blk, 0x7F000001u, mcast, "mps3");
    log_reset();
    resolve_text(0, blk, &id, &st);
    CHECK(g_logs == 3 && id.src[MPS3_ID_IP] == MPS3_ID_SRC_DEFAULT &&
          id.src[MPS3_ID_MAC] == MPS3_ID_SRC_DEFAULT && id.src[MPS3_ID_LABEL] == MPS3_ID_SRC_DEFAULT,
          "127.x, multicast, lowercase: each refused + logged (%d)\n%s", g_logs, g_log);
    s0_block(blk, 0, 0, "AB");
    blk[W(label_hi)] = S0_LABEL_WORD('Z', 0, 0, 0);     /* "AB\0\0Z": not NUL-padded */
    log_reset();
    CHECK(mps3_id_from_stage0(blk, &s, cap_log, 0) == MPS3_ID_S0_VALID &&
          !(s.have & MPS3_ID_F(MPS3_ID_LABEL)) && strstr(g_log, "NUL-padded"),
          "a label with bytes after its NUL is refused");
}

struct vcase { const char *in; int ok; };

static void t_validation(void)
{
    printf("test: validation tables\n");
    static const struct vcase lab[] = {
        { "MPS3-02", 1 }, { "A", 1 }, { "ABCDEFGHIJKLMNOPQRS", 1 }, { "ABCDEFGHIJKLMNOPQRST", 0 },
        { "", 0 }, { "mps3", 0 }, { "MPS3_02", 0 }, { "MPS 3", 0 },
    };
    for (unsigned i = 0; i < sizeof(lab) / sizeof(lab[0]); i++) {
        CHECK((mps3_id_check_label(lab[i].in) == 0) == lab[i].ok, "label '%s'", lab[i].in);
    }
    static const struct vcase host[] = {
        { "mps3-02", 1 }, { "a.b-c.d", 1 }, { "MPS3", 1 }, { "-a", 0 }, { "a-", 0 }, { "a..b", 0 },
        { "a_b", 0 }, { "", 0 }, { ".a", 0 }, { "a.", 0 },
        { "abcdefghijabcdefghijabcdefghijabcdefghijabcdefghijabcdefghijabc", 1 },
        { "abcdefghijabcdefghijabcdefghijabcdefghijabcdefghijabcdefghijabcd", 0 },
    };
    for (unsigned i = 0; i < sizeof(host) / sizeof(host[0]); i++) {
        CHECK((mps3_id_check_hostname(host[i].in) == 0) == host[i].ok, "hostname '%s'", host[i].in);
    }
    static const struct vcase ip[] = {
        { "192.168.11.101/24", 1 }, { "10.0.0.1/8", 1 }, { "192.168.1.2/30", 1 },
        { "192.168.1.1/31", 0 }, { "10.0.0.1/7", 0 }, { "192.168.1.0/24", 0 }, { "192.168.1.255/24", 0 },
        { "127.0.0.1/8", 0 }, { "0.1.2.3/24", 0 }, { "224.0.0.1/24", 0 }, { "256.1.1.1/24", 0 },
        { "1.2.3/24", 0 }, { "1.2.3.4/24x", 0 }, { "1.2.3.4/", 0 }, { "", 0 }, { "192.168.1.5", 1 },
    };
    for (unsigned i = 0; i < sizeof(ip) / sizeof(ip[0]); i++) {
        uint32_t v;
        unsigned p;
        CHECK((mps3_id_parse_ip(ip[i].in, 0, 24u, &v, &p) == 0) == ip[i].ok, "ip '%s'", ip[i].in);
    }
    {
        uint32_t v;
        unsigned p;
        CHECK(mps3_id_parse_ip("192.168.1.5", 1, 24u, &v, &p) != 0, "need_prefix refuses a bare quad");
        CHECK(mps3_id_parse_ip("192.168.1.5", 0, 24u, &v, &p) == 0 && p == 24u, "bare quad -> /24");
    }
    static const struct vcase mac[] = {
        { "02:00:00:00:02:FE", 1 }, { "0200000002fe", 1 }, { "02-00-00-00-02-fe", 1 },
        { "01:00:00:00:00:00", 0 }, { "00:00:00:00:00:00", 0 }, { "ff:ff:ff:ff:ff:ff", 0 },
        { "02:00:00:00:02", 0 }, { "02:00:00:00:02:fg", 0 }, { "02:00-00:00:02:fe", 0 }, { "", 0 },
    };
    for (unsigned i = 0; i < sizeof(mac) / sizeof(mac[0]); i++) {
        uint8_t m[6];
        CHECK((mps3_id_parse_mac(mac[i].in, m) == 0) == mac[i].ok, "mac '%s'", mac[i].in);
    }
}

static void t_roundtrips(void)
{
    uint32_t blk[64];
    mps3_identity_t id, back;
    char text[1024];
    printf("test: run file / override render <-> parse; edits; pending\n");
    s0_block(blk, 0xC0A80B65u, MAC_B2, "MPS3-02");
    resolve_text("MPS3_HOSTNAME=bench-two\n", blk, &id, 0);
    int n = mps3_id_render_run(&id, MPS3_ID_S0_VALID, 1, text, sizeof(text));
    CHECK(n > 0 && strstr(text, "MPS3_IP=192.168.11.101/24\n") &&
          strstr(text, "MPS3_MAC=02:00:00:00:02:fe\n") && strstr(text, "MPS3_HOSTNAME_SRC=override\n") &&
          strstr(text, "MPS3_STAGE0=valid\n") && strstr(text, "MPS3_PERSIST=1\n"), "run file:\n%s", text);
    CHECK(mps3_id_parse_run(text, (size_t)n, &back) == 0 && mps3_id_diff(&id, &back) == 0u &&
          memcmp(back.src, id.src, sizeof(id.src)) == 0, "run file parses back identically");
    CHECK(mps3_id_parse_run("MPS3_LABEL=X\n", 12, &back) != 0, "a partial run file is refused");

    mps3_id_fields_t o;
    memset(&o, 0, sizeof(o));
    CHECK(mps3_id_apply_edit(&o, "ip", "10.9.8.7") == 0 && o.prefix == 24u, "edit ip (bare -> /24)");
    CHECK(mps3_id_apply_edit(&o, "mac", "02:11:22:33:44:55") == 0, "edit mac");
    CHECK(mps3_id_apply_edit(&o, "label", "lower") != 0 && !(o.have & MPS3_ID_F(MPS3_ID_LABEL)),
          "a refused edit leaves the override unchanged");
    CHECK(mps3_id_apply_edit(&o, "colour", "red") != 0, "unknown key refused");
    CHECK(mps3_id_apply_edit(&o, "mac", "") == 0 && !(o.have & MPS3_ID_F(MPS3_ID_MAC)),
          "an empty value drops the key");
    n = mps3_id_render_override(&o, text, sizeof(text));
    mps3_id_fields_t o2;
    CHECK(n > 0 && mps3_id_parse_override(text, (size_t)n, &o2, 0, 0) == 0 && o2.have == o.have &&
          o2.ip == o.ip && o2.prefix == 24u, "override renders and parses back:\n%s", text);

    mps3_identity_t next;
    mps3_id_fields_t s;
    mps3_id_from_stage0(blk, &s, 0, 0);
    mps3_id_resolve(&o, &s, &next);
    char pend[256];
    mps3_id_json_pending(&next, &id, pend, sizeof(pend));
    CHECK(strcmp(pend, "{\"hostname\":\"mps3-02\",\"ip\":\"10.9.8.7/24\"}") == 0,
          "pending = the fields the next boot changes: %s", pend);
    mps3_id_json_pending(&id, &id, pend, sizeof(pend));
    CHECK(strcmp(pend, "null") == 0, "no change -> null");

    mps3_id_status_t st;
    memset(&st, 0, sizeof(st));
    st.running = id;
    st.s0_state = MPS3_ID_S0_VALID;
    st.s0 = s;
    st.ovr_present = 1;
    st.ovr = o;
    st.persist = 1;
    st.next = next;
    char body[1024];
    CHECK(mps3_id_json_status(&st, body, sizeof(body)) > 0 &&
          strcmp(body, "\"label\":\"MPS3-02\",\"hostname\":\"bench-two\",\"ip\":\"192.168.11.101/24\","
                       "\"mac\":\"0200000002fe\",\"source\":{\"label\":\"stage0\",\"hostname\":"
                       "\"override\",\"ip\":\"stage0\",\"mac\":\"stage0\"},\"stage0\":{\"label\":"
                       "\"MPS3-02\",\"ip\":\"192.168.11.101/24\",\"mac\":\"0200000002fe\"},"
                       "\"override\":{\"ip\":\"10.9.8.7/24\"},\"pending\":{\"hostname\":\"mps3-02\","
                       "\"ip\":\"10.9.8.7/24\"},\"persist\":true") == 0, "status body:\n%s", body);
    st.s0_state = MPS3_ID_S0_INVALID;
    st.ovr_present = 0;
    mps3_id_json_status(&st, body, sizeof(body));
    CHECK(strstr(body, "\"stage0\":null,\"override\":null") != 0, "invalid block / no file: nulls");
    s0_block(blk, 0xC0A80B65u, 0, 0);
    mps3_id_from_stage0(blk, &st.s0, 0, 0);
    st.s0_state = MPS3_ID_S0_VALID;
    mps3_id_json_status(&st, body, sizeof(body));
    CHECK(strstr(body, "\"stage0\":{\"label\":null,\"ip\":\"192.168.11.101/24\",\"mac\":null}") != 0,
          "an absent stage0 field is null: %s", body);
}

int main(void)
{
    t_each_source_alone();
    t_mixed();
    t_corrupt_override();
    t_stage0_invalid_or_zero();
    t_validation();
    t_roundtrips();
    printf("identity core: %d checks, %d failures\n", g_n, g_fail);
    return g_fail ? 1 : 0;
}
