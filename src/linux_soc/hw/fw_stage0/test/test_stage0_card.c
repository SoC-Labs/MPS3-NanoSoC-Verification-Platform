/*
 * test_stage0_card.c -- boot a CARD IMAGE produced by stage0_mkcard.py through
 * the real boot order (stage0_flow.c + stage0_core.c), reading it block by
 * block exactly as stage0 reads the uSD. This is the check that the Python
 * layout writer and the C layout reader agree (the Python test drives it).
 *
 *   test_stage0_card CARD.img EXPECT [PAYLOAD@0xADDR ...]
 *     EXPECT = A | B | NONE; each PAYLOAD must then sit at its DDR address.
 */
#define _POSIX_C_SOURCE 200809L
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "s0_testutil.h"
#include "../stage0_boot.h"
#include "../stage0_flow.h"

#define DDR_BASE 0x80000000u
#define DDR_SPAN (96u << 20)
static int g_fd;
static uint32_t g_blocks;
static uint8_t *g_ddr;

static int op_ddr(void *c) { (void)c; return S0_DDR_OK; }
static int op_init(void *c, uint32_t *b, uint32_t *d) { (void)c; *b = g_blocks; *d = 0; return S0_SD_READY; }
static int op_read(void *c, uint32_t lba, uint32_t n, void *dst)
{
    (void)c;
    if (lba >= g_blocks || n > g_blocks - lba)
        return -1;
    return pread(g_fd, dst, (size_t)n * 512u, (off_t)lba * 512) == (ssize_t)n * 512 ? 0 : -1;
}
static void *op_a2p(uint32_t a, uint32_t len, void *c)
{
    (void)c;
    return s0_region_in_bounds(DDR_BASE, DDR_SPAN, a, len) ? g_ddr + (a - DDR_BASE) : NULL;
}
static void op_log(void *c, const char *l) { (void)c; printf("  | %s\n", l); }

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s CARD.img A|B|NONE [payload@0xADDR ...]\n", argv[0]);
        return 2;
    }
    struct stat sb;
    g_fd = open(argv[1], O_RDONLY);
    if (g_fd < 0 || fstat(g_fd, &sb) != 0) {
        perror(argv[1]);
        return 2;
    }
    g_blocks = (uint32_t)(sb.st_size / 512);
    g_ddr = calloc(1, DDR_SPAN);
    const struct s0_flow_ops ops = { op_ddr, op_init, op_read, op_a2p, op_log, NULL, NULL, NULL, NULL };
    const struct s0_build_ids ids = { 1, 2, 3 };
    static struct s0_status st;
    struct s0_result res;
    int ok;
    s0_status_open(&st, &ids, 0, 2);
    int from = s0_boot_select(&st, &ops, &res, &ok);
    const char *got = from == S0_FROM_A ? "A" : from == S0_FROM_B ? "B" : "NONE";
    CHECK(strcmp(got, argv[2]) == 0, "booted %s, expected %s (a=%u b=%u reason=%u)", got, argv[2],
          st.slot_a_rc, st.slot_b_rc, st.rescue_reason);
    for (int i = 3; i < argc; i++) {
        char path[512];
        snprintf(path, sizeof path, "%s", argv[i]);
        char *at = strrchr(path, '@');
        if (!at) { fprintf(stderr, "bad payload spec %s\n", argv[i]); return 2; }
        *at = 0;
        uint32_t addr = (uint32_t)strtoul(at + 1, 0, 0);
        FILE *f = fopen(path, "rb");
        if (!f) { perror(path); return 2; }
        static uint8_t buf[64u << 20];
        size_t n = fread(buf, 1, sizeof buf, f);
        fclose(f);
        CHECK(from != S0_FROM_NONE && memcmp(g_ddr + (addr - DDR_BASE), buf, n) == 0,
              "%s (%zu B) at 0x%08X", path, n, addr);
    }
    printf("card %s: booted %s -- %s\n", argv[1], got, g_fails ? "FAILED" : "PASSED");
    return g_fails ? 1 : 0;
}
