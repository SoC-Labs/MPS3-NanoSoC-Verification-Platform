/*
 * overlay_store_bm.c -- the overlay store's BARE-METAL engine provider
 * (overlay_store.h "ENGINE PROVIDER"): the user microSD through the shell's
 * usd_spi block (firmware/usd/usd.c + ovl_bdev_usd.c), the boot latch in an
 * initialised .data word, and the greybox clearing baked into the image.
 *
 * Linked into the MicroBlaze shell image only. harnessd has its own provider
 * (src/linux_harness/sw/harnessd/ovlstore_linux.c): under Linux the kernel owns
 * usd_spi, so usd.c must never run there.
 */
#include <stdint.h>

#include "overlay_store.h"
#include "ovl_bdev.h"
#include "../usd/usd.h"

/* ---- the device ----------------------------------------------------------------------- */

int ovlstore_engine_bind(ovl_bdev_t *bd, ovlstore_sd_cfg_t *cfg)
{
    /* usd_init() probes the block ID and, with no block or no card, writes
     * NOTHING to the DATA register and never sets EN (usd.h rule 1): an empty
     * slot costs one STATUS + one CTRL read a pass. own_poll = true: the store's
     * poll runs usd_poll() first, so ONE service row ("usd") drives both, and
     * never twice a pass. */
    usd_init();
    ovl_bdev_usd_bind(bd, true);
    /* The explicit "erase-all" wipe is allowed on the bare-metal shell only: the
     * user microSD is not on USB, so an Ethernet-only user has no other way to
     * take over a factory card. (Under Linux the card holds the running system.) */
    cfg->allow_wipe = true;
    cfg->raw_partition = false;
    return 0;
}

/* ---- THE BOOT LATCH (overlay_store.h) ---------------------------------------------------
 *
 * WHY .data IS A PER-CONFIGURATION LATCH ON THIS CPU, verified in the tree:
 *   - firmware/platform/lscript.ld.in puts .data/.sdata in local_lmb with NO AT()
 *     (no load address), so nothing is copied at start-up: the initial value is
 *     the BRAM INIT content updatemem bakes into the BITSTREAM with the ELF.
 *   - Vitis 2024.1's MicroBlaze crt0.S (_start1) calls _crtinit, and crtinit.S
 *     ZEROES .sbss and .bss and then calls _program_init / __init / main -- it
 *     never touches .data (data/embeddedsw/lib/microblaze/src/crtinit.S).
 *   - A WDOG reset (proc_sys_reset aux_reset_in, the `reboot` verb) resets the
 *     MicroBlaze, not the BRAM, so it re-runs crt0 over the SAME .data.
 * So this word reads OVL_BM_LATCH_FRESH exactly once per FPGA configuration (and
 * once per JTAG ELF download -- a new image is a new configuration of the
 * firmware), and whatever the glue wrote survives every warm restart.
 *
 * It MUST be initialised to a NON-ZERO value: a zero-initialised static goes to
 * .bss, which crtinit zeroes on EVERY reset -- the exact opposite of a latch.
 * The section attribute pins it in .data whatever the small-data threshold. */
#define OVL_BM_LATCH_FRESH 0x46524553u   /* "FRES": never a valid latch word */

__attribute__((section(".data.mps3_ovl_boot_latch"), used))
static volatile uint32_t s_boot_latch = OVL_BM_LATCH_FRESH;

uint32_t ovlstore_engine_latch_get(void)
{
    return s_boot_latch;
}

void ovlstore_engine_latch_set(uint32_t word)
{
    s_boot_latch = word;
}

#ifdef OVLSTORE_BM_TEST_HOOKS
/* Host tests only: what a RECONFIGURATION does to the latch (the bitstream's
 * BRAM INIT puts the fresh value back). A WDOG reset is modelled by NOT calling
 * this. */
void overlay_store_bm_test_reconfigure(void)
{
    s_boot_latch = OVL_BM_LATCH_FRESH;
}
#endif

/* ---- the greybox clearing ----------------------------------------------------------------
 * Baked into the shell image by the build (firmware/platform/generated/
 * greybox_blob.c, from gen_greybox_blob.py), NOT read from any card or flash. A
 * host binary that links this file defines the five symbols itself. */
extern const uint8_t  mps3_greybox_clearing_bin[];
extern const uint32_t mps3_greybox_clearing_len_words;
extern const uint32_t mps3_greybox_clearing_crc32;
extern const uint32_t mps3_greybox_clearing_rm_id;     /* fpga/dfx/rm_list.tcl RM_LIB(rm_greybox,rm_id) = 0x0 */
extern const uint32_t mps3_greybox_clearing_static_id; /* this shell build's own static_id */

int overlay_store_get_greybox_clearing(overlay_manifest_info_t *out)
{
    out->static_id       = mps3_greybox_clearing_static_id;
    out->rm_id           = mps3_greybox_clearing_rm_id;
    out->clear_len_words = mps3_greybox_clearing_len_words;
    out->clear_crc32     = mps3_greybox_clearing_crc32;
    out->clear_data      = mps3_greybox_clearing_bin;
    return 0;
}
