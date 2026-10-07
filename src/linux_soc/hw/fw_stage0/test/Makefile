# Host tests for the stage0 first-stage loader (board-free; host gcc + python3).
#
#   make            build + run everything below
#   make clean
#
# Every test links the SAME portable sources the rv32 target links. Two come
# from elsewhere and are compiled UNMODIFIED, read-only:
#   firmware/smsc911x + firmware/test/{fake_lan9220,mock_regs}.c   (this tree)
#   $(USD_SRC)/usd.c + $(USD_SRC)/../test/{fake_usd,mock_regs}.c   (D13)
# D13's driver is in-tree once D13 lands; until then USD_SRC falls back to the
# usd worktree. With neither present the uSD test is SKIPPED LOUDLY (CI before
# D13 lands) -- every other test still runs.
#
#   test_stage0_core / _edge   the loader core: pack -> parse -> CRC -> copy
#   test_stage0_flow           boot order, status block, try-once-then-confirm
#   test_stage0_sd             the uSD backend over D13's driver + card fake
#   test_stage0_cold           B2: READY-then-sector-0-fails cards (slow after power-up,
#                              deaf while a block is pending, corrupted R1, debris at
#                              entry, never readable) -- recovered, or bounded rescue
#   test_stage0_net            rescue: ARP/ICMP/UDP/TFTP/identify at frame level
#   test_stage0_eth            rescue over the real smsc911x driver + chip fake
#   test_stage0_hw             DDR gate (TELEM CTRL rewritten every entry, the calib
#                              HOLD) + cold settle + the watchdog arm/kick/WRS
#                              sequences against an HDL-faithful WDT model + the
#                              usd_spi hand-over (EN=0, CD kept), register by register
#   test_stage0_entry          S0-COLDFIX end to end: real flow + core + hw over the
#                              WDT + glitchy-TELEM models (s0_models.h): cold settle,
#                              hold, a 45 s load kept alive by kicks, a DDR stall ->
#                              watchdog -> run 1 -> run 2 -> rescue, PB0, Linux-era WDOG
#   test_stage0_cd_*           the uSD card-detect build knobs (default / CD_POL=1 /
#                              CD_IGNORE=1) through D13's driver + card fake
#   dfx_preflight              board_scripts/dfx_preflight.sh's cable filter, over a
#                              stub Vivado (tclsh): never another board's target
#   layout gate                stage0_status.py's table == the C header's
#   test_tools.py              pack --check, mkcard <-> C reader, push <-> the
#                              real TFTP server over localhost UDP, identify

CC   ?= cc
PY   ?= python3
CFLAGS := -O2 -Wall -Wextra -std=c11 -g
MOCKFLAGS := -std=c11 -Wall -Wextra -Wno-unused-parameter -Werror -g -O0 -DMPS3_HAL_MOCK

REPO := $(abspath ../../../../..)
FW   := $(REPO)/firmware
# Fallback for a checkout without the in-tree firmware/usd (READ-ONLY). No default.
USD_WORKTREE ?=
USD_SRC ?= $(if $(wildcard $(FW)/usd/usd.c),$(FW)/usd,$(if $(wildcard $(USD_WORKTREE)/usd.c),$(USD_WORKTREE),))
USD_TEST = $(USD_SRC)/../test

WORK := work
PACK := ../stage0_pack.py
S0   := ..
CORE := $(S0)/stage0_core.c $(S0)/stage0_boot.h
FLOW := $(S0)/stage0_flow.c $(S0)/stage0_flow.h $(S0)/stage0_status.h $(S0)/stage0_fmt.h
NET  := $(S0)/stage0_net.c $(S0)/stage0_tftp.c $(S0)/stage0_ident.c $(S0)/stage0_rescue.c \
        $(S0)/stage0_net.h $(S0)/stage0_tftp.h $(S0)/stage0_ident.h $(S0)/stage0_rescue.h
NET_C := $(S0)/stage0_net.c $(S0)/stage0_tftp.c $(S0)/stage0_ident.c $(S0)/stage0_rescue.c

