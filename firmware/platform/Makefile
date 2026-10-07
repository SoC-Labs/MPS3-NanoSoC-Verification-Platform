# firmware/platform/Makefile — MicroBlaze ELF cross-compile (A3, W-VITIS).
#
# Stage 2 of the two-step target build (see README.md "Build (repro)"):
#   1. xsct -nodisp create_platform.tcl <xsa> build/vitis_fw
#        -> platform + standalone BSP + lwip220 (libxil.a / liblwip4.a /
#           xparameters.h / lwipopts.h)
#   2. make -C firmware/platform elf [WS=...] [LMB_KB=128|256] [BLOB=...]
#        -> build/vitis_fw ELF via mb-gcc directly
#
# WHY a plain Makefile instead of `app create`+`app build`: the classic
# xsct app commands talk to an Eclipse "XSDx" backend that needs an X
# display even under xsct (confirmed live 2026-07-07: "Unable to init
# server: Could not connect: Connection refused" on a headless host, which
# is also why the xsct wrapper insists on Xvfb). hsi-side commands
# (platform/BSP) are display-free, so the split is: xsct generates the
# BSP, this Makefile compiles/links the app — the tasking's sanctioned
# "plain Makefile using the Vitis mb-gcc with BSP includes/libs" route.
#
# NOT related to firmware/test/Makefile (host-gcc harness) — that flow is
# untouched and must stay green independently.

VITIS  ?= /apps/Xilinx/Vitis/2024.1
REPO   := $(abspath ../..)
WS     ?= $(REPO)/build/vitis_fw
BSP    := $(WS)/shell_platform/microblaze_0/standalone_domain/bsp/microblaze_0

# DEFAULTS = the build that will actually run once the W-BD one-parameter
# LMB doubling lands (fpga/shell README address-map note). 128 KiB — the
# shell AS BUILT TODAY — is proven not to fit: .bss overflows by 53,200
# bytes even after the sniprintf slimming below (and by 101,016 before
# it); see BUILD_RESULT.txt. `make elf LMB_KB=128` reproduces that
# evidence loudly rather than silently shipping an unrunnable image.
# 1024 = the fielded shell (docs/FIELDED_SHELL.md `lmb_kb`, the one authority).
# 512 / 256 = earlier shells (build/shell_proj_512k, build/shell_proj_256k).
#
# This MUST track the shell you are building against. It is not cosmetic: the
# diag mailbox is anchored to the END of the LMB (256 B since diag v8), so
# LMB_KB moves it
#   256 KB -> 0x0003FF00   512 KB -> 0x0007FF00   1 MB -> 0x000FFF00
#   (v5..v7 images, 128 B: 0x0003FF80 / 0x0007FF80 / 0x000FFF80)
# and the LMB address decode ALIASES, so reading the 1 MB address on a 512 KB
# shell silently returns the 512 KB mailbox's contents rather than failing.
# scripts/mps3_diag.tcl scans for the magic instead of trusting any constant.
# 1 MiB is what lets nanosoc's 155,864 B clearing live in RAM; see the two
# clearing buffers below and fpga/shell/bd/shell_bd.tcl's local_ram note.
LMB_KB ?= 1024
# Greybox clearing blob mode. DEFAULT = real (gate 3): with large clearings
# now QSPI-resident (config_agent/overlay_store/swap_fsm route them to the
# OVLSTORE clearing STAGE/CACHE region instead of RAM), the RAM staging
# buffers shrink to STAGING_BYTES below and the reclaimed LMB fits the true
# 47,808 B greybox clearing baked in .rodata (BUILD_RESULT.txt has the
# numbers). `BLOB=placeholder` (64 ICAP NOPs) remains for size experiments.
BLOB   ?= real

# ===========================================================================
# PRODUCT=1 — THE FIELDED FLAG SET, as ONE name.
# ===========================================================================
# `make elf` with the DEFAULTS below builds an image that pings, reports the
# right static_id, passes every acceptance gate -- and cannot load a single RM
# (HWICAP unset => LITE writers vs a FIFO shell; see the HWICAP_FIFO block).
# The set that is actually ON THE BOARD existed only as five literals inside a
# mint script, which is exactly how a mismatched image ships.
#
#   PRODUCT=1  ==  CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1 DUT_EGRESS=1
#
# That is the flag set the FIELDED shell's firmware was built with
# (docs/FIELDED_SHELL.md; static_id 0xA8C1C535). Its FABRIC pair is
# SHELL_TOUCH=1 on the bitstream build -- the AXI IIC master the touch driver
# talks to only exists in a shell minted with it, so PRODUCT=1 firmware belongs
# on a SHELL_TOUCH=1 bitstream and nowhere else.
#
# `?=` on purpose: an explicit knob on the command line still wins, so
# `make elf PRODUCT=1 TOUCH=` builds the fielded set minus touch. This block
# MUST stay above the five `?=` defaults it feeds -- a `FOO ?=` that has
# already run has DEFINED FOO (as empty), and a later `FOO ?= 1` is then a
# no-op.
PRODUCT ?=
ifeq ($(PRODUCT),1)
CLCD        ?= 1
CLCD_KVM    ?= 1
HWICAP_FIFO ?= 1
WINDOWED    ?= 1
TOUCH       ?= 1
DUT_EGRESS  ?= 1
endif

