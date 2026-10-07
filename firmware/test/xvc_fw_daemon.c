/*
 * xvc_fw_daemon.c — run the REAL firmware XVC engine on the host, on a REAL TCP
 * socket, so a REAL external client (the Synopsys Identify debugger) can drive
 * it with no board, no MicroBlaze, no lwIP and no Vivado.
 *
 * NOT a test binary: it waits on a socket rather than self-checking, so it lives
 * in `make -C firmware/test tools` alongside bin/ctrl_echo and
 * bin/ovlstore_pack. The self-checking counterpart is
 * test_xvc_posix_loopback.c, which drives this exact stack from an in-process
 * socket client and IS in $(TESTS).
 *
 * ===========================================================================
 * WHY — what this changes about what is proven
 * ===========================================================================
 * firmware/xvc_server/xvc_server.c built with -DMPS3_XVC_TARGET_SWDBB is the
 * Identify/IICE bit-bang path (docs/planning/IDENTIFY_IICE_DFX_PLAN.md §3b).
 * Until this daemon existed it was proven only by INFERENCE: unit tests
 * (test_xvc_server_swdbb.c, test_xvc_identify_stream.c) push hand-built or
 * previously-captured XVC byte streams through it and check the bits. Correct,
 * and necessary — but the actual vendor client had never spoken to it. The
 * HOST-side Python server (host/socket_harness/xvc_server.py) had, which is how
 * the getinfo-is-a-chunk-hint defect was found; the firmware engine only
 * inherited the fix.
 *
 * With this binary the real `identify_debugger_shell` connects to the real C
 * protocol engine over a real socket. Every byte of framing, the accept
 * ceiling, the command accumulator sizing, the reply back-pressure retry and
 * the 3-accesses-per-JTAG-bit loop are EXECUTED by the tool that will drive
 * them on silicon.
 *
 * ===========================================================================
 * WHAT IS AND IS NOT REAL HERE — read this before quoting a log from it
 * ===========================================================================
 * REAL:  xvc_server.c (the same file the MicroBlaze compiles — never a copy),
 *        the XVC 1.0 wire protocol over a real TCP socket, the POSIX net_if
 *        backend, and the 3n+1 DRIVE/SAMPLE access sequence per JTAG bit.
 * MODEL: what the accesses land on. mock_regs.c's behavioral hook routes the
 *        SWDBB page to fake_jtag_tap.c's in-memory IEEE-1149.1 TAP, which
 *        presents ONE device with a settable IDCODE and an IR of 4 supporting
 *        IDCODE + BYPASS. There is NO Identify IICE behind it, so a debugger
 *        gets through cable check + chain autodetect and then fails at
 *        "Checking Hardware ID" with signature 0x00000000 for the unknown
 *        device. That is the expected and honest stopping point of a board-free
 *        run, exactly as the host server's --fake-tap mode stops.
 * ABSENT: MicroBlaze AXI timing. Locality (the whole reason the SWDBB target
 *        exists) is a property of the real bus, not of this loopback.
 *
 * ===========================================================================
 * USAGE
 * ===========================================================================
 *   make -C firmware/test tools
 *   firmware/test/bin/xvc_fw_daemon --port 2542 &
 *
 *   module load identify/2022.09-SP2
 *   IICE_XVC_PORT=2542 identify_debugger_shell \
 *       -prj <build>/nanosoc_iice.prj -f host/identify/com_check.tcl
 *
 * host/identify/fw_com_check.sh does both halves and gates on the outcome.
 * host/identify/debug_session.tcl needs NO edit to point here: it already takes
 * IICE_XVC_HOST / IICE_XVC_PORT.
 *
 * SINGLE CLIENT, and it matters: xvc_server_poll() serves one connection and
 * close-refuses extras (hw_server/Identify hold one XVC connection per cable).
 * Do NOT probe the port with nc/telnet while a session is live — a bare TCP
 * connect IS the one client.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../xvc_server/xvc_server.h"
#include "../coordinator/coordinator.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "mock_regs.h"
#include "posix_net_if.h"
#include "fake_jtag_tap.h"

/* xvc_server.c reads g_shell_state.xvc_gated only; nothing here ever gates. */
mps3_shell_state_t g_shell_state;

/* The DUT's real JTAG TAP id (host/openocd/nanosoc_mps3_jtag.cfg) — the same
 * default the host server's --fake-tap uses, so a log from this daemon is
 * directly comparable with the host-server run that preceded it. */
#define DEFAULT_IDCODE 0x6BA00477u