.PHONY: all run clean layout sd
all: run

$(WORK):
	mkdir -p $(WORK)

# Synthetic payloads with distinctive, size-varied content, then a 4-region
# image at the real harness load addresses (matches dfx_swap_boot.tcl).
$(WORK)/boot.img: $(PACK) | $(WORK)
	$(PY) -c "import os; open('$(WORK)/r_fwjump.bin','wb').write(bytes((i*7+1)&0xff for i in range(4096)))"
	$(PY) -c "import os; open('$(WORK)/r_image.bin','wb').write(bytes((i*13+5)&0xff for i in range(200000)))"
	$(PY) -c "import os; open('$(WORK)/r_dtb.bin','wb').write(bytes((i*3+2)&0xff for i in range(1024)))"
	$(PY) -c "import os; open('$(WORK)/r_rootfs.bin','wb').write(bytes((i*11+9)&0xff for i in range(150000)))"
	$(PY) $(PACK) --out $@ --pc 0x80000000 --a1 0x82200000 \
	    $(WORK)/r_fwjump.bin@0x80000000 $(WORK)/r_image.bin@0x80400000 \
	    $(WORK)/r_dtb.bin@0x82200000 $(WORK)/r_rootfs.bin@0x84000000

$(WORK)/test_stage0_core: test_stage0_core.c $(CORE) | $(WORK)
	$(CC) $(CFLAGS) test_stage0_core.c $(S0)/stage0_core.c -o $@

# Edge-case companion: every s0_load() return code + boundary, images built in
# memory (no packer dependency). Links the same real stage0_core.c.
$(WORK)/test_stage0_edge: test_stage0_edge.c $(CORE) | $(WORK)
	$(CC) $(CFLAGS) test_stage0_edge.c $(S0)/stage0_core.c -o $@

$(WORK)/test_stage0_flow: test_stage0_flow.c s0_testutil.h $(CORE) $(FLOW) | $(WORK)
	$(CC) $(CFLAGS) -Werror test_stage0_flow.c $(S0)/stage0_flow.c $(S0)/stage0_core.c -o $@

$(WORK)/test_stage0_card: test_stage0_card.c s0_testutil.h $(CORE) $(FLOW) | $(WORK)
	$(CC) $(CFLAGS) -Werror test_stage0_card.c $(S0)/stage0_flow.c $(S0)/stage0_core.c -o $@

$(WORK)/test_stage0_net: test_stage0_net.c s0_testutil.h $(CORE) $(FLOW) $(NET) | $(WORK)
	$(CC) $(CFLAGS) -Werror test_stage0_net.c $(NET_C) $(S0)/stage0_flow.c $(S0)/stage0_core.c -o $@

$(WORK)/tftp_posix: tftp_posix.c $(CORE) $(FLOW) $(NET) | $(WORK)
	$(CC) $(CFLAGS) -Werror tftp_posix.c $(S0)/stage0_tftp.c $(S0)/stage0_ident.c \
	    $(S0)/stage0_flow.c $(S0)/stage0_core.c -o $@

$(WORK)/test_stage0_eth: test_stage0_eth.c s0_testutil.h $(CORE) $(FLOW) $(NET) \
        $(S0)/stage0_eth.c $(S0)/stage0_spin.c $(FW)/smsc911x/smsc911x.c | $(WORK)
	$(CC) $(MOCKFLAGS) -I$(FW)/smsc911x -I$(FW)/test test_stage0_eth.c \
	    $(S0)/stage0_eth.c $(S0)/stage0_spin.c $(NET_C) $(S0)/stage0_flow.c $(S0)/stage0_core.c \
	    $(FW)/smsc911x/smsc911x.c $(FW)/test/fake_lan9220.c $(FW)/test/mock_regs.c -o $@

