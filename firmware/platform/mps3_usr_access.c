/*
 * mps3_usr_access.c — read the FABRIC's build identity out of USRACC.
 *
 * The missing sensor of the firmware/bitstream cross-check. `ba2f4be` built the
 * whole check -- the wire field, the codec's three-state verdict, the pyverify
 * client, the operator command, the FakeShell conformance -- and then recorded,
 * honestly, that it would read `null` on every board until somebody built the
 * register `docs/VERSIONING_PLAN.md` §3.4 specifies. This is the other end of
 * that: `fpga/shell/ip/usr_access_rd/` is the register, and this is the read.
 *
 * WHAT IT IS COMPARING, AND WHY THE COMPARISON IS WORTH ANYTHING
 * -------------------------------------------------------------
 * One generator (`scripts/gen_version.py`) produces HARNESS_VER32.
 * `fpga/dfx/build_dfx.tcl` stamps it into `BITSTREAM.CONFIG.USR_ACCESS` (the
 * device AXSS register) and the same number is compiled into the image. They
 * agree BY CONSTRUCTION -- unless the `.bit` and the image baked into it came
 * from different builds. That is the "flashable base whose `updatemem` was
 * never re-run" hazard this platform has already been bitten by, and until this
 * file existed nothing running on the board could see it.
 *
 * WHY THERE IS A MAGIC READ BEFORE THE VALUE READ
 * ----------------------------------------------
 * Every unmapped page in the shell's AXI-Lite window reads back 0. On a shell
 * without the USRACC block -- which is every shell fielded to date -- a bare
 * VALUE read returns 0, and 0 is a perfectly plausible build identity. Reporting
 * it would mean answering "the fabric says 0x00000000, and it disagrees with
 * your image" when the truth is "there is no sensor here". So MAGIC is read
 * first and a mismatch means NO ANSWER, which the codec renders as `null`.
 *
 * This is also why this file, not the coordinator, owns the decision: the whole
 * of `coordinator_handle_version()`'s involvement is one call, and no future
 * edit to that function can accidentally turn "not checked" into "checked, and
 * fine".
 */
#include <stddef.h>
#include <stdio.h>
#include <inttypes.h>

#include "../common/platform_regs.h"
#include "mps3_version.h"

void mps3_fabric_usr_access_str(char *dst, unsigned dst_sz)
{
    uint32_t magic;
    uint32_t status;

    if (dst == NULL || dst_sz == 0u) {
        return;
    }
    dst[0] = '\0';   /* the default answer is "I did not read one" */

    /* 1. Is there a block behind this page at all? */
    magic = mps3_reg_read32(MPS3_USRACC_BASE, USRACC_MAGIC);
    if (magic != USRACC_MAGIC_VALUE) {
        return;
    }

    /* 2. Has the USR_ACCESSE2 primitive actually presented its word? An
     *    un-captured value reads 0, and 0 is not distinguishable from a real
     *    stamp of 0 -- so an unset VALID is also "no answer", not "zero". */
    status = mps3_reg_read32(MPS3_USRACC_BASE, USRACC_STATUS);
    if ((status & USRACC_STATUS_VALID) == 0u) {
        return;
    }

    /* 3. The word. Rendered EXACTLY as coordinator.c's format_id_hex() renders
     *    ver32 -- "0x" + 8 lowercase hex digits -- because net_proto.c derives
     *    the skew verdict by comparing the two STRINGS. A drift between the two
     *    formatters would report skew on a board whose numbers agree;
     *    firmware/test/test_coordinator_dispatch.c's skew-false case is what
     *    holds them together. */
    (void)snprintf(dst, (size_t)dst_sz, "0x%08" PRIx32,
                   mps3_reg_read32(MPS3_USRACC_BASE, USRACC_VALUE));
}
