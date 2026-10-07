# qspi_loader — M0-side QSPI flash loader.
#
#   make            -> build/qspi_loader.bin (+ .elf, .map, .size)
#   make clean
#
# Produces a raw binary the host writes straight to IMEM_BASE (0x1000_0000) over
# SWD, then enters with SP/PC set. No bootrom, no REMAP — see qspi_loader.ld.

# Site-local tool paths come from the repo-root tools.env (NOT tracked; see
# tools.env.example), `-include`d so an explicit `make VAR=...` still wins.
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)

# The arm-none-eabi bin directory: ARM_GCC_BIN in tools.env. Empty = whatever
# arm-none-eabi-gcc is on PATH. (Byte-reproducible images need the same
# toolchain the recorded md5s were built with: GNU Arm 10.3-2021.10.)
TOOLCHAIN_BIN ?= $(ARM_GCC_BIN)
TC_PFX  := $(if $(strip $(TOOLCHAIN_BIN)),$(TOOLCHAIN_BIN)/,)
CC      := $(TC_PFX)arm-none-eabi-gcc
OBJCOPY := $(TC_PFX)arm-none-eabi-objcopy
SIZE    := $(TC_PFX)arm-none-eabi-size
NM      := $(TC_PFX)arm-none-eabi-nm

BUILD   := build
SRC     := qspi_loader.c
LDSCRIPT:= qspi_loader.ld

# -mcpu=cortex-m0 -mthumb matches the nanoSoC core (firmware/micropython/port).
# -Os because the loader shares IMEM with a 48 KB payload window.
# -ffreestanding/-nostdlib: no libc, no startup — the host sets SP and PC.
CFLAGS  := -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -Os -g3 \
           -ffreestanding -fno-builtin -std=c11 \
           -Wall -Wextra -Werror -Wno-unused-parameter \
           -ffunction-sections -fdata-sections
LDFLAGS := -mcpu=cortex-m0 -mthumb -nostdlib -nostartfiles \
           -T $(LDSCRIPT) -Wl,--gc-sections -Wl,-Map,$(BUILD)/qspi_loader.map

.PHONY: all clean
all: $(BUILD)/qspi_loader.bin

$(BUILD):
	@mkdir -p $(BUILD)

$(BUILD)/qspi_loader.elf: $(SRC) $(LDSCRIPT) | $(BUILD)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRC)

$(BUILD)/qspi_loader.bin: $(BUILD)/qspi_loader.elf
	$(OBJCOPY) -O binary $< $@
	@echo "=== size ==="
	@$(SIZE) $<
	@echo "=== entry must be at the image base 0x10000000 ==="
	@$(NM) $< | grep -iE " (t|T) loader_main" || (echo "loader_main missing!"; exit 1)

clean:
	rm -rf $(BUILD)
