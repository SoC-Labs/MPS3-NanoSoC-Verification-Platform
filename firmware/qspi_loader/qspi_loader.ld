/* qspi_loader.ld — link the M0 flash loader to run IN PLACE at the IMEM
 * physical alias (0x1000_0000).
 *
 * Deliberately NOT linked at 0x0 like the normal apps: the host writes this
 * image straight to IMEM_BASE and jumps to it with SP/PC set over SWD, so the
 * bootrom and the REMAP dance are bypassed entirely. That matters because this
 * loader exists to POPULATE the flash the boot path reads — it must not depend
 * on that path working.
 *
 * Cortex-M0 executes from the code region 0x0000_0000-0x1FFF_FFFF, and
 * 0x1000_0000 is inside it, so running from the alias is legal.
 *
 * Layout inside the 128 KB IMEM (swd.py IMEM_SIZE, cross-checked against
 * nanosoc.ld's 128K and the RM's IMEM_RAM_ADDR_W=17):
 *   0x1000_0000  code + rodata + data + bss   (64 KB budget, actual is ~2 KB)
 *   0x1001_0000  mailbox (64 B, FIXED — the host pokes this address directly)
 *   0x1001_0040  payload buffer (48 KB)
 *   0x1002_0000  stack top (grows down; ~16 KB of slack below it)
 *
 * The mailbox/buffer addresses are hard-coded in qspi_loader.c rather than
 * exported symbols, so the host needs no ELF parsing — it writes fixed
 * addresses. Keep the two files in step.
 */
ENTRY(loader_main)

MEMORY
{
    CODE (rx) : ORIGIN = 0x10000000, LENGTH = 64K
}

SECTIONS
{
    .text 0x10000000 :
    {
        KEEP(*(.text.loader_main))   /* entry first: the host jumps to base */
        *(.text*)
        *(.rodata*)
    } > CODE

    .data : { *(.data*) } > CODE

    .bss (NOLOAD) : { *(.bss*) *(COMMON) } > CODE

    /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
