/*
 * test_ctrl_reap.c — the 6900 control channel's single-client slot under
 * close-then-reconnect: REAP BEFORE REFUSE (2026-09-28).
 *
 * THE BUG. coordinator_net_poll() accepts FIRST and reads the current client
 * later in the same pass (and a client parked on a held verb is not read at
 * all). So a client that closed and at once reconnected found its OLD
 * connection still registered and was refused -- every time. On harnessd that
 * was an unanswered close, or an RST when a line had already been sent: it cost
 * a soak run and broke Harness Manager's back-to-back requests.
 *
 * THE FIX. A newcomer first asks mps3_net_peer_closed(current); a current
 * client that is already gone is dropped, the newcomer adopted. Single-client
 * semantics are otherwise unchanged, and each is pinned here:
 *
 *   close, reconnect at once, ping: answered, 200 times  -> test_back_to_back_200
 *   a second LIVE client is still refused                -> test_second_live_client_refused
 *   a client that closed with its request still unread
 *   keeps the slot until that request has been served    -> test_unread_request_is_served_first
 *   a PARKED client (held verb) that closed is reaped,
 *   the newcomer is served, the held verb still settles  -> test_parked_client_that_closed_is_reaped
 *   a PARKED client that is still connected keeps the
 *   slot and gets its held response                      -> test_parked_live_client_keeps_the_slot
 *
 * TWO BINARIES (the negative control, same pattern as test_posix_net_if_nodrain):
 *   test_ctrl_reap          positive
 *   test_ctrl_reap_noprobe  -DFAKE_NET_TEST_NO_PEER_PROBE: fake_net_if.c drops
 *                           its probe, so net_if.c's WEAK "presumed alive"
 *                           default links -- the pre-fix behaviour. The cases
 *                           that exist because of the bug assert the REFUSAL
 *                           there, which proves the positive assertions can fail.
 *
 * Links: the real coordinator.c + coordinator_net.c + swap_fsm.c (+transitions),
 * clkrst.c, net_proto.c, net_if.c, diag.c, log_ring.c, service.c against
 * mock_regs.c and fake_net_if / fake_config_agent / fake_overlay_store /
 * fake_services (DISPATCH_SRCS). -DMPS3_HAL_MOCK.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "fake_net_if.h"
#include "fake_config_agent.h"
#include "fake_overlay_store.h"

#ifndef CTRL_REAP_TEST_NAME
#define CTRL_REAP_TEST_NAME "test_ctrl_reap"
#endif

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

#define TEST_STATIC_ID 0xA1B2C3D4u

uint32_t mps3_shell_static_id(void)
{
    return TEST_STATIC_ID;
}

static const char PING[] = "{\"op\":\"ping\"}\n";
static const char PONG_RM0[] = "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000000\"}\n";
static const char PONG_RM1[] = "{\"ok\":true,\"shell_id\":\"0xa1b2c3d4\",\"rm_id\":\"0x00000001\"}\n";
static const char SWAP[] = "{\"op\":\"swap\",\"rm\":\"nanosoc\",\"src\":\"tftp\"}\n";
static const char SWAP_OK[] = "{\"ok\":true,\"rm_id\":\"0x00000001\",\"verified\":true}\n";

static void boot(void)
{
    mock_regs_reset();
    fake_net_reset();
    fake_config_agent_reset();
    overlay_manifest_info_t greybox = {
        .static_id = TEST_STATIC_ID, .rm_id = 0,
        .clear_len_words = 4, .clear_crc32 = 0x1111,
    };
    fake_overlay_store_reset();
    fake_overlay_store_set_greybox(&greybox, 0);
    coordinator_init();
    mock_regs_poke(MPS3_HWICAP_BASE, HWICAP_WFV, 1024u);
}

/* The control channel alone (no swap in flight). */
static void polls(int n)
{
    for (int i = 0; i < n; i++) {
        coordinator_net_poll();
    }
}

/* The swap FSM + the control channel, with the vendor-IP choreography the mocks
 * cannot self-drive (test_coordinator_dispatch.c's pump_swap). The incoming pair
 * "arrives" once, via the fake config_agent, when arm_pair is set. */