static volatile sig_atomic_t s_stop;

static void on_signal(int sig)
{
    (void)sig;
    s_stop = 1;
}

/* 1149.1 state names, for the run log only. */
static const char *tap_state_name(fake_tap_state_t s)
{
    static const char *k[FAKE_TAP_NSTATES] = {
        "Test-Logic-Reset", "Run-Test/Idle", "Select-DR", "Capture-DR",
        "Shift-DR", "Exit1-DR", "Pause-DR", "Exit2-DR", "Update-DR",
        "Select-IR", "Capture-IR", "Shift-IR", "Exit1-IR", "Pause-IR",
        "Exit2-IR", "Update-IR",
    };
    return (s >= 0 && s < FAKE_TAP_NSTATES) ? k[s] : "?";
}

static void usage(const char *argv0)
{
    printf(
        "usage: %s [options]\n"
        "  --port N          TCP port to listen on (default %u = MPS3_PORT_XVC;\n"
        "                    the Identify debugger's OWN default is 57015, so pass\n"
        "                    --port 57015 to serve a client that sets no port;\n"
        "                    0 = kernel-chosen ephemeral, printed on startup)\n"
        "  --idcode 0xN      IDCODE the in-memory TAP presents (default 0x%08X)\n"
        "  --bind-any        bind 0.0.0.0 instead of 127.0.0.1 (off by default:\n"
        "                    this is an unauthenticated JTAG bit-bang)\n"
        "  --max-seconds N   exit after N seconds (0 = run until signalled)\n"
        "  --port-file PATH  write the bound port number to PATH (for --port 0)\n"
        "  --quiet           suppress the per-shift progress lines\n"
        "  --help\n",
        argv0, (unsigned)MPS3_PORT_XVC, (unsigned)DEFAULT_IDCODE);
}

