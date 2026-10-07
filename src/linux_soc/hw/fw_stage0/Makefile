# Makefile -- MicroBlaze-V first-stage loader (stage0) for the Linux harness.
#
#   make                  target build: stage0.elf/.bin/.lst/.map/.coe + budget
#   make stage0.elf MPS3_STATIC_ID=0x... MPS3_HARNESS_VER32=0x...
#                         what FLOW's `mint-stage0` runs (after `make clean`):
#                         a stage0 FOR one static (STAGE0_CONTRACT §2)
#   make size-gate        the budget gate + its negative controls
#                         (includes: the watchdog is armed and usd_spi handed over
#                         (EN=0) before every hand-off)
#   make stage0.elf MPS3_USD_CD_POL=1 | MPS3_USD_CD_IGNORE=1
#                         the uSD card-detect polarity / override (stage0_hw.h)
#   make stage0.elf COLD_SETTLE_MS=10000 CALIB_HOLD_MS=1000
#                         cold-entry settle + DDR calib hold (the defaults)
#   make stage0.elf S0_LABEL=MPS3-02 [S0_IP=a.b.c.d S0_MAC=xx:..:xx | S0_BOARD=mps3_02]
#                         this BOARD's identity (IP/MAC may instead come as -DS0_IP /
#                         -DMPS3_MACn in S0_EXTRA_DEFS), published in the status block
#                         at every entry (STAGE0_CONTRACT §3.3)
#   make test             host tests (board-free; same as make -C test)
#
# Freestanding rv32 build (own crt0.S + stage0.ld, no libc). The portable
# modules are shared verbatim with the host tests under test/. Two sources come
# from elsewhere, compiled UNMODIFIED:
#   $(FW)/smsc911x/smsc911x.c   the bare-metal LAN9220 driver (rescue network)
#   $(USD_SRC)/usd.c            D13's user-microSD driver (slot reads)

REPO    := $(abspath ../../../..)
FW      := $(REPO)/firmware
# D13's driver: in-tree (firmware/usd). USD_WORKTREE is only a fallback for a
# checkout that predates it: an out-of-tree firmware/usd, READ-ONLY. No default.
USD_WORKTREE ?=
USD_SRC ?= $(if $(wildcard $(FW)/usd/usd.c),$(FW)/usd,$(USD_WORKTREE))

XILINX_VIVADO ?= /research/CAD/Xilinx/Vivado/2026.1
CROSS         ?= $(XILINX_VIVADO)/gnu/riscv/lin/bin/riscv64-unknown-elf-

CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump
SIZE    := $(CROSS)size
PY      ?= python3

# ---- identity + policy knobs (STAGE0_CONTRACT §2) ------------------------------
MPS3_STATIC_ID     ?= 0x00000000
MPS3_VER32         ?= 0x00000000
MPS3_HARNESS_VER32 ?= $(MPS3_VER32)
S0_BUILD_ID        ?= 0x$(or $(shell git -C $(REPO) rev-parse --short=8 HEAD 2>/dev/null),00000000)
BOOT_LIMIT         ?= 2
CALIB              ?= telem
WDOG_ARM           ?= 1
# uSD card detect (stage0_hw.h; STAGE0_CONTRACT §2). usd_spi's CD bits are not
# JTAG-reachable on the MBV and every reset clears them, so stage0 sets them:
# an inverted detect on silicon is a rebuild + updatemem re-bake, not a re-mint.
MPS3_USD_CD_POL    ?= 0
MPS3_USD_CD_IGNORE ?= 0
# uSD SCK once the card is READY, as a CLKDIV (stage0_usd_cfg.h): 15 = 3.125 MHz
# (default, the B2 no-mint mitigation), 7 = 6.25 MHz, 3 = 12.5 MHz (D13's rate).
S0_USD_CLKDIV      ?= 15
# Cold-boot hardening (lane S0-COLDFIX, stage0_flow.h / stage0_hw.h): the wait
# on a cold entry (FPGA just configured: the MCC is still setting clocks up)
# before the first DDR/card access, and how long the DDR calib bit must hold 1.
COLD_SETTLE_MS     ?= 10000
CALIB_HOLD_MS      ?= 1000
S0_EXTRA_DEFS      ?=