static void superloop(int n, int arm_pair)
{
    if (arm_pair) {
        config_agent_bitstream_info_t clr = {
            .rm_id = 1, .static_id = TEST_STATIC_ID, .len_words = 8, .crc32 = 0x2222,
        };
        config_agent_bitstream_info_t par = {
            .rm_id = 1, .static_id = TEST_STATIC_ID, .len_words = 4, .crc32 = 0x3333,
        };
        fake_config_agent_arm_clearing(&clr);
        fake_config_agent_arm_partial(&par);
    }
    for (int i = 0; i < n; i++) {
        switch (swap_fsm_state()) {
        case SWAP_DECOUPLE_ASSERT:
        case SWAP_REISOLATE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,
                           DFXCTL_STATUS_DECOUPLED | DFXCTL_STATUS_RP_IN_RESET);
            break;
        case SWAP_VERIFY:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID, 1u);
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
            break;
        case SWAP_RELEASE:
            mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS, 0u);
            break;
        default:
            break;
        }
        swap_fsm_poll();
        coordinator_net_poll();
    }
}

/* Send `line` on `cli`, poll, and return 1 iff exactly `want` came back. */
static int exchange(int cli, const char *line, const char *want)
{
    char rsp[MPS3_CTRL_RESP_MAX];
    if (fake_net_send(cli, line, (int)strlen(line)) != (int)strlen(line)) {
        return 0;
    }
    polls(4);
    int n = fake_net_recv(cli, rsp, (int)sizeof(rsp) - 1);
    if (n <= 0) {
        return 0;
    }
    rsp[n] = '\0';
    return strcmp(rsp, want) == 0;
}

/* ---- the bug's own case ---------------------------------------------------- */

static void test_back_to_back_200(void)
{
    boot();
    int cli = fake_net_connect(MPS3_PORT_CONTROL);
    CHECK(cli >= 0);
    CHECK(exchange(cli, PING, PONG_RM0));

    int answered = 0;
    int refused = 0;
    for (int i = 0; i < 200; i++) {
        fake_net_close(cli);
        /* IMMEDIATELY: no poll between the close and the new connect, so the
         * server meets the newcomer before it has read the old client's EOF. */
        cli = fake_net_connect(MPS3_PORT_CONTROL);
        CHECK(cli >= 0);
        if (exchange(cli, PING, PONG_RM0)) {
            answered++;
            CHECK(!fake_net_fw_closed(cli));
        } else {
            refused++;
            CHECK(fake_net_fw_closed(cli));  /* accept-then-close, unanswered */
            break;                           /* the control needs to see it once */
        }
    }
#if defined(FAKE_NET_TEST_NO_PEER_PROBE)
    /* NEGATIVE CONTROL: without the probe the very first reconnect is refused --
     * the exact failure the soak and Harness Manager hit. */
    CHECK(answered == 0 && refused == 1);
#else
    CHECK(answered == 200 && refused == 0);
#endif
    fake_net_close(cli);
    polls(2);
}

/* ---- single-client semantics that must NOT change -------------------------- */

static void test_second_live_client_refused(void)
{
    boot();
    int first = fake_net_connect(MPS3_PORT_CONTROL);
    CHECK(exchange(first, PING, PONG_RM0));

    int second = fake_net_connect(MPS3_PORT_CONTROL);
    polls(2);
    CHECK(fake_net_fw_closed(second));          /* a live client wins the slot */
    CHECK(!fake_net_fw_closed(first));
    CHECK(exchange(first, PING, PONG_RM0));     /* ...and is untouched */

    /* The refused client closing (and even an abortive death of a refused one)
     * does not disturb the adopted client either. */
    fake_net_close(second);
    polls(2);
    CHECK(exchange(first, PING, PONG_RM0));
    fake_net_close(first);
    polls(2);
}

/* The probe is conservative on purpose: a client that sent a request and closed
 * WITHOUT reading the answer still has that request unread when the newcomer
 * arrives, so it is not reapable yet (reaping it would silently drop a verb the
 * client did send -- a fire-and-forget `reset`, say). The newcomer is refused
 * once; the request is still served; the slot then frees for the next client. */
static void test_unread_request_is_served_first(void)
{
    boot();
    int first = fake_net_connect(MPS3_PORT_CONTROL);
    polls(1);
    CHECK(fake_net_send(first, PING, (int)strlen(PING)) == (int)strlen(PING));
    fake_net_close(first);                      /* request unread, then FIN */
    int second = fake_net_connect(MPS3_PORT_CONTROL);
    polls(1);
    CHECK(fake_net_fw_closed(second));          /* refused: first not dead yet */
    polls(2);                                   /* first's ping served, EOF read */
    fake_net_close(second);

    int third = fake_net_connect(MPS3_PORT_CONTROL);
    CHECK(exchange(third, PING, PONG_RM0));
    fake_net_close(third);
    polls(2);
}