# Which overlay identity the greybox blob is generated FROM. UNSET => whatever
# fpga/dfx/overlay/greybox/ currently holds (gen_greybox_blob.py's own defaults),
# which is the right thing when you are baking firmware for the shell you just
# built.
#
# ⚠ IT IS THE WRONG THING FOR A FIRMWARE-ONLY RE-BAKE INTO AN OLDER .bit, and
# there was no way to say so before these two knobs existed. fpga/dfx/overlay/ is
# ONE directory that every re-key OVERWRITES in place, and the re-keyed manifests
# are what get committed — so the tree only ever carries the identity of the LAST
# shell built. Baking firmware for any earlier bitstream therefore silently
# produces an ELF whose strong mps3_shell_static_id() returns the WRONG static_id:
# the shell pings, answers, passes a link check, reports an id that does not match
# the fabric it is running on, and config_agent rejects every overlay pushed to it
# (net_proto's static_id match). Same failure SHAPE as the LITE-vs-FIFO HWICAP trap
# below — healthy-looking shell, no RM will ever load — and equally invisible at
# build time.
#
# Measured instance of the hazard (2026-07-31, this tree): the shipped
# 0x0EE58A4D shell (fpga/dfx/build_qspi_kvm_irq) has NO surviving manifest
# anywhere — fpga/dfx/overlay/ on master carries 0xCD74B6AE (the JTAG re-mint)
# and on this branch 0xD84A2E7A (retired), and `git log -S0x0EE58A4D --
# fpga/dfx/overlay/` is EMPTY, so that identity was never committed. It has to
# be reconstructed from the build tree's own record (prod/static_id.txt +
# prod/overlay_inputs.txt + the prod clearing/partial .bin pair, via
# fpga/dfx/gen_manifest.py) and pointed at from here.
#
#   make elf BLOB_MANIFEST=/path/to/manifest.json BLOB_BIN=/path/to/greybox_clear.bin
#
# Both are passed straight through to gen_greybox_blob.py --manifest/--bin; unset
# means "don't pass the option", so a plain `make elf` is byte-identical to before
# these knobs existed. BLOB_BIN only matters when BLOB=real (placeholder mode
# never reads the payload, only the identity).
BLOB_MANIFEST ?=
BLOB_BIN      ?=
BLOB_ARGS := --mode $(BLOB)
ifneq ($(BLOB_MANIFEST),)
BLOB_ARGS += --manifest $(BLOB_MANIFEST)
endif
ifneq ($(BLOB_BIN),)
BLOB_ARGS += --bin $(BLOB_BIN)
endif

CC     := $(VITIS)/gnu/microblaze/lin/bin/mb-gcc
SIZE   := $(VITIS)/gnu/microblaze/lin/bin/mb-size
OBJDUMP:= $(VITIS)/gnu/microblaze/lin/bin/mb-objdump

# CPU flags MUST match the BSP library build exactly (multilib/ABI): these
# are lifted from the platform-generate log's COMPILER_FLAGS (xsa-derived:
# MicroBlaze v11.0, little-endian AXI, HW mul/div, barrel shifter).
CPUFLAGS := -mcpu=v11.0 -mlittle-endian -mno-xl-soft-div -mno-xl-soft-mul -mxl-barrel-shift

# -Dsnprintf=sniprintf: newlib's documented integer-only printf variant.
# snprintf is the ONLY printf-family call in portable firmware (10 sites,
# all %s/%d/%u/%x/%c — no float formatting anywhere), but plain snprintf
# drags newlib's float engine into .text: svfprintf (11.2 KiB) + dtoa
# (7.2 KiB) + mprec (5.0 KiB), measured from the link map. The redirect
# keeps the exact same semantics for every format string this tree uses.
# Staging (see README "Memory honesty"). GATE 3 removed the big RAM clearing
# buffers: a LARGE clearing (nanosoc 117,684 B) is now QSPI-resident in the
# OVLSTORE clearing STAGE/CACHE region (in_qspi==1), never RAM — so the three
# RAM staging buffers (config_agent clearing + partial, swap_fsm arena) only
# have to cover the small-clearing / greybox fast path and shrink to a few
# KiB each. This is the structural win that reclaims the LMB and lets the
# real 47,808 B greybox blob bake in. Clearings are still RM-SPECIFIC
# (greybox 47,808 / led 49,628 / nanosoc 117,684 B), but size no longer
# forces a RAM buffer — the gate-3 path handles any of them.
STAGING_BYTES ?= 4096    # config_agent's staging (partial fast path)