int main(int argc, char **argv)
{
    unsigned    port        = MPS3_PORT_XVC;
    uint32_t    idcode      = DEFAULT_IDCODE;
    int         bind_any    = 0;
    long        max_seconds = 0;
    const char *port_file   = 0;
    int         quiet       = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage(argv[0]);
            return 0;
        } else if (!strcmp(a, "--bind-any")) {
            bind_any = 1;
        } else if (!strcmp(a, "--quiet")) {
            quiet = 1;
        } else if (!strcmp(a, "--port") && i + 1 < argc) {
            port = (unsigned)strtoul(argv[++i], 0, 0);
            if (port > 65535u) {
                fprintf(stderr, "xvc_fw_daemon: --port out of range\n");
                return 2;
            }
        } else if (!strcmp(a, "--idcode") && i + 1 < argc) {
            idcode = (uint32_t)strtoul(argv[++i], 0, 0);
        } else if (!strcmp(a, "--max-seconds") && i + 1 < argc) {
            max_seconds = strtol(argv[++i], 0, 0);
        } else if (!strcmp(a, "--port-file") && i + 1 < argc) {
            port_file = argv[++i];
        } else {
            fprintf(stderr, "xvc_fw_daemon: unknown argument '%s'\n", a);
            usage(argv[0]);
            return 2;
        }
    }

    /* A build without the flag would silently serve the DEBUG BRIDGE target
     * instead — i.e. write LENGTH/TMS/TDI/CTRL at 0x44A8 and spin forever on a
     * GO bit no model clears. Refuse rather than produce a confusing hang. */
    if (!MPS3_XVC_TARGET_IS_SWDBB) {
        fprintf(stderr, "xvc_fw_daemon: built WITHOUT -DMPS3_XVC_TARGET_SWDBB "
                        "-- this tool only makes sense for the SWDBB bit-bang "
                        "target. Refusing to run.\n");
        return 2;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, 0);
    sigaction(SIGTERM, &sa, 0);

    /* ---- the "hardware": one in-memory TAP behind the SWDBB page ---------- */
    static fake_swdbb_t swdbb;
    mock_regs_reset();
    memset(&swdbb, 0, sizeof(swdbb));
    fake_tap_reset(&swdbb.tap, idcode);
    mock_regs_set_hook(MPS3_SWDBB_BASE, fake_swdbb_hook, &swdbb);

    /* ---- the transport: real sockets behind common/net_if.h --------------- */
    posix_net_reset();
    posix_net_set_bind_any(bind_any);
    posix_net_set_log(stdout);
    /* xvc_server_init() binds MPS3_PORT_XVC by name and must not be edited to be
     * testable, so the port choice is expressed as a map. */
    posix_net_map_port(MPS3_PORT_XVC, (uint16_t)port);

    /* ---- the firmware ----------------------------------------------------- */
    xvc_server_init();

    uint16_t bound = posix_net_actual_port(MPS3_PORT_XVC);
    if (bound == 0) {
        fprintf(stderr, "xvc_fw_daemon: could not listen on port %u "
                        "(already in use? another daemon still running?)\n", port);
        return 1;
    }
    if (port_file) {
        FILE *pf = fopen(port_file, "w");
        if (!pf) {
            fprintf(stderr, "xvc_fw_daemon: cannot write --port-file %s: %s\n",
                    port_file, strerror(errno));
            return 1;
        }
        fprintf(pf, "%u\n", (unsigned)bound);
        fclose(pf);
    }

    printf("=== xvc_fw_daemon =================================================\n");
    printf("  REAL   firmware/xvc_server/xvc_server.c  (SWDBB target, the same\n");
    printf("         file the MicroBlaze compiles -- never a copy)\n");
    printf("  REAL   XVC 1.0 over TCP %s:%u   advertised=%u accepted=%u bits\n",
           bind_any ? "0.0.0.0" : "127.0.0.1", (unsigned)bound,
           (unsigned)MPS3_XVC_MAX_VECTOR_BITS,
           (unsigned)MPS3_XVC_ACCEPT_VECTOR_BITS);
    printf("  MODEL  in-memory IEEE-1149.1 TAP, IDCODE 0x%08X, IR len %u\n",
           (unsigned)idcode, (unsigned)FAKE_TAP_IR_LEN);
    printf("         (no Identify IICE behind it -- a debugger will pass cable\n");
    printf("          check and chain autodetect, then report signature\n");
    printf("          0x00000000 at 'Checking Hardware ID'. Expected.)\n");
    printf("  ABSENT MicroBlaze AXI timing -- locality is not measured here.\n");
    printf("  single client; do NOT probe this port with nc/telnet while a\n");
    printf("  session is live (a bare TCP connect IS the one client)\n");
    printf("===================================================================\n");
    fflush(stdout);

    /* ---- the firmware's own superloop, in a plain while() ----------------- */
    time_t   started      = time(0);
    uint32_t last_edges   = 0;
    uint64_t last_act     = posix_net_activity();
    unsigned shift_bursts = 0;

    while (!s_stop) {
        xvc_server_poll();

        uint32_t edges = swdbb.tap.rising_edges;
        uint64_t act   = posix_net_activity();

        if (edges != last_edges) {
            shift_bursts++;
            if (!quiet) {
                printf("[fwxvc] shift: +%u TCK edges (total %u) tap=%s ir=0x%X "
                       "sample_reads=%d stray=%d\n",
                       (unsigned)(edges - last_edges), (unsigned)edges,
                       tap_state_name(swdbb.tap.state), (unsigned)swdbb.tap.ir,
                       swdbb.sample_reads, swdbb.stray);
                fflush(stdout);
            }
            last_edges = edges;
        }

        if (act == last_act && edges == last_edges) {
            /* Nothing happened: yield rather than burn a core. 200 us is four
             * orders of magnitude below the debugger's own 1 ms-per-edge
             * expectation (host/identify/debug_session.tcl IICE_XVC_SPEED_NS). */
            struct timespec ts = { .tv_sec = 0, .tv_nsec = 200000L };
            nanosleep(&ts, 0);
        }
        last_act = act;

        if (max_seconds > 0 && (time(0) - started) >= max_seconds) {
            printf("[fwxvc] --max-seconds %ld reached\n", max_seconds);
            break;
        }
    }

    printf("=== xvc_fw_daemon summary =========================================\n");
    printf("  shift bursts served : %u\n", shift_bursts);
    printf("  TCK rising edges    : %u\n", (unsigned)swdbb.tap.rising_edges);
    printf("  SAMPLE reads        : %d\n", swdbb.sample_reads);
    printf("  DRIVE writes        : %d\n", swdbb.drive_writes);
    printf("  stray accesses      : %d  (must be 0 -- anything outside "
           "DRIVE@0x00 / SAMPLE@0x04)\n", swdbb.stray);
    printf("  final TAP state     : %s   ir=0x%X\n",
           tap_state_name(swdbb.tap.state), (unsigned)swdbb.tap.ir);
    printf("===================================================================\n");
    fflush(stdout);

    /* A stray access means the production code touched an offset swd_bb.sv does
     * not decode — a real defect, so report it in the exit status rather than
     * only in the log. */
    return swdbb.stray == 0 ? 0 : 1;
}