/* ---- held verbs ------------------------------------------------------------ */

static void test_parked_client_that_closed_is_reaped(void)
{
    boot();
    int parked = fake_net_connect(MPS3_PORT_CONTROL);
    polls(1);
    CHECK(fake_net_send(parked, SWAP, (int)strlen(SWAP)) == (int)strlen(SWAP));
    superloop(4, 0);
    char rsp[MPS3_CTRL_RESP_MAX];
    CHECK(fake_net_recv(parked, rsp, sizeof(rsp)) == 0);   /* HELD */
    CHECK(coordinator_held_pending());

    /* The parked client hangs up and a new one connects at once. A parked
     * connection is never read, so without the probe nothing would ever see
     * that EOF until the held verb settled. */
    fake_net_close(parked);
    int next = fake_net_connect(MPS3_PORT_CONTROL);
    polls(1);
#if defined(FAKE_NET_TEST_NO_PEER_PROBE)
    CHECK(fake_net_fw_closed(next));            /* NEGATIVE CONTROL: refused */
    fake_net_close(next);
    superloop(64, 1);       /* the settle's send to the dead client drops it... */
    CHECK(swap_fsm_idle());
    next = fake_net_connect(MPS3_PORT_CONTROL); /* ...only then is the slot free */
    CHECK(exchange(next, PING, PONG_RM1));
    fake_net_close(next);
    polls(2);
#else
    CHECK(!fake_net_fw_closed(next));           /* adopted */
    CHECK(coordinator_held_pending());          /* the swap is still in flight... */
    CHECK(exchange(next, PING, PONG_RM0));      /* ...and the newcomer is served */

    /* The held verb still settles with nobody owed its answer: the newcomer
     * receives NOTHING it did not ask for. */
    superloop(64, 1);
    CHECK(swap_fsm_idle());
    CHECK(!coordinator_held_pending());
    CHECK(fake_net_recv(next, rsp, sizeof(rsp)) == 0);
    CHECK(exchange(next, PING, PONG_RM1));      /* the swap really landed */
    fake_net_close(next);
    polls(2);
#endif
}

static void test_parked_live_client_keeps_the_slot(void)
{
    boot();
    int parked = fake_net_connect(MPS3_PORT_CONTROL);
    polls(1);
    CHECK(fake_net_send(parked, SWAP, (int)strlen(SWAP)) == (int)strlen(SWAP));
    superloop(4, 0);
    CHECK(coordinator_held_pending());

    int other = fake_net_connect(MPS3_PORT_CONTROL);
    polls(1);
    CHECK(fake_net_fw_closed(other));           /* parked but alive: still refused */
    CHECK(!fake_net_fw_closed(parked));
    fake_net_close(other);

    superloop(64, 1);
    CHECK(swap_fsm_idle());
    char rsp[MPS3_CTRL_RESP_MAX];
    int n = fake_net_recv(parked, rsp, (int)sizeof(rsp) - 1);
    CHECK(n > 0);
    rsp[n] = '\0';
    CHECK(strcmp(rsp, SWAP_OK) == 0);           /* the held answer reached its owner */
    fake_net_close(parked);
    polls(2);
}

/* An abortively-dead current client (RST / lwIP keepalive reap) is reaped the
 * same way as a FIN'd one. */
static void test_reset_client_is_reaped(void)
{
    boot();
    int dead = fake_net_connect(MPS3_PORT_CONTROL);
    CHECK(exchange(dead, PING, PONG_RM0));
    fake_net_kill_peer(dead);
    int next = fake_net_connect(MPS3_PORT_CONTROL);
#if defined(FAKE_NET_TEST_NO_PEER_PROBE)
    polls(1);
    CHECK(fake_net_fw_closed(next));
#else
    CHECK(exchange(next, PING, PONG_RM0));
#endif
    fake_net_close(next);
    fake_net_close(dead);
    polls(2);
}

int main(void)
{
    test_back_to_back_200();
    test_second_live_client_refused();
    test_unread_request_is_served_first();
    test_parked_client_that_closed_is_reaped();
    test_parked_live_client_keeps_the_slot();
    test_reset_client_is_reaped();
    printf("%s: %d checks passed\n", CTRL_REAP_TEST_NAME, s_checks);
    return 0;
}
