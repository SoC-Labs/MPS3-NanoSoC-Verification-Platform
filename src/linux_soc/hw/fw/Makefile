# Makefile - bare-metal DDR4 memtest for a MicroBlaze V (RISC-V) soft core.
#
# Freestanding build: -nostartfiles (our own crt0.S), custom linker script,
# no libc. See memtest.c / crt0.S for the details.
#
# IMPORTANT (verified on disk): this toolchain has NO rv32ima multilib. The
# available rv32 multilibs are rv32i[m][f][c]... only. We therefore build for
# -march=rv32im -mabi=ilp32. Omitting -march selects a default that pulls in the
# 'a' and 'f'/'d' extensions (defines __riscv_atomic / __riscv_flen) and fails to
# link against the rv32im multilib - so -march is passed EXPLICITLY and asserted
# at the end of the build. Running IM code on the atomics-enabled core is fine;
# we simply never emit A instructions.

# Toolchain prefix -- BARE-METAL RISC-V (newlib), not the Linux/glibc one.
#
# PORTED to 2026.1: the spike pointed at /apps/Xilinx/Vitis/2024.1/... which does
# not exist on this machine. The bare-metal toolchain now lives under the Vivado
# install; the Linux glibc toolchain (used to build the kernel/userland, NOT this
# memtest) is the separate gnu/riscv/linux_toolchain/lin32 tree.
XILINX_VIVADO ?= /research/CAD/Xilinx/Vivado/2026.1
CROSS         ?= $(XILINX_VIVADO)/gnu/riscv/lin/bin/riscv64-unknown-elf-

CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump
SIZE    := $(CROSS)size
READELF := $(CROSS)readelf

TARGET  := memtest
LDSCRIPT:= $(TARGET).ld

# ISA / ABI: MUST be rv32im (see header note). zicsr is enabled locally inside
# crt0.S via `.option arch,+zicsr` for the csrr/csrw ops; the multilib stays
# rv32im so no rv32ima/float library is required.
ARCH    := -march=rv32im -mabi=ilp32 -mcmodel=medany -mno-relax

CFLAGS  := $(ARCH) -ffreestanding -nostartfiles -O2 -g -Wall -Wextra \
           -fno-common -ffunction-sections -fdata-sections
ASFLAGS := $(ARCH) -ffreestanding -nostartfiles -g
LDFLAGS := $(ARCH) -ffreestanding -nostartfiles -nostdlib \
           -T $(LDSCRIPT) -Wl,--gc-sections -Wl,-Map=$(TARGET).map -Wl,--build-id=none \
           -Wl,--no-warn-rwx-segments
# ^ the whole image lives in one RWX LMB block RAM; the RWX-segment warning is
#   expected here (no MMU/MPU), so silence it to keep the build log clean.

OBJS    := crt0.o memtest.o

.PHONY: all size clean check-isa
all: $(TARGET).elf $(TARGET).bin $(TARGET).lst check-isa

crt0.o: crt0.S
	$(CC) $(ASFLAGS) -c $< -o $@

memtest.o: memtest.c
	$(CC) $(CFLAGS) -c $< -o $@

$(TARGET).elf: $(OBJS) $(LDSCRIPT)
	$(CC) $(CFLAGS) $(OBJS) $(LDFLAGS) -o $@

$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@

$(TARGET).lst: $(TARGET).elf
	$(OBJDUMP) -d -S $< > $@

# Build-time ISA assertion:
#   * the merged ELF arch string must be rv32i...m... (M present),
#   * the atomic extension ('a') must NOT have leaked in.
# (zicsr is expected and allowed - it comes from crt0.S's CSR ops.)
check-isa: $(TARGET).elf
	@arch=`$(READELF) -A $< | sed -n 's/.*Tag_RISCV_arch: *"\(.*\)"/\1/p'`; \
	echo "  ISA attribute: $$arch"; \
	case "$$arch" in \
	  rv32i*) : ;; \
	  *) echo "BUILD ASSERT FAILED: not an rv32i ELF ($$arch)"; exit 1 ;; \
	esac; \
	case "$$arch" in \
	  *_m[0-9]*|*_m) : ;; \
	  *) echo "BUILD ASSERT FAILED: M extension missing ($$arch)"; exit 1 ;; \
	esac; \
	case "$$arch" in \
	  *_a[0-9]*) echo "BUILD ASSERT FAILED: atomic ext leaked in ($$arch)"; exit 1 ;; \
	esac; \
	case "$$arch" in \
	  *_f[0-9]*|*_d[0-9]*) echo "BUILD ASSERT FAILED: float ext leaked in ($$arch)"; exit 1 ;; \
	esac; \
	echo "  ISA assertion OK: rv32im (no atomic/float), zicsr allowed"

size: $(TARGET).elf
	$(SIZE) $<
	@echo "  (64 KiB LMB budget = 65536 bytes; .text+.data+.bss must fit, plus stack)"

clean:
	rm -f $(OBJS) $(TARGET).elf $(TARGET).bin $(TARGET).lst $(TARGET).map
