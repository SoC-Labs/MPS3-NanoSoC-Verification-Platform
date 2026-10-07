/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright (C) 2026, SoC Labs (www.soclabs.org)
 *
 * startup.S -- vector table and reset handler for nanosoc_hello.
 *
 * The `nanosoc` design's processor is a Cortex-M0. At reset, and when you
 * start the program from GDB, it takes two words from the vector table:
 *   word 0  the initial stack pointer
 *   word 1  the address of the first instruction (Reset_Handler)
 * The reset handler then sets up memory for C and calls main().
 */

        .syntax unified
        .cpu    cortex-m0
        .thumb

/* ---- vector table: first in IMEM (0x1000_0000), see nanosoc_hello.ld ---- */
        .section .vectors, "a", %progbits
        .align  2
        .global __vectors
__vectors:
        .word   _stack_top              /*  0  initial stack pointer      */
        .word   Reset_Handler           /*  1  reset                      */
        .word   NMI_Handler             /*  2  NMI                        */
        .word   HardFault_Handler       /*  3  HardFault                  */
        .word   0, 0, 0, 0, 0, 0, 0     /*  4-10  reserved on Cortex-M0   */
        .word   Default_Handler         /* 11  SVCall                     */
        .word   0, 0                    /* 12-13  reserved                */
        .word   Default_Handler         /* 14  PendSV                     */
        .word   Default_Handler         /* 15  SysTick                    */
        /* No interrupt entries: this program enables no interrupts. */

/* ---- reset handler ---- */
        .text
        .align  1
        .global Reset_Handler
        .thumb_func
        .type   Reset_Handler, %function
Reset_Handler:
        ldr     r0, =_stack_top         /* set the stack again, so a jump  */
        mov     sp, r0                  /* to Reset_Handler also works     */

        ldr     r0, =_sdata             /* copy initialised data from IMEM */
        ldr     r1, =_edata             /* to DMEM                         */
        ldr     r2, =_sidata
copy_data:
        cmp     r0, r1
        bhs     zero_bss
        ldr     r3, [r2]
        str     r3, [r0]
        adds    r0, r0, #4
        adds    r2, r2, #4
        b       copy_data

zero_bss:
        ldr     r0, =_sbss              /* clear uninitialised data        */
        ldr     r1, =_ebss
        movs    r2, #0
zero_loop:
        cmp     r0, r1
        bhs     call_main
        str     r2, [r0]
        adds    r0, r0, #4
        b       zero_loop

call_main:
        bl      main
park:
        b       park                    /* main() returned: stop here      */
        .size   Reset_Handler, . - Reset_Handler

/* ---- fault and unused-exception handler: stop where GDB can see it ---- */
        .align  1
        .global Default_Handler
        .thumb_func
        .type   Default_Handler, %function
Default_Handler:
        b       .                       /* loop here: halt in GDB to see it */
        .size   Default_Handler, . - Default_Handler

        .weak   NMI_Handler
        .thumb_set NMI_Handler, Default_Handler
        .weak   HardFault_Handler
        .thumb_set HardFault_Handler, Default_Handler
