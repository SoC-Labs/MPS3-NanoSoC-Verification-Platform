# src/linux_harness/sw/harnessd/Makefile — mps3-harnessd, the MPS3 shell
# firmware's service modules as ONE Linux process (HARNESSD_CONTRACT.md).
#
# ONE Makefile, four builds of the SAME sources (the firmware modules are
# compiled from firmware/ in place — never copied):
#
#   make host        build/host/mps3-harnessd       x86, MOCK fabric, PRODUCT set
#   make host-echo   build/host-echo/mps3-harnessd  x86, MOCK, ctrl_echo's flag set
#                                                   (the conformance twin, §8)
#   make host-uio    build/host-uio/mps3-harnessd   x86, UIO (the no-hw / UIO tests)
#   make rv32        build/rv32/mps3-harnessd       rv32 Linux, UIO — the product
#   make rv32-mock   build/rv32-mock/mps3-harnessd  rv32 Linux, MOCK — QEMU -M virt
#   make check       every host test (tests/, §8)   <- HOST's `make check-linux`
#                    (incl. scripts/linux_board/lcdmirror_grab.py, the host
#                    tool that grabs a PNG off 6940: tests/test_lcdmirror_grab.py,
#                    and lcdmirror_capture.py, HM's golden fixtures:
#                    tests/test_lcdmirror_capture.py)
#   (every build also makes mps3-lcdmirror beside mps3-harnessd: the LCD mirror's
#    6940 server, a child harnessd spawns -- lcdmirror.h)
#   make audit       §1's unresolved-symbol table, re-derived
#
# Run it under `nice -n 19` on the shared build host (plan §4).
#
# THE FLAG SET. The PRODUCT=1 image (firmware/platform/Makefile) is CLCD=1
# CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1 DUT_EGRESS=1. harnessd builds all
# of it EXCEPT WINDOWED, deliberately (HARNESSD_CONTRACT.md §9.5):
# window-as-grant exists to pace an lwIP receiver whose recv callback queues
# pbufs whether or not the consumer drained them; a KERNEL socket is paced by
# its consumer already (it advertises only what its buffer holds and reopens the
# window as recv() consumes), and config_agent recv()s only what it has just fed
# to its sink. The WIRE is identical either way — window-as-grant sends ZERO
# application bytes, it only shapes TCP windows — so pyverify's plain AND
# windowed pushers both work, and `version.features` honestly omits "windowed".
# posix_net_if.c keeps its #error against MPS3_CFG_AGENT_WINDOWED.
#
# Sizes that differ from the MicroBlaze image, because RAM is not scarce under
# Linux: the clearing slot and the resident-clearing arena are 1 MiB each, so no
# clearing ever needs the QSPI path (overlay_store.c is not linked; §6). Partials
# larger than PARTIAL_RAM stream ICAP-direct, exactly as on the board.

REPO    := $(abspath ../../../..)
FW      := $(REPO)/firmware
HD      := $(abspath .)
BUILD   ?= $(HD)/build

HOST_CC ?= gcc
# The Buildroot host toolchain (AMD rv32 glibc toolchain behind a wrapper; see
# src/linux_soc/linux/README.md). Default = THIS checkout's harness build
# (src/linux_harness/sw/build.sh output). Override CROSS for another one;
# Buildroot's package passes CC directly.
CROSS   ?= $(REPO)/src/linux_harness/sw/build/buildroot/output/host/bin/riscv32-amd-linux-gnu-
RV_CC   ?= $(CROSS)gcc
# The F/D LANDMINE (linux_soc/linux/README.md): -mabi=ilp32 alone still lets gcc
# emit F/D instructions, which trap on the MBV. The -march is what turns them off.
RV_ARCH := -march=rv32imac_zicsr_zifencei -mabi=ilp32

HARNESSD_BUILD := $(shell git -C $(REPO) rev-parse --short=8 HEAD 2>/dev/null || echo unknown)$(shell git -C $(REPO) diff --quiet 2>/dev/null || echo -dirty)

WARN    := -Wall -Wextra -Werror -Wno-unused-parameter

# harnessd-only behaviour inside the SHARED firmware modules, which bare metal
# (frozen at v0.11, DL4) is built without: the console relay's Linux policy --
# drain-and-drop with no client, flush on every swap ungate edge, optional input
# pacing (--uart-pace-ms) -- firmware/uart_over_eth/uart_over_eth.h.
HD_POLICY := -DMPS3_UART_LINUX_POLICY

