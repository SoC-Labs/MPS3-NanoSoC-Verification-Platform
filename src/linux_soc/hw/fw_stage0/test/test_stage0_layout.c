/*
 * test_stage0_layout.c -- print the status-block layout FROM THE C HEADER's
 * X-macro, one "name 0xOFF" per line. test/Makefile diffs this against
 * `stage0_status.py --layout`, so the Python decoder (and anything that copies
 * it, e.g. pyverify's reader) cannot drift from what stage0 writes.
 */
#include <stdio.h>
#include "../stage0_status.h"

int main(void)
{
#define S0_X_PRINT(name, off, doc) printf("%s 0x%02X\n", #name, (unsigned)(off));
    S0_STATUS_FIELDS(S0_X_PRINT)
#undef S0_X_PRINT
    printf("magic_end 0x%02X\n", 0xFCu);
    return 0;
}
