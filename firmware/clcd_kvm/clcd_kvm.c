/*
 * clcd_kvm.c -- harness-side mechanism for the CLCD KVM CSR block @ 0x44AD_0000.
 * See clcd_kvm.h for the two rules this module exists to enforce (never RMW
 * CTRL's ownership bit; EVENT is W1C, not read-to-clear) and the HAZARD note.
 *
 * FROZEN contract: fpga/shell/ip/clcd_kvm/README.md §5 / shell-regmap.md v0.5.
 * Compiled ONLY when MPS3_HAS_CLCD_KVM is defined (the whole file is inside the
 * gate); with it undefined, clcd_kvm.h supplies inline no-ops instead.
 *
 * Every function here is a handful of single-beat AXI4-Lite accesses -- tens of
 * cycles, no loops, no waits. NOTHING in this file blocks: it runs in the same
 * superloop as the lwIP TCP/ARP timers, and a stall there drops the network,
 * which is the board's only ingress.
 */
#include "clcd_kvm.h"

#ifdef MPS3_HAS_CLCD_KVM

/* The write-1-pulse bits. They read back 0 in the shipped RTL, but strip them
 * from every read-modify-write anyway: a RMW must not be able to re-arm a
 * one-shot, whatever a future revision of the block decides to read back. */
#define CLCDKVM_CTRL_W1P_MASK \
    (CLCDKVM_CTRL_PANEL_RST_PULSE | CLCDKVM_CTRL_FORCE_SWITCH | CLCDKVM_CTRL_SRC_SEL_WE)

/* CTRL bits that are genuinely read-write state and must be preserved across a
 * RMW. Note src_sel (bit 0) is DELIBERATELY ABSENT: it reads back tgt_owner, and
 * writing it back without src_sel_we is a no-op in hardware -- but keeping it out
 * of the mask makes that explicit, so nobody later "fixes" a RMW by adding
 * src_sel_we to it. Ownership moves through clcd_kvm_request_owner(), only. */
#define CLCDKVM_CTRL_RW_MASK \
    (CLCDKVM_CTRL_FORCE_HARNESS | CLCDKVM_CTRL_TIMEOUT_EN | CLCDKVM_CTRL_PB_EN | \
     CLCDKVM_CTRL_DUT_REQ_EN | CLCDKVM_CTRL_BACKLIGHT | CLCDKVM_CTRL_PANEL_RST_N | \
     CLCDKVM_CTRL_BL_RST_SRC)

static uint32_t ctrl_rw(void)
{
    uint32_t v = mps3_reg_read32(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL);
    return v & CLCDKVM_CTRL_RW_MASK;      /* drops src_sel + every W1P bit */
}

void clcd_kvm_init(void)
{
    /* ONE write for the whole steady state. Staging it -- bl_rst_src first, the
     * backlight and reset-release after -- would hand BL/RST to the KVM while
     * CTRL[5]/[6] were still at their reset value of 0, i.e. blank the panel and
     * hold it in reset for the gap between the two writes. */
    mps3_reg_write32(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL,
                     CLCDKVM_CTRL_BL_RST_SRC   |   /* the KVM owns BL/RST (README §10) */
                     CLCDKVM_CTRL_BACKLIGHT    |   /* CLCD_BL  = 1 (active-HIGH: lit)   */
                     CLCDKVM_CTRL_PANEL_RST_N  |   /* CLCD_RST = 1 (active-LOW: released)*/
                     CLCDKVM_CTRL_TIMEOUT_EN   |   /* keep the hung-owner timeout armed */
                     CLCDKVM_CTRL_PB_EN);          /* keep the button live              */
    /* dut_req_en stays 0: firmware opts the DUT in explicitly, later, if at all.
     * force_harness stays 0. src_sel is untouched (no src_sel_we in that write),
     * so a button press racing us cannot be clobbered. */

    /* Clear whatever the KVM latched before we looked (e.g. a power-on
     * panel_reset_done). W1C of exactly the bits we read -- never a blind
     * write of EVENT_ALL, which would destroy an event we never saw. */
    (void)clcd_kvm_take_events();
}

void clcd_kvm_release_bl_rst(void)
{
    /* Back to the drop-in default: BL/RST follow clcd_0's CTRL[1]/[2], i.e.
     * exactly the behaviour on the board today. The KVM's hardware reset
     * sequencer still overrides CLCD_RST in this mode -- recoverability is
     * intact either way (README §10). */
    clcd_kvm_ctrl_update(0u, CLCDKVM_CTRL_BL_RST_SRC);
}

uint32_t clcd_kvm_status(void)
{
    return mps3_reg_read32(MPS3_CLCDKVM_BASE, CLCDKVM_STATUS);
}

uint32_t clcd_kvm_take_events(void)
{
    uint32_t ev = mps3_reg_read32(MPS3_CLCDKVM_BASE, CLCDKVM_EVENT);
    if (ev)
        mps3_reg_write32(MPS3_CLCDKVM_BASE, CLCDKVM_EVENT, ev);  /* W1C: clear
                                                                  * exactly what
                                                                  * we read      */
    return ev;
}

void clcd_kvm_ctrl_update(uint32_t set_bits, uint32_t clr_bits)
{
    /* Cannot move ownership by construction: src_sel_we is stripped from both the
     * read-back and the caller's `set_bits`, so tgt_owner is never write-enabled.
     * A USER_nPB1 press landing between the read and the write is therefore
     * preserved -- which is the entire reason CTRL[16] exists. */
    uint32_t v = ctrl_rw();
    v |=  (set_bits & CLCDKVM_CTRL_RW_MASK);
    v &= ~(clr_bits & CLCDKVM_CTRL_RW_MASK);
    v &= ~CLCDKVM_CTRL_W1P_MASK;
    mps3_reg_write32(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL, v);
}

void clcd_kvm_request_owner(unsigned owner)
{
    uint32_t v = ctrl_rw() | CLCDKVM_CTRL_SRC_SEL_WE;
    if (owner == CLCDKVM_OWNER_DUT)
        v |= CLCDKVM_CTRL_SRC_SEL;
    mps3_reg_write32(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL, v);
}

void clcd_kvm_panel_reset(void)
{
    /* W1P. Reads back 0 and is never stored, so it cannot linger and re-fire. */
    mps3_reg_write32(MPS3_CLCDKVM_BASE, CLCDKVM_CTRL,
                     ctrl_rw() | CLCDKVM_CTRL_PANEL_RST_PULSE);
}

void clcd_kvm_set_backlight(int on)
{
    if (on) clcd_kvm_ctrl_update(CLCDKVM_CTRL_BACKLIGHT, 0u);
    else    clcd_kvm_ctrl_update(0u, CLCDKVM_CTRL_BACKLIGHT);
}

void clcd_kvm_set_dut_req_en(int en)
{
    if (en) clcd_kvm_ctrl_update(CLCDKVM_CTRL_DUT_REQ_EN, 0u);
    else    clcd_kvm_ctrl_update(0u, CLCDKVM_CTRL_DUT_REQ_EN);
}

#endif /* MPS3_HAS_CLCD_KVM */