BASE_CFLAGS := -std=gnu11 -O2 -g $(WARN) -ffunction-sections -fdata-sections \
               -I$(HD) -I$(HD)/compat -I$(REPO)/src/linux_soc/hw/fw_stage0 \
               -DMPS3_LMB_KB=128 -DMPS3_HWICAP_MSB_FIRST=1 \
               -DMPS3_CFG_AGENT_ICAP_DIRECT \
               -DMPS3_CFG_AGENT_STAGING_BYTES=4096 \
               -DMPS3_CFG_AGENT_PARTIAL_RAM_BYTES=2048 \
               -DMPS3_CFG_AGENT_CLEARING_RAM_BYTES=1048576 \
               -DMPS3_SWAP_CLEARING_ARENA_BYTES=1048576 \
               -DOVLSTORE_SD_OP_BLOCKS=64u -DOVLSTORE_SD_CRC_BYTES_PER_POLL=65536u \
               -DHARNESSD_BUILD='"$(HARNESSD_BUILD)"' \
               $(HD_POLICY)

# The watchdog stage, which the `reboot` verb's in_ms bound and harnessd's
# reboot(2) fallback are computed from (coordinator.h MPS3_WDT_STAGE_MS). The MBV
# static runs axi_timebase_wdt with C_WDT_INTERVAL=31: 2^31 / 100 MHz = 21.47 s,
# reset at ~43 s (SHELL_CONTRACT.md §6). The host builds keep the bare-metal
# shell's 2^27 (1.34 s) so the reboot test stays fast; the mock has no reset.
WDT_STAGE_MS_MBV  := 21475
WDT_STAGE_MS_HOST := 1342

# THE USER MICROSD (D13 + the v0.14 slots). harnessd links the overlay store glue
# + ovlstore_sd.c over ovl_bdev_posix.c (FW_CORE below) -- NEVER usd.c /
# ovl_bdev_usd.c: under Linux the kernel owns usd_spi. ONE whole-disk node serves
# BOTH the store and stage0's slots (--card; --usd-dev / --slot-disk are aliases
# that must agree). It defaults to /dev/mmcblk0 on the rv32 PRODUCT build only;
# every host build defaults to "no card" so a test can never open the build
# host's own disk (tests pass --card <temp image>).
# Per-call store budgets are raised for a kernel block device: 64 blocks (32 KiB)
# a device op, 64 KiB of CRC a poll (INTEGRATION_L3.md §8).
RV_CARD := -DHARNESSD_CARD_DEV='"/dev/mmcblk0"'

# PRODUCT=1 minus WINDOWED (above).
# CLCD_BYTES_PER_PASS (clcd.h; bare metal 256 = one glyph a superloop pass): under
# harnessd a clcd_poll() is one step of the drain, and each costs two clock
# syscalls (no rv32 vDSO) + the KVM STATUS/EVENT reads -- at 256 B that was ~15%
# of a repaint once the bus seam made a byte ~0.6 us. 1024 B keeps the drain's
# overshoot (one clcd_poll) near 0.6 ms (lane CLCD-SPEED, tests/test_clcd_speed.c).
HD_CLCD_BYTES := 1024u
PRODUCT_FLAGS := -DMPS3_HAS_CLCD -DMPS3_HAS_CLCD_KVM -DMPS3_HAS_TOUCH \
                 -DMPS3_HWICAP_FIFO=1 -DMPS3_HAS_DUT_EGRESS \
                 -DCLCD_BYTES_PER_PASS=$(HD_CLCD_BYTES)
# firmware/test/Makefile's ctrl_echo recipe: -DMPS3_HAS_DUT_EGRESS and nothing else.
ECHO_FLAGS    := -DMPS3_HAS_DUT_EGRESS

FW_CORE := common/crc32.c common/net_if.c common/net_proto.c common/diag.c \
           common/service.c common/log_ring.c common/hwicap_writer.c \
           coordinator/coordinator.c coordinator/coordinator_net.c \
           coordinator/swap_fsm.c coordinator/swap_fsm_transitions.c \
           config_agent/config_agent.c clkrst/clkrst.c \
           jtag_server/jtag_server.c xvc_server/xvc_server.c \
           uart_over_eth/uart_over_eth.c identify/identify.c \
           platform/mps3_version_weak.c platform/mps3_usr_access.c \
           overlay_store/overlay_store.c overlay_store/ovlstore_sd.c \
           overlay_store/ovl_bdev_posix.c overlay_store/ovlstore_codec.c \
           test/posix_net_if.c
FW_PRODUCT := $(FW_CORE) clcd/clcd.c clcd/hx8347_init.c clcd_kvm/clcd_kvm.c touch/touch.c

# STAGE0's portable loader core (no MMIO, no libc): the `slot` provider verifies a
# card slot with the SAME s0_load() stage0 boots it with (slot_linux.c), and reads
# the MBR / boot-select with stage0's own parsers. Compiled in place, never copied.
S0      := $(REPO)/src/linux_soc/hw/fw_stage0

HD_COMMON := main_linux.c hal_front.c timebase_linux.c log_linux.c platform_linux.c \
             identity.c version_linux.c ovlstore_linux.c tofu_linux.c sshfp.c slot_linux.c \
             slot_card.c clk_linux.c lcdmirror_tap.c lcdmirror_model.c \
             identity_linux.c identity_core.c locate_linux.c jtag_linux.c \
             presence_core.c panel_linux.c