ifeq ($(CALIB),telem)
CALIB_DEF := -DS0_CALIB_TELEM=1
else ifeq ($(CALIB),none)
CALIB_DEF := -DS0_CALIB_TELEM=0
else
$(error CALIB must be telem or none, not '$(CALIB)')
endif

ifneq ($(filter-out 0 1,$(MPS3_USD_CD_POL) $(MPS3_USD_CD_IGNORE)),)
$(error MPS3_USD_CD_POL and MPS3_USD_CD_IGNORE must each be 0 or 1, not '$(MPS3_USD_CD_POL)' / '$(MPS3_USD_CD_IGNORE)')
endif

DEFS := -DMPS3_STATIC_ID=$(MPS3_STATIC_ID) -DMPS3_VER32=$(MPS3_HARNESS_VER32) \
        -DS0_BUILD_ID=$(S0_BUILD_ID) -DS0_BOOT_LIMIT=$(BOOT_LIMIT)u $(CALIB_DEF) \
        -DS0_WDOG_ARM=$(WDOG_ARM) -DMPS3_USD_CD_POL=$(MPS3_USD_CD_POL) \
        -DMPS3_USD_CD_IGNORE=$(MPS3_USD_CD_IGNORE) -DS0_USD_CLKDIV=$(S0_USD_CLKDIV)u \
        -DS0_COLD_SETTLE_MS=$(COLD_SETTLE_MS)u -DS0_CALIB_HOLD_MS=$(CALIB_HOLD_MS)u \
        $(S0_EXTRA_DEFS)
# D13's usd.c, compiled unmodified: its USD_CTRL_CD_DEFAULT hook (usd.h) takes
# stage0's card-detect bits (stage0_hw.h S0_USD_CD_CTRL), and stage0_usd_cfg.h
# sets the usd.h tunables for a blocking loader (data SCK, NCR, token wait,
# CMD0 drain, ACMD41) -- stage0_sd.c includes the same header first.
USD_CD_HOOK := -include stage0_usd_cfg.h -include stage0_hw.h -DUSD_CTRL_CD_DEFAULT=S0_USD_CD_CTRL

