# ============================================================================
# src/linux_soc/fw_memtest/Makefile
#
# Bare-metal DDR4 memtest for the linux_soc MicroBlaze V, plus the xsim co-sim
# that runs it against the real block design and the DDR4 BFM.
#
#   make fw     -> build/memtest.elf      (HARDWARE: full 1 GiB word-by-word walk)
#                  build/memtest_sim.elf  (SIM:      strided sweep, same patterns)
#   make sim    -> build the BD + DDR4 BFM + memtest_sim.elf co-sim and run xsim.
#                  GATE: "SIMRESULT: PASS" in build/sim.log
#   make clean
#
# TOOLCHAIN
#   Bare-metal RISC-V:  $(XILINX_VIVADO)/gnu/riscv/lin/bin/riscv64-unknown-elf-*
#     (really riscv32-xilinx-elf-gcc 13.4.0; ships an rv32imac_zicsr_zifencei
#      multilib, which the 2024.1 toolchain used by poc/ddr4_mbv did NOT --
#      that is why the spike had to build rv32im and this does not.)
#   Vivado: the module ships `vivado` as a shell ALIAS that does not survive into
#     a Makefile, and $(XILINX_VIVADO)/bin/vivado does not exist. The binary is at
#     $(XILINX_VIVADO)/Vivado/bin/vivado -- note the NESTED path.
# ============================================================================

XILINX_VIVADO ?= /research/CAD/Xilinx/Vivado/2026.1
VIVADO        := $(XILINX_VIVADO)/Vivado/bin/vivado

CROSS   := $(XILINX_VIVADO)/gnu/riscv/lin/bin/riscv64-unknown-elf-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump
SIZE    := $(CROSS)size
READELF := $(CROSS)readelf

HERE    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
# Run dir. Overridable so a run can be given a FRESH directory rather than
# fighting an orphaned xsim still holding the last one:  make sim B=build_v2
B       ?= $(HERE)/build

# Size of the contiguous block tested at the BASE and at the TOP of the 1 GiB
# aperture, in the SIM build. THE SINGLE SOURCE OF TRUTH: it is passed to the
# compiler as -DMT_BLK_BYTES and exported to sim.tcl, which derives the TB's
# "the read-back really came from DRAM" thresholds from the same number. Raising
# it costs ~(B/4 stores + B/8 line fills) DDR transactions, and in this co-sim a
# DDR transaction is not free -- see the header of memtest.c.
#
# 256 B (64 words) per block. Sized from MEASURED xsim throughput, not taste:
# once DDR traffic starts this design simulates at ~0.07 us of simulated time per
# wall-clock second, and EVERY STORE IS A DDR TRANSACTION (the D-cache is
# write-through). So the run length is set by the DDR transaction count, and
# 1024 B blocks put it out past an hour. At 256 B the whole test is ~380 DDR
# transactions:
#     base  64 stores + 16 evict + 16 read-back line fills =  96
#     top   64 stores + 16 evict + 16 read-back line fills =  96
#     dbus  64 stores + 16 evict + 16 read-back line fills =  96
#     abus  30 stores + 30 evict + 30 read-back line fills =  90
# The COVERAGE does not come from block size -- it comes from the walking-1s on
# the data bus (all 32 lanes), the walking-1s on the address bus (every address
# bit across the full 1 GiB), and the two blocks sitting at the two ends of the
# aperture. The board build (no -DMT_SIM) still walks every word of the 1 GiB.
MT_BLK_BYTES ?= 256
export MT_BLK_BYTES

# ISA/ABI. The BD core is rv32imac + Zba/Zbb/Zbs with an Sv32 MMU; the toolchain
# has no rv32imacb multilib, and we link -nostdlib anyway, so the bit-manip
# extensions are simply not used. zicsr is REQUIRED (crt0.S does csrr/csrw).
ARCH    := -march=rv32imac_zicsr_zifencei -mabi=ilp32 -mcmodel=medany -mno-relax

CFLAGS  := $(ARCH) -ffreestanding -nostartfiles -O2 -g -Wall -Wextra \
           -fno-common -ffunction-sections -fdata-sections
ASFLAGS := $(ARCH) -ffreestanding -nostartfiles -g
LDFLAGS := $(ARCH) -ffreestanding -nostartfiles -nostdlib \
           -T $(HERE)/memtest.ld -Wl,--gc-sections -Wl,--build-id=none \
           -Wl,--no-warn-rwx-segments
# ^ one RWX LMB block RAM, no MMU/MPU in use here: the RWX-segment warning is
#   expected, so silence it rather than let it hide a real warning.

.PHONY: all fw sim clean help size
all: fw
help:
	@echo "targets: fw (both ELFs) | sim (BD + DDR4 BFM co-sim, the GATE) | size | clean"

# ---------------------------------------------------------------------------
# Firmware. Two ELFs from ONE source: the sim build only redefines the sweep
# sizes (-DMT_SIM). Identical code paths, identical patterns, identical verdict.
# ---------------------------------------------------------------------------
fw: $(B)/memtest.elf $(B)/memtest_sim.elf