HD_UIO    := $(HD_COMMON) hal_uio.c
HD_MOCK   := $(HD_COMMON) hal_mock.c
FW_MOCKX  := test/fake_jtag_tap.c

.PHONY: all host host-echo host-uio host-wdkick rv32 rv32-mock check conformance audit clean tests \
        lcdmirror-vectors FORCE
FORCE:

all: host host-echo

# ---- one generic link rule per variant -------------------------------------
# $(1)=variant $(2)=CC $(3)=CFLAGS $(4)=firmware srcs $(5)=harnessd srcs
# A flag change must rebuild (make compares timestamps, not flags — the trap
# firmware/platform/Makefile documents for XVC_TARGET): every object depends on
# a per-variant stamp that is rewritten only when the command line changes.
define VARIANT
$(1)_OBJS := $$(patsubst %.c,$(BUILD)/$(1)/fw/%.o,$(4)) $$(patsubst %.c,$(BUILD)/$(1)/hd/%.o,$(5)) \
             $(BUILD)/$(1)/s0/stage0_core.o
$(BUILD)/$(1)/.flags: FORCE
	@mkdir -p $$(dir $$@)
	@echo '$(2) $(3) -MMD' | cmp -s - $$@ || echo '$(2) $(3) -MMD' > $$@
$(BUILD)/$(1)/fw/%.o: $(FW)/%.c $(BUILD)/$(1)/.flags
	@mkdir -p $$(dir $$@)
	@echo "  CC [$(1)] firmware/$$*.c"
	@$(2) $(3) -MMD -MP -c -o $$@ $$<
$(BUILD)/$(1)/hd/%.o: $(HD)/%.c $(HD)/harnessd.h $(HD)/hal.h $(HD)/slot_card.h $(HD)/lcdmirror.h $(HD)/identity_core.h $(HD)/presence_core.h $(BUILD)/$(1)/.flags
	@mkdir -p $$(dir $$@)
	@echo "  CC [$(1)] harnessd/$$*.c"
	@$(2) $(3) -MMD -MP -c -o $$@ $$<
$(BUILD)/$(1)/s0/%.o: $(S0)/%.c $(S0)/stage0_boot.h $(BUILD)/$(1)/.flags
	@mkdir -p $$(dir $$@)
	@echo "  CC [$(1)] fw_stage0/$$*.c"
	@$(2) $(3) -MMD -MP -c -o $$@ $$<
# HEADER DEPENDENCIES (-MMD -MP, 2026-09-26): an object used to depend on its .c
# alone, so a changed struct in a shared header (net_proto.h's response grew a
# field) left every unchanged .c's object stale -- a different struct size on
# each side of a call, and a stack smash after the first swap. Now each object
# also depends on every header it includes.
-include $$($(1)_OBJS:.o=.d)
$(BUILD)/$(1)/mps3-harnessd: $$($(1)_OBJS)
	@$(2) $(3) -Wl,--gc-sections -o $$@ $$^
	@echo "=== $$@ ($(1)) ==="
endef

$(eval $(call VARIANT,host,$(HOST_CC),$(BASE_CFLAGS) $(PRODUCT_FLAGS) -DMPS3_HAL_MOCK -DMPS3_WDT_STAGE_MS=$(WDT_STAGE_MS_HOST)u,$(FW_PRODUCT) $(FW_MOCKX),$(HD_MOCK)))
$(eval $(call VARIANT,host-echo,$(HOST_CC),$(BASE_CFLAGS) $(ECHO_FLAGS) -DMPS3_HAL_MOCK -DMPS3_WDT_STAGE_MS=$(WDT_STAGE_MS_HOST)u,$(FW_CORE) $(FW_MOCKX),$(HD_MOCK)))
$(eval $(call VARIANT,host-uio,$(HOST_CC),$(BASE_CFLAGS) $(PRODUCT_FLAGS) -DMPS3_HAL_UIO -DMPS3_WDT_STAGE_MS=$(WDT_STAGE_MS_HOST)u,$(FW_PRODUCT),$(HD_UIO)))
$(eval $(call VARIANT,rv32,$(RV_CC),$(BASE_CFLAGS) $(PRODUCT_FLAGS) -DMPS3_HAL_UIO -DMPS3_WDT_STAGE_MS=$(WDT_STAGE_MS_MBV)u $(RV_ARCH) $(RV_CARD),$(FW_PRODUCT),$(HD_UIO)))
$(eval $(call VARIANT,rv32-mock,$(RV_CC),$(BASE_CFLAGS) $(PRODUCT_FLAGS) -DMPS3_HAL_MOCK -DMPS3_WDT_STAGE_MS=$(WDT_STAGE_MS_HOST)u $(RV_ARCH),$(FW_PRODUCT) $(FW_MOCKX),$(HD_MOCK)))