# ---- The two clearing buffers. Default = QSPI-FREE, every RM. ---------------
# Sized so that neither the incoming clearing nor the resident one ever reaches
# the QSPI flash. That interface has never worked on silicon
# (docs/QSPI_CLEARING_CACHE_HW_FINDINGS.md) and D16 (ARCHITECTURE_SPEC.md §15)
# says keep it idle: one physical 8 MiB part backs BOTH the DFX overlay store and
# the nanoSoC boot map, and overlay_store_bp_unlock() strips its write protection.
#
# 196,608 = 192 KiB, chosen for MARGIN not fit: config_agent.c:359 routes to QSPI
# on `payload > cap`, so an exact-size buffer clears the flash path by ONE BYTE.
# 40,744 B of slack over the largest clearing.
#
# Clearing sizes at static_id 0xAAF21306:
#   regdemo_a/b 44,332 · led 44,332 · greybox 52,820 · uart_echo 68,604
#   eth_ss 85,420 · nanosoc 155,864   <-- the one that forced LMB_KB 512 -> 1024
#
# The two buffers must COEXIST: the incoming clearing sits in config_agent's slot
# while SWAP_STREAM_CLEARING replays the outgoing one from swap_fsm's arena. So
# dec = 226,652 + 2X. At LMB_KB=512 (usable 524,080) X capped at 148,714 and
# nanosoc's 155,864 B clearing overflowed local_lmb by 14,152 B -- measured. At
# LMB_KB=1024 (usable 1,048,368) X caps at ~410,858, so every RM fits in RAM.
#
# Gated by scripts/harness_gates/check_clearing_fits.py against the real overlay
# manifests -- a new RM with a bigger clearing cannot slip through unnoticed. It
# is what caught nanosoc's growth (117,684 -> 155,864) in the first place.

# The RESIDENT clearing lives here. It must be big enough to hold a real one, or
# swap-AWAY is impossible: swap_fsm refuses to cache an oversized clearing and the
# next swap fails closed at SWAP_STREAM_CLEARING. Measured on silicon: regdemo_b's
# clearing is 51,952 B. 4 KiB (the old shared value) could never work; 69,632 (the
# old default) held neither nanosoc nor eth_ss. 196608 held every RM through
# 2026-07-10; the multicore-eth RM (rm_nanosoc_multicore) clearing is 219,464 B,
# so both buffers were raised to 262,144 (per clearing_fit_waivers.txt guidance:
# raise the buffer, don't waive). Budget dec = 226,652 + 2X = 750,940 B, well
# under the 1 MiB LMB usable 1,048,368 B (X caps at 410,858 at LMB_KB=1024).
SWAP_CLEARING_ARENA_BYTES ?= 262144
PARTIAL_RAM   ?= 2048    # dead partial slot: partials > this stream to QSPI
# config_agent's CLEARING receive slot. UNSET => config_agent.h falls back to
# STAGING_BYTES (4 KiB), which sends EVERY real clearing to QSPI. Defaulted here
# instead so a plain `make elf` is QSPI-free. Override to shrink it only if you
# intend to exercise the QSPI clearing-cache path (see D16 first).
CLEARING_RAM_BYTES ?= 262144

# Path 3 (stream-direct): route a LARGE partial (886 KB–1.65 MB) straight to
# HWICAP as it arrives over 6910 — no RAM, no QSPI staging — which removes the
# partial size limit for every RM without QSPI first-light (docs/
# OVER_THE_WIRE_RECONFIG_PLAN.md §5). DEFAULT ON: it is the only path that
# unblocks the real DUT RMs (nanosoc/eth_ss) on the proven 0x394227AF static.
# Set ICAP_DIRECT=0 to fall back to the QSPI A/B-slot staging sink instead.
ICAP_DIRECT ?= 1

# HWICAP.WF byte packing for the RAM/QSPI clearing writers. UNSET => platform_regs.h
# defaults to MSB-first on the real target (matches the stream-direct partial;
# the .bin is big-endian, MB is little-endian). Set HWICAP_MSB_FIRST=0 to force the
# native memcpy packing on hardware if the axi_hwicap core turns out to byte-swap
# internally (HW byte-order bring-up). Host tests always build native (MPS3_HAL_MOCK).
HWICAP_MSB_FIRST ?=

# HWICAP write-path protocol. UNSET => platform_regs.h default 0 = LITE (one
# StartConfig per word).
#
# ⚠ THE SHELL IS NO LONGER LITE. shell_bd.tcl:898 instantiates axi_hwicap with
# C_MODE {0} (FIFO) + C_WRITE_FIFO_DEPTH {1024}, so a default (LITE) build does
# NOT match the current bitstream and over-the-wire reconfig is dead on arrival
# — the shell pings, reports the right static_id, passes the acceptance gate,
# and silently cannot load any RM. Build HWICAP_FIFO=1 for the current shell.
# (This comment used to say "matches the CURRENT shell, axi_hwicap C_MODE {1}";
# that was true before the LITE->FIFO change and is what let both rekey scripts
# ship a mismatched shell. Silicon-confirmed 2026-07-17.)
# Set HWICAP_FIFO=1 to build the writers for FIFO mode
# (poll WFV, batch up to WFV words, one StartConfig, poll CR, refill) — REQUIRED to
# match a shell rebuilt with axi_hwicap C_MODE {0} + C_WRITE_FIFO_DEPTH (fpga/shell/
# bd/shell_bd.tcl). Mismatch bricks over-the-wire reconfig, so flip this in lockstep
# with the bitstream. Host tests build the default (lite), so plain `make test` is
# byte-identical.
HWICAP_FIFO ?=

