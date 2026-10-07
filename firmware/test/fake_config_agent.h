/*
 * fake_config_agent.h — test double for the config_agent functions
 * swap_fsm.c and coordinator.c call. Used by test_swap_fsm_hw.c,
 * test_coordinator_dispatch.c and ctrl_echo.c, which link swap_fsm.c (and,
 * for the latter two, coordinator.c) but NOT the real config_agent.c (that
 * module's receive pipeline is a network-facing TODO(A3) that can never
 * actually produce a validated payload without a live TFTP/TCP transfer --
 * see config_agent.c's file header). One-shot arm/consume semantics for
 * the intake pair, matching the real functions' "return 0 once, then go
 * back to -1" shape; config_agent_init()/_poll() are link-satisfying
 * no-ops (their real bodies are pure network plumbing, nothing to fake).
 */
#ifndef MPS3_FAKE_CONFIG_AGENT_H
#define MPS3_FAKE_CONFIG_AGENT_H

#include "../config_agent/config_agent.h"

#ifdef __cplusplus
extern "C" {
#endif

void fake_config_agent_reset(void);
void fake_config_agent_arm_clearing(const config_agent_bitstream_info_t *info);
void fake_config_agent_arm_partial(const config_agent_bitstream_info_t *info);

#ifdef __cplusplus
}
#endif

void fake_config_agent_set_rx_progress(uint32_t got, uint32_t expect);

int fake_config_agent_abort_calls(void);

#endif /* MPS3_FAKE_CONFIG_AGENT_H */
