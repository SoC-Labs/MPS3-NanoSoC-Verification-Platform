#include <string.h>
#include "fake_config_agent.h"

static int s_clearing_armed;
static config_agent_bitstream_info_t s_clearing_info;
static int s_partial_armed;
static config_agent_bitstream_info_t s_partial_info;

void fake_config_agent_reset(void)
{
    s_clearing_armed = 0;
    s_partial_armed = 0;
    memset(&s_clearing_info, 0, sizeof(s_clearing_info));
    memset(&s_partial_info, 0, sizeof(s_partial_info));
}

void fake_config_agent_arm_clearing(const config_agent_bitstream_info_t *info)
{
    s_clearing_info = *info;
    s_clearing_armed = 1;
}

void fake_config_agent_arm_partial(const config_agent_bitstream_info_t *info)
{
    s_partial_info = *info;
    s_partial_armed = 1;
}

int config_agent_take_validated_clearing(config_agent_bitstream_info_t *info_out)
{
    if (!s_clearing_armed) {
        return -1;
    }
    *info_out = s_clearing_info;
    s_clearing_armed = 0;
    return 0;
}

int config_agent_take_validated_partial(config_agent_bitstream_info_t *info_out)
{
    if (!s_partial_armed) {
        return -1;
    }
    *info_out = s_partial_info;
    s_partial_armed = 0;
    return 0;
}

/* Link-satisfying no-ops for coordinator.c's init/main-loop calls -- the
 * real bodies are the network receive plumbing (config_agent.c, real as of
 * W-NET-SEAM, but network-driven); tests inject "received" payloads via
 * the arm_* functions above instead. */
void config_agent_init(void)
{
}

void config_agent_set_running_static_id(uint32_t static_id)
{
    (void)static_id;
}

/* Link-satisfying no-op for coordinator_init()'s QSPI-sink registration --
 * these dispatch-level tests inject "received" payloads via the arm_*
 * functions, so the real large-partial->QSPI staging path is never driven. */
void config_agent_set_qspi_sink(const mps3_cfg_agent_qspi_sink_t *sink)
{
    (void)sink;
}

/* Symmetric no-op for coordinator_init()'s clearing-sink registration --
 * these dispatch-level tests inject "received" payloads via the arm_*
 * functions, so the real large-clearing->QSPI staging path is never driven. */
void config_agent_set_qspi_clearing_sink(const mps3_cfg_agent_qspi_sink_t *sink)
{
    (void)sink;
}

void config_agent_poll(void)
{
}

/* ---- RX progress (drives swap_fsm's fail-IDLE timer) --------------------- */
static uint32_t s_fake_rx_got;
static uint32_t s_fake_rx_expect;

void config_agent_rx_progress(uint32_t *got, uint32_t *expect)
{
    if (got)    { *got    = s_fake_rx_got; }
    if (expect) { *expect = s_fake_rx_expect; }
}

/* Tests use this to simulate a transfer that IS progressing, which must re-arm
 * the AWAIT_* idle timeout rather than let it fire. */
void fake_config_agent_set_rx_progress(uint32_t got, uint32_t expect)
{
    s_fake_rx_got    = got;
    s_fake_rx_expect = expect;
}

/* ---- session abort (swap failure must free the single push session) ------ */
static int s_fake_abort_calls;

void config_agent_abort_session(void)
{
    s_fake_abort_calls++;
    s_fake_rx_got = 0;
    s_fake_rx_expect = 0;
}

int fake_config_agent_abort_calls(void) { return s_fake_abort_calls; }
