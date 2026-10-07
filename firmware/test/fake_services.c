/*
 * fake_services.c — link-satisfying no-op _init()/_poll() bodies for the
 * three port servers coordinator.c references (swd_server, xvc_server,
 * uart_over_eth). Used by test_coordinator_dispatch.c and ctrl_echo.c,
 * which link the REAL coordinator.c: its init/main-loop call these
 * symbols, but the control-channel dispatch under test never goes through
 * them, and the real bodies are lwIP-facing TODO(A3) stubs anyway (see
 * each module's README). Same pattern as fake_config_agent.c /
 * fake_overlay_store.c — one fake per real module, no behavioral content
 * here at all.
 */
#include "../swd_server/swd_server.h"
#include "../jtag_server/jtag_server.h"
#include "../xvc_server/xvc_server.h"
#include "../uart_over_eth/uart_over_eth.h"

/* swd_server_* kept (harmless) post-JTAG cutover; coordinator.c now calls
 * jtag_server_init instead — hence the jtag_server stubs below. */
void swd_server_init(void)
{
}

void swd_server_poll(void)
{
}

void jtag_server_init(void)
{
}

void jtag_server_poll(void)
{
}

void xvc_server_init(void)
{
}

void xvc_server_poll(void)
{
}

void uart_over_eth_init(void)
{
}

void uart_over_eth_poll(void)
{
}