# ---- the BOARD's identity (lane IDENT; STAGE0_CONTRACT §2/§3.3) --------------------
# S0_LABEL (default MPS3-01) is always compiled in. The IP and MAC default to
# stage0.c's (board 1: 192.168.10.101, 02:00:00:4D:50:53) and come either as make
# vars (S0_IP= / S0_MAC=, or S0_BOARD=<name> -> boards/<name>.mk sets all three;
# a command-line value beats the board file) or as -DS0_IP / -DMPS3_MACn in
# S0_EXTRA_DEFS (the bake scripts' form) -- never both for the same value.
# stage0_board.py validates what it is given (the Linux resolver's rules) and
# emits the -D flags; a bad value fails the build.
S0_BOARD ?=
ifneq ($(S0_BOARD),)
ifeq ($(wildcard boards/$(S0_BOARD).mk),)
$(error S0_BOARD=$(S0_BOARD): no boards/$(S0_BOARD).mk (have: $(notdir $(basename $(wildcard boards/*.mk)))))
endif
include boards/$(S0_BOARD).mk
endif
S0_LABEL ?= MPS3-01
S0_IP    ?=
S0_MAC   ?=
ifneq ($(and $(S0_IP),$(filter -DS0_IP=%,$(S0_EXTRA_DEFS))),)
$(error S0_IP=$(S0_IP) and S0_EXTRA_DEFS -DS0_IP: give the board's IP once)
endif
ifneq ($(and $(S0_MAC),$(filter -DMPS3_MAC%,$(S0_EXTRA_DEFS))),)
$(error S0_MAC=$(S0_MAC) and S0_EXTRA_DEFS -DMPS3_MACn: give the board's MAC once)
endif
ifneq ($(filter -DS0_LABEL%,$(S0_EXTRA_DEFS)),)
$(error S0_EXTRA_DEFS carries -DS0_LABEL_*: use S0_LABEL=<name> (validated))
endif
IDENT_DEFS := $(shell $(PY) stage0_board.py --label '$(S0_LABEL)' --ip '$(S0_IP)' --mac '$(S0_MAC)' 2>&1)
ifneq ($(.SHELLSTATUS),0)
$(error $(IDENT_DEFS))
endif

TARGET  := stage0
LDSCRIPT:= $(TARGET).ld
OBJDIR  := obj

ARCH    := -march=rv32imac -mabi=ilp32 -mcmodel=medany -mno-relax
CFLAGS  := $(ARCH) -ffreestanding -nostartfiles -O2 -g -Wall -Wextra -Werror \
           -fno-common -ffunction-sections -fdata-sections \
           -I. -I$(FW)/common -I$(FW)/smsc911x -I$(USD_SRC) $(DEFS) $(IDENT_DEFS)
LIBCFLAGS := $(CFLAGS) -fno-builtin -fno-tree-loop-distribute-patterns
ASFLAGS := $(ARCH) -ffreestanding -nostartfiles -g
LDFLAGS := $(ARCH) -ffreestanding -nostartfiles -nostdlib \
           -T $(LDSCRIPT) -Wl,--gc-sections \
           -Wl,--build-id=none -Wl,--no-warn-rwx-segments

LOCAL_SRCS := stage0.c stage0_core.c stage0_flow.c stage0_sd.c stage0_net.c \
              stage0_tftp.c stage0_ident.c stage0_rescue.c stage0_eth.c stage0_spin.c \
              stage0_hw.c
OBJS := $(OBJDIR)/crt0.o $(LOCAL_SRCS:%.c=$(OBJDIR)/%.o) $(OBJDIR)/stage0_libc.o \
        $(OBJDIR)/smsc911x.o $(OBJDIR)/usd.o
HDRS := $(wildcard *.h) $(FW)/smsc911x/smsc911x.h $(FW)/common/platform_regs.h \
        $(USD_SRC)/usd.h $(USD_SRC)/usd_regs.h

.PHONY: all size clean test size-gate check-usd-src FORCE
all: $(TARGET).elf $(TARGET).bin $(TARGET).lst $(TARGET).coe size

check-usd-src:
	@test -f $(USD_SRC)/usd.c || { echo "error: USD_SRC=$(USD_SRC) has no usd.c (D13's firmware/usd)"; exit 1; }

# A changed -D (static_id!) must rebuild: make cannot see it, so a stamp does.
$(OBJDIR)/flags.stamp: FORCE | $(OBJDIR)
	@echo '$(CFLAGS) $(USD_SRC)' | cmp -s - $@ || echo '$(CFLAGS) $(USD_SRC)' > $@

$(OBJDIR):
	mkdir -p $(OBJDIR)

$(OBJDIR)/crt0.o: crt0.S | $(OBJDIR)
	$(CC) $(ASFLAGS) -c $< -o $@
$(OBJDIR)/stage0_libc.o: stage0_libc.c $(OBJDIR)/flags.stamp
	$(CC) $(LIBCFLAGS) -c $< -o $@
$(OBJDIR)/%.o: %.c $(HDRS) $(OBJDIR)/flags.stamp
	$(CC) $(CFLAGS) -c $< -o $@
$(OBJDIR)/smsc911x.o: $(FW)/smsc911x/smsc911x.c $(HDRS) $(OBJDIR)/flags.stamp
	$(CC) $(CFLAGS) -c $< -o $@
$(OBJDIR)/usd.o: $(USD_SRC)/usd.c $(HDRS) $(OBJDIR)/flags.stamp | check-usd-src
	$(CC) $(CFLAGS) $(USD_CD_HOOK) -c $< -o $@

$(TARGET).elf: $(OBJS) $(LDSCRIPT)
	$(CC) $(CFLAGS) $(OBJS) $(LDFLAGS) -Wl,-Map=$(TARGET).map -o $@
$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@
$(TARGET).lst: $(TARGET).elf
	$(OBJDUMP) -d -S $< > $@
$(TARGET).coe: $(TARGET).bin gen_coe.py
	$(PY) gen_coe.py $(TARGET).bin $(TARGET).coe

size: $(TARGET).elf
	$(SIZE) $<
	@$(PY) check_budget.py --map $(TARGET).map --elf $< --report-only

# ---- the budget gate + negative controls --------------------------------------------
# check_budget.py (map file + ELF symbols) requires: >= 48 KiB free between the
# image end and the stack bottom, stack top == 0x1FE00, no .data, the rescue
# network stack <= 24 KiB, NO card-write path linked (stage0 is read-only on the
# card), and the watchdog arm + DDR gate + the usd_spi hand-over (EN=0 before
# the jump, D13 §12.2) ARE linked (--require-wdog-arm). Each negative control
# links a stage0 that breaks exactly one rule and the gate must REFUSE it --
# otherwise the gate proves nothing.
NEG_PAD_KIB := 80
$(OBJDIR)/negctl_pad.o: | $(OBJDIR)
	printf '\t.section .rodata.negctl_pad,"a"\n\t.global negctl_pad\nnegctl_pad:\n\t.space %d\n' \
	    $$(( $(NEG_PAD_KIB) * 1024 )) > $(OBJDIR)/negctl_pad.S
	$(CC) $(ASFLAGS) -c $(OBJDIR)/negctl_pad.S -o $@
$(OBJDIR)/negctl_write.o: | $(OBJDIR)
	printf '#include "usd.h"\nint negctl_write(const void *b) { return usd_write_start(0u, 1u, b); }\n' \
	    > $(OBJDIR)/negctl_write.c
	$(CC) $(CFLAGS) -c $(OBJDIR)/negctl_write.c -o $@

$(OBJDIR)/stage0_nowdog.o: stage0.c $(HDRS) $(OBJDIR)/flags.stamp
	$(CC) $(filter-out -DS0_WDOG_ARM=%,$(CFLAGS)) -DS0_WDOG_ARM=0 -c $< -o $@
$(OBJDIR)/stage0_norelease.o: stage0.c $(HDRS) $(OBJDIR)/flags.stamp
	$(CC) $(CFLAGS) -DS0_NEGCTL_NO_USD_RELEASE -c $< -o $@

negctl_pad.elf: $(OBJS) $(OBJDIR)/negctl_pad.o $(LDSCRIPT)
	$(CC) $(CFLAGS) $(OBJS) $(OBJDIR)/negctl_pad.o $(LDFLAGS) \
	    -Wl,--undefined=negctl_pad -Wl,-Map=negctl_pad.map -o $@
negctl_write.elf: $(OBJS) $(OBJDIR)/negctl_write.o $(LDSCRIPT)
	$(CC) $(CFLAGS) $(OBJS) $(OBJDIR)/negctl_write.o $(LDFLAGS) \
	    -Wl,--undefined=negctl_write -Wl,-Map=negctl_write.map -o $@

negctl_nowdog.elf: $(filter-out $(OBJDIR)/stage0.o,$(OBJS)) $(OBJDIR)/stage0_nowdog.o $(LDSCRIPT)
	$(CC) $(CFLAGS) $(filter-out $(OBJDIR)/stage0.o,$(OBJS)) $(OBJDIR)/stage0_nowdog.o $(LDFLAGS) \
	    -Wl,-Map=negctl_nowdog.map -o $@
negctl_norelease.elf: $(filter-out $(OBJDIR)/stage0.o,$(OBJS)) $(OBJDIR)/stage0_norelease.o $(LDSCRIPT)
	$(CC) $(CFLAGS) $(filter-out $(OBJDIR)/stage0.o,$(OBJS)) $(OBJDIR)/stage0_norelease.o \
	    $(LDFLAGS) -Wl,-Map=negctl_norelease.map -o $@

size-gate: $(TARGET).elf negctl_pad.elf negctl_write.elf negctl_nowdog.elf negctl_norelease.elf
	@echo "== stage0 size gate =="
	$(PY) check_budget.py --require-wdog-arm --map $(TARGET).map --elf $(TARGET).elf
	@echo "== negative control 1: +$(NEG_PAD_KIB) KiB of rodata must FAIL the free-space rule =="
	@if $(PY) check_budget.py --map negctl_pad.map --elf negctl_pad.elf; then \
	   echo "NEGATIVE CONTROL FAILED: the gate accepted a stage0 with < 48 KiB free"; exit 1; \
	 else echo "   ok: refused, as it must be"; fi
	@echo "== negative control 2: a linked usd_write_start must FAIL the read-only rule =="
	@if $(PY) check_budget.py --map negctl_write.map --elf negctl_write.elf; then \
	   echo "NEGATIVE CONTROL FAILED: the gate accepted a stage0 that can write the card"; exit 1; \
	 else echo "   ok: refused, as it must be"; fi
	@echo "== negative control 3: a WDOG_ARM=0 build must FAIL the shipping rule =="
	@if $(PY) check_budget.py --require-wdog-arm --map negctl_nowdog.map --elf negctl_nowdog.elf; then \
	   echo "NEGATIVE CONTROL FAILED: the gate accepted a stage0 that never arms the watchdog"; exit 1; \
	 else echo "   ok: refused, as it must be"; fi
	@echo "== negative control 4: a stage0 that jumps with usd_spi still enabled must FAIL =="
	@if $(PY) check_budget.py --require-wdog-arm --map negctl_norelease.map --elf negctl_norelease.elf; then \
	   echo "NEGATIVE CONTROL FAILED: the gate accepted a stage0 that never hands usd_spi over"; exit 1; \
	 else echo "   ok: refused, as it must be"; fi
	@echo "== negative control 5: a card-detect knob that is not 0/1 must FAIL the build =="
	@if $(MAKE) --no-print-directory -n $(TARGET).elf MPS3_USD_CD_POL=2 >/dev/null 2>&1; then \
	   echo "NEGATIVE CONTROL FAILED: MPS3_USD_CD_POL=2 was accepted"; exit 1; \
	 else echo "   ok: refused, as it must be"; fi
	@echo "== negative control 6: a bad or doubled board identity must FAIL the build (IDENT) =="
	@for bad in "S0_LABEL=mps3-02" "S0_LABEL=MPS3-0001" "S0_LABEL=" "S0_MAC=01:00:00:00:00:01" \
	            "S0_MAC=00:00:00:00:00:00" "S0_IP=192.168.10.300" "S0_IP=192.168.11.255" \
	            "S0_BOARD=no_such_board" "S0_EXTRA_DEFS=-DS0_LABEL_LO=0x41u" \
	            "S0_IP=192.168.11.101 S0_EXTRA_DEFS=-DS0_IP=0xC0A80B65u"; do \
	   if $(MAKE) --no-print-directory -n $(TARGET).elf $$bad >/dev/null 2>&1; then \
	     echo "NEGATIVE CONTROL FAILED: $$bad was accepted"; exit 1; fi; \
	   echo "   ok: $$bad refused"; done
	@echo "size-gate: PASS"

test:
	$(MAKE) -C test

clean:
	rm -rf $(OBJDIR) $(TARGET).elf $(TARGET).bin $(TARGET).lst $(TARGET).map $(TARGET).coe \
	       negctl_pad.elf negctl_pad.map negctl_write.elf negctl_write.map \
	       negctl_nowdog.elf negctl_nowdog.map negctl_norelease.elf negctl_norelease.map
	$(MAKE) -C test clean 2>/dev/null || true

FORCE:
