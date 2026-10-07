# Top-level convenience wrapper — builds the NanoSoC MicroPython port.
#
#   make            build build/micropython_word.hex (the resident FPGA IMEM image)
#   make xip        build the hybrid-XiP artifacts under build/xip/:
#                     micropython_hot_word.hex  resident IMEM image (linked @0x0)
#                     micropython_cold.bin/.hex cold .xip segment for QSPI flash
#                     xip_manifest.txt          hot/cold size split (headline)
#   make clean      remove the build/ tree (both resident and xip)
#
# The real build lives in port/Makefile. The arm-none-eabi toolchain must be on
# PATH; see README.md.

# Site-local tool paths come from the repo-root tools.env (NOT tracked; see
# tools.env.example), `-include`d so an explicit `make VAR=...` still wins.
REPO_ROOT ?= $(abspath $(CURDIR)/../..)
TOOLS_ENV ?= $(REPO_ROOT)/tools.env
-include $(TOOLS_ENV)

# The arm-none-eabi bin directory: ARM_GCC_BIN in tools.env. Empty = whatever
# arm-none-eabi-gcc is on PATH. (Byte-reproducible images need the same
# toolchain the recorded md5s were built with: GNU Arm 10.3-2021.10.)
TOOLCHAIN_BIN ?= $(ARM_GCC_BIN)
PYTHON        ?= python3
# hex_byte_to_word.py comes from the nanoSoC SoC tree: SOCLABS_NANOSOC_SOC_DIR
# (tools.env) when set, else the pinned submodule fpga/deps/nanosoc_m0_soc.
NANOSOC_SOC_FOR_HEX := $(or $(SOCLABS_NANOSOC_SOC_DIR),$(REPO_ROOT)/fpga/deps/nanosoc_m0_soc)
HEX_BYTE_TO_WORD    ?= $(NANOSOC_SOC_FOR_HEX)/pynq/scripts/hex_byte_to_word.py
TC_PATH := $(if $(strip $(TOOLCHAIN_BIN)),PATH=$(TOOLCHAIN_BIN):$$PATH ,)

# Reproducible images (2026-09-23). The ONLY bytes that differed between two
# builds of the same source were MicroPython's banner date ("... on 2026-07-15"),
# which vendor/micropython/py/makeversionhdr.py takes from the wall clock unless
# SOURCE_DATE_EPOCH is set. Pin it to the last commit that touched THIS
# directory, so the flash image is a function of the source alone and its md5
# (fpga/rp/nanosoc_upy/flash_image.json) can be re-derived by anyone. Override
# on the command line if you really want a different stamp.
ifeq ($(strip $(SOURCE_DATE_EPOCH)),)
SOURCE_DATE_EPOCH := $(shell git -C $(CURDIR) log -1 --format=%ct -- . 2>/dev/null)
endif
ifeq ($(strip $(SOURCE_DATE_EPOCH)),)
$(error SOURCE_DATE_EPOCH is unset and this is not a git checkout -- pass SOURCE_DATE_EPOCH=<unix time> so the image is reproducible)
endif
export SOURCE_DATE_EPOCH

.PHONY: all xip clean flash
all xip clean:
	@$(TC_PATH)$(MAKE) -C port PYTHON=$(PYTHON) HEX_BYTE_TO_WORD=$(HEX_BYTE_TO_WORD) $@

# `make flash` == `make xip`: the xip recipe now also packs flash_image.{bin,hex}
# (boot table + HOT image + COLD segment). Alias for "give me the flash image".
flash:
	@$(TC_PATH)$(MAKE) -C port PYTHON=$(PYTHON) HEX_BYTE_TO_WORD=$(HEX_BYTE_TO_WORD) xip
