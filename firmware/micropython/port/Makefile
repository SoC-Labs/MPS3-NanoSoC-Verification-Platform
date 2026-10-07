# Makefile — MicroPython port for the SoC Labs NanoSoC (Cortex-M0, armv6-m).
#
# Builds a bare-metal REPL-over-UART2 image and emits the word-hex the FPGA
# flow preloads into IMEM. One command:  `make`  (from firmware/micropython/).
#
# Layout:
#   firmware/micropython/port/     <- this dir (port sources + this Makefile)
#   firmware/micropython/vendor/micropython  <- upstream clone (TOP)
#   firmware/micropython/build/    <- ELF, .bin, byte-hex and word-hex outputs

# ---- Point at the upstream tree --------------------------------------------
TOP := $(abspath ../vendor/micropython)

# All build artefacts go under firmware/micropython/build.
BUILD ?= $(abspath ../build)

include $(TOP)/py/mkenv.mk

# ---- Toolchain: cortex-m0 cross build --------------------------------------
CROSS_COMPILE ?= arm-none-eabi-

# qstr definitions (must come before including py.mk)
QSTR_DEFS = qstrdefsport.h

# MicroPython feature configuration
MICROPY_ROM_TEXT_COMPRESSION ?= 1

include $(TOP)/py/py.mk

# ---- Includes --------------------------------------------------------------
INC += -I.
INC += -I$(TOP)
INC += -I$(BUILD)

# ---- Cortex-M0 (armv6-m) flags ---------------------------------------------
CFLAGS_CPU = -mcpu=cortex-m0 -mthumb -mfloat-abi=soft
CFLAGS += $(INC) -Wall -Werror -std=c99 -nostdlib $(CFLAGS_CPU) $(COPT)
CFLAGS += -fdata-sections -ffunction-sections
CFLAGS += -g   # debug info in the ELF (stripped from the binary image)
# EXTRA_CFLAGS lets the `xip` target inject -DNANOSOC_XIP without editing this
# file; empty for the default resident build.
CFLAGS += $(EXTRA_CFLAGS)

# Size-optimise; the interpreter must fit in 128 KB IMEM.
CSUPEROPT = -Os
COPT = -Os -DNDEBUG

# ---- Link ------------------------------------------------------------------
# Link through gcc (not ld) so libgcc (__aeabi_uidiv etc. — the M0 has no
# hardware divide) and the right multilib are found automatically.
LD = $(CC)
# LDSCRIPT selects the layout: nanosoc.ld (resident, default) or nanosoc_xip.ld
# (hybrid hot/cold XiP, set by the `xip` target).
LDSCRIPT ?= nanosoc.ld
LDFLAGS += $(CFLAGS_CPU) -nostdlib -T $(LDSCRIPT)
LDFLAGS += -Wl,-Map=$@.map,--cref -Wl,--gc-sections
# libgcc provides the armv6-m soft-arithmetic helpers; libc string0.c below
# covers memcpy/memset. -lgcc must come last.
LIBS = -lgcc

# ---- Sources ---------------------------------------------------------------
SRC_C = \
	main.c \
	nanosoc_uart.c \
	xip_bringup.c \
	modnanosoc.c \
	extmod/machine_mem.c \
	shared/libc/string0.c \
	shared/libc/printf.c \
	shared/readline/readline.c \
	shared/runtime/gchelper_native.c \
	shared/runtime/pyexec.c \
	shared/runtime/stdout_helpers.c \

SRC_SS = \
	shared/runtime/gchelper_thumb1.s \

# Files that contribute qstrs / MP_REGISTER_MODULE entries.
SRC_QSTR += modnanosoc.c extmod/machine_mem.c shared/readline/readline.c shared/runtime/pyexec.c

# Vendor sources live under $(TOP); port sources live here. Objects for both
# land under $(BUILD).
OBJ += $(PY_CORE_O)
OBJ += $(addprefix $(BUILD)/, $(SRC_C:.c=.o))
OBJ += $(addprefix $(BUILD)/, $(SRC_SS:.s=.o))

# vpath so make finds port sources (here) and vendor sources (under TOP).
vpath %.c . $(TOP)
vpath %.s . $(TOP)

# ---- Outputs ---------------------------------------------------------------
# Passed down by ../Makefile (SOCLABS_NANOSOC_SOC_DIR from tools.env, else the
# pinned fpga/deps/nanosoc_m0_soc submodule); this default is the submodule.
HEX_BYTE_TO_WORD ?= $(abspath $(CURDIR)/../../../fpga/deps/nanosoc_m0_soc/pynq/scripts/hex_byte_to_word.py)

all: $(BUILD)/micropython_word.hex

$(BUILD)/firmware.elf: $(OBJ)
	$(ECHO) "LINK $@"
	$(Q)$(LD) $(LDFLAGS) -o $@ $(OBJ) $(LIBS)
	$(Q)$(SIZE) $@

# Byte-format verilog hex of exactly the sections that live in IMEM.
$(BUILD)/firmware_byte.hex: $(BUILD)/firmware.elf
	$(ECHO) "OBJCOPY $@"
	$(Q)$(OBJCOPY) -O verilog --verilog-data-width 1 \
		-j .isr_vector -j .text -j .data $< $@

# Word-format hex (@00000000 header then one 32-bit LE word per line) for the
# FPGA $readmemh into the 32-bit IMEM BRAM.
$(BUILD)/micropython_word.hex: $(BUILD)/firmware_byte.hex
	$(ECHO) "WORDHEX $@"
	$(Q)$(PYTHON) $(HEX_BYTE_TO_WORD) $< $@

# ===========================================================================
# Hybrid-XiP build (hot IMEM set + cold QSPI-flash XiP segment)
# ===========================================================================
# `make xip` re-invokes this Makefile into a SEPARATE build tree (../build/xip)
# with -DNANOSOC_XIP and the hot/cold linker script, so its objects never clash
# with the resident build's. It emits:
#   micropython_hot_word.hex  resident IMEM image (word-hex, linked at 0x0)
#   micropython_cold.bin      cold .xip segment as raw flash bytes (aperture off 0)
#   micropython_cold.hex      cold .xip segment as @00000000 word-hex ($readmemh)
#   xip_manifest.txt          __xip_* symbols + hot/cold size split (headline)
XIP_BUILD := $(abspath ../build/xip)

.PHONY: xip
xip:
	$(Q)$(MAKE) xip-artifacts flash \
		BUILD=$(XIP_BUILD) \
		LDSCRIPT=nanosoc_xip.ld \
		EXTRA_CFLAGS=-DNANOSOC_XIP

.PHONY: xip-artifacts
xip-artifacts: $(BUILD)/micropython_hot_word.hex \
               $(BUILD)/micropython_hot.bin \
               $(BUILD)/micropython_cold.bin \
               $(BUILD)/micropython_cold.hex \
               $(BUILD)/xip_manifest.txt

# --- Resident (HOT) IMEM image: exactly the sections that live in IMEM. The
# cold .text.xip / .rodata.xip output sections are NOT selected, so they are
# excluded automatically. Same pipeline as the resident micropython_word.hex.
$(BUILD)/firmware_hot_byte.hex: $(BUILD)/firmware.elf
	$(ECHO) "OBJCOPY $@"
	$(Q)$(OBJCOPY) -O verilog --verilog-data-width 1 \
		-j .isr_vector -j .text -j .data $< $@

$(BUILD)/micropython_hot_word.hex: $(BUILD)/firmware_hot_byte.hex
	$(ECHO) "WORDHEX $@"
	$(Q)$(PYTHON) $(HEX_BYTE_TO_WORD) $< $@

# --- Raw HOT image as flash bytes: exactly the loadable IMEM sections
# (.isr_vector + .text + .data), contiguous from IMEM VMA 0x0. This is the blob
# stage-0 CPU-copies from flash (@0x1000) into IMEM; flash_pack.py CRC32s it and
# names it as boot-table entry 0's app image. objcopy -O binary uses section
# LMA, so .data's initialiser lands right after .text exactly as in IMEM.
$(BUILD)/micropython_hot.bin: $(BUILD)/firmware.elf
	$(ECHO) "OBJCOPY $@"
	$(Q)$(OBJCOPY) -O binary \
		-j .isr_vector -j .text -j .data $< $@

