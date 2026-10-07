# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
#
# Build nanosoc_hello.elf for the `nanosoc` design (a Cortex-M0).
# Needs the Arm GNU Toolchain (arm-none-eabi-gcc) on your PATH.
# No C library: -nostdlib, our own startup.S and linker script.

CROSS   ?= arm-none-eabi-
CC      := $(CROSS)gcc
SIZE    := $(CROSS)size

TARGET  := nanosoc_hello.elf
SRCS    := startup.S main.c
LDSCRIPT:= nanosoc_hello.ld

CFLAGS  := -mcpu=cortex-m0 -mthumb -Os -g -std=c99 -Wall -Wextra \
           -ffreestanding -ffunction-sections -fdata-sections
LDFLAGS := -nostdlib -T $(LDSCRIPT) -Wl,--gc-sections -Wl,-Map=nanosoc_hello.map

$(TARGET): $(SRCS) $(LDSCRIPT) Makefile
	$(CC) $(CFLAGS) $(SRCS) $(LDFLAGS) -o $@
	$(SIZE) $@

clean:
	rm -f $(TARGET) nanosoc_hello.map

.PHONY: clean