$(B):
	mkdir -p $(B)

# $(1) = tag ("" or "_sim"), $(2) = extra CFLAGS
define BUILD_ELF
$(B)/crt0$(1).o: $(HERE)/crt0.S | $(B)
	$$(CC) $$(ASFLAGS) $(2) -c $$< -o $$@

$(B)/memtest$(1).o: $(HERE)/memtest.c | $(B)
	$$(CC) $$(CFLAGS) $(2) -c $$< -o $$@

$(B)/memtest$(1).elf: $(B)/crt0$(1).o $(B)/memtest$(1).o $(HERE)/memtest.ld
	$$(CC) $$(CFLAGS) $(2) $(B)/crt0$(1).o $(B)/memtest$(1).o $$(LDFLAGS) \
	    -Wl,-Map=$(B)/memtest$(1).map -o $$@
	$$(OBJCOPY) -O binary $$@ $(B)/memtest$(1).bin
	$$(OBJDUMP) -d -S $$@ > $(B)/memtest$(1).lst
	@$$(MAKE) --no-print-directory check-isa ELF=$$@
	@$$(SIZE) $$@
endef

$(eval $(call BUILD_ELF,,))
$(eval $(call BUILD_ELF,_sim,-DMT_SIM -DMT_BLK_BYTES=$(MT_BLK_BYTES)u))

# Build-time ISA assertion. The IP silently keeps old parameter values on an
# illegal set_property (see mbv_soc.tcl), and a toolchain silently picks a
# default -march if you let it -- so assert the ELF is what we think it is
# rather than trusting either end.
.PHONY: check-isa
check-isa:
	@arch=`$(READELF) -A $(ELF) | sed -n 's/.*Tag_RISCV_arch: *"\(.*\)"/\1/p'`; \
	echo "  ISA attribute: $$arch"; \
	case "$$arch" in rv32i*) : ;; *) echo "ASSERT FAILED: not rv32i ($$arch)"; exit 1;; esac; \
	case "$$arch" in *_m[0-9]*|*_m) : ;; *) echo "ASSERT FAILED: no M ext ($$arch)"; exit 1;; esac; \
	case "$$arch" in *_a[0-9]*|*_a) : ;; *) echo "ASSERT FAILED: no A ext ($$arch)"; exit 1;; esac; \
	case "$$arch" in *_c[0-9]*|*_c) : ;; *) echo "ASSERT FAILED: no C ext ($$arch)"; exit 1;; esac; \
	case "$$arch" in *_f[0-9]*|*_d[0-9]*) echo "ASSERT FAILED: float leaked in ($$arch)"; exit 1;; esac; \
	echo "  ISA assertion OK: rv32imac(+zicsr/zifencei), no float"

size: $(B)/memtest.elf
	@$(SIZE) $(B)/memtest.elf $(B)/memtest_sim.elf
	@echo "  (128 KiB LMB budget; memtest.ld ASSERTs 4 KiB of stack headroom)"

# ---------------------------------------------------------------------------
# CO-SIM -- the gate. Builds the REAL block design (hw/ddr4_ip.tcl +
# hw/mbv_soc.tcl, sourced unmodified), loads memtest_sim.elf into the LMB BRAM,
# and runs it in xsim against the DDR4 BFM. See sim.tcl for the honest statement
# of what BFM mode does and does not model.
# ---------------------------------------------------------------------------
sim: $(B)/memtest_sim.elf
	@echo "=== co-sim: BD + DDR4 BFM + memtest_sim.elf (this is LONG) ==="
	cd $(B) && $(VIVADO) -mode batch -nojournal -notrace \
	    -log $(B)/vivado_sim.log \
	    -source $(HERE)/sim.tcl -tclargs $(B) 2>&1 | tee $(B)/sim.log; \
	    echo "SIM_MAKE_EXIT=$${PIPESTATUS[0]}" | tee -a $(B)/sim.log
	@grep -E "^SIMRESULT:" $(B)/sim.log || (echo "no SIMRESULT line -- see $(B)/sim.log"; exit 1)

# Re-run the co-sim WITHOUT rebuilding the block design (~8 min saved). Use this
# whenever only the testbench or the firmware changed -- which is most iterations.
resim: $(B)/memtest_sim.elf
	@echo "=== co-sim RE-RUN on the existing BD ==="
	cd $(B) && $(VIVADO) -mode batch -nojournal -notrace \
	    -log $(B)/vivado_resim.log \
	    -source $(HERE)/resim.tcl -tclargs $(B) 2>&1 | tee $(B)/resim.log; \
	    echo "SIM_MAKE_EXIT=$${PIPESTATUS[0]}" | tee -a $(B)/resim.log
	@grep -E "^SIMRESULT:" $(B)/resim.log || (echo "no SIMRESULT line -- see $(B)/resim.log"; exit 1)

clean:
	rm -rf $(B)