# App-level WINDOWED flow control on 6910 (docs: the HW-pivot primary fix for the
# over-the-wire receive stall). UNSET => OFF: config_agent's fire-and-hose path is
# byte-identical to today. Set WINDOWED=1 to build the lock-step handshake — the
# shell grants each drained chunk inline on 6910 and the host paces to it. REQUIRES
# a matched windowed host pusher (pyverify tcp_send_windowed / BitstreamPusher
# windowed=True), and ACK_WINDOW must equal the host's window.
#
# ACK_WINDOW default = 16384 to MATCH the BSP's tcp_wnd (create_platform.tcl):
# equal windows avoid the mid-window-reopen stall, and the big window is the
# THROUGHPUT fix (the 1-byte grant egresses only after an RTO on this LAN9220
# TX-while-RX path, so cost is ~1 RTO per window — an 8x window = 8x fewer RTOs).
# The matching host default lives in pyverify.pusher.DEFAULT_ACK_WINDOW.
WINDOWED ?=
ACK_WINDOW ?= 16384

# -Werror: a warning in a cross-compile whose only feedback channel is a board
# is a defect report nobody reads. The tree builds warning-free today (the three
# host-build warning sites -- clcd.c's nested "/*", diag.c's volatile memset,
# a stray unused variable -- were fixed alongside this flag), so -Werror costs
# nothing now and is the only thing that keeps it true.
#
# -DMPS3_LMB_KB: the `version` verb reports the LMB size this image was LINKED
# for, so a 1 MiB image running on a 512 KiB shell is visible over the wire
# instead of aliasing silently (see the LMB_KB note above). Passed from the ONE
# knob rather than restated in C.
#
# -Wno-error=cpp is the ONE carve-out, and it is deliberate: a `#warning`
# directive is an author talking to a reader, not a defect the compiler
# found. net_if_lwip.c:133 uses one to shout that this BSP was generated
# with LWIP_TCP_KEEPALIVE off (the fix is to regenerate the BSP, not to edit
# firmware), and promoting that to an error would make every target build in
# such a workspace fail on a condition the build cannot correct. The message
# still prints, in full, on every build.
CFLAGS := $(CPUFLAGS) -Os -g -ffunction-sections -fdata-sections \
          -Wall -Wextra -Werror -Wno-error=cpp \
          -DMPS3_CFG_AGENT_STAGING_BYTES=$(STAGING_BYTES) -DMPS3_SWAP_CLEARING_ARENA_BYTES=$(SWAP_CLEARING_ARENA_BYTES) \
          -DMPS3_CFG_AGENT_PARTIAL_RAM_BYTES=$(PARTIAL_RAM) \
          -DMPS3_LMB_KB=$(LMB_KB) \
          -Dsnprintf=sniprintf \
          -I$(BSP)/include

ifeq ($(ICAP_DIRECT),1)
CFLAGS += -DMPS3_CFG_AGENT_ICAP_DIRECT
endif

# XVC (port 2542) shift target. Default (unset) = the Xilinx Debug Bridge, i.e.
# the shipped behaviour, and an unset build is byte-identical to today's image.
#
# XVC_TARGET=jtagbb instead bit-bangs the JTAGBB DRIVE/SAMPLE pins
# (fpga/shell/ip/jtag_bb/jtag_bb.sv @ 0x44A7_0000, instantiated by the fielded BD
# at shell_bd.tcl:560/1401), which is how the host reaches a Synopsys Identify
# IICE soft TAP inside the reconfigurable partition -- a BSCANE2 is illegal in an
# RP (HDPR-16), so the IICE has to be reached over the DUT's own JTAG wires.
# On the IICE RM those wires carry TWO TAPs in a daisy chain (Identify's soft TAP
# plus the DUT's SoC-400 SWJ-DP); the firmware is a bit shifter and needs to know
# nothing about that -- the BYPASS padding is the client's arithmetic. See
# firmware/xvc_server/README.md and docs/planning/IICE_JTAG_CHAIN.md.
#
# XVC_TARGET=swdbb is the SAME registers under the names of swd_bb.sv, a block
# the A6 SWD->JTAG cutover deleted. It still produces working code only because
# jtag_bb reuses the same page and bit positions -- a coincidence xvc_server.h
# now asserts at compile time. It is kept because firmware/platform/
# verify_shell_image.py, board_xvc_throughput.sh, four firmware/test sources and
# host/socket_harness/xvc_server.py all still spell it that way. NEW WORK USES
# jtagbb.
#
# Either bit-bang target is firmware-only: no shell rebuild, so NO static_id
# re-mint and the shipped overlay partials stay valid. Ships as a firmware
# re-bake + updatemem into the existing .bit.
#
# `make clean` FIRST when changing this: make compares timestamps, not flags, so
# a stale xvc_server.o survives adding XVC_TARGET= and you get a byte-identical
# ELF with the retarget silently absent.
XVC_TARGET ?=
ifeq ($(XVC_TARGET),jtagbb)
CFLAGS += -DMPS3_XVC_TARGET_JTAGBB
else ifeq ($(XVC_TARGET),swdbb)
CFLAGS += -DMPS3_XVC_TARGET_SWDBB
else ifneq ($(XVC_TARGET),)
$(error XVC_TARGET='$(XVC_TARGET)' is not one of: <empty>=dbgbr, jtagbb, swdbb. \
 An unrecognised value used to be silently ignored, which builds the DEFAULT \
 target and looks exactly like success)