# stage0_usd_cfg.h force-included into every TU: usd.c is built exactly as the
# target builds it (the Makefile's USD_CD_HOOK), and the test sees the same values.
S0_USD_CFG := -include $(S0)/stage0_usd_cfg.h
$(WORK)/test_stage0_sd: test_stage0_sd.c s0_testutil.h $(CORE) $(FLOW) $(S0)/stage0_sd.c \
        $(S0)/stage0_sd.h $(S0)/stage0_usd_cfg.h | $(WORK)
	$(CC) $(MOCKFLAGS) $(S0_USD_CFG) -I$(USD_SRC) -I$(USD_TEST) test_stage0_sd.c $(S0)/stage0_sd.c \
	    $(S0)/stage0_flow.c $(S0)/stage0_core.c $(USD_SRC)/usd.c $(USD_TEST)/fake_usd.c \
	    $(USD_TEST)/mock_regs.c -o $@

# stage0_hw.c reads D13's usd_regs.h (the usd_spi hand-over): in-tree firmware/usd
# since the D13 merge, whatever USD_SRC resolved to.
HW_USD_INC := $(or $(USD_SRC),$(FW)/usd)
$(WORK)/test_stage0_hw: test_stage0_hw.c s0_testutil.h s0_models.h $(S0)/stage0_hw.c $(S0)/stage0_hw.h \
        $(FW)/common/platform_regs.h | $(WORK)
	$(CC) $(MOCKFLAGS) -I$(FW)/common -I$(FW)/test -I$(HW_USD_INC) test_stage0_hw.c \
	    $(S0)/stage0_hw.c $(FW)/test/mock_regs.c -o $@

$(WORK)/test_stage0_entry: test_stage0_entry.c s0_testutil.h s0_models.h $(CORE) $(FLOW) \
        $(S0)/stage0_hw.c $(S0)/stage0_hw.h $(FW)/common/platform_regs.h | $(WORK)
	$(CC) $(MOCKFLAGS) -I$(FW)/common -I$(FW)/test -I$(HW_USD_INC) test_stage0_entry.c \
	    $(S0)/stage0_flow.c $(S0)/stage0_core.c $(S0)/stage0_hw.c $(FW)/test/mock_regs.c -o $@

# The B2 cold-card models (test_stage0_cold.c): D13's fake is #included INTO the
# test (its statics are the card model), so fake_usd.c is not linked separately.
$(WORK)/test_stage0_cold: test_stage0_cold.c s0_testutil.h $(CORE) $(FLOW) $(S0)/stage0_sd.c \
        $(S0)/stage0_sd.h $(S0)/stage0_usd_cfg.h | $(WORK)
	$(CC) $(MOCKFLAGS) $(S0_USD_CFG) -I$(USD_SRC) -I$(USD_TEST) test_stage0_cold.c $(S0)/stage0_sd.c \
	    $(S0)/stage0_flow.c $(S0)/stage0_core.c $(USD_SRC)/usd.c $(USD_TEST)/mock_regs.c -o $@

# The uSD card-detect build knobs (stage0_hw.h MPS3_USD_CD_POL / _IGNORE) through
# D13's REAL usd.c + card fake, one binary per knob setting, each compiled the way
# the target build compiles usd.o (the USD_CTRL_CD_DEFAULT hook). The default
# build on an inverted board is the negative control: no card seen.
CD_HOOK := -include $(S0)/stage0_usd_cfg.h -include $(S0)/stage0_hw.h -DUSD_CTRL_CD_DEFAULT=S0_USD_CD_CTRL
CD_SRCS := test_stage0_cd.c $(S0)/stage0_sd.c $(S0)/stage0_hw.c
define CD_VARIANT
$(WORK)/test_stage0_cd_$(1): $(CD_SRCS) s0_testutil.h $(S0)/stage0_hw.h $(S0)/stage0_sd.h $(S0)/stage0_usd_cfg.h | $(WORK)
	$(CC) $(MOCKFLAGS) $(CD_HOOK) $(2) -I$(FW)/common -I$(USD_SRC) -I$(USD_TEST) -o $$@ \
	    $(CD_SRCS) $(USD_SRC)/usd.c $(USD_TEST)/fake_usd.c $(USD_TEST)/mock_regs.c
