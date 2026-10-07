/*
 * test_status_linux.c — the status collector against a FAKED sysroot
 * (mocked /proc, /sys, /etc files — no live system state is read; the
 * collector's root override guarantees it, and case 3 proves absence
 * handling). Also validates the JSON surface shape.
 *
 * argv[1] = scratch dir for the fake roots.
 */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

#include "../clcd_core.h"
#include "../status_linux.h"

static int g_checks;
#define CHECK(cond, ...) do { \
    g_checks++; \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
        fprintf(stderr, __VA_ARGS__); \
        fprintf(stderr, "\n"); \
        return 1; \
    } \
} while (0)

static char g_root[512];

static void put_file(const char *rel, const char *content)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s%s", g_root, rel);
    mkdir(g_root, 0755);
    /* mkdir -p the parents */
    for (char *p = path + strlen(g_root) + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(path, 0755);
            *p = '/';
        }
    }
    FILE *f = fopen(path, "w");
    assert(f);
    fputs(content, f);
    fclose(f);
}

int main(int argc, char **argv)
{
    const char *scratch = (argc > 1) ? argv[1] : ".";

    /* ---- case 1: fully populated fake root -------------------------------- */
    snprintf(g_root, sizeof(g_root), "%s/fakeroot1", scratch);
    put_file("/proc/uptime", "93784.56 180000.00\n");   /* 1d 2h 3m 4s */
    put_file("/proc/sys/kernel/osrelease", "6.18.7\n");
    put_file("/etc/mps3/static_id", "0x14E1A2D8\n");
    put_file("/sys/class/net/eth0/operstate", "up\n");
    put_file("/sys/class/net/eth0/speed", "100\n");
    put_file("/sys/class/net/eth0/duplex", "full\n");
    put_file("/sys/class/net/eth0/address", "02:00:00:4d:50:53\n");
    put_file("/sys/class/net/eth0/statistics/rx_dropped", "7\n");
    put_file("/sys/class/net/eth0/statistics/tx_errors", "1\n");
    put_file("/run/mps3/ip", "192.168.10.101\n");
    put_file("/sys/class/misc/mps3dfx/state", "10\n");          /* DONE  */
    put_file("/sys/class/misc/mps3dfx/rm_id", "0x010000b2\n");
    put_file("/sys/class/misc/mps3dfx/icap_bytes", "123456\n");

    mps3_status_cfg_t cfg;
    mps3_status_cfg_default(&cfg);
    cfg.root = g_root;

    clcd_status_t st;
    CHECK(mps3_status_collect(&cfg, &st) == 0, "collect 1");
    CHECK(st.uptime_ms == 93784560ull, "uptime_ms %llu",
          (unsigned long long)st.uptime_ms);
    CHECK(!strcmp(st.kernel, "6.18.7"), "kernel '%s'", st.kernel);
    CHECK(st.static_id == 0x14E1A2D8u, "static_id 0x%08x", st.static_id);
    CHECK(st.link_up == 1 && st.speed_mbps == 100 && st.full_duplex == 1,
          "net fields %d/%d/%d", st.link_up, st.speed_mbps, st.full_duplex);
    CHECK(st.mac_valid && st.mac[3] == 0x4D, "mac");
    CHECK(!strcmp(st.ip, "192.168.10.101"), "ip '%s'", st.ip);
    CHECK(st.rx_dropped == 7 && st.tx_errors == 1, "netdev stats");
    CHECK(st.dfx_present == 1, "dfx present");
    CHECK(st.swap_state == CLCD_SWAP_DONE, "swap state %d", st.swap_state);
    CHECK(st.rm_id == 0x010000B2u && st.rm_id_valid == 1, "rm_id");
    CHECK(st.icap_bytes == 123456, "icap_bytes");

    /* the frame built from it names regdemo_b */
    {
        char frame[CLCD_NCELLS];
        uint8_t inv[CLCD_ROWS];
        snprintf(st.board_name, sizeof(st.board_name), "MPS3-01");
        clcd_build_frame(&st, 0, frame, inv);
        int found = 0;
        for (unsigned r = 0; r < CLCD_ROWS && !found; r++)
            for (unsigned c = 0; c + 9 <= CLCD_COLS; c++)
                if (!memcmp(frame + r * CLCD_COLS + c, "regdemo_b", 9)) {
                    found = 1;
                    break;
                }
        CHECK(found, "frame names regdemo_b");
    }

    /* JSON shape (structure asserted here; python validates it in run_tests) */
    {
        char jb[1024];
        unsigned n = mps3_status_json(&st, jb, sizeof(jb));
        CHECK(n > 0 && jb[0] == '{' && jb[n - 1] == '}', "json brackets");
        CHECK(strstr(jb, "\"static_id\":\"0x14e1a2d8\"") != NULL, "json sid");
        CHECK(strstr(jb, "\"rm_name\":\"regdemo_b\"") != NULL, "json rm");
        CHECK(strstr(jb, "\"power_sensor\":false") != NULL,
              "json power_sensor (the TELEM truth)");
        char sp[600];
        snprintf(sp, sizeof(sp), "%s/status_json_sample.json", scratch);
        FILE *f = fopen(sp, "w");
        if (f) { fprintf(f, "%s\n", jb); fclose(f); }
    }

    /* ---- case 2: mid-swap state must NOT qualify the rm_id ----------------- */
    put_file("/sys/class/misc/mps3dfx/state", "6\n");   /* STREAM_PARTIAL */
    CHECK(mps3_status_collect(&cfg, &st) == 0, "collect 2");
    CHECK(st.dfx_present == 1 && st.rm_id_valid == 0,
          "mid-swap rm_id must be unqualified");

    /* ---- case 3: empty root — everything degrades honestly ----------------- */
    snprintf(g_root, sizeof(g_root), "%s/fakeroot2", scratch);
    put_file("/placeholder", "x\n");   /* just create the dir */
    cfg.root = g_root;
    CHECK(mps3_status_collect(&cfg, &st) == 0, "collect 3");
    CHECK(st.dfx_present == 0, "no dfx");
    CHECK(st.link_up == -1, "no netdev -> unknown link");
    CHECK(st.static_id == 0, "no static_id -> 0 (not provisioned)");
    CHECK(st.ip[0] == '\0', "faked root must not fall through to live IP");
    {
        char jb[1024];
        mps3_status_json(&st, jb, sizeof(jb));
        CHECK(strstr(jb, "\"dfx\":{\"present\":false}") != NULL, "json no-dfx");
        CHECK(strstr(jb, "\"link\":null") != NULL, "json link unknown");
    }

    printf("test_status_linux: PASS (%d checks)\n", g_checks);
    return 0;
}