endif

# Only define the clearing-slot override when explicitly set (see above); unset
# -> config_agent.h's default (= STAGING_BYTES) keeps plain builds byte-identical.
# Mirrors the -DMPS3_CFG_AGENT_STAGING_BYTES form above.
ifneq ($(CLEARING_RAM_BYTES),)
CFLAGS += -DMPS3_CFG_AGENT_CLEARING_RAM_BYTES=$(CLEARING_RAM_BYTES)
endif

# Only force the HWICAP pack order when explicitly set; unset -> platform_regs.h
# default (MSB-first on target, native under host mock).
ifneq ($(HWICAP_MSB_FIRST),)
CFLAGS += -DMPS3_HWICAP_MSB_FIRST=$(HWICAP_MSB_FIRST)
endif

# Only select the HWICAP FIFO write-path when explicitly set; unset ->
# platform_regs.h default (0 = lite, current proven behaviour). Mirrors the
# -DMPS3_HWICAP_MSB_FIRST form above.
ifneq ($(HWICAP_FIFO),)
CFLAGS += -DMPS3_HWICAP_FIFO=$(HWICAP_FIFO)
endif

# Per-octet MAC override (give a 2nd board a distinct MAC on the same network).
# Default MAC (net_if_lwip.c) is the C-board 02:00:00:4D:50:53; e.g. the
# HBI0309B board is built with MAC5=0x42 => 02:00:00:4D:50:42. Unset octets keep
# their default; a plain `make elf` is byte-identical to the shipped image.
$(foreach n,0 1 2 3 4 5,$(if $(MAC$(n)),$(eval CFLAGS += -DMPS3_MAC$(n)=$(MAC$(n)))))

# Windowed 6910 flow control (default off -> byte-identical fire-hose path).
ifeq ($(WINDOWED),1)
CFLAGS += -DMPS3_CFG_AGENT_WINDOWED
endif
ifneq ($(ACK_WINDOW),)
CFLAGS += -DCFG_AGENT_ACK_WINDOW_BYTES=$(ACK_WINDOW)
endif

# CLCD on-board status display. Default OFF: there is no AXI slave at 0x44AC on
# the shipped shell (platform_regs.h:100-105, shell-regmap.md v0.4), so an
# ungated clcd_poll() would spin on a DECERR'ing void every superloop pass on a
# board whose only ingress is this same firmware. The Phase-D batch that lands
# the BD slave (fpga/shell/bd/shell_bd.tcl clcd_0 @ 0x44AC_0000) builds CLCD=1,
# which both defines MPS3_HAS_CLCD (main.c's #ifdef-gated clcd_init/clcd_poll
# hooks) and links the driver + panel init table (CLCD_SRCS below). Flip this
# in lockstep with the CLCD-bearing bitstream.
CLCD ?=
ifeq ($(CLCD),1)
CFLAGS += -DMPS3_HAS_CLCD
endif

# Phase-2 resistive touchscreen (firmware/touch/touch.c, via an AXI IIC master
# + an I2C TSC). Default OFF, mirroring CLCD: there is NO AXI IIC slave on the
# shipped shell (MPS3_TOUCH_BASE is generated into platform_regs.h from
# fpga/shell/bd/touch_iic_add.tcl, which only a SHELL_TOUCH=1 mint sources), so an ungated
# touch_poll() would poke an unmapped void. TOUCH=1 defines MPS3_HAS_TOUCH
# (gates the clcd_poll() integration hook + touch_init call site) and links the
# driver (TOUCH_SRCS below). Flip this in lockstep with the IIC-bearing
# bitstream, and only alongside CLCD=1 (touch dispatches into clcd_hittest).
TOUCH ?=
ifeq ($(TOUCH),1)
CFLAGS += -DMPS3_HAS_TOUCH
endif