endef
$(eval $(call CD_VARIANT,default,))
$(eval $(call CD_VARIANT,pol,-DMPS3_USD_CD_POL=1))
$(eval $(call CD_VARIANT,ignore,-DMPS3_USD_CD_IGNORE=1))
CD_BINS := $(WORK)/test_stage0_cd_default $(WORK)/test_stage0_cd_pol $(WORK)/test_stage0_cd_ignore

$(WORK)/test_stage0_layout: test_stage0_layout.c $(S0)/stage0_status.h | $(WORK)
	$(CC) $(CFLAGS) -Werror test_stage0_layout.c -o $@

# THE BOARD IDENTITY (lane IDENT): built with board 2's stage0_board.py flags, as a
# board-2 bake is; plus the publish-before-boot-order source check (+ its negative
# control). Run from `layout` (the status-block gate).
IDENT_B2_DEFS = $(shell $(PY) $(S0)/stage0_board.py --label MPS3-02 --ip 192.168.11.101 --mac 02:00:00:00:02:FE)
$(WORK)/test_stage0_ident: test_stage0_ident.c s0_testutil.h $(S0)/stage0_status.h $(FLOW) \
        $(S0)/stage0_board.py | $(WORK)
	$(CC) $(CFLAGS) -Werror $(IDENT_B2_DEFS) test_stage0_ident.c $(S0)/stage0_flow.c $(S0)/stage0_core.c -o $@

# the layout gate: the Python decoder's table must equal the C header's
layout: $(WORK)/test_stage0_layout $(WORK)/test_stage0_ident
	@$(WORK)/test_stage0_ident
	@$(PY) check_ident_order.py $(S0)/stage0.c
	@$(WORK)/test_stage0_layout > $(WORK)/layout_c.txt
	@$(PY) $(S0)/stage0_status.py --layout > $(WORK)/layout_py.txt
	@diff -u $(WORK)/layout_c.txt $(WORK)/layout_py.txt || \
	  { echo "LAYOUT GATE FAIL: stage0_status.py FIELDS != stage0_status.h S0_STATUS_FIELDS"; exit 1; }
	@echo "layout gate: stage0_status.py == stage0_status.h ($$(wc -l < $(WORK)/layout_c.txt) fields)"

sd:
ifeq ($(USD_SRC),)
	@echo "   SKIP test_stage0_sd + test_stage0_cd_*: no D13 firmware/usd (in-tree or $(USD_WORKTREE)) -- the uSD backend is UNTESTED in this run"
else
	@$(MAKE) --no-print-directory $(WORK)/test_stage0_sd $(WORK)/test_stage0_cold $(CD_BINS) USD_SRC=$(USD_SRC)
	$(WORK)/test_stage0_sd
	$(WORK)/test_stage0_cold
	@set -e; for t in $(CD_BINS); do $$t; done
endif

run: $(WORK)/test_stage0_core $(WORK)/test_stage0_edge $(WORK)/boot.img $(WORK)/test_stage0_flow \
     $(WORK)/test_stage0_net $(WORK)/test_stage0_eth $(WORK)/test_stage0_card $(WORK)/tftp_posix \
     $(WORK)/test_stage0_hw $(WORK)/test_stage0_entry
	$(WORK)/test_stage0_core $(WORK)/boot.img \
	    $(WORK)/r_fwjump.bin@0x80000000 $(WORK)/r_image.bin@0x80400000 \
	    $(WORK)/r_dtb.bin@0x82200000 $(WORK)/r_rootfs.bin@0x84000000
	$(WORK)/test_stage0_edge
	$(WORK)/test_stage0_flow
	@$(MAKE) --no-print-directory sd
	$(WORK)/test_stage0_net
	$(WORK)/test_stage0_eth
	$(WORK)/test_stage0_hw
	$(WORK)/test_stage0_entry
	@$(MAKE) --no-print-directory layout
	$(PY) test_tools.py
	sh $(S0)/../board_scripts/test_dfx_preflight.sh
	@echo "stage0 host tests: ALL PASSED"

clean:
	rm -rf $(WORK)