host:      $(BUILD)/host/mps3-harnessd $(BUILD)/host/mps3-lcdmirror $(BUILD)/host-uio/mps3-identity
host-echo: $(BUILD)/host-echo/mps3-harnessd
host-uio:  $(BUILD)/host-uio/mps3-harnessd $(BUILD)/host-uio/mps3-lcdmirror
host-wdkick: $(BUILD)/host-uio/mps3-wdkick
rv32:      $(BUILD)/rv32/mps3-harnessd $(BUILD)/rv32/mps3-wdkick $(BUILD)/rv32/mps3-lcdmirror \
           $(BUILD)/rv32/mps3-identity
	@$(CROSS)readelf -A $< | grep -E "Tag_RISCV_arch" || true
	@if $(CROSS)objdump -d $< | grep -Eq '\s(f(add|sub|mul|div|mv|lw|sw|ld|sd|cvt)\.|flw|fsw|fld|fsd)\s'; then \
	    echo "FATAL: F/D instructions in $< (the MBV has no FPU)"; exit 1; fi
	@echo "rv32: no F/D instructions (landmine checked)"
rv32-mock: $(BUILD)/rv32-mock/mps3-harnessd $(BUILD)/rv32-mock/mps3-lcdmirror

# mps3-lcdmirror: the LCD mirror's 6940 server (lcdmirror_main.c), a child of
# harnessd at nice 10 that reads the aperture harnessd's model writes. harnessd
# runs it from BESIDE its own binary, so every build puts one next to
# mps3-harnessd. The image package must install it next to /usr/sbin/mps3-harnessd
# (br2_external mps3-harnessd.mk: one $(INSTALL) line, like mps3-wdkick). Host
# builds add -DLCDM_TEST_HOOKS (--trusted-peer, MPS3_LCDMIRROR_MUTATE): the
# product binary has neither.
LCDM_SRCS  := lcdmirror_main.c lcdmirror_enc.c
LCDM_CFLAGS := -std=gnu11 -O2 -g $(WARN) -I$(HD)
$(BUILD)/host/mps3-lcdmirror $(BUILD)/host-uio/mps3-lcdmirror: $(LCDM_SRCS) lcdmirror.h
	@mkdir -p $(dir $@)
	@echo "  CC [host] mps3-lcdmirror"
	@$(HOST_CC) $(LCDM_CFLAGS) -DLCDM_TEST_HOOKS -o $@ $(LCDM_SRCS)
$(BUILD)/rv32/mps3-lcdmirror $(BUILD)/rv32-mock/mps3-lcdmirror: $(LCDM_SRCS) lcdmirror.h
	@mkdir -p $(dir $@)
	@echo "  CC [rv32] mps3-lcdmirror"
	@$(RV_CC) $(LCDM_CFLAGS) $(RV_ARCH) -o $@ $(LCDM_SRCS)
	@if $(CROSS)objdump -d $@ | grep -Eq '\s(f(add|sub|mul|div|mv|lw|sw|ld|sd|cvt)\.|flw|fsw|fld|fsd)\s'; then \
	    echo "FATAL: F/D instructions in $@ (the MBV has no FPU)"; exit 1; fi

# mps3-wdkick: the BOUNDED watchdog bridge from init to harnessd (mps3_wdkick.c;
# HARNESSD_CONTRACT.md §5.3). It links hal_uio.c UNCHANGED, so it finds the
# watchdog's UIO window exactly the way harnessd does. The rv32 product build rides
# `make rv32` (the package installs both); host-wdkick is the test build.
WDKICK_SRCS := mps3_wdkick.c hal_uio.c
$(BUILD)/rv32/mps3-wdkick: $(WDKICK_SRCS) hal.h harnessd.h $(FW)/common/platform_regs.h
	@mkdir -p $(dir $@)
	@echo "  CC [rv32] mps3-wdkick"
	@$(RV_CC) $(BASE_CFLAGS) -DMPS3_HAL_UIO $(RV_ARCH) -o $@ $(WDKICK_SRCS)
	@if $(CROSS)objdump -d $@ | grep -Eq '\s(f(add|sub|mul|div|mv|lw|sw|ld|sd|cvt)\.|flw|fsw|fld|fsd)\s'; then \
	    echo "FATAL: F/D instructions in $@ (the MBV has no FPU)"; exit 1; fi
$(BUILD)/host-uio/mps3-wdkick: $(WDKICK_SRCS) hal.h harnessd.h $(FW)/common/platform_regs.h
	@mkdir -p $(dir $@)
	@$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_UIO -o $@ $(WDKICK_SRCS)