# The DUT's Ethernet RETURN path (fpga/shell/ip/dut_egress, DUTEGR @0x44B2_0000,
# instantiated by shell_bd.tcl since e2062c1 and carried by the 2026-09 mint).
# DUT_EGRESS=1 defines MPS3_HAS_DUT_EGRESS, which is the ONLY thing that turns
# the `dutrx` verb (net-protocol v0.10, dc1cc48) from "dut_egress not present"
# into a drain of the capture FIFO. Default OFF like TOUCH -- a shell without
# the block would be poked at an unmapped address -- and ON under PRODUCT=1,
# because the product fabric has it. dc1cc48 shipped the verb behind this
# define and named the knob as the follow-up; the first PRODUCT=1 bake after
# it (the 2026-09 mint, 2026-09-15) therefore produced exactly the half-mint
# docs/FIELDED_SHELL.md warns about -- fabric with the block, firmware that
# declines the verb -- caught 2026-09-16 before the SD write. The host twin
# is firmware/test/Makefile's test_dutrx_dispatch (built with the define).
DUT_EGRESS ?=
ifeq ($(DUT_EGRESS),1)
CFLAGS += -DMPS3_HAS_DUT_EGRESS
endif

BUILD  := $(WS)/shell_fw
ELF    := $(BUILD)/shell_fw.elf

FW := $(REPO)/firmware

# CLCD driver + HX8347-D panel init table (firmware/clcd/, owned by the CLCD
# sibling — WIRED here, never edited). Linked ONLY when CLCD=1 so a plain
# `make elf` (CLCD unset) is byte-identical to the shipping image. clcd.c
# streams the init table through the 0x44AC slave; hx8347_init.c is that table
# (its own provenance record). Flat obj names: obj/clcd/clcd.o, obj/clcd/hx8347_init.o.
CLCD_SRCS :=
ifeq ($(CLCD),1)
CLCD_SRCS := \
    $(FW)/clcd/clcd.c \
    $(FW)/clcd/hx8347_init.c
endif

# CLCD KVM handover (harness<->DUT panel ownership). Default OFF: no slave at
# 0x44AD on the shipped shell -- it lands in the Wave-4 rebuild that re-mints
# static_id. CLCD_KVM=1 defines MPS3_HAS_CLCD_KVM (gates clcd.c's kvm_service()
# hook + the clcd_kvm_* CSR helpers) and links clcd_kvm.c. Requires CLCD=1: the
# handover lives inside clcd_poll(). With the gate off, clcd_kvm.h is header-only
# inline no-ops, so a plain `make CLCD=1` for today's bitstream is unchanged.
# Placed after CLCD_SRCS := so this += is not wiped by that assignment.
CLCD_KVM ?=
ifeq ($(CLCD_KVM),1)
CFLAGS    += -DMPS3_HAS_CLCD_KVM
CLCD_SRCS += $(FW)/clcd_kvm/clcd_kvm.c
endif

# ===========================================================================
# CONSISTENCY GATES — the silent bricks, made loud.
# ===========================================================================
# Until this block there was no $(error) anywhere in this Makefile, and each of
# the combinations below produced an image that BUILDS and then fails somewhere
# far away from the mistake. Each gate names the fix.
#
# Skipped when the ONLY goal is `clean` (a `make clean TOUCH=1` must not become
# an error the user has to decode before they can even clean the tree). Any
# other goal -- including the default `elf` -- is gated.
MPS3_GATED_GOALS := $(filter-out clean,$(if $(MAKECMDGOALS),$(MAKECMDGOALS),elf))
ifneq ($(MPS3_GATED_GOALS),)

ifeq ($(TOUCH),1)
ifneq ($(CLCD),1)
$(error TOUCH=1 without CLCD=1. touch.c dispatches into clcd_hittest(), so this \
build dies at LINK with an undefined reference -- minutes after the mistake, in \
a message that names neither knob. Fix: add CLCD=1, or build the fielded set \
with PRODUCT=1 (= CLCD=1 CLCD_KVM=1 HWICAP_FIFO=1 WINDOWED=1 TOUCH=1 DUT_EGRESS=1). The \
bitstream must be a SHELL_TOUCH=1 mint either way -- there is no AXI IIC \
master to talk to otherwise.)
endif
endif

ifeq ($(CLCD_KVM),1)
ifneq ($(CLCD),1)
$(error CLCD_KVM=1 without CLCD=1. This one LINKS and boots: the KVM handover \
lives inside clcd_poll(), which is never called without CLCD=1, so the panel \
handover silently never happens and `display` answers from a CSR nothing \
services. Fix: add CLCD=1, or build PRODUCT=1.)
endif
endif

