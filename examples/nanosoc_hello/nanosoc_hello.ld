/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (C) 2026, SoC Labs (www.soclabs.org)
 *
 * nanosoc_hello.ld -- linker script for the `nanosoc` design.
 *
 *   IMEM  0x1000_0000, 16 KB: code, constants, the vector table first.
 *         The boot ROM also maps it at address 0 before it starts a program,
 *         so the processor finds this vector table at reset too.
 *   DMEM  0x1800_0000, 16 KB: data, then the stack, which grows down from
 *         the top (0x1800_4000).
 */

ENTRY(Reset_Handler)

MEMORY
{
  IMEM (rx)  : ORIGIN = 0x10000000, LENGTH = 16K
  DMEM (rwx) : ORIGIN = 0x18000000, LENGTH = 16K
}

_stack_top = ORIGIN(DMEM) + LENGTH(DMEM);

SECTIONS
{
  .text :
  {
    KEEP(*(.vectors))
    *(.text*)
    *(.rodata*)
    . = ALIGN(4);
  } > IMEM

  /* Initialised data: runs in DMEM, stored in IMEM, copied by Reset_Handler. */
  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > DMEM AT > IMEM
  _sidata = LOADADDR(.data);

  /* Uninitialised data: cleared by Reset_Handler. */
  .bss (NOLOAD) :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > DMEM

  /* Leave at least 1 KB of DMEM for the stack. */
  ASSERT(_ebss + 1024 <= _stack_top, "nanosoc_hello: DMEM is full")

  /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