# --- Cold XiP segment as raw flash bytes (as it appears at aperture offset 0,
# i.e. flashed so it maps to 0x70000000). A plain `-O binary` over the whole ELF
# would span 0x0..0x70000000 (a ~1.8 GB sparse file); --only-section keeps just
# the cold blob.
$(BUILD)/micropython_cold.bin: $(BUILD)/firmware.elf
	$(ECHO) "OBJCOPY $@"
	$(Q)$(OBJCOPY) -O binary \
		--only-section=.text.xip --only-section=.rodata.xip $< $@

# --- Cold XiP segment as $readmemh-loadable word-hex, rebased to @00000000 so a
# flash VIP can load it at aperture offset 0 (byte offset 0 == 0x70000000).
# Derived from the raw .bin: a binary input re-addresses from 0, side-stepping
# the fact that `-O verilog` emits the section VMA (0x70000000) rather than LMA.
$(BUILD)/firmware_cold_byte.hex: $(BUILD)/micropython_cold.bin
	$(ECHO) "OBJCOPY $@"
	$(Q)$(OBJCOPY) -I binary -O verilog --verilog-data-width 1 $< $@

$(BUILD)/micropython_cold.hex: $(BUILD)/firmware_cold_byte.hex
	$(ECHO) "WORDHEX $@"
	$(Q)$(PYTHON) $(HEX_BYTE_TO_WORD) $< $@

# --- Manifest: the headline hot/cold split. __xip_* from nm, section sizes from
# size/objdump. This is THE key output: it says how small IMEM can be.
$(BUILD)/xip_manifest.txt: $(BUILD)/firmware.elf $(BUILD)/micropython_cold.bin
	$(ECHO) "MANIFEST $@"
	$(Q){ \
	  echo "# NanoSoC MicroPython hybrid-XiP manifest"; \
	  echo "# elf: $(BUILD)/firmware.elf   (LDSCRIPT=$(LDSCRIPT))"; \
	  echo ""; \
	  echo "## XiP (cold) segment symbols"; \
	  $(NM) $(BUILD)/firmware.elf | grep -E '__xip_(start|end|size)__' \
	    | sort -k3; \
	  echo ""; \
	  echo "## arm-none-eabi-size (whole image: hot .text + .data + .bss)"; \
	  $(SIZE) $(BUILD)/firmware.elf; \
	  echo ""; \
	  echo "## Section headers (objdump -h)"; \
	  $(OBJDUMP) -h $(BUILD)/firmware.elf \
	    | grep -E 'Idx|\.text|\.rodata|\.data|\.bss|\.isr'; \
	  echo ""; \
	  echo "## Cold flash blob (micropython_cold.bin) size, bytes:"; \
	  wc -c < $(BUILD)/micropython_cold.bin; \
	} > $@
	$(Q)cat $@

# ===========================================================================
# Packed QSPI flash image (boot table + HOT image + COLD segment)
# ===========================================================================
# `make flash` (from firmware/micropython/) assembles ONE flash image the true
# product boot consumes: boot table @0x0 (magic "BOOT", entry 0 -> the HOT
# image), the raw HOT .bin @0x1000 (copied into IMEM by stage-0), and the COLD
# .xip segment @0x20000 (executed in place from flash). Emits both a raw .bin
# and a byte-hex $readmemh file for the SST26 flash VIP.
FLASH_PACK   := $(abspath ../flash_pack.py)
FLASH_BIN    := $(BUILD)/flash_image.bin
FLASH_HEX    := $(BUILD)/flash_image_byte.hex
HOT_FLASH_OFF  ?= 0x1000
COLD_FLASH_OFF ?= 0x20000

.PHONY: flash
flash: $(BUILD)/micropython_hot.bin $(BUILD)/micropython_cold.bin
	$(ECHO) "FLASHPACK $(FLASH_BIN)"
	$(Q)$(PYTHON) $(FLASH_PACK) \
		--hot  $(BUILD)/micropython_hot.bin  --hot-offset  $(HOT_FLASH_OFF) \
		--cold $(BUILD)/micropython_cold.bin --cold-offset $(COLD_FLASH_OFF) \
		--out-bin $(FLASH_BIN) --out-hex $(FLASH_HEX)

# arm-none-eabi tool aliases the MicroPython env does not pre-define.
NM      ?= $(CROSS_COMPILE)nm
OBJDUMP ?= $(CROSS_COMPILE)objdump

include $(TOP)/py/mkrules.mk
