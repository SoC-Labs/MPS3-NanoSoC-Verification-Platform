# firmware/test/Makefile — host-gcc unit/integration test harness.
#
# NOT part of the MicroBlaze/Vitis build (see firmware/README.md's "Build
# model" -- that toolchain is untouched by anything here). This builds and
# RUNS a handful of small, independent test binaries with the system gcc,
# each compiling the real firmware/ source files listed in its recipe
# (never a copy) plus a small assert-based test file. `make test` (or plain
# `make`) is the self-check: it must both build and pass for this
# deliverable to be considered done.
#
# Two flavours of test binary:
#   - pure-logic units (crc32, bitstream header, ovlstore header, the
#     swap-FSM transition table, config_agent's validation/CRC logic): no
#     -DMPS3_HAL_MOCK needed at all -- these files have zero register
#     dependency by construction (see each file's header comment).
#   - test_swap_fsm_hw / test_coordinator_dispatch: binaries that exercise
#     *impure* module code (swap_fsm.c's step_*() functions; coordinator.c's
#     full line-in->response-out dispatch), built with -DMPS3_HAL_MOCK so
#     platform_regs.h's mps3_reg_*32 accessors resolve to mock_regs.c's
#     in-memory register file instead of real MMIO -- see platform_regs.h's
#     HAL-split comment.
#
# One extra NON-test binary: bin/ctrl_echo (`make tools`), the C half of
# the cross-language golden test -- tests/firmware_logic/test_json_golden.py
# builds it explicitly and drives it over stdin/stdout with pyverify's real
# ShellClient. Not in $(TESTS) because it waits on stdin rather than
# self-checking.

CC      ?= gcc
# -Werror: this harness is the ONLY place most of firmware/ is compiled by a
# compiler whose output a human reads promptly -- the target build's feedback
# channel is a board. Three warnings had been sitting in the log for weeks
# (clcd.c's nested "/*", diag.c's volatile-discarding memset, an unused
# variable here) precisely because nothing failed on them. The tree is
# warning-free as of this flag; keeping it that way is what the flag buys.
# Overridable (CFLAGS is ?=) for a one-off bisect.
CFLAGS  ?= -std=c11 -Wall -Wextra -Wno-unused-parameter -Werror -g -O0
COMMON  := ../common
BIN     := bin

TESTS := test_crc32 \
         test_bitstream_header \
         test_ovlstore_header \
         test_net_proto_json \
         test_swap_fsm_transitions \
         test_config_agent \
         test_diag \
         test_service \
         test_net_if_lwip \
         test_net_if_lwip_tx \
         test_clcd \
         test_clcd_drain \
         test_clcd_engine \
         test_clcd_kvm \
         test_clcd_usd \
         test_clcd_golden \
         test_clcd_roles \
         test_touch \
         test_touch_kvm \
         test_swap_fsm_hw \
         test_swap_fsm_faults \
         test_hwicap_byte_lane \
         test_hwicap_writer \
         test_hwicap_writer_fifo \
         test_coordinator_dispatch \
         test_display_dispatch \
         test_dutrx_dispatch \
         test_macgen \
         test_net_linebuf \
         test_config_agent_net \
         test_config_agent_windowed \
         test_config_agent_keepalive \
         test_uart_over_eth \
         test_uart_over_eth_linux \
         test_uart_over_eth_baremetal \
         test_swd_server \
         test_jtag_server \
         test_swap_e2e_net \
         test_ctrl_reap \
         test_ctrl_reap_noprobe \
         test_swap_qspi_free \
         test_swap_qspi_free_tinyarena \
         test_swap_icap_direct \
         test_swap_icap_defer \
         test_tftp_large_swap \
         test_usd \
         test_usd_tinybudget \
         test_ovlstore_sd \
         test_ovlstore_sd_tiny \
         test_config_agent_commit \
         test_config_agent_commit_windowed \
         test_usd_dispatch \
         test_usd_boot \
         test_smsc911x \
         test_smsc911x_spin \
         test_xvc_server \
         test_xvc_server_swdbb \
         test_xvc_server_swdbb_latesample \
         test_xvc_server_swdbb_stretch \
         test_xvc_identify_stream \
         test_xvc_identify_stream_ratio1 \
         test_posix_net_if \
         test_posix_net_if_nodrain \
         test_xvc_posix_loopback \
         test_xvc_posix_loopback_latesample \
         test_xvc_jtagbb_chain \
         test_xvc_jtagbb_chain_latesample \
         test_version \
         test_log_ring \
         test_v011_dispatch \
         test_touch_cal_dispatch \
         test_slot_dispatch \
         test_slot_dispatch_provider \
         test_config_agent_slot \
         test_config_agent_slot_provider

.PHONY: all test run-tests tools clean coverage

all: test tools

$(BIN):
	mkdir -p $(BIN)

# ---- pure-logic units, no HAL mock needed --------------------------------

$(BIN)/test_crc32: test_crc32.c $(COMMON)/crc32.c $(COMMON)/crc32.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_crc32.c $(COMMON)/crc32.c

$(BIN)/test_bitstream_header: test_bitstream_header.c $(COMMON)/net_proto.c $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_bitstream_header.c $(COMMON)/net_proto.c

$(BIN)/test_net_proto_json: test_net_proto_json.c $(COMMON)/net_proto.c $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_net_proto_json.c $(COMMON)/net_proto.c

$(BIN)/test_ovlstore_header: test_ovlstore_header.c ../overlay_store/ovlstore_codec.c ../overlay_store/ovlstore_codec.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_ovlstore_header.c ../overlay_store/ovlstore_codec.c

$(BIN)/test_swap_fsm_transitions: test_swap_fsm_transitions.c ../coordinator/swap_fsm_transitions.c ../coordinator/swap_fsm.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_swap_fsm_transitions.c ../coordinator/swap_fsm_transitions.c

$(BIN)/test_config_agent: test_config_agent.c ../config_agent/config_agent.c ../config_agent/config_agent.h $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_config_agent.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c

$(BIN)/test_net_linebuf: test_net_linebuf.c $(COMMON)/net_if.c $(COMMON)/net_if.h fake_net_if.c fake_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_net_linebuf.c $(COMMON)/net_if.c fake_net_if.c

# Harness-version seam (VERSION -> HARNESS_VER32). Links ONLY the committed WEAK
# fallback TU and NOT firmware/platform/generated/mps3_version.c -- that
# omission is the point: the seam must link with no generated identity present,
# which is precisely the situation every binary in this harness (and a fresh
# clone) is in. Zero register/HAL dependency, so no -DMPS3_HAL_MOCK.
$(BIN)/test_version: test_version.c ../platform/mps3_version_weak.c \
                     ../platform/mps3_version.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_version.c ../platform/mps3_version_weak.c

# Links the platform strong override (ovlstore_phase.c) as well as diag.c so the
# v6 QSPI-phase hook is exercised against the REAL override, not a stand-in. The
# override TU has zero Xilinx/register dependency (see its header), so it builds
# host-side unchanged; it is the same source main.c folds into the firmware.
$(BIN)/test_diag: test_diag.c $(COMMON)/diag.c $(COMMON)/diag.h \
                  ../platform/src/ovlstore_phase.c ../platform/src/ovlstore_phase.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_diag.c $(COMMON)/diag.c ../platform/src/ovlstore_phase.c

# The superloop's time discipline: common/service.c's bounded wait
# (mps3_spin_until) and the service table's budget / sick-service policy.
# mock_regs.c is here for its FAKE CLOCK, not for its registers -- service.c
# touches no register at all, which is exactly why the policy is testable.
$(BIN)/test_service: test_service.c $(COMMON)/service.c $(COMMON)/service.h \
                     mock_regs.c mock_regs.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_service.c $(COMMON)/service.c mock_regs.c

# firmware/platform/src/net_if_lwip.c on host gcc -- the REAL file (each test TU
# #includes it), compiled against fake_lwip/, a directory of lwIP-shaped shim
# headers plus fake_lwip.c's behavioural model (a counted pbuf pool, a tcp_recved
# recorder, a settable sndbuf). 799 lines, the file the 6910 receive-path
# incident lived in, and it had ZERO host tests until now.
#
#   test_net_if_lwip     the rx ring / window / rx_poll budget / udp ring /
#                        accept ring -- pure seam behaviour, no registers.
#   test_net_if_lwip_tx  lan_linkoutput against the REAL smsc911x.c driver over
#                        mock_regs + fake_lan9220, including a DELIBERATELY STUCK
#                        TX-space register. That one asserts a DURATION (the
#                        MPS3_TX_SPACE_TIMEOUT_US bound), which is only testable
#                        because mock_regs_set_us_per_read() advances the fake
#                        clock per register read.
#
# -I fake_lwip must come BEFORE anything else so `#include "lwip/tcp.h"` resolves
# to the shim; -DMPS3_HAL_MOCK is for smsc911x.c's register accessors.
NET_IF_LWIP_SRCS := fake_lwip.c ../smsc911x/smsc911x.c $(COMMON)/service.c \
                    mock_regs.c fake_lan9220.c
NET_IF_LWIP_DEPS := ../platform/src/net_if_lwip.c ../platform/src/net_if_lwip.h \
                    $(COMMON)/service.h fake_lwip/lwip/tcp.h fake_lwip/fake_lwip_ctl.h

$(BIN)/test_net_if_lwip: test_net_if_lwip.c $(NET_IF_LWIP_SRCS) $(NET_IF_LWIP_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -I fake_lwip -o $@ \
		test_net_if_lwip.c $(NET_IF_LWIP_SRCS)

$(BIN)/test_net_if_lwip_tx: test_net_if_lwip_tx.c $(NET_IF_LWIP_SRCS) $(NET_IF_LWIP_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -I fake_lwip -o $@ \
		test_net_if_lwip_tx.c $(NET_IF_LWIP_SRCS)

# CLCD status-display driver (firmware/clcd/clcd.c) against mock registers +
# a behavioural FIFO model in the test. Links the REAL landed panel init table
# (../clcd/hx8347_init.c, a sibling deliverable -- read-only here) so the
# no-loss test proves the driver streams the actual table faithfully.
# -DMPS3_HAS_CLCD exercises the gated code; -DMPS3_CLCD_TEST_HOOKS exposes the
# byte-accounting / state introspection the byte-bound proof needs.
$(BIN)/test_clcd: test_clcd.c ../clcd/clcd.c ../clcd/hx8347_init.c \
                  ../clcd/clcd.h ../clcd/font8x16.h ../clcd/hx8347_init.h \
                  mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS -o $@ \
		test_clcd.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c

# THE DRAIN (lane CLCD-SLOW, clcd.h clcd_poll_drain()): the harnessd-side fix for
# "the Linux panel repaints glyph by glyph". The SAME link set as test_clcd; the
# test models a harnessd pass on mock_regs' fake clock and A/B-compares one
# clcd_poll() a pass (today, the negative control) with clcd_poll_drain(4 ms).
$(BIN)/test_clcd_drain: test_clcd_drain.c ../clcd/clcd.c ../clcd/hx8347_init.c \
                        ../clcd/clcd.h ../clcd/font8x16.h ../clcd/hx8347_init.h \
                        mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS -o $@ \
		test_clcd_drain.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c

# The engine row (row 12) with a provider, as mps3-harnessd has it: the SAME
# test source and link set as test_clcd, plus -DTEST_CLCD_ENGINE (a strong
# mps3_clcd_engine()). test_clcd itself is the bare-metal half: no provider, so
# row 12 must render exactly as before.
$(BIN)/test_clcd_engine: test_clcd.c ../clcd/clcd.c ../clcd/hx8347_init.c \
                         ../clcd/clcd.h ../clcd/font8x16.h ../clcd/hx8347_init.h \
                         mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS -DTEST_CLCD_ENGINE -o $@ \
		test_clcd.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c

# CLCD KVM handover: the SAME real clcd.c as test_clcd, now built WITH
# -DMPS3_HAS_CLCD_KVM so the handover state machine and firmware/clcd_kvm/
# clcd_kvm.c are compiled and linked. The test models both the CLCD FIFO and the
# KVM CSR block (src_sel_we gating + W1C EVENT) over mock_regs. This is the only
# binary that defines MPS3_HAS_CLCD_KVM.
$(BIN)/test_clcd_kvm: test_clcd_kvm.c ../clcd/clcd.c ../clcd/hx8347_init.c \
                      ../clcd_kvm/clcd_kvm.c \
                      ../clcd/clcd.h ../clcd/font8x16.h ../clcd/hx8347_init.h \
                      ../clcd_kvm/clcd_kvm.h mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_CLCD_KVM \
		-DMPS3_CLCD_TEST_HOOKS -o $@ \
		test_clcd_kvm.c ../clcd/clcd.c ../clcd/hx8347_init.c \
		../clcd_kvm/clcd_kvm.c mock_regs.c

# CLCD row 4 "USD : <text>" (lane I-CLCD): the SAME real clcd.c, with the
# overlay store's two exports supplied by the test (strong over clcd.c's weak
# fallbacks), so every provider text renders inside col 40.
$(BIN)/test_clcd_usd: test_clcd_usd.c ../clcd/clcd.c ../clcd/hx8347_init.c ../clcd/clcd.h ../clcd/font8x16.h ../clcd/hx8347_init.h mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS -o $@ test_clcd_usd.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c

# THE ALIGNED FRONT PANEL (Harness Manager CLCD_ALIGNMENT R4/R5 + the clcd.c half
# of R2): one test source, two builds. test_clcd_golden links NO seam provider --
# the bare-metal image -- and holds the default theme to the bus bytes and pixels
# captured from the pre-R4 clcd.c (bb69f61); test_clcd_roles supplies every seam
# (theme, badge, session row, overlay, locate, swap progress) as mps3-harnessd
# does and tests roles, glyphs, the progress row/bar, the page-aware hit test,
# the event ring and frame access, with its negative controls.
CLCD_ROLE_DEPS := test_clcd_roles.c ../clcd/clcd.c ../clcd/hx8347_init.c ../clcd/clcd.h \
                  ../clcd/clcd_palette.h ../clcd/font8x16.h ../clcd/clcd_glyphs.h \
                  ../clcd/hx8347_init.h mock_regs.c
$(BIN)/test_clcd_golden: $(CLCD_ROLE_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS -DTEST_GOLDEN_ONLY -o $@ \
		test_clcd_roles.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c
$(BIN)/test_clcd_roles: $(CLCD_ROLE_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_CLCD_TEST_HOOKS -o $@ \
		test_clcd_roles.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c

# Phase-2 resistive-touch driver (firmware/touch/touch.c) against a mock AXI IIC
# (PG090) modelled in the test. Links the REAL clcd.c (for clcd_hittest) + its
# panel init table + mock_regs, exactly the link set test_clcd uses, plus touch.c.
# -DMPS3_HAS_CLCD/-DMPS3_HAS_TOUCH keep the build in parity with the shipping
# gated code; -DMPS3_TOUCH_TEST_HOOKS exposes the direct AXI-IIC read + the last
# mapped pixel the assertions need.
$(BIN)/test_touch: test_touch.c ../touch/touch.c ../clcd/clcd.c ../clcd/hx8347_init.c \
                   ../touch/touch.h ../clcd/clcd.h ../clcd/font8x16.h \
                   ../clcd/hx8347_init.h mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_TOUCH \
		-DMPS3_TOUCH_TEST_HOOKS -o $@ \
		test_touch.c ../touch/touch.c ../clcd/clcd.c ../clcd/hx8347_init.c mock_regs.c

# The same touch test on a CLCD_KVM build (lane I-CLCD).
$(BIN)/test_touch_kvm: test_touch.c ../touch/touch.c ../clcd/clcd.c ../clcd/hx8347_init.c ../clcd_kvm/clcd_kvm.c ../touch/touch.h ../clcd/clcd.h ../clcd/font8x16.h ../clcd/hx8347_init.h ../clcd_kvm/clcd_kvm.h mock_regs.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_TOUCH -DMPS3_HAS_CLCD_KVM -DMPS3_TOUCH_TEST_HOOKS -o $@ test_touch.c ../touch/touch.c ../clcd/clcd.c ../clcd/hx8347_init.c ../clcd_kvm/clcd_kvm.c mock_regs.c

$(BIN)/test_config_agent_net: test_config_agent_net.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_config_agent_net.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c

# Dead-client keepalive reap (the "dead client wedges the shell" closure): a
# peer reaped by lwIP TCP keepalive surfaces at the seam as MPS3_NET_ERR
# (fake_net_kill_peer -> abortive close), and config_agent must RELEASE its 6910
# session so the port is usable again. Same link set as test_config_agent_net.
$(BIN)/test_config_agent_keepalive: test_config_agent_keepalive.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_config_agent_keepalive.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c

# App-level WINDOWED flow control on 6910 (the HW-pivot primary fix): REAL
# config_agent.c built with the flag on + a small (1 KiB) ack window so the
# multi-window/final-short-window grant handshake is exercised with tiny frames.
# -DMPS3_CFG_AGENT_STAGING_BYTES=512 makes "large" mean ">512 B" so a modest test
# frame routes through a registered SINK (the test's fake ICAP/QSPI sink) — that
# is what lets the test prove the load-bearing point: the grant trails the sink
# DRAIN (write/finish), not the bare recv. Payloads <512 B still exercise the RAM
# fast path. The default (flag-off) fire-hose path stays covered by
# test_config_agent_net.
$(BIN)/test_config_agent_windowed: test_config_agent_windowed.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_CFG_AGENT_WINDOWED -DCFG_AGENT_ACK_WINDOW_BYTES=1024 -DMPS3_CFG_AGENT_STAGING_BYTES=512 -o $@ \
		test_config_agent_windowed.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c

# ---- impure module exercised against mock register memory ---------------

$(BIN)/test_swap_fsm_hw: test_swap_fsm_hw.c \
                         ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                         $(COMMON)/hwicap_writer.c \
                         mock_regs.c fake_config_agent.c fake_overlay_store.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ \
		test_swap_fsm_hw.c \
		../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
		$(COMMON)/hwicap_writer.c \
		mock_regs.c fake_config_agent.c fake_overlay_store.c

# Same link set as test_swap_fsm_hw, exercising the fail-OPEN/fail-STUCK
# closures (stuck HWICAP -> SWAP_FAILED; DECOUPLE/RELEASE confirm timeouts).
$(BIN)/test_swap_fsm_faults: test_swap_fsm_faults.c \
                         ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                         $(COMMON)/hwicap_writer.c \
                         mock_regs.c fake_config_agent.c fake_overlay_store.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ \
		test_swap_fsm_faults.c \
		../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
		$(COMMON)/hwicap_writer.c \
		mock_regs.c fake_config_agent.c fake_overlay_store.c

# ICAP byte-lane order (R3/I18): forces -DMPS3_HWICAP_MSB_FIRST=1 so the REAL
# target big-endian packing is exercised on the host through the real writer.
$(BIN)/test_hwicap_byte_lane: test_hwicap_byte_lane.c \
                         ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                         $(COMMON)/hwicap_writer.c \
                         mock_regs.c fake_config_agent.c fake_overlay_store.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HWICAP_MSB_FIRST=1 -o $@ \
		test_hwicap_byte_lane.c \
		../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
		$(COMMON)/hwicap_writer.c \
		mock_regs.c fake_config_agent.c fake_overlay_store.c

# Shared link set for the binaries that pull in the REAL coordinator.c
# (dispatch + handlers + the coordinator_net.c 6900 listener): real codec,
# real clkrst, real swap FSM; mocks/fakes for registers, the network
# backend (fake_net_if.c behind common/net_if.h) and the other
# network-facing modules (see fake_services.c).
# ../platform/mps3_version_weak.c is in the link set (and the GENERATED strong
# override deliberately is NOT) because coordinator_handle_version() reads the
# mps3_harness_*() seam. Linking only the weak TU is the point: these binaries
# then report the honest "not provisioned" identity (0.0.0 / ver32 0 / sha
# "unknown" / not dirty), which is a FIXED, scenario-free answer -- so the
# golden and conformance suites can pin the `version` line byte-for-byte
# without depending on whatever commit the tree happens to sit on.
#
# ../platform/mps3_usr_access.c is the OTHER half of that verb (2026-09-14):
# the read of USRACC, the fabric's own build identity. It is the one register
# access in `version`, and it is in the link set for the same reason as
# everything else here -- the REAL function runs, against mock register memory.
# Its default answer with an un-poked register file is "no sensor", so every
# binary that does not deliberately seed USRACC keeps reporting
# `"usr_access":null,"skew":null` and the golden/conformance suites are
# unchanged. test_coordinator_dispatch seeds it and asserts both outcomes.
DISPATCH_SRCS := ../coordinator/coordinator.c ../coordinator/coordinator_net.c \
                 ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                 $(COMMON)/hwicap_writer.c \
                 ../clkrst/clkrst.c $(COMMON)/net_proto.c $(COMMON)/net_if.c $(COMMON)/diag.c \
                 $(COMMON)/log_ring.c $(COMMON)/service.c \
                 ../platform/mps3_version_weak.c ../platform/mps3_usr_access.c \
                 mock_regs.c fake_net_if.c fake_config_agent.c fake_overlay_store.c fake_services.c

$(BIN)/test_coordinator_dispatch: test_coordinator_dispatch.c $(DISPATCH_SRCS) \
                                  ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_coordinator_dispatch.c $(DISPATCH_SRCS)

# Reap before refuse (2026-09-28): the 6900 single-client slot under close-then-
# reconnect, parked (held-verb) clients, and a second live client -- the SAME
# dispatch link set. The _noprobe twin is the NEGATIVE CONTROL: fake_net_if.c
# built without its mps3_net_peer_closed(), so net_if.c's WEAK "presumed alive"
# default links -- the pre-fix behaviour -- and the test asserts the refusals.
$(BIN)/test_ctrl_reap: test_ctrl_reap.c $(DISPATCH_SRCS) \
                       ../coordinator/coordinator.h $(COMMON)/net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_ctrl_reap.c $(DISPATCH_SRCS)

$(BIN)/test_ctrl_reap_noprobe: test_ctrl_reap.c $(DISPATCH_SRCS) \
                               ../coordinator/coordinator.h $(COMMON)/net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DFAKE_NET_TEST_NO_PEER_PROBE \
		-DCTRL_REAP_TEST_NAME='"test_ctrl_reap_noprobe"' -o $@ test_ctrl_reap.c $(DISPATCH_SRCS)

# The `display` verb on a build that HAS the CLCD KVM slave: the SAME real
# coordinator link set as test_coordinator_dispatch PLUS firmware/clcd_kvm/
# clcd_kvm.c, built WITH -DMPS3_HAS_CLCD_KVM so coordinator_handle_display()
# drives the real button-safe CSR helpers against the mock CLCDKVM page. The
# OFF-build ("clcd_kvm not present") path is covered inside
# test_coordinator_dispatch itself, which is built WITHOUT the flag.
$(BIN)/test_display_dispatch: test_display_dispatch.c $(DISPATCH_SRCS) \
                              ../clcd_kvm/clcd_kvm.c ../clcd_kvm/clcd_kvm.h \
                              ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD_KVM -o $@ \
		test_display_dispatch.c $(DISPATCH_SRCS) ../clcd_kvm/clcd_kvm.c

# The `dutrx` verb on a build that HAS the DUT-egress capture block: the SAME
# real coordinator link set as test_coordinator_dispatch, built WITH
# -DMPS3_HAS_DUT_EGRESS so coordinator_handle_dutrx() drains a BEHAVIOURAL
# DUTEGR model (a mock_regs hook that lives inside the test file -- DATA is a
# destructive read, which a slot array cannot model). The OFF-build
# ("dut_egress not present") path is covered inside test_coordinator_dispatch
# itself, which is built WITHOUT the flag -- the same split as display.
$(BIN)/test_dutrx_dispatch: test_dutrx_dispatch.c $(DISPATCH_SRCS) \
                            ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_DUT_EGRESS -o $@ \
		test_dutrx_dispatch.c $(DISPATCH_SRCS)

# net-protocol v0.11 verbs (stats / log / reboot) + the v0.11 version.features
# bits, through the REAL dispatcher and codec. Same link set as
# test_coordinator_dispatch, built WITHOUT -DMPS3_HAS_TOUCH so touch_cal's
# OFF-build decline is covered here.
$(BIN)/test_v011_dispatch: test_v011_dispatch.c $(DISPATCH_SRCS) \
                           ../coordinator/coordinator.h $(COMMON)/net_proto.h \
                           $(COMMON)/log_ring.h $(COMMON)/service.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_v011_dispatch.c $(DISPATCH_SRCS)

# slot (v0.14): the SAME dispatch link set, twice -- with no provider (the
# bare-metal image: every act declines "slot not supported") and with a strong
# test provider (-DTEST_SLOT_PROVIDER: the reply bytes, the no-card shape, the
# fail-closed encode, the worst-case line).
$(BIN)/test_slot_dispatch: test_slot_dispatch.c $(DISPATCH_SRCS) \
                           ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_slot_dispatch.c $(DISPATCH_SRCS)
$(BIN)/test_slot_dispatch_provider: test_slot_dispatch.c $(DISPATCH_SRCS) \
                                    ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DTEST_SLOT_PROVIDER -o $@ test_slot_dispatch.c $(DISPATCH_SRCS)

# The v0.14 slot-image push kind through config_agent's REAL 6910/TFTP sessions
# (the test_config_agent_net link set): refused as an unknown kind with no
# provider; streamed verbatim to a recording sink with one.
CA_SLOT_SRCS := ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c \
                $(COMMON)/net_if.c fake_net_if.c
$(BIN)/test_config_agent_slot: test_config_agent_slot.c $(CA_SLOT_SRCS) \
                               ../config_agent/config_agent.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_config_agent_slot.c $(CA_SLOT_SRCS)
$(BIN)/test_config_agent_slot_provider: test_config_agent_slot.c $(CA_SLOT_SRCS) \
                                        ../config_agent/config_agent.h | $(BIN)
	$(CC) $(CFLAGS) -DTEST_SLOT_PROVIDER -o $@ test_config_agent_slot.c $(CA_SLOT_SRCS)

# touch_cal (v0.11) on a TOUCH=1 build: the SAME dispatch link set plus the REAL
# touch driver and the clcd.c it hit-tests against, with the MPS3_HAS_TOUCH /
# MPS3_HAS_CLCD defines the PRODUCT=1 image uses. The test supplies the LAN9220
# / MAC stubs clcd.c's status page links against.
$(BIN)/test_touch_cal_dispatch: test_touch_cal_dispatch.c $(DISPATCH_SRCS) \
                                ../touch/touch.c ../touch/touch.h \
                                ../clcd/clcd.c ../clcd/hx8347_init.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_TOUCH -o $@ \
		test_touch_cal_dispatch.c $(DISPATCH_SRCS) \
		../touch/touch.c ../clcd/clcd.c ../clcd/hx8347_init.c

# The console log ring behind `log` (v0.11): wrap-around, the dropped count, the
# chunk reader -- plus the codec's log reply at full chunk width against
# MPS3_CTRL_RESP_MAX. Pure logic: log_ring.c + net_proto.c, no HAL.
$(BIN)/test_log_ring: test_log_ring.c $(COMMON)/log_ring.c $(COMMON)/log_ring.h \
                      $(COMMON)/net_proto.c $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ test_log_ring.c $(COMMON)/log_ring.c $(COMMON)/net_proto.c

# macgen (I10 tail): same real coordinator link set, exercising the GENCHK
# control verb's CTRL/INJECT drive + TX/RX/ERR_CNT readback against mock regs.
$(BIN)/test_macgen: test_macgen.c $(DISPATCH_SRCS) \
                    ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_macgen.c $(DISPATCH_SRCS)

# ---- W-NET-SEAM: real poll bodies against the seam + mock registers -------

$(BIN)/test_uart_over_eth: test_uart_over_eth.c ../uart_over_eth/uart_over_eth.c \
                           $(COMMON)/net_if.c mock_regs.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_uart_over_eth.c \
		../uart_over_eth/uart_over_eth.c $(COMMON)/net_if.c mock_regs.c fake_net_if.c

# The console relay's Linux policy (mps3-harnessd: -DMPS3_UART_LINUX_POLICY --
# drain-and-drop with no client, flush on every swap ungate edge, optional input
# pacing). ONE source, twice: with the flag (the policy) and without it (the
# frozen bare-metal relay, where the same scenarios reproduce the two silicon
# failures -- the negative control).
UOE_SRCS := ../uart_over_eth/uart_over_eth.c $(COMMON)/net_if.c mock_regs.c fake_net_if.c
$(BIN)/test_uart_over_eth_linux: test_uart_over_eth_linux.c $(UOE_SRCS) \
                                 ../uart_over_eth/uart_over_eth.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_UART_LINUX_POLICY -o $@ \
		test_uart_over_eth_linux.c $(UOE_SRCS)
$(BIN)/test_uart_over_eth_baremetal: test_uart_over_eth_linux.c $(UOE_SRCS) \
                                     ../uart_over_eth/uart_over_eth.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_uart_over_eth_linux.c $(UOE_SRCS)

$(BIN)/test_swd_server: test_swd_server.c ../swd_server/swd_server.c \
                        $(COMMON)/net_if.c mock_regs.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_swd_server.c \
		../swd_server/swd_server.c $(COMMON)/net_if.c mock_regs.c fake_net_if.c

$(BIN)/test_jtag_server: test_jtag_server.c ../jtag_server/jtag_server.c \
                        $(COMMON)/net_if.c mock_regs.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_jtag_server.c \
		../jtag_server/jtag_server.c $(COMMON)/net_if.c mock_regs.c fake_net_if.c

# W-VITIS: xvc_server's real XVC protocol engine vs a behavioral DBGBR fake
# (LENGTH/TMS/TDI/TDO/CTRL hook lives inside the test file itself). Built
# WITHOUT -DMPS3_XVC_TARGET_SWDBB, i.e. at the DEFAULT target -- so this binary
# is also the regression that the shipped Debug-Bridge path still works after
# the SWDBB retarget below.

# The three DBGBR-target binaries (this one and the two identify-stream ones)
# additionally link common/service.c: the per-chunk GO wait is mps3_spin_until()
# now, not an iteration count. The SWDBB/JTAGBB binaries below do not need it --
# shift_dbgbr() is #ifdef'd out of those builds entirely.
$(BIN)/test_xvc_server: test_xvc_server.c ../xvc_server/xvc_server.c \
                        ../xvc_server/xvc_server.h $(COMMON)/service.h \
                        $(COMMON)/net_if.c $(COMMON)/service.c \
                        mock_regs.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_xvc_server.c \
		../xvc_server/xvc_server.c $(COMMON)/net_if.c $(COMMON)/service.c \
		mock_regs.c fake_net_if.c

# The SAME real xvc_server.c retargeted to the SWDBB pin-wiggler
# (-DMPS3_XVC_TARGET_SWDBB), the Identify/IICE path of
# docs/planning/IDENTIFY_IICE_DFX_PLAN.md §3b. THREE binaries from one source,
# because the behaviour under test is compile-time and so cannot be a runtime
# branch (same pattern as test_swap_qspi_free / _tinyarena):
#
#   test_xvc_server_swdbb             positive: an IEEE-1149.1 IDCODE read
#                                     comes back through the real bit-bang loop
#   test_xvc_server_swdbb_latesample  NEGATIVE CONTROL: identical source with
#                                     -DMPS3_XVC_SWDBB_SAMPLE_LATE=1 moves the
#                                     SAMPLE read after the rising edge; it
#                                     asserts the damage (IDCODE >> 1). If the
#                                     shipped sampling phase were ever
#                                     inverted, this binary would pass and the
#                                     positive one would fail -- which is the
#                                     whole point of building both.
#   test_xvc_server_swdbb_stretch     the TCK half-period stretch knob
#                                     (-DMPS3_XVC_SWDBB_TCK_STRETCH=2): extra
#                                     idempotent DRIVE re-writes must lengthen
#                                     the period WITHOUT adding a TCK edge, and
#                                     must return the identical TDO.
#
# The IEEE-1149.1 TAP + behavioral SWDBB register model is fake_jtag_tap.c, NOT
# part of this test file: xvc_fw_daemon (`make tools`) and the real-socket
# loopback suite link the very same model, so a model fix lands everywhere at
# once. It is compiled with the same flags for every consumer and is
# deliberately independent of both knobs below (see its header).
XVC_SWDBB_SRCS := ../xvc_server/xvc_server.c $(COMMON)/net_if.c mock_regs.c \
                  fake_net_if.c fake_jtag_tap.c
XVC_SWDBB_DEFS := -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_SWDBB

$(BIN)/test_xvc_server_swdbb: test_xvc_server_swdbb.c $(XVC_SWDBB_SRCS) \
                        ../xvc_server/xvc_server.h fake_jtag_tap.h | $(BIN)
	$(CC) $(CFLAGS) $(XVC_SWDBB_DEFS) \
		-DXVC_SWDBB_TEST_NAME='"test_xvc_server_swdbb"' -o $@ \
		test_xvc_server_swdbb.c $(XVC_SWDBB_SRCS)

$(BIN)/test_xvc_server_swdbb_latesample: test_xvc_server_swdbb.c $(XVC_SWDBB_SRCS) \
                        ../xvc_server/xvc_server.h fake_jtag_tap.h | $(BIN)
	$(CC) $(CFLAGS) $(XVC_SWDBB_DEFS) -DMPS3_XVC_SWDBB_SAMPLE_LATE=1 \
		-DXVC_SWDBB_TEST_NAME='"test_xvc_server_swdbb_latesample"' -o $@ \
		test_xvc_server_swdbb.c $(XVC_SWDBB_SRCS)

$(BIN)/test_xvc_server_swdbb_stretch: test_xvc_server_swdbb.c $(XVC_SWDBB_SRCS) \
                        ../xvc_server/xvc_server.h fake_jtag_tap.h | $(BIN)
	$(CC) $(CFLAGS) $(XVC_SWDBB_DEFS) -DMPS3_XVC_SWDBB_TCK_STRETCH=2u \
		-DXVC_SWDBB_TEST_NAME='"test_xvc_server_swdbb_stretch"' -o $@ \
		test_xvc_server_swdbb.c $(XVC_SWDBB_SRCS)

# The VERBATIM byte stream the real Synopsys Identify debugger sent, replayed
# through the real protocol engine. TWO binaries from one source, because the
# accept ceiling is compile-time and so cannot be a runtime branch:
#
#   test_xvc_identify_stream         positive: at the shipped
#                                   MPS3_XVC_ACCEPT_RATIO the 2053-bit shift --
#                                   5 bits MORE than the 2048 we advertise -- is
#                                   served, with a full 257-byte TDO reply.
#   test_xvc_identify_stream_ratio1  NEGATIVE CONTROL: -DMPS3_XVC_ACCEPT_RATIO=1u
#                                   re-couples the accept ceiling to the
#                                   advertised size, restoring the defect, and
#                                   asserts the exact damage (4 commands served,
#                                   then dropped). A future refactor that
#                                   re-conflates "advertised" with "accepted"
#                                   makes THIS binary pass and the positive one
#                                   fail, which is the point of building both.
#
# Built at the DEFAULT (DBGBR) target: both halves of the fix -- the num_bits
# bound and the command-accumulator size -- are target-independent.
XVC_IDENT_SRCS := ../xvc_server/xvc_server.c $(COMMON)/net_if.c \
                  $(COMMON)/service.c mock_regs.c fake_net_if.c

$(BIN)/test_xvc_identify_stream: test_xvc_identify_stream.c $(XVC_IDENT_SRCS) \
                        ../xvc_server/xvc_server.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK \
		-DXVC_IDENTIFY_TEST_NAME='"test_xvc_identify_stream"' -o $@ \
		test_xvc_identify_stream.c $(XVC_IDENT_SRCS)

$(BIN)/test_xvc_identify_stream_ratio1: test_xvc_identify_stream.c $(XVC_IDENT_SRCS) \
                        ../xvc_server/xvc_server.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_ACCEPT_RATIO=1u \
		-DXVC_IDENTIFY_TEST_NAME='"test_xvc_identify_stream_ratio1"' -o $@ \
		test_xvc_identify_stream.c $(XVC_IDENT_SRCS)

# ---- W-NET-SEAM, third backend: REAL POSIX sockets ------------------------
#
# posix_net_if.c is the third common/net_if.h backend (after fake_net_if.c and
# the target-only platform/src/net_if_lwip.c). It exists so a REAL external
# client can drive REAL firmware protocol code over a REAL socket, board-free —
# see bin/xvc_fw_daemon below.
#
#   test_posix_net_if          the seam CONTRACT, clause by clause, against a
#                              real loopback client
#   test_posix_net_if_nodrain  NEGATIVE CONTROL: the same sources built with
#                              -DMPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN, which
#                              makes mps3_net_recv() answer the FIN EVENT
#                              (POLLRDHUP) instead of a drained read(). It
#                              asserts the damage — CLOSED first, the peer's last
#                              bytes LOST. Without it, the drain assertion in the
#                              positive binary would pass for a backend that had
#                              never considered the clause, because Linux hands
#                              buffered bytes over before EOF anyway.
POSIX_NET_SRCS := $(COMMON)/net_if.c posix_net_if.c

$(BIN)/test_posix_net_if: test_posix_net_if.c $(POSIX_NET_SRCS) \
                          posix_net_if.h $(COMMON)/net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DPOSIX_NET_TEST_NAME='"test_posix_net_if"' -o $@ \
		test_posix_net_if.c $(POSIX_NET_SRCS)

$(BIN)/test_posix_net_if_nodrain: test_posix_net_if.c $(POSIX_NET_SRCS) \
                          posix_net_if.h $(COMMON)/net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_POSIX_NET_TEST_CLOSE_BEFORE_DRAIN=1 \
		-DPOSIX_NET_TEST_NAME='"test_posix_net_if_nodrain"' -o $@ \
		test_posix_net_if.c $(POSIX_NET_SRCS)

# The SAME real xvc_server.c as the three test_xvc_server_swdbb binaries, now
# driven over a REAL TCP socket by a REAL socket client instead of fake_net_if.c's
# in-memory rings. Single-threaded: the client and xvc_server_poll() interleave in
# one process, which is the superloop shape the firmware runs on target. The
# listener binds an EPHEMERAL port, so concurrent runs cannot collide on 2542.
#
#   test_xvc_posix_loopback             positive: IDCODE 0x6BA00477 round-trips
#   test_xvc_posix_loopback_latesample  NEGATIVE CONTROL: same source with
#                                       -DMPS3_XVC_SWDBB_SAMPLE_LATE=1, asserting
#                                       the IDCODE >> 1 damage — so "it came back
#                                       over a socket" cannot degenerate into
#                                       "the socket works".
#
# This is the self-checking counterpart of bin/xvc_fw_daemon (`make tools`): same
# stack, socket client replaced by the Synopsys Identify debugger.
XVC_LOOPBACK_SRCS := ../xvc_server/xvc_server.c $(COMMON)/net_if.c \
                     posix_net_if.c mock_regs.c fake_jtag_tap.c

$(BIN)/test_xvc_posix_loopback: test_xvc_posix_loopback.c $(XVC_LOOPBACK_SRCS) \
                          ../xvc_server/xvc_server.h fake_jtag_tap.h posix_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_SWDBB \
		-DXVC_LOOPBACK_TEST_NAME='"test_xvc_posix_loopback"' -o $@ \
		test_xvc_posix_loopback.c $(XVC_LOOPBACK_SRCS)

$(BIN)/test_xvc_posix_loopback_latesample: test_xvc_posix_loopback.c $(XVC_LOOPBACK_SRCS) \
                          ../xvc_server/xvc_server.h fake_jtag_tap.h posix_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_SWDBB \
		-DMPS3_XVC_SWDBB_SAMPLE_LATE=1 \
		-DXVC_LOOPBACK_TEST_NAME='"test_xvc_posix_loopback_latesample"' -o $@ \
		test_xvc_posix_loopback.c $(XVC_LOOPBACK_SRCS)

# ---- the JTAGBB target, and the TWO-TAP CHAIN it now has to carry -----------
#
# Two changes needed a host test and had none. (1) -DMPS3_XVC_TARGET_SWDBB names
# swd_bb.sv, a block the A6 SWD->JTAG cutover DELETED; the live one is
# fpga/shell/ip/jtag_bb at the SAME 0x44A7 page with the SAME bit positions, so
# the old build works BY COINCIDENCE through the names of something that no
# longer exists (xvc_server.h now asserts that coincidence at compile time).
# -DMPS3_XVC_TARGET_JTAGBB is the honest spelling. (2) The IICE RM daisy-chains
# Identify's soft TAP with the DUT's SWJ-DP on the one jtag_* wire-set
# (docs/planning/IICE_JTAG_CHAIN.md), so the engine now shifts for TWO devices.
#
# Same real POSIX sockets as test_xvc_posix_loopback (posix_net_if.c), same real
# xvc_server.c, but a chain behind the register model instead of one TAP. The
# chain model is cross-checked against fake_jtag_tap.c inside the test before it
# is used for anything.
#
#   test_xvc_jtagbb_chain             positive: BOTH IDCODEs (0x6BA00477 nearest
#                                     TDO, then 0x1063E4CD) round-trip over a
#                                     kernel socket; the engine's register
#                                     traffic is 3n+1 and knows nothing of TAPs
#   test_xvc_jtagbb_chain_latesample  NEGATIVE CONTROL: the same source with
#                                     -DMPS3_XVC_SWDBB_SAMPLE_LATE=1 -- the
#                                     inverted sampling phase cannot recover
#                                     either IDCODE. Without it, "two IDCODEs
#                                     came back over a socket" could degenerate
#                                     into "the socket works".
XVC_CHAIN_SRCS := ../xvc_server/xvc_server.c $(COMMON)/net_if.c \
                  posix_net_if.c mock_regs.c fake_jtag_tap.c

$(BIN)/test_xvc_jtagbb_chain: test_xvc_jtagbb_chain.c $(XVC_CHAIN_SRCS) \
                          ../xvc_server/xvc_server.h fake_jtag_tap.h posix_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_JTAGBB \
		-DXVC_CHAIN_TEST_NAME='"test_xvc_jtagbb_chain"' -o $@ \
		test_xvc_jtagbb_chain.c $(XVC_CHAIN_SRCS)

$(BIN)/test_xvc_jtagbb_chain_latesample: test_xvc_jtagbb_chain.c $(XVC_CHAIN_SRCS) \
                          ../xvc_server/xvc_server.h fake_jtag_tap.h posix_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_JTAGBB \
		-DMPS3_XVC_SWDBB_SAMPLE_LATE=1 \
		-DXVC_CHAIN_TEST_NAME='"test_xvc_jtagbb_chain_latesample"' -o $@ \
		test_xvc_jtagbb_chain.c $(XVC_CHAIN_SRCS)

# The seam's acceptance binary: full swap end-to-end (6900 line in -> real
# config_agent receive on 6910 -> real swap FSM + HWICAP words -> held
# response line out). Same link set as dispatch but with the REAL
# config_agent.c instead of the fake, plus crc32.
E2E_SRCS := ../coordinator/coordinator.c ../coordinator/coordinator_net.c \
            ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
            $(COMMON)/hwicap_writer.c \
            ../clkrst/clkrst.c ../config_agent/config_agent.c \
            $(COMMON)/net_proto.c $(COMMON)/net_if.c $(COMMON)/crc32.c $(COMMON)/diag.c \
            $(COMMON)/log_ring.c $(COMMON)/service.c \
            ../platform/mps3_version_weak.c ../platform/mps3_usr_access.c \
            mock_regs.c fake_net_if.c fake_overlay_store.c fake_services.c

$(BIN)/test_swap_e2e_net: test_swap_e2e_net.c $(E2E_SRCS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_swap_e2e_net.c $(E2E_SRCS)

# ---- D13: the retired SST26 store, and what replaced its tests ----------------
#
# The QSPI/SST26 overlay-store backend is DELETED (D13 L3a: 0x44A4 is usd_spi,
# the DUT owns the real SST26). Retired with it, because they tested nothing but
# the SST26 leaves or the QSPI staging/promote paths: test_overlay_store_spi,
# test_qspi_fault_inject, test_qspi_chunking, test_qspi_promote_resumable,
# test_swap_qspi_staging(_fifo) and test_swap_clearing_cache, plus
# fake_qspi_flash.{c,h}. Their store semantics are test_ovlstore_sd's now; the
# large-partial path is test_swap_icap_direct / _defer; the large-clearing path
# is the RAM arena (test_swap_qspi_free below).

# QSPI-FREE swap config, RETARGETED (D13): no store sink is registered at all
# (coordinator_init() no longer registers the QSPI ones), config_agent's clearing
# RAM slot and swap_fsm's resident arena both hold nanosoc's full 117,684 B
# clearing, and a greybox->a->b->a swap-away cycle completes with every clearing
# on the RAM path and every large partial ICAP-direct. "Zero flash ops" is now
# "no store sink registered, and not one byte into one".
QSPI_FREE_SRCS := ../config_agent/config_agent.c \
                  ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                  $(COMMON)/hwicap_writer.c \
                  $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c \
                  mock_regs.c fake_net_if.c fake_overlay_store.c
QSPI_FREE_DEFS := -DMPS3_HAL_MOCK -DMPS3_CFG_AGENT_STAGING_BYTES=4096 \
                 -DMPS3_CFG_AGENT_CLEARING_RAM_BYTES=117684

$(BIN)/test_swap_qspi_free: test_swap_qspi_free.c $(QSPI_FREE_SRCS) fake_overlay_store.h | $(BIN)
	$(CC) $(CFLAGS) $(QSPI_FREE_DEFS) -DMPS3_SWAP_CLEARING_ARENA_BYTES=117684 \
		-DQSPI_FREE_TEST_NAME='"test_swap_qspi_free"' -o $@ \
		test_swap_qspi_free.c $(QSPI_FREE_SRCS)

# NEGATIVE CONTROL: identical source + defs EXCEPT the resident arena is
# deliberately too small (4096 B) for nanosoc's clearing. The clearing still
# RAM-stages in config_agent's big slot, but step_cache_clearing() refuses the
# copy into the tiny arena and marks it invalid, so the NEXT swap-away fails
# CLOSED at SWAP_STREAM_CLEARING (the exact silicon bug). The arena is a
# compile-time-fixed array, so this must be a separate binary.
$(BIN)/test_swap_qspi_free_tinyarena: test_swap_qspi_free.c $(QSPI_FREE_SRCS) fake_overlay_store.h | $(BIN)
	$(CC) $(CFLAGS) $(QSPI_FREE_DEFS) -DMPS3_SWAP_CLEARING_ARENA_BYTES=4096 \
		-DQSPI_FREE_TEST_NAME='"test_swap_qspi_free_tinyarena"' -o $@ \
		test_swap_qspi_free.c $(QSPI_FREE_SRCS)

# TFTP transport (UDP 69) carrying a >STAGING_BYTES partial into the ICAP-direct
# sink, plus a mid-transfer TFTP ERROR (the sink is aborted, nothing staged).
# RETARGETED (D13): the two QSPI-sink cases went with the SST26 backend.
$(BIN)/test_tftp_large_swap: test_tftp_large_swap.c $(QSPI_FREE_SRCS) fake_overlay_store.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_CFG_AGENT_STAGING_BYTES=4096 -o $@ \
		test_tftp_large_swap.c $(QSPI_FREE_SRCS)

# Path 3 (stream-direct config_agent -> HWICAP): a LARGE partial streamed
# straight to HWICAP.WF as it arrives (no RAM, no QSPI) through the REAL
# config_agent + swap FSM. Pins the I18 MSB-first word order and the
# swap-armed-first ICAP-write guard. No overlay_store — the ICAP-direct sink
# lives in swap_fsm.c, and the greybox seed comes from fake_overlay_store.c.
ICAP_DIRECT_SRCS := ../config_agent/config_agent.c \
                    ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                    $(COMMON)/hwicap_writer.c \
                    $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c \
                    mock_regs.c fake_net_if.c fake_overlay_store.c

$(BIN)/test_swap_icap_direct: test_swap_icap_direct.c $(ICAP_DIRECT_SRCS) \
                              fake_overlay_store.h fake_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_CFG_AGENT_STAGING_BYTES=4096 -o $@ \
		test_swap_icap_direct.c $(ICAP_DIRECT_SRCS)

# The clearing->partial HANDOFF-RACE regression: the partial header arrives while
# the FSM is still BEFORE SWAP_AWAIT_PARTIAL (a large outgoing clearing keeps it in
# STREAM_CLEARING). config_agent must DEFER the ICAP-direct partial (hold the
# session, backpressure) instead of tearing it — the silicon "swap-away from a DAP
# RM fails" fix. Same link set + threshold as test_swap_icap_direct.
$(BIN)/test_swap_icap_defer: test_swap_icap_defer.c $(ICAP_DIRECT_SRCS) \
                             fake_overlay_store.h fake_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_CFG_AGENT_STAGING_BYTES=4096 -o $@ \
		test_swap_icap_defer.c $(ICAP_DIRECT_SRCS)

# ---- D13: the user microSD (usd.c), the SD overlay store, and their wiring ----
#
# User-microSD driver (firmware/usd/usd.c, D13-L2) against fake_usd.c: a
# register-level usd_spi + an SD card in SPI mode, over mock_regs. Built twice:
# default budgets, and budgets at their floor so every data phase and wait is
# split across polls and every resume path runs.
USD_TEST_SRCS := ../usd/usd.c fake_usd.c mock_regs.c
USD_TEST_DEPS := $(USD_TEST_SRCS) ../usd/usd.h ../usd/usd_regs.h fake_usd.h mock_regs.h \
                 $(COMMON)/platform_regs.h

$(BIN)/test_usd: test_usd.c $(USD_TEST_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_usd.c $(USD_TEST_SRCS)

$(BIN)/test_usd_tinybudget: test_usd.c $(USD_TEST_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DUSD_POLL_BUDGET_FAST=24u -DUSD_POLL_BUDGET_SLOW=21u \
		-DUSD_WAIT_BYTES_PER_POLL=5u -o $@ test_usd.c $(USD_TEST_SRCS)

# SD overlay store (firmware/overlay_store/ovlstore_sd.c, D13-L3) over the POSIX
# provider on temp-file card images (under $TMPDIR, unlinked at once), plus one
# end-to-end case over ovl_bdev_usd + usd.c + fake_usd. Built twice: default
# tunables, and 1-block ops with 157 CRC bytes/poll so every buffer and hand-out
# boundary splits. OVLSTORE_SD_TEST_HOOKS exposes the write guard to the test.
OVLSD_TEST_SRCS := ../overlay_store/ovlstore_sd.c ../overlay_store/ovl_bdev_posix.c \
                   ../overlay_store/ovl_bdev_usd.c ../overlay_store/ovlstore_codec.c \
                   $(COMMON)/crc32.c ../usd/usd.c fake_usd.c mock_regs.c
OVLSD_TEST_DEPS := $(OVLSD_TEST_SRCS) ../overlay_store/ovlstore_sd.h ../overlay_store/ovl_bdev.h \
                   ../overlay_store/ovlstore_codec.h ../usd/usd.h ../usd/usd_regs.h \
                   fake_usd.h mock_regs.h $(COMMON)/crc32.h $(COMMON)/platform_regs.h

$(BIN)/test_ovlstore_sd: test_ovlstore_sd.c $(OVLSD_TEST_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DOVLSTORE_SD_TEST_HOOKS -o $@ test_ovlstore_sd.c $(OVLSD_TEST_SRCS)

$(BIN)/test_ovlstore_sd_tiny: test_ovlstore_sd.c $(OVLSD_TEST_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DOVLSTORE_SD_TEST_HOOKS -DOVLSTORE_SD_OP_BLOCKS=1u \
		-DOVLSTORE_SD_CRC_BYTES_PER_POLL=157u -o $@ test_ovlstore_sd.c $(OVLSD_TEST_SRCS)

# config_agent's v0.13 COMMIT sink: every 6910 push routed to it, header-first,
# back-pressured through write_some() with the <=512 B carry (no ring), TFTP
# refused, and each failure reported through abort() exactly once. Built twice:
# the fire-hose receive, and WINDOWED (the window reopens only for bytes the
# sink has TAKEN -- the back-pressure the host sees).
$(BIN)/test_config_agent_commit: test_config_agent_commit.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DCOMMIT_TEST_NAME='"test_config_agent_commit"' -o $@ \
		test_config_agent_commit.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c

$(BIN)/test_config_agent_commit_windowed: test_config_agent_commit.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_CFG_AGENT_WINDOWED -DCFG_AGENT_ACK_WINDOW_BYTES=1024 \
		-DCOMMIT_TEST_NAME='"test_config_agent_commit_windowed"' -o $@ \
		test_config_agent_commit.c ../config_agent/config_agent.c $(COMMON)/crc32.c $(COMMON)/net_proto.c $(COMMON)/net_if.c fake_net_if.c

# The `usd` verb and the v0.13 `commit` through the REAL dispatcher + codec, the
# store scripted (fake_overlay_store.c): status in every state, every action,
# commit accept + every refusal, the held responses. Same link set as
# test_coordinator_dispatch.
$(BIN)/test_usd_dispatch: test_usd_dispatch.c $(DISPATCH_SRCS) \
                          ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_usd_dispatch.c $(DISPATCH_SRCS)

# THE D13 END TO END: the REAL coordinator + coordinator_net + swap FSM +
# config_agent + store glue + bare-metal engine (overlay_store_bm.c) + ovlstore_sd
# + ovl_bdev_usd + usd.c, against fake_usd's card and fake_net_if's sockets. The
# power-on hook (once per configuration, PB1 skip, stale, no card, interrupted),
# the FSM's "usd" source streaming the partial into HWICAP and the clearing into
# the arena, and a commit pushed over 6910 with the control connection parked.
# CLCD_KVM on (PB1 lives in CLCDKVM.STATUS), so clcd_kvm.c is linked for the
# display handler.
USD_BOOT_SRCS := ../coordinator/coordinator.c ../coordinator/coordinator_net.c \
                 ../coordinator/swap_fsm.c ../coordinator/swap_fsm_transitions.c \
                 $(COMMON)/hwicap_writer.c ../clkrst/clkrst.c ../config_agent/config_agent.c \
                 $(COMMON)/net_proto.c $(COMMON)/net_if.c $(COMMON)/crc32.c $(COMMON)/diag.c \
                 $(COMMON)/log_ring.c $(COMMON)/service.c \
                 ../platform/mps3_version_weak.c ../platform/mps3_usr_access.c \
                 ../overlay_store/overlay_store.c ../overlay_store/overlay_store_bm.c \
                 ../overlay_store/ovlstore_sd.c ../overlay_store/ovl_bdev_usd.c \
                 ../overlay_store/ovlstore_codec.c ../usd/usd.c ../clcd_kvm/clcd_kvm.c \
                 mock_regs.c fake_net_if.c fake_usd.c fake_services.c
$(BIN)/test_usd_boot: test_usd_boot.c $(USD_BOOT_SRCS) ../overlay_store/overlay_store.h \
                      ../overlay_store/ovlstore_sd.h fake_usd.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD_KVM -DOVLSTORE_BM_TEST_HOOKS \
		-DOVLSTORE_GLUE_TEST_HOOKS -DFAKE_USD_STORE_SLOTS=256u -o $@ test_usd_boot.c $(USD_BOOT_SRCS)

# ---- THE HWICAP writer, both write-path modes ----------------------------
#
# common/hwicap_writer.c is the ONE writer behind swap_fsm.c's and
# overlay_store.c's ICAP paths (it replaced a duplicated pair, one per file, each
# with its own `#if MPS3_HWICAP_FIFO`). It is built TWICE here because the
# FIELDED shell runs the FIFO path (PRODUCT=1 => HWICAP_FIFO=1) and until now NO
# host binary defined MPS3_HWICAP_FIFO at all -- the half of the writer that is
# actually on the board was compiled only by the MicroBlaze cross-build, whose
# feedback channel is a board. The test source carries a BEHAVIOURAL axi_hwicap
# core model (a write FIFO of configurable depth that LOSES a word written with
# no room), because mock_regs.c's flat register file cannot fail an unpaced
# writer. Tiny link set on purpose: the writer + the register mock, nothing else.
#
# -DMPS3_HWICAP_MSB_FIRST=1 forces the REAL TARGET packing (the host-mock default
# is native memcpy, kept only so older byte-pattern assertions stay identical --
# see platform_regs.h). Without it the incremental write() path, which always
# assembles MSB-first with explicit shifts, and the write_words() path, which
# goes through mps3_hwicap_pack_word(), would disagree on the host ONLY, and the
# cross-entry-point assertion could not be written at all.
HWICAP_WRITER_SRCS := $(COMMON)/hwicap_writer.c mock_regs.c

$(BIN)/test_hwicap_writer: test_hwicap_writer.c $(HWICAP_WRITER_SRCS) \
                           $(COMMON)/hwicap_writer.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HWICAP_MSB_FIRST=1 \
		-DHWICAP_WRITER_TEST_NAME='"test_hwicap_writer"' -o $@ \
		test_hwicap_writer.c $(HWICAP_WRITER_SRCS)

# The FIELDED protocol. Same source, same assertions, opposite branch of every
# mode split -- so "it passes" cannot mean "only the default path was built".
$(BIN)/test_hwicap_writer_fifo: test_hwicap_writer.c $(HWICAP_WRITER_SRCS) \
                                $(COMMON)/hwicap_writer.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HWICAP_FIFO=1 -DMPS3_HWICAP_MSB_FIRST=1 \
		-DHWICAP_WRITER_TEST_NAME='"test_hwicap_writer_fifo"' -o $@ \
		test_hwicap_writer.c $(HWICAP_WRITER_SRCS)

# ---- W-SMSC: LAN9220 driver (Apache-2.0-derived sequences) vs chip model --

# common/service.c is linked because smsc911x.c's busy/ready waits ARE
# mps3_spin_until() now (they were SMSC911X_POLL_BOUND iteration counts), and
# mock_regs.c supplies the fake microsecond clock those waits read.
SMSC911X_SRCS := ../smsc911x/smsc911x.c $(COMMON)/service.c mock_regs.c \
                 fake_lan9220.c
SMSC911X_DEPS := ../smsc911x/smsc911x.h $(COMMON)/service.h fake_lan9220.h

$(BIN)/test_smsc911x: test_smsc911x.c $(SMSC911X_SRCS) $(SMSC911X_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_smsc911x.c $(SMSC911X_SRCS)

# The driver's TIME discipline, as its own binary: the six bounded waits and the
# two DRAINS. Separate from test_smsc911x because its cases are duration and
# access-count assertions against DELIBERATELY STUCK registers, which need
# mock_regs_set_us_per_read() and a register interposer -- machinery no other
# case in that file wants. Every case here FAILS against the old iteration-count
# code (see the file header).
$(BIN)/test_smsc911x_spin: test_smsc911x_spin.c $(SMSC911X_SRCS) $(SMSC911X_DEPS) | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -o $@ test_smsc911x_spin.c $(SMSC911X_SRCS)

# ---- non-test tools: golden-test harnesses (see each header comment) -----
#
# Both are the C halves of cross-language golden tests driven from
# tests/firmware_logic/; neither is in $(TESTS) because they read stdin /
# emit raw bytes rather than self-checking:
#   - bin/ctrl_echo      -> test_json_golden.py       (control-line JSON codec)
#   - bin/ovlstore_pack  -> test_ovlstore_header_golden.py (on-flash A/B header,
#                           I15 endianness lock: real ovlstore_codec.c vs
#                           tests/common/ovlstore_header.py, byte-for-byte)
#   - bin/xvc_fw_daemon  -> host/identify/fw_com_check.sh (the REAL Synopsys
#                           Identify debugger driving the REAL firmware XVC
#                           engine over a real socket, board-free). Waits on a
#                           socket, so not in $(TESTS); its self-checking
#                           counterpart is test_xvc_posix_loopback above.
#   - bin/xvc_fw_daemon_ratio1 -> the SYSTEM-LEVEL NEGATIVE CONTROL for that
#                           run: the same daemon with the accept ceiling
#                           re-coupled to the advertised size, i.e. the
#                           pre-f93d565 defect restored. Real Identify must FAIL
#                           against it. Without this, "Identify was happy" could
#                           mean "Identify is easy to please".

#   - bin/xvc_throughput -> xvc_loopback_throughput.sh (the board-free MEASUREMENT
#                           of the XVC engine's own bits/s ceiling, the number
#                           the ~1000x SWDBB claim was missing). An XVC CLIENT,
#                           so it waits on a server rather than self-checking;
#                           deliberately links NO firmware source, so a shared
#                           bug in xvc_server.c cannot cancel itself out of the
#                           measurement. The same binary measures a real board.
tools: $(BIN)/ctrl_echo $(BIN)/ovlstore_pack $(BIN)/xvc_fw_daemon \
       $(BIN)/xvc_fw_daemon_ratio1 $(BIN)/xvc_throughput

$(BIN)/ctrl_echo: ctrl_echo.c $(DISPATCH_SRCS) \
                  ../coordinator/coordinator.h $(COMMON)/net_proto.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_DUT_EGRESS -o $@ \
		ctrl_echo.c $(DISPATCH_SRCS)

# Links ovlstore_codec.c ONLY -- same zero-register-dependency link set as
# test_ovlstore_header (no HAL mock, no mock_regs.c).
$(BIN)/ovlstore_pack: ovlstore_pack.c ../overlay_store/ovlstore_codec.c \
                      ../overlay_store/ovlstore_codec.h | $(BIN)
	$(CC) $(CFLAGS) -o $@ ovlstore_pack.c ../overlay_store/ovlstore_codec.c

# EXACTLY the same link set and flags as test_xvc_posix_loopback -- the same real
# xvc_server.c, the same POSIX socket backend, the same in-memory TAP -- with the
# in-process socket client replaced by whatever connects to the port. That
# identity is the point: whatever the Identify debugger proves against this
# daemon, the loopback test regresses on every `make check`.
$(BIN)/xvc_fw_daemon: xvc_fw_daemon.c $(XVC_LOOPBACK_SRCS) \
                      ../xvc_server/xvc_server.h fake_jtag_tap.h posix_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_SWDBB -o $@ \
		xvc_fw_daemon.c $(XVC_LOOPBACK_SRCS)

# SYSTEM-LEVEL NEGATIVE CONTROL for the Identify run. Identical to the daemon
# above except -DMPS3_XVC_ACCEPT_RATIO=1u, which re-couples the accept ceiling to
# the ADVERTISED size and so restores the exact pre-f93d565 defect (the same
# knob test_xvc_identify_stream_ratio1 uses, here carried all the way out to the
# real debugger). MEASURED against identify_debugger_shell T-2022.09-SP2: the
# session dies at "Checking communication with the XilinxVirtualCable cable and
# the hardware..." with "Error: Connection reset by peer", after only the two
# small navigation shifts -- the firmware fails closed on Identify's 2053-bit
# shift, exactly as designed. host/identify/fw_com_check.sh asserts that failure,
# which is what stops the positive run from meaning "Identify is easy to please".
# The measurement client. NO firmware sources and NO -DMPS3_* flags on purpose:
# it is an independent implementation of the client half of XVC 1.0, so it cannot
# inherit a server-side convention error and score itself correct. Links only
# libc (clock_gettime is in glibc proper on this toolchain, no -lrt needed).
$(BIN)/xvc_throughput: xvc_throughput.c | $(BIN)
	$(CC) $(CFLAGS) -o $@ xvc_throughput.c

$(BIN)/xvc_fw_daemon_ratio1: xvc_fw_daemon.c $(XVC_LOOPBACK_SRCS) \
                      ../xvc_server/xvc_server.h fake_jtag_tap.h posix_net_if.h | $(BIN)
	$(CC) $(CFLAGS) -DMPS3_HAL_MOCK -DMPS3_XVC_TARGET_SWDBB \
		-DMPS3_XVC_ACCEPT_RATIO=1u -o $@ \
		xvc_fw_daemon.c $(XVC_LOOPBACK_SRCS)

# ---- run everything -------------------------------------------------------

# Build isolation: `make test` links + runs the $(TESTS) binaries in a PRIVATE
# per-invocation dir (a sub-make with $(BIN) overridden) -- $(BIN) itself has no
# isolation, so two builds overlapping in this shared tree (the gating pytest,
# the other track's `make all`) would relink one binary while the other execs
# it. The dir sits OUTSIDE $(BIN) so a concurrent `make clean` (rm -rf $(BIN),
# which the pytest runs first) can't pull it out from under a running suite; it
# is removed on exit (EXIT/INT/TERM). `clean` deliberately does NOT glob-sweep
# sibling run dirs -- that would reintroduce the same clobber against a
# concurrent invocation. The tools (bin/ctrl_echo, bin/ovlstore_pack) still
# build into $(BIN) directly, for the golden harnesses that read them by path.
test:
	@dir=$$(mktemp -d .testrun.XXXXXX); \
	trap 'rm -rf "$$dir"' EXIT INT TERM; \
	$(MAKE) --no-print-directory BIN="$$dir" run-tests

# `$(BIN)/$$t`, NOT `./$(BIN)/$$t`: the leading ./ made an ABSOLUTE BIN
# unrunnable (".//abs/path/test_crc32: No such file or directory"), which is
# exactly how this harness gets driven when a build directory is kept outside
# the tree. Any path containing a slash needs no ./ in sh, so both forms of BIN
# work now.
run-tests: $(addprefix $(BIN)/,$(TESTS))
	@echo "=== firmware host-gcc test suite ==="
	@total=0; \
	for t in $(TESTS); do \
		echo "--- $$t ---"; \
		"$(BIN)/$$t" || exit 1; \
	done; \
	echo "=== ALL PASS ($(words $(TESTS)) binaries) ==="

# ---- advisory coverage ----------------------------------------------------
# gcov line-coverage for the critical swap-path files (config_agent.c,
# swap_fsm*.c) in their real build configs -- see coverage.sh's header for why
# it groups by flag set. ADVISORY: prints a table, exits 0. Gate it with
# `COV_STRICT=1 COV_FLOOR=NN make -C firmware/test coverage`.
coverage:
	@./coverage.sh

clean:
	rm -rf $(BIN) cov
