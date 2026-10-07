/*
 * test_stage0_ident.c -- THE BOARD IDENTITY in the status block (lane IDENT,
 * stage0_status.h "THE BOARD IDENTITY"; STAGE0_CONTRACT §3.3).
 *
 * The Makefile builds this with the -D flags stage0_board.py makes for board 2
 * (boards/mps3_02.mk), exactly as the stage0 Makefile does for a board-2 bake, and
 * checks the published words BYTE BY BYTE against hand-written expectations -- so
 * the Python encoder, the C helper and the offsets are checked by one path each
 * that does not share code with the others:
 *   1. board 2's identity lands at 0xB0/0xB4/0xB8/0xEC/0xF0 as the bytes a Linux
 *      reader (identity_core.c) and stage0_status.py decode;
 *   2. the stage0.c defaults (board 1) are "MPS3-01" / 192.168.10.101 /
 *      02:00:00:4D:50:53 -- the values boards/mps3_01.mk bakes;
 *   3. a reconfiguration (invalid block -> zeroed) then an entry publishes it;
 *      a warm entry over another board's stale words REWRITES them (a re-baked
 *      board takes its new identity at its next entry, not "when rescue ran");
 *   4. the label is NUL-padded: a 4-char label leaves label_hi 0.
 * The ORDER in stage0.c (published before the boot order touches DDR or the
 * card) is checked by check_ident_order.py, with its own negative control.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "../stage0_status.h"
#include "../stage0_flow.h"
#include "s0_testutil.h"

#ifndef S0_IP
#error "build with stage0_board.py's -D flags (test/Makefile)"
#endif

static struct s0_status ST;

static void expect_bytes(unsigned off, const uint8_t *want, unsigned n, const char *what)
{
    const uint8_t *b = (const uint8_t *)&ST;
    CHECK(memcmp(b + off, want, n) == 0, "%s at 0x%02X: %02x %02x %02x %02x ...", what, off,
          b[off], b[off + 1], b[off + 2], b[off + 3]);
}

int main(void)
{
    static const struct s0_build_ids ids = { 0x12345678u, 0x44EE76D5u, 0x01000000u };
    static const uint8_t mac_b2[6] = { MPS3_MAC0, MPS3_MAC1, MPS3_MAC2,
                                       MPS3_MAC3, MPS3_MAC4, MPS3_MAC5 };

    printf("test: board 2's identity (stage0_board.py flags) lands byte-exact\n");
    memset(&ST, 0xA5, sizeof(ST));                      /* garbage: a reconfigured BRAM */
    s0_status_open(&ST, &ids, 0u, 2u);
    CHECK(ST.label_lo == 0u && ST.label_hi == 0u && ST.ip_addr == 0u,
          "a re-initialised block reads 0 (absent) until the entry publishes");
    s0_status_publish_identity(&ST, S0_IP, mac_b2, S0_LABEL_LO, S0_LABEL_HI);
    {
        static const uint8_t ip[4]  = { 101, 11, 168, 192 };   /* host order, LE word */
        static const uint8_t mlo[4] = { 0x02, 0x00, 0x00, 0x00 };
        static const uint8_t mhi[4] = { 0x02, 0xFE, 0x00, 0x00 };
        static const uint8_t lab[8] = { 'M', 'P', 'S', '3', '-', '0', '2', 0 };
        expect_bytes(0xB0, ip, 4, "ip_addr 192.168.11.101");
        expect_bytes(0xB4, mlo, 4, "mac_lo 02:00:00:00");
        expect_bytes(0xB8, mhi, 4, "mac_hi 02:FE");
        expect_bytes(0xEC, lab, 8, "label MPS3-02");
    }
    CHECK(ST.magic == S0_STATUS_MAGIC && ST.magic_end == S0_STATUS_MAGIC && ST.version == 1u,
          "still a valid block, version unchanged (1)");

    printf("test: the stage0.c defaults are board 1 (boards/mps3_01.mk)\n");
    {
        static const uint8_t mac_b1[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
        s0_status_publish_identity(&ST, 0xC0A80A65u, mac_b1,
                                   S0_LABEL_WORD('M', 'P', 'S', '3'),
                                   S0_LABEL_WORD('-', '0', '1', 0));
        static const uint8_t lab[8] = { 'M', 'P', 'S', '3', '-', '0', '1', 0 };
        static const uint8_t mlo[4] = { 0x02, 0x00, 0x00, 0x4D };
        static const uint8_t mhi[4] = { 0x50, 0x53, 0x00, 0x00 };
        expect_bytes(0xEC, lab, 8, "label MPS3-01");
        expect_bytes(0xB4, mlo, 4, "mac_lo");
        expect_bytes(0xB8, mhi, 4, "mac_hi");
        CHECK(ST.ip_addr == 0xC0A80A65u, "ip 0x%08X", ST.ip_addr);
    }

    printf("test: a warm entry REWRITES another bake's stale identity (not only in rescue)\n");
    s0_status_open(&ST, &ids, 0u, 2u);                 /* valid block: a warm entry */
    CHECK(ST.label_hi == S0_LABEL_WORD('-', '0', '1', 0), "open alone keeps the words (0x%08X)",
          ST.label_hi);
    s0_status_publish_identity(&ST, S0_IP, mac_b2, S0_LABEL_LO, S0_LABEL_HI);
    CHECK(ST.ip_addr == S0_IP && ST.label_hi == S0_LABEL_HI && (ST.mac_hi & 0xFFFFu) == 0xFE02u,
          "the entry published THIS bake's values (ip 0x%08X)", ST.ip_addr);

    printf("test: a 4-char label leaves label_hi 0 (NUL-padded)\n");
    s0_status_publish_identity(&ST, S0_IP, mac_b2, S0_LABEL_WORD('L', 'A', 'B', '1'), 0u);
    {
        static const uint8_t lab[8] = { 'L', 'A', 'B', '1', 0, 0, 0, 0 };
        expect_bytes(0xEC, lab, 8, "label LAB1");
    }

    printf("stage0 identity: %d checks, %d failures\n", g_checks, g_fails);
    return g_fails ? 1 : 0;
}