# mps3-identity: the BOARD identity's boot-time resolver (S13mps3identity, before
# S41mps3net) and board CLI (get/set/clear) -- identity_core.h. It links hal_uio.c
# UNCHANGED, like mps3-wdkick, so it reads stage0's status block through the same
# LMB-tail window harnessd does; identity_core.c is the code harnessd's
# `identity`/`identity_set` verbs run, so the CLI and the verb judge alike. The
# package installs it beside harnessd (+ S13mps3identity).
IDENT_SRCS := mps3_identity.c identity_core.c hal_uio.c
$(BUILD)/rv32/mps3-identity: $(IDENT_SRCS) identity_core.h hal.h harnessd.h $(S0)/stage0_status.h
	@mkdir -p $(dir $@)
	@echo "  CC [rv32] mps3-identity"
	@$(RV_CC) $(BASE_CFLAGS) -DMPS3_HAL_UIO $(RV_ARCH) -o $@ $(IDENT_SRCS)
	@if $(CROSS)objdump -d $@ | grep -Eq '\s(f(add|sub|mul|div|mv|lw|sw|ld|sd|cvt)\.|flw|fsw|fld|fsd)\s'; then \
	    echo "FATAL: F/D instructions in $@ (the MBV has no FPU)"; exit 1; fi
$(BUILD)/host-uio/mps3-identity: $(IDENT_SRCS) identity_core.h hal.h harnessd.h $(S0)/stage0_status.h
	@mkdir -p $(dir $@)
	@echo "  CC [host] mps3-identity"
	@$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_UIO -o $@ $(IDENT_SRCS)

# ---- tests (HARNESSD_CONTRACT.md §8) ----------------------------------------
TBIN := $(BUILD)/tests
$(TBIN):
	@mkdir -p $@

# 1. the UIO HAL against a fake sysfs tree + plain files as /dev/uioN.
$(TBIN)/test_hal_uio: tests/test_hal_uio.c hal_uio.c hal_front.c hal.h harnessd.h | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) $(PRODUCT_FLAGS) -DMPS3_HAL_UIO -o $@ tests/test_hal_uio.c \
		hal_uio.c hal_front.c

# 2. version.impl / id_skew / the omit masks / os_up_ms: BYTE IDENTITY. The
#    same dumper (tests/codec_dump.c: deterministic pseudo-random responses for
#    every op the codec encodes) is linked against THREE codecs:
#      head   net_proto.c as it was at BASELINE_REV (the branch point) — what
#             bare metal emitted before this lane
#      new    today's net_proto.c, weak seams (= every bare-metal image)
#      linux  today's net_proto.c + strong seams (= harnessd)
#    tests/test_codec_identity.py asserts head == new byte for byte, and that
#    linux differs ONLY by the documented additive keys / omissions.
#    BASELINE_REV is a commit of the PRIVATE history. A one-commit export (the
#    public repo) does not have it: then `head` is not built, a marker file
#    records why, and test_bare_metal_bytes_unchanged SKIPS with that reason.
#    Where the commit exists the head build is mandatory (a failure fails make).
BASELINE_REV ?= eafe787
HAVE_BASELINE := $(shell git -C $(REPO) cat-file -e '$(BASELINE_REV)^{commit}' 2>/dev/null && echo 1)
CODEC_HEAD_SKIP := $(TBIN)/codec_dump_head.SKIP
ifeq ($(HAVE_BASELINE),1)
CODEC_HEAD := codec_dump_head
else
CODEC_HEAD := codec_dump_head.SKIP
endif
$(CODEC_HEAD_SKIP): | $(TBIN)
	@echo "SKIP codec_dump_head: baseline commit $(BASELINE_REV) is not in this repository's history (one-commit public export); the head==new byte-identity check needs the private history"
	@echo "baseline commit $(BASELINE_REV) is not in this repository's history (one-commit public export); the head==new bare-metal byte-identity check runs only in the private repo" > $@
$(TBIN)/head/firmware/common/net_proto.c:
	@mkdir -p $(TBIN)/head
	git -C $(REPO) archive $(BASELINE_REV) firmware/common | tar -x -C $(TBIN)/head
$(TBIN)/codec_dump_head: tests/codec_dump.c $(TBIN)/head/firmware/common/net_proto.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -I$(TBIN)/head/firmware/common -o $@ tests/codec_dump.c \
		$(TBIN)/head/firmware/common/net_proto.c
$(TBIN)/codec_dump_new: tests/codec_dump.c $(FW)/common/net_proto.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -I$(FW)/common -o $@ tests/codec_dump.c $(FW)/common/net_proto.c
$(TBIN)/codec_dump_linux: tests/codec_dump.c $(FW)/common/net_proto.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -I$(FW)/common -DCODEC_DUMP_LINUX_SEAMS -o $@ \
		tests/codec_dump.c $(FW)/common/net_proto.c

# 3./4. identify + TOFU, against firmware/test's in-memory net backend.
$(TBIN)/test_identify: tests/test_identify.c $(FW)/identify/identify.c $(FW)/common/net_proto.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_MOCK -o $@ tests/test_identify.c \
		$(FW)/identify/identify.c $(FW)/common/net_proto.c $(FW)/common/net_if.c \
		$(FW)/test/fake_net_if.c $(FW)/platform/mps3_version_weak.c