# NOT an error: LITE shells legitimately exist. Only a static minted BEFORE the
# 2026-07-17 LITE->FIFO change (shell_bd.tcl axi_hwicap CONFIG.C_MODE {1}, no
# write FIFO) wants the default LITE writers. EVERY shell built from today's
# fpga/shell/bd/shell_bd.tcl -- C_MODE {0} + C_WRITE_FIFO_DEPTH {1024}, which
# includes the bare-metal rollback image 0x72BB0A36 (docs/FIELDED_SHELL.md) and every mint back to that change
# -- needs HWICAP_FIFO=1, and a mismatch is the exact silent brick described in
# the HWICAP_FIFO block above: pings, right static_id, gates green, cannot load
# an RM.
ifeq ($(HWICAP_FIFO),)
$(warning HWICAP_FIFO is unset -> building the LITE HWICAP write path. That is \
correct ONLY for a static minted before the 2026-07-17 LITE->FIFO change \
(shell_bd.tcl axi_hwicap C_MODE {1}). For any shell built from today's \
shell_bd.tcl -- C_MODE {0} + C_WRITE_FIFO_DEPTH {1024}, the bare-metal rollback image 0x72BB0A36 \
included -- this image will ping, report the right static_id, pass every gate \
and load NO RM. Build HWICAP_FIFO=1 (or PRODUCT=1) for those.)
endif

endif  # MPS3_GATED_GOALS


# Touch driver source (firmware/touch/), linked ONLY when TOUCH=1 so a plain
# `make elf` (or a CLCD=1 build) is byte-identical to the shipping image. The
# driver dispatches into clcd_hittest(), so a TOUCH=1 build only makes sense with
# CLCD=1. Flat obj name: obj/touch/touch.o.
TOUCH_SRCS :=
ifeq ($(TOUCH),1)
TOUCH_SRCS := $(FW)/touch/touch.c
endif

# Harness version seam (VERSION -> HARNESS_VER32; docs/VERSIONING_PLAN.md §3.1).
# BOTH TUs are linked, deliberately:
#   mps3_version_weak.c            committed WEAK fallbacks (0.0.0 / "unknown")
#   generated/mps3_version.c       GENERATED strong overrides (scripts/gen_version.py)
# The strong definitions win at link time; the weak TU is what keeps a build
# WITHOUT a generated identity linking (a fresh clone, or firmware/test/'s
# host-gcc binaries). Identical mechanism to greybox_blob.c strongly overriding
# coordinator.c's weak mps3_shell_static_id().
# mps3_usr_access.c is the OTHER end of the same axis (2026-09-14): the read of
# USRACC @0x44B3_0000, i.e. what the FABRIC says its identity is, against what
# this image was built as. Grouped here rather than in SRCS because it is part
# of the version seam, not a peripheral driver -- and because a build that ever
# drops it is a build whose `version` verb stops answering the cross-check.
VERSION_SRCS := \
    $(FW)/platform/mps3_version_weak.c \
    $(FW)/platform/mps3_usr_access.c \
    $(FW)/platform/generated/mps3_version.c

# Legacy SWD bit-bang server (TCP 6920). NOT built by default: the JTAG cutover
# made jtag_server.c (TCP 6921, jtag_bb @0x44A7) the live debug service and
# NOTHING on the target calls swd_server_init()/swd_server_poll() any more --
# coordinator_init() calls jtag_server_init(), and platform/src/main.c's
# superloop polls jtag_server_poll(). The file was still in SRCS, so every
# shipped image carried a service that could never be reached.
#
# The SOURCE stays (firmware/swd_server/swd_server.c is NOT deleted): its
# host-gcc test, firmware/test/test_swd_server.c, links it directly and pins the
# OpenOCD remote_bitbang byte encoding that jtag_server.c inherits. Set
# LEGACY_SWD=1 to put the 6920 server back in the image -- which also needs a
# call site restored, since removing it from SRCS removed the only thing that
# referenced it and main.c has no #ifdef'd swd_server_poll() to re-enable.
LEGACY_SWD ?=
SWD_SRCS :=
ifeq ($(LEGACY_SWD),1)
SWD_SRCS := $(FW)/swd_server/swd_server.c
endif

SRCS := \
    $(FW)/common/crc32.c \
    $(FW)/common/net_if.c \
    $(FW)/common/net_proto.c \
    $(FW)/common/diag.c \
    $(FW)/common/service.c \
    $(FW)/common/log_ring.c \
    $(FW)/common/hwicap_writer.c \
    $(FW)/coordinator/coordinator.c \
    $(FW)/coordinator/coordinator_net.c \
    $(FW)/coordinator/swap_fsm.c \
    $(FW)/coordinator/swap_fsm_transitions.c \
    $(FW)/config_agent/config_agent.c \
    $(FW)/overlay_store/overlay_store.c \
    $(FW)/overlay_store/overlay_store_bm.c \
    $(FW)/overlay_store/ovlstore_sd.c \
    $(FW)/overlay_store/ovl_bdev_usd.c \
    $(FW)/overlay_store/ovlstore_codec.c \
    $(FW)/usd/usd.c \
    $(FW)/clkrst/clkrst.c \
    $(FW)/jtag_server/jtag_server.c \
    $(FW)/xvc_server/xvc_server.c \
    $(FW)/uart_over_eth/uart_over_eth.c \
    $(FW)/smsc911x/smsc911x.c \
    $(FW)/platform/src/main.c \
    $(FW)/platform/src/net_if_lwip.c \
    $(FW)/platform/generated/greybox_blob.c \
    $(VERSION_SRCS) \
    $(SWD_SRCS) \
    $(CLCD_SRCS) \
    $(TOUCH_SRCS)

# Flat object names (module_file.o) — no basename collisions in this tree.
OBJS := $(patsubst $(FW)/%.c,$(BUILD)/obj/%.o,$(SRCS))

.PHONY: elf clean blob version

elf: $(ELF)
	@echo "=== $(ELF) (LMB_KB=$(LMB_KB), BLOB=$(BLOB), harness v$(HARNESS_VERSION) $(HARNESS_VER32)) ==="
	@# The baked shell identity, echoed because getting it wrong is silent on
	@# silicon (see BLOB_MANIFEST above). A build log that does not say which
	@# static_id went in cannot be audited after the fact.
	@grep -h "mps3_greybox_clearing_static_id\|^ \* identity:" \
	    $(FW)/platform/generated/greybox_blob.c 2>/dev/null || true
	$(SIZE) $(ELF)

# Greybox blob is regenerated every build so identity tracks the current
# A2 overlay manifest (single .PHONY dependency keeps this simple).
blob:
	python3 $(FW)/platform/gen_greybox_blob.py $(BLOB_ARGS)

$(FW)/platform/generated/greybox_blob.c: blob

# Harness version (VERSION -> generated/mps3_version.{c,h}). Regenerated every
# build for the SAME reason the blob is: provenance that is not re-derived at
# build time is provenance that goes stale. It is cheap (a VERSION read + two
# git calls) and it is what makes a firmware-only release bump real -- rebuild
# the firmware, get a new HARNESS_VER32, and static_id is untouched, which is
# precisely the orthogonality the scheme promises.
#
# NOTE the ordering guarantee this buys with the bitstream: fpga/dfx/
# build_dfx.tcl stamps the SAME scripts/gen_version.py --print ver32 into
# BITSTREAM.CONFIG.USR_ACCESS, so the number the firmware reports and the number
# JTAG reads out of the fabric come from one source. They can only disagree if
# the .bit and the image baked into it are from different builds -- which is
# exactly the skew we want to be able to detect.
version:
	python3 $(REPO)/scripts/gen_version.py

$(FW)/platform/generated/mps3_version.c: version
$(FW)/platform/generated/mps3_version.h: version

# The generated identity header is included by generated/mps3_version.c only;
# the committed seam (mps3_version.h) is what everything else includes.
$(BUILD)/obj/platform/generated/mps3_version.o: $(FW)/platform/generated/mps3_version.h \
                                                $(FW)/platform/mps3_version.h
$(BUILD)/obj/platform/mps3_version_weak.o: $(FW)/platform/mps3_version.h

# Echoed in the `elf` banner above so a build log records WHICH harness this
# image is (:= would run the generator at parse time on every target, including
# `clean`, so these are lazily evaluated).
HARNESS_VERSION = $(shell python3 $(REPO)/scripts/gen_version.py --print version 2>/dev/null || echo "?")
HARNESS_VER32   = $(shell python3 $(REPO)/scripts/gen_version.py --print ver32   2>/dev/null || echo "?")

$(BUILD)/obj/%.o: $(FW)/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

# Linker script from the template: LMB length = LMB_KB*1024 - 0x50 (vectors
# live below 0x50 at their fixed addresses). FORCE'd + content-compared so
# switching LMB_KB between invocations regenerates it (and only then).
.PHONY: FORCE
$(BUILD)/lscript.ld: $(FW)/platform/lscript.ld.in FORCE
	@mkdir -p $(BUILD)
	@python3 -c "import os,sys; kb=int('$(LMB_KB)'.strip()); \
	    t=open('$(FW)/platform/lscript.ld.in').read().replace('@LMB_LENGTH@', hex(kb*1024-0x50)); \
	    p='$@'; old=open(p).read() if os.path.exists(p) else ''; \
	    (old != t) and (open(p,'w').write(t), print('lscript.ld: LMB region = %d KiB' % kb))"

$(ELF): $(OBJS) $(BUILD)/lscript.ld
	$(CC) $(CPUFLAGS) -Wl,-T,$(BUILD)/lscript.ld -L$(BSP)/lib \
	    -Wl,--gc-sections -Wl,-Map,$(BUILD)/shell_fw.map \
	    -o $@ $(OBJS) \
	    -Wl,--start-group,-llwip4,-lxil,-lgcc,-lc,--end-group

clean:
	rm -rf $(BUILD)/obj $(ELF) $(BUILD)/lscript.ld $(BUILD)/shell_fw.map