$(TBIN)/test_identify_linux: tests/test_identify.c $(FW)/identify/identify.c $(FW)/common/net_proto.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_MOCK -DTEST_LINUX_PROVIDERS -o $@ tests/test_identify.c \
		$(FW)/identify/identify.c $(FW)/common/net_proto.c $(FW)/common/net_if.c \
		$(FW)/test/fake_net_if.c $(FW)/platform/mps3_version_weak.c
TOFU_SRCS := $(FW)/config_agent/config_agent.c $(FW)/common/crc32.c $(FW)/common/net_proto.c \
             $(FW)/common/net_if.c $(FW)/test/fake_net_if.c
$(TBIN)/test_tofu: tests/test_tofu.c tofu_linux.c sshfp.c $(TOFU_SRCS) | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_MOCK -o $@ tests/test_tofu.c tofu_linux.c sshfp.c $(TOFU_SRCS)
# The bare-metal default provider: the SAME test source, no tofu_linux.c linked.
$(TBIN)/test_tofu_baremetal: tests/test_tofu.c $(TOFU_SRCS) | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_MOCK -DTEST_BAREMETAL_PROVIDER -o $@ tests/test_tofu.c $(TOFU_SRCS)
$(TBIN)/test_sshfp: tests/test_sshfp.c sshfp.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -o $@ tests/test_sshfp.c sshfp.c

# The CLCD engine-row provider (tofu_linux.c, built with the panel on).
$(TBIN)/test_engine_row: tests/test_engine_row.c tofu_linux.c sshfp.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -o $@ tests/test_engine_row.c \
		tofu_linux.c sshfp.c

# `stats.dut_mhz` after a respawn (ILA-mint finding #12) + the boot clock
# re-sync: the REAL coordinator/clkrst over firmware/test's mock registers, with
# clk_linux.c's provider (harnessd) and without it (bare metal's shadow -- the
# negative control: the same respawn reads 50 while the fabric runs 100).
DUTCLK_SRCS := $(FW)/coordinator/coordinator.c $(FW)/coordinator/coordinator_net.c \
               $(FW)/coordinator/swap_fsm.c $(FW)/coordinator/swap_fsm_transitions.c \
               $(FW)/common/hwicap_writer.c $(FW)/clkrst/clkrst.c $(FW)/common/net_proto.c \
               $(FW)/common/net_if.c $(FW)/common/diag.c $(FW)/common/log_ring.c \
               $(FW)/common/service.c $(FW)/platform/mps3_version_weak.c \
               $(FW)/platform/mps3_usr_access.c $(FW)/test/mock_regs.c $(FW)/test/fake_net_if.c \
               $(FW)/test/fake_config_agent.c $(FW)/test/fake_overlay_store.c \
               $(FW)/test/fake_services.c
DUTCLK_CFLAGS := -std=gnu11 -O0 -g $(WARN) -DMPS3_HAL_MOCK -I$(HD)
$(TBIN)/test_dut_clk: tests/test_dut_clk.c clk_linux.c harnessd.h $(DUTCLK_SRCS) | $(TBIN)
	$(HOST_CC) $(DUTCLK_CFLAGS) -o $@ tests/test_dut_clk.c clk_linux.c $(DUTCLK_SRCS)
$(TBIN)/test_dut_clk_baremetal: tests/test_dut_clk.c $(DUTCLK_SRCS) | $(TBIN)
	$(HOST_CC) $(DUTCLK_CFLAGS) -DTEST_BAREMETAL_PROVIDER -o $@ tests/test_dut_clk.c $(DUTCLK_SRCS)

# The LCD mirror: the GRAM model against synthetic vectors, the REAL clcd.c
# renderer (+ the real init table) through the model against a font-rendered
# oracle of clcd.c's own cell shadow, the codec round trip + its mutation
# controls, and the per-byte / per-pass cost (tests/test_lcdmirror.c).
LCDMT_SRCS := tests/test_lcdmirror.c lcdmirror_model.c lcdmirror_enc.c \
              $(FW)/clcd/clcd.c $(FW)/clcd/hx8347_init.c $(FW)/test/mock_regs.c
$(TBIN)/test_lcdmirror: $(LCDMT_SRCS) lcdmirror.h | $(TBIN)
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -I$(HD) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD \
		-DMPS3_CLCD_TEST_HOOKS -o $@ $(LCDMT_SRCS)
# The replay tool: an {rs,byte} stream through the model, the aperture out (the
# shared-vector check against the Python golden model, tests/test_lcdmirror_e2e.py).
$(TBIN)/lcdmirror_replay: tests/lcdmirror_replay.c lcdmirror_model.c lcdmirror.h | $(TBIN)
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -I$(HD) -o $@ tests/lcdmirror_replay.c lcdmirror_model.c

# The 6940 wire vectors (HM's H1): the fixtures under tests/fixtures/lcdmirror_wire/
# are this tool's output; tests/test_lcdmirror_e2e.py regenerates and compares.
$(TBIN)/lcdmirror_vectors: tests/lcdmirror_vectors.c lcdmirror_enc.c lcdmirror.h | $(TBIN)
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -I$(HD) -o $@ tests/lcdmirror_vectors.c lcdmirror_enc.c
lcdmirror-vectors: $(TBIN)/lcdmirror_vectors
	@mkdir -p tests/fixtures/lcdmirror_wire
	$(TBIN)/lcdmirror_vectors tests/fixtures/lcdmirror_wire

# The BOARD identity (lane IDENT): the resolver's precedence, the validation, the
# stage0 decode (incl. an old block with the label words 0) and the renderers.
$(TBIN)/test_identity_core: tests/test_identity_core.c identity_core.c identity_core.h \
        $(S0)/stage0_status.h | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -o $@ tests/test_identity_core.c identity_core.c

# `locate` (v0.16, HM R3): the REAL locate_linux.c over a fake clock, the REAL
# clcd_kvm.c (the backlight = CLCDKVM.CTRL[5]) and clcd.c renderer over the mock
# registers: the timer, replace, the restore + its negative control, the tap, the
# banner below the fault banners. locate_linux.c/clcd_kvm.c are compiled WITH the
# KVM; clcd.c without (its own KVM calls stay the inline stubs), as test_lcdmirror.
LOCATE_OBJ := $(TBIN)/locate_objs
$(TBIN)/test_locate: tests/test_locate.c locate_linux.c harnessd.h $(FW)/clcd/clcd.c \
        $(FW)/clcd/clcd.h $(FW)/clcd_kvm/clcd_kvm.c $(FW)/common/net_proto.c | $(TBIN)
	@mkdir -p $(LOCATE_OBJ)
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -I$(HD) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD \
		-DMPS3_HAS_CLCD_KVM -c locate_linux.c -o $(LOCATE_OBJ)/locate_linux.o
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD_KVM \
		-c $(FW)/clcd_kvm/clcd_kvm.c -o $(LOCATE_OBJ)/clcd_kvm.o
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -I$(HD) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD \
		-DMPS3_CLCD_TEST_HOOKS -o $@ tests/test_locate.c $(LOCATE_OBJ)/locate_linux.o \
		$(LOCATE_OBJ)/clcd_kvm.o $(FW)/clcd/clcd.c $(FW)/clcd/hx8347_init.c \
		$(FW)/common/net_proto.c $(FW)/test/mock_regs.c

# THE REPAINT COST MODEL (lane CLCD-SPEED): the REAL clcd.c + clcd_kvm.c +
# hal_front.c (the strong bus seam) + the mirror's GRAM model + service.c's
# superloop over a backend that charges MBV costs to a modelled clock: a full
# screen and a page change within 150 ms, the superloop inside its budgets, the
# FIFO never overrun, the mirror pixel-exact, the KVM owner rule. The _negctl
# build is rc2_v6 (the weak per-byte push, 4 ms drain, 256 B polls) and must
# reproduce the glyph-by-glyph symptom (tests/test_clcd_speed.c).
CLCDSPEED_SRCS := tests/test_clcd_speed.c hal_front.c lcdmirror_model.c \
                  $(FW)/clcd/clcd.c $(FW)/clcd/hx8347_init.c $(FW)/clcd_kvm/clcd_kvm.c \
                  $(FW)/common/service.c
CLCDSPEED_DEPS := $(CLCDSPEED_SRCS) hal.h harnessd.h lcdmirror.h $(FW)/clcd/clcd.h
CLCDSPEED_CFLAGS := -std=gnu11 -O2 -g $(WARN) -I$(HD) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD \
                    -DMPS3_HAS_CLCD_KVM -DMPS3_CLCD_TEST_HOOKS
$(TBIN)/test_clcd_speed: $(CLCDSPEED_DEPS) | $(TBIN)
	$(HOST_CC) $(CLCDSPEED_CFLAGS) -DCLCD_BYTES_PER_PASS=$(HD_CLCD_BYTES) -o $@ $(CLCDSPEED_SRCS)
$(TBIN)/test_clcd_speed_negctl: $(CLCDSPEED_DEPS) | $(TBIN)
	$(HOST_CC) $(CLCDSPEED_CFLAGS) -DHARNESSD_CLCD_PUSH_PER_BYTE -DHARNESSD_CLCD_DRAIN_US=4000u \
		-DCLCD_BYTES_PER_PASS=256u -o $@ $(CLCDSPEED_SRCS)
# v0.17 presence + the panel (HM R1/R2): the session table's rules on a fake clock
# (the REAL presence_core.c + net_proto.c's parser), and the REAL panel_linux.c
# providers through the REAL codec with the clcd side (PANEL-CLCD's accessors) as
# test doubles: the reply budgets at their worst, the frame halves, the tap ring,
# the page refusal while the DUT owns the panel, the seam providers.
$(TBIN)/test_presence_core: tests/test_presence_core.c presence_core.c presence_core.h \
        $(FW)/common/net_proto.c | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -o $@ tests/test_presence_core.c presence_core.c $(FW)/common/net_proto.c
$(TBIN)/test_panel_render: tests/test_panel_render.c panel_linux.c presence_core.c presence_core.h \
        harnessd.h $(FW)/common/net_proto.c $(FW)/clcd/clcd.h | $(TBIN)
	$(HOST_CC) $(BASE_CFLAGS) -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_CLCD_KVM -DMPS3_HAS_TOUCH \
		-o $@ tests/test_panel_render.c panel_linux.c presence_core.c $(FW)/common/net_proto.c


# 6921 BATCHED (lane JTAG-SERVER-FIX): jtag_linux.c against jtag_server.c's own loop
# over the same streams -- identical replies and register accesses -- and the cost
# model (mps3_net_recv/send wrapped and counted).
JTAGB_SRCS := tests/test_jtag_batch.c jtag_linux.c $(FW)/jtag_server/jtag_server.c \
              $(FW)/common/net_if.c $(FW)/test/fake_net_if.c $(FW)/test/mock_regs.c \
              $(FW)/test/fake_jtag_tap.c
$(TBIN)/test_jtag_batch: $(JTAGB_SRCS) harnessd.h | $(TBIN)
	$(HOST_CC) -std=gnu11 -O2 -g $(WARN) -DMPS3_HAL_MOCK -I$(HD) \
		-Wl,--wrap=mps3_net_recv,--wrap=mps3_net_send -o $@ $(JTAGB_SRCS)

C_TESTS := test_jtag_batch test_hal_uio test_identify test_identify_linux \
           test_tofu test_tofu_baremetal test_sshfp test_engine_row \
           test_dut_clk test_dut_clk_baremetal test_lcdmirror test_identity_core test_locate \
           test_clcd_speed test_clcd_speed_negctl \
           test_presence_core test_panel_render

CODEC_DUMPS := $(CODEC_HEAD) codec_dump_new codec_dump_linux
tests: $(addprefix $(TBIN)/,$(C_TESTS) $(CODEC_DUMPS) lcdmirror_replay lcdmirror_vectors)

PYTEST ?= python3 -m pytest
# The conformance run against the EXISTING suite is HOST's stage 2 of `make
# check-linux` (HOST_CONTRACT §1/§3); `make conformance` is the same command, for
# a lane-local run. It is not repeated inside `check`: one harness per case table.
check: host host-echo host-uio tests
	@set -e; for t in $(C_TESTS); do echo "--- $$t ---"; $(TBIN)/$$t; done
	cd $(HD)/tests && HARNESSD_BIN=$(BUILD)/host/mps3-harnessd \
	    HARNESSD_UIO_BIN=$(BUILD)/host-uio/mps3-harnessd \
	    HARNESSD_TBIN=$(TBIN) \
	    MPS3_IDENTITY_BIN=$(BUILD)/host-uio/mps3-identity \
	    $(PYTEST) -q -p no:cacheprovider test_codec_identity.py test_harnessd_e2e.py \
	        test_slot_e2e.py test_slot_lock_e2e.py test_card_sharing_e2e.py \
	        test_fakeshell_parity.py test_lcdmirror_e2e.py test_lcdmirror_grab.py \
	        test_lcdmirror_capture.py \
	        test_identity_tool.py test_identity_e2e.py test_locate_e2e.py test_jtag_io_e2e.py \
	        test_panel_e2e.py
	@echo "=== harnessd check: ALL PASS ==="

conformance: host-echo
	cd $(REPO) && MPS3_HARNESSD_BIN=$(BUILD)/host-echo/mps3-harnessd \
	    MPS3_CONFORMANCE_REQUIRE=harnessd \
	    $(PYTEST) -q -p no:cacheprovider tests/firmware_logic/test_fakeshell_conformance.py

# ---- audit (HARNESSD_CONTRACT.md §1) -----------------------------------------
audit:
	@rm -rf $(BUILD)/audit && mkdir -p $(BUILD)/audit
	@for s in $(filter-out test/posix_net_if.c,$(FW_PRODUCT)); do \
	    $(HOST_CC) $(BASE_CFLAGS) $(PRODUCT_FLAGS) -DMPS3_HAL_UIO -c $(FW)/$$s \
	        -o $(BUILD)/audit/$$(echo $$s | tr / _).o || exit 1; done
	@nm -u $(BUILD)/audit/*.o | awk 'NF==2{print $$2}' | sort -u > $(BUILD)/audit/undef
	@nm --defined-only $(BUILD)/audit/*.o | awk 'NF==3{print $$3}' | sort -u > $(BUILD)/audit/def
	@echo "symbols the service modules need from outside themselves (libc omitted):"
	@comm -23 $(BUILD)/audit/undef $(BUILD)/audit/def | \
	    grep -Ev '^(mem(chr|cmp|cpy|move|set)|str(cmp|len|ncpy|chr)|v?snprintf|__.*)$$' | sed 's/^/  /'

clean:
	rm -rf $(BUILD)
