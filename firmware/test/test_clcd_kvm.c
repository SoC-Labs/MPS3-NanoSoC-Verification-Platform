/*
 * test_clcd_kvm.c -- host-gcc tests for the CLCD KVM HANDOVER: firmware/clcd/
 * clcd.c's lose/regain/repaint-all state machine driven off the KVM's STATUS/
 * EVENT registers, plus firmware/clcd_kvm/clcd_kvm.c's CSR mechanism (the
 * src_sel_we clobber-safety and the EVENT W1C rule).
 *
 * Built with -DMPS3_HAL_MOCK -DMPS3_HAS_CLCD -DMPS3_HAS_CLCD_KVM
 * -DMPS3_CLCD_TEST_HOOKS. This is the ONLY firmware/test binary that defines
 * MPS3_HAS_CLCD_KVM, so it is the only place the KVM code path is compiled.
 *
 * Two register models over the mock file:
 *   - MPS3_CLCD_BASE    : the same behavioural {RS,byte} FIFO as test_clcd.c
 *                         (NEVER_FULL here -- we are testing handover, not the
 *                         byte bound, which test_clcd.c already proves).
 *   - MPS3_CLCDKVM_BASE : a faithful CSR model of clcd_kvm -- CTRL write-decode
 *                         (src_sel gated by src_sel_we, W1P bits), STATUS driven
 *                         by the test, and a real W1C EVENT register. This lets
 *                         us assert BOTH what the firmware writes and how it
 *                         reacts to what the KVM reports.
 *
 * What these prove (the required set: gain / lose / reset / repaint-all + the
 * EVENT W1C rule + the src_sel_we clobber safety + the gained-vs-reset subtlety):
 *   1. EVENT is W1C, read does NOT clear, and take_events() clears exactly the
 *      bits it read (a bit set after the read survives).
 *   2. clcd_kvm_init() hands BL/RST to the KVM in ONE write (bl_rst_src +
 *      backlight + panel_rst_n + timeout_en + pb_en), and never sets src_sel_we.
 *   3. A read-modify-write of CTRL (backlight, dut_req_en) NEVER moves ownership:
 *      a concurrent button press that changed tgt_owner survives the RMW.
 *   4. clcd_kvm_request_owner() is the ONLY thing that moves tgt_owner, and it
 *      does so via src_sel_we.
 *   5. LOSE: switch_pending (still owner=HARNESS, S_DRAIN) makes the driver paint
 *      the "DUT has the display" OSD; when the KVM then drives the pads the
 *      driver RELINQUISHES -- it pushes nothing and does not spin (50k passes).
 *   6. REGAIN: harness_gained|panel_reset_done while owner=HARNESS restarts the
 *      init stream and repaints EVERY cell (a full-screen byte count, not a diff).
 *   7. THE SUBTLETY: panel_reset_done with NO harness_gained and NO owner change
 *      (the interlock-mid-settle case) STILL forces a full re-init+repaint --
 *      a harness_gained-only rule would leave the harness painting a wiped panel.
 *   8. PB1 HELD THROUGH POWER-UP (the D13 skip-default escape hatch) is ignored
 *      until its first release: no page cycle, no handover, no give-back request.
 *   9. The one-time KVM setup (clcd_kvm_init + pb_en clear) runs exactly once
 *      even when the DUT owns the panel at the first poll (relinquished before
 *      ST_RESET), so hardware and firmware never both act on one PB1 press.
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../clcd/clcd.h"
#include "../clcd_kvm/clcd_kvm.h"
#include "../common/platform_regs.h"
#include "../common/net_proto.h"
#include "../common/diag.h"
#include "../coordinator/coordinator.h"
#include "../coordinator/swap_fsm.h"
#include "../smsc911x/smsc911x.h"
#include "../clcd/hx8347_init.h"
#include "mock_regs.h"

static int s_checks = 0;
#define CHECK(cond) do { assert(cond); s_checks++; } while (0)

/* ==== symbols clcd.c links against (the firmware/test/ fake pattern) ======== */
mps3_shell_state_t   g_shell_state;
volatile mps3_diag_t g_mps3_diag;
static mps3_swap_result_t s_swap_res;
const mps3_swap_result_t *swap_fsm_last_result(void) { return &s_swap_res; }
static uint32_t s_icap_bytes;
uint32_t swap_fsm_icap_bytes(void) { return s_icap_bytes; }
static int s_link_up; static uint16_t s_anlpar;
int smsc911x_link_up(void) { return s_link_up; }
int smsc911x_mii_read(uint32_t reg, uint16_t *v) { if (v) *v = (reg==0x05u)?s_anlpar:0u; return 0; }
static uint8_t s_mac[6] = { 0x02, 0x00, 0x00, 0x4D, 0x50, 0x53 };
void mps3_platform_mac(uint8_t mac[6]) { memcpy(mac, s_mac, 6); }

/* ==== CLCD FIFO model (infinite sink; test_clcd.c owns the byte-bound proof) = */
static struct { unsigned total; } clcdfifo;
static int clcd_fifo_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b;
    if (!wr) {                       /* STATUS: never full, always "empty enough" */
        *val = (off == CLCD_STATUS) ? CLCD_STATUS_FIFO_EMPTY : 0u;
        return 1;
    }
    if (off == CLCD_CMD || off == CLCD_DATA) clcdfifo.total++;
    return 1;
}

/* ==== KVM CSR model =========================================================
 * A faithful little clcd_kvm: CTRL decode (src_sel gated by src_sel_we, W1P bits
 * self-clearing), a real W1C EVENT register, and STATUS/TUNNEL the test poses.
 * tgt_owner lives here so we can prove the firmware moves it only via src_sel_we
 * and a "button press" (a direct poke) is never clobbered by a firmware RMW. */
static struct {
    uint32_t ctrl_persist;   /* the RW bits the block stores (no src_sel/W1P)   */
    uint32_t tgt_owner;      /* 0/1 -- moved by src_sel_we writes OR a "button"  */
    uint32_t status;         /* what STATUS reads (test-posed)                   */
    uint32_t event;          /* W1C                                             */
    uint32_t tunnel;
    unsigned last_ctrl_writes;
    unsigned we_writes;      /* CTRL writes carrying src_sel_we (ownership moves) */
    unsigned init_writes;    /* CTRL writes of exactly clcd_kvm_init()'s word      */
    unsigned hw_toggles;     /* ownership toggles the HARDWARE button made (pb_en) */
} kvm;

/* clcd_kvm_init()'s one-write steady state (clcd_kvm.c). */
#define KVM_INIT_WORD (CLCDKVM_CTRL_BL_RST_SRC | CLCDKVM_CTRL_BACKLIGHT | \
                       CLCDKVM_CTRL_PANEL_RST_N | CLCDKVM_CTRL_TIMEOUT_EN | \
                       CLCDKVM_CTRL_PB_EN)
/* CTRL's RTL reset value (clcd_kvm.sv): timeout_en=1, pb_en=1 -- "press the
 * button and it works, with no firmware at all". Everything else 0. */
#define KVM_CTRL_RESET (CLCDKVM_CTRL_TIMEOUT_EN | CLCDKVM_CTRL_PB_EN)

/* CTRL RW bits the model persists (mirror clcd_kvm.c's RW mask). */
#define KVM_CTRL_RW (CLCDKVM_CTRL_FORCE_HARNESS | CLCDKVM_CTRL_TIMEOUT_EN | \
                     CLCDKVM_CTRL_PB_EN | CLCDKVM_CTRL_DUT_REQ_EN | \
                     CLCDKVM_CTRL_BACKLIGHT | CLCDKVM_CTRL_PANEL_RST_N | \
                     CLCDKVM_CTRL_BL_RST_SRC)

static int kvm_hook(void *c, int wr, uint32_t b, uint32_t off, uint32_t *val)
{
    (void)c; (void)b;
    if (!wr) {
        switch (off) {
        /* CTRL reads back its RW bits + src_sel==tgt_owner; W1P bits read 0. */
        case CLCDKVM_CTRL:   *val = kvm.ctrl_persist |
                                    (kvm.tgt_owner ? CLCDKVM_CTRL_SRC_SEL : 0u); return 1;
        case CLCDKVM_STATUS: *val = kvm.status; return 1;
        case CLCDKVM_EVENT:  *val = kvm.event;  return 1;   /* read does NOT clear */
        case CLCDKVM_TUNNEL: *val = kvm.tunnel; return 1;
        default:             *val = 0; return 1;            /* no side effects */
        }
    }
    switch (off) {
    case CLCDKVM_CTRL:
        kvm.last_ctrl_writes++;
        if (*val == KVM_INIT_WORD) kvm.init_writes++;
        kvm.ctrl_persist = (*val & KVM_CTRL_RW);
        if (*val & CLCDKVM_CTRL_SRC_SEL_WE) {             /* gated ownership move */
            kvm.tgt_owner = (*val & CLCDKVM_CTRL_SRC_SEL) ? 1u : 0u;
            kvm.we_writes++;
        }
        /* panel_rst_pulse / force_switch are W1P: consumed, nothing latched. */
        return 1;
    case CLCDKVM_EVENT:
        kvm.event &= ~(*val);                             /* W1C */
        return 1;
    default:
        return 1;                                         /* PANEL_TMR/TIMEOUT/... */
    }
}

static void kvm_reset_model(void)
{
    memset(&kvm, 0, sizeof(kvm));
    kvm.ctrl_persist = KVM_CTRL_RESET;   /* the block's power-on CTRL, not 0 */
    memset(&clcdfifo, 0, sizeof(clcdfifo));
    mock_regs_set_hook(MPS3_CLCDKVM_BASE, kvm_hook, 0);
    mock_regs_set_hook(MPS3_CLCD_BASE,    clcd_fifo_hook, 0);
}

/* STATUS helpers: pose an owner + FSM state. owner: 0 HARNESS / 1 DUT. */
static void kvm_pose(unsigned owner, unsigned tgt, int kvm_drives)
{
    uint32_t st = 0;
    if (owner) st |= CLCDKVM_STATUS_OWNER;
    if (tgt)   st |= CLCDKVM_STATUS_TGT_OWNER;
    if (owner != tgt) st |= CLCDKVM_STATUS_SWITCH_PENDING;
    if (kvm_drives)   st |= CLCDKVM_STATUS_KVM_DRIVES_PADS;
    kvm.status = st;
    kvm.tgt_owner = tgt;
}

/* Healthy DFXCTL/CLKRST so reformat()'s status gather does not fault. */
static void seed_healthy(void)
{
    memset(&g_shell_state, 0, sizeof(g_shell_state));
    memset((void *)&g_mps3_diag, 0, sizeof(g_mps3_diag));
    memset(&s_swap_res, 0, sizeof(s_swap_res));
    g_shell_state.static_id = 0xE4B1C44Au;
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_STATUS,    0u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_ID,     0x01000001u);
    mock_regs_poke(MPS3_DFXCTL_BASE, DFXCTL_RM_STATUS, DFXCTL_RM_STATUS_RM_ID_VALID);
    s_icap_bytes = 0; s_link_up = 1; s_anlpar = (1u << 8);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_STATUS,
                   CLKRST_STATUS_MMCM_LOCKED | CLKRST_STATUS_DUT_CLK_ALIVE);
    mock_regs_poke(MPS3_CLKRST_BASE, CLKRST_RESET_CTRL, CLKRST_RESET_CTRL_DUT_RESETN);
}

static void poll_step(uint32_t dt) { mock_time_advance_ms(dt); clcd_poll(); }

/* The palette seam (HM CLCD_ALIGNMENT R4), as mps3-harnessd supplies it. Today's
 * theme unless a test switches it -- every other test here sees the old pixels. */
static const mps3_clcd_theme_t *s_theme = &clcd_theme_today;
const mps3_clcd_theme_t *mps3_clcd_palette(void) { return s_theme; }

static unsigned full_screen_bytes(void);   /* defined below */

/* Drive the driver to steady-state IDLE, past init AND the first FULL paint.
 * NB: right after the init stream the driver is briefly IDLE with dirty==0 and a
 * pending force_refresh -- so we must additionally require that a whole screen
 * of bytes has actually gone out, or we would "settle" on a blank shadow. */
static void bring_to_idle(void)
{
    for (int i = 0; i < 40000; i++) {
        poll_step(5);
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0 &&
            clcd_test_bytes_total() >= full_screen_bytes())
            break;
    }
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcd_test_bytes_total() >= full_screen_bytes());   /* screen painted */
}

/* ==========================================================================
 * 1. EVENT is W1C: read never clears; take_events clears exactly what it read.
 * ========================================================================== */
static void test_event_w1c(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();

    kvm.event = CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE;

    /* A plain READ must NOT clear (the destructive-read trap). */
    uint32_t peek = mps3_reg_read32(MPS3_CLCDKVM_BASE, CLCDKVM_EVENT);
    CHECK(peek == (CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE));
    CHECK(kvm.event == peek);                       /* still set after the read */

    /* take_events() reads then W1C-clears exactly those bits. */
    uint32_t got = clcd_kvm_take_events();
    CHECK(got == peek);
    CHECK(kvm.event == 0u);

    /* A bit the KVM sets AFTER our read must survive take_events (we only wrote
     * 1 to the bits we saw; a 0 leaves a W1C bit untouched). Model it: set a new
     * bit, then take again -- the FIRST take cleared nothing of it. */
    kvm.event = CLCDKVM_EVENT_HARNESS_GAINED;
    got = clcd_kvm_take_events();                   /* clears harness_gained */
    CHECK(got == CLCDKVM_EVENT_HARNESS_GAINED);
    kvm.event |= CLCDKVM_EVENT_FORCED_REVERT;       /* KVM sets a new one */
    got = clcd_kvm_take_events();
    CHECK(got == CLCDKVM_EVENT_FORCED_REVERT);      /* the new one, not lost */
    CHECK(kvm.event == 0u);
}

/* ==========================================================================
 * 2. clcd_kvm_init(): one CTRL write, hands BL/RST to the KVM, never src_sel_we.
 * ========================================================================== */
static void test_kvm_init_one_write(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    kvm.tgt_owner = 0;
    kvm.last_ctrl_writes = 0;

    clcd_kvm_init();

    CHECK(kvm.last_ctrl_writes == 1);               /* atomic -- no dark gap */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_BL_RST_SRC);   /* KVM owns BL/RST */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_BACKLIGHT);    /* lit */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_PANEL_RST_N);  /* released */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_TIMEOUT_EN);   /* cannot get stuck */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN);        /* button live */
    CHECK(!(kvm.ctrl_persist & CLCDKVM_CTRL_DUT_REQ_EN));/* DUT opted OUT at boot */
    CHECK(kvm.tgt_owner == 0);                           /* ownership NOT touched */
}

/* ==========================================================================
 * 3. RMW of CTRL never moves ownership -- a concurrent button press survives.
 * ========================================================================== */
static void test_ctrl_rmw_never_clobbers_owner(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_kvm_init();

    /* The hardware button moves tgt_owner to DUT, behind firmware's back. */
    kvm.tgt_owner = 1;

    /* Firmware now does an unrelated RMW (turn the backlight off, opt the DUT
     * req in). If it wrote src_sel back without src_sel_we the model ignores it;
     * if it wrongly set src_sel_we it would SNAP tgt_owner back to 0. */
    clcd_kvm_set_backlight(0);
    clcd_kvm_set_dut_req_en(1);

    CHECK(kvm.tgt_owner == 1);                       /* the press SURVIVED */
    CHECK(!(kvm.ctrl_persist & CLCDKVM_CTRL_BACKLIGHT));
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_DUT_REQ_EN);
}

/* ==========================================================================
 * 4. request_owner() is the ONLY mover, and it uses src_sel_we.
 * ========================================================================== */
static void test_request_owner_moves_via_we(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_kvm_init();
    CHECK(kvm.tgt_owner == 0);

    clcd_kvm_request_owner(CLCDKVM_OWNER_DUT);
    CHECK(kvm.tgt_owner == 1);                       /* moved */
    clcd_kvm_request_owner(CLCDKVM_OWNER_HARNESS);
    CHECK(kvm.tgt_owner == 0);                       /* and back */
}

/* ==========================================================================
 * 5. LOSE: switch_pending -> OSD banner; kvm_drives_pads -> relinquish + no spin.
 * ========================================================================== */
static void test_lose_paints_banner_then_relinquishes(void)
{
    char grid[CLCD_NCELLS]; uint8_t inv[CLCD_ROWS];

    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);   /* we own it */
    bring_to_idle();
    CHECK(!clcd_test_banner_mode());
    CHECK(!clcd_test_relinquished());

    /* Button pressed: KVM enters S_DRAIN -- switch pending, we STILL drive pads. */
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_DUT, /*kvm_drives=*/0);
    poll_step(5);
    CHECK(clcd_test_banner_mode());                 /* OSD armed */
    CHECK(!clcd_test_relinquished());               /* still pushing it out */

    /* The banner is what is on the glass now. */
    clcd_test_render(grid, inv);
    CHECK(strstr(grid + 6u * CLCD_COLS, "DUT HAS THE DISPLAY") != NULL);
    CHECK(strstr(grid + 8u * CLCD_COLS, "PRESS") != NULL);
    /* ...and no status field bled through (row 2's "DUT :" label is gone). */
    CHECK(strncmp(grid + 2u * CLCD_COLS, "DUT : ", 6) != 0);

    /* KVM now drives the pads (S_RST/S_SETTLE/S_GRANT) and hands to the DUT. */
    kvm_pose(CLCDKVM_OWNER_DUT, CLCDKVM_OWNER_DUT, /*kvm_drives=*/0);
    poll_step(5);
    CHECK(clcd_test_relinquished());

    /* HAMMER: while the DUT owns it, every pass returns having pushed nothing --
     * no spin on a STATUS that is not ours (the cooperative-poll property). */
    clcdfifo.total = 0;
    for (int i = 0; i < 50000; i++) {
        clcd_poll();
        CHECK(clcd_test_bytes_last_pass() == 0);
    }
    CHECK(clcdfifo.total == 0);                      /* not one byte to the panel */
}

/* ==========================================================================
 * 6. REGAIN: harness_gained|panel_reset_done -> re-init + repaint EVERY cell.
 * ========================================================================== */
static unsigned full_screen_bytes(void)
{
    unsigned init = 0;
    for (unsigned i = 0; i < hx8347_init_len; i++)
        if (hx8347_init[i].op != HX_DLY) init++;
    return init + CLCD_NCELLS * (17u + 8u * 16u * 2u);   /* init + 600 cells */
}

static void drive_full_repaint(void)
{
    /* Break only once a WHOLE screen has been pushed AND the driver is idle with
     * nothing left dirty. NB: right after the init stream the driver is briefly
     * IDLE with dirty==0 and a pending force_refresh -- an "IDLE && !dirty" break
     * would stop there, before the repaint even began. */
    for (int i = 0; i < 40000; i++) {
        poll_step(5);
        if (clcd_test_state() == CLCD_ST_IDLE && clcd_test_dirty_count() == 0 &&
            clcdfifo.total >= full_screen_bytes())
            break;
    }
}

static void test_regain_reinits_and_repaints_all(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();

    /* Go through a full lose... */
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_DUT, 0);  poll_step(5);
    kvm_pose(CLCDKVM_OWNER_DUT,     CLCDKVM_OWNER_DUT, 0);  poll_step(5);
    CHECK(clcd_test_relinquished());

    /* ...then the DUT hands it back. The KVM commits owner=HARNESS and sets
     * BOTH harness_gained and panel_reset_done (README §7 commit block). */
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    kvm.event |= CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE;

    clcdfifo.total = 0;
    poll_step(5);
    CHECK(!clcd_test_relinquished());                /* we own it again */
    CHECK(!clcd_test_banner_mode());                 /* status, not OSD */
    CHECK(kvm.event == 0u);                          /* events cleared BEFORE repaint */
    /* Re-streaming the init table (INIT, or INIT_WAIT on the table's leading
     * {HX_DLY,5}). Either way we are re-initing, not idling. */
    CHECK(clcd_test_state() == CLCD_ST_INIT ||
          clcd_test_state() == CLCD_ST_INIT_WAIT);

    /* Drive it to completion: the panel was reset, so EVERY cell must be pushed
     * -- a full-screen byte count, not a handful of changed cells. */
    drive_full_repaint();
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcdfifo.total >= full_screen_bytes());    /* whole screen repainted */
}

/* ==========================================================================
 * 7. THE SUBTLETY: panel_reset_done with NO harness_gained and NO owner change
 *    (a DFX interlock firing mid-settle) STILL forces a full re-init+repaint.
 *    A harness_gained-only rule would leave the harness painting a wiped panel.
 * ========================================================================== */
static void test_reset_without_gain_still_repaints(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();

    /* We never lost ownership -- owner stayed HARNESS throughout. The KVM's
     * shared reset sequencer ran (CTRL.panel_rst_pulse, or an interlock during a
     * settle that reverted tgt to HARNESS) and fired ONLY panel_reset_done. */
    kvm.event = CLCDKVM_EVENT_PANEL_RESET_DONE;      /* NO harness_gained */
    CHECK(!(kvm.event & CLCDKVM_EVENT_HARNESS_GAINED));

    clcdfifo.total = 0;
    poll_step(5);
    CHECK(clcd_test_state() == CLCD_ST_INIT ||       /* re-init anyway */
          clcd_test_state() == CLCD_ST_INIT_WAIT);
    CHECK(kvm.event == 0u);

    drive_full_repaint();
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcdfifo.total >= full_screen_bytes());    /* full repaint, not a diff */
}

/* ==========================================================================
 * 8. No KVM events, we own it: steady state is a normal diff, NOT a full repaint
 *    (proves the re-init path fires ONLY on the events, not every poll).
 * ========================================================================== */
static void test_steady_state_is_a_diff(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();

    /* No events. Let several refresh windows pass. Only the uptime/heartbeat
     * cells change, so the byte count per window is a tiny fraction of a full
     * screen -- if the re-init path fired spuriously it would blow past this. */
    kvm.event = 0;
    clcdfifo.total = 0;
    for (int i = 0; i < 400; i++) poll_step(5);      /* ~8 refresh windows */
    /* Only the uptime/heartbeat cells change per window, so the byte count is a
     * tiny fraction of a full screen. A spurious re-init (600 cells) would blow
     * past this bound many times over -- that is the property under test. */
    CHECK(clcdfifo.total < full_screen_bytes() / 4u);
}

/* ==========================================================================
 * 9. USER_nPB[1] FIRMWARE INTERPRETER (Phase 1 page nav). Firmware clears pb_en
 *    at init so it owns the button; pb_service() times PB_LEVEL:
 *      harness owns:  short press -> next page;  long hold -> hand to DUT.
 *      DUT owns:      any press   -> request the panel back.
 * ========================================================================== */
static void pb_press(void)   { kvm.status |=  CLCDKVM_STATUS_PB_LEVEL; }
static void pb_release(void) { kvm.status &= ~CLCDKVM_STATUS_PB_LEVEL; }

/* pb_en is cleared once, at the top of the first kvm_service(), right after
 * clcd_kvm_init(). */
static void test_pb_en_cleared_at_init(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    for (int i = 0; i < 4; i++) poll_step(5);        /* first passes */
    CHECK(!(kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN));  /* firmware owns the button */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_BL_RST_SRC);/* init still ran (BL/RST) */
    CHECK(kvm.init_writes == 1u);                     /* exactly once            */
}

static void test_pb_short_press_cycles_page(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);

    /* Short press: the page advances on RELEASE, not on the press edge. */
    pb_press();   poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);

    /* Ownership never moved -- a short press must not hand the panel over. */
    CHECK(kvm.tgt_owner == 0);

    /* Cycle again: wraps back to STATUS. */
    pb_press();   poll_step(50);
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
}

static void test_pb_long_press_hands_to_dut(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();
    CHECK(kvm.tgt_owner == 0);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);

    /* Hold past the long threshold: request DUT ownership, and DO NOT cycle. */
    pb_press();
    poll_step(50);            /* press edge -- timer armed */
    poll_step(900);           /* held > CLCD_PB_LONG_MS -> long fires */
    CHECK(kvm.tgt_owner == 1);                 /* panel requested for the DUT */
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);/* NOT a page cycle */

    /* Release after a long press: still no cycle (the press was consumed). */
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
}

static void test_pb_press_returns_from_dut(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();

    /* DUT owns the panel; the harness has relinquished. */
    kvm_pose(CLCDKVM_OWNER_DUT, CLCDKVM_OWNER_DUT, 0);
    poll_step(5);
    CHECK(clcd_test_relinquished());
    CHECK(kvm.tgt_owner == 1);

    /* A press asks for the panel back -- the hardware no longer does this now
     * that pb_en is clear, so pb_service() must (owner=DUT still in STATUS). */
    pb_press();
    poll_step(5);
    CHECK(kvm.tgt_owner == 0);                  /* requested HARNESS back */
}

/* ==========================================================================
 * 10. PB1 HELD THROUGH POWER-UP (D13). That hold is the "skip the card's default
 *     load" escape hatch, read as a LEVEL by the D13 boot hook -- not a UI
 *     gesture. pb_service() must ignore a press that is already down when it
 *     first runs and act only on edges after the first release. Before the gate,
 *     the first pass saw pb=1 with s_pb_was_down=0 -> "press edge" -> 800 ms
 *     later the long-hold fired and the panel went to the DUT.
 * ========================================================================== */
static void test_pb_held_at_powerup_is_ignored(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    pb_press();                                  /* down BEFORE the first poll */
    clcd_init();

    /* Boot, init, first full paint, then hold for another 5 s -- 6x the long-
     * press threshold. Nothing may move: no ownership request, no page cycle,
     * no OSD, no relinquish. */
    bring_to_idle();
    for (int i = 0; i < 1000; i++) poll_step(5);
    CHECK(kvm.we_writes == 0u);                  /* never asked for the DUT   */
    CHECK(kvm.tgt_owner == 0u);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
    CHECK(!clcd_test_banner_mode());
    CHECK(!clcd_test_relinquished());
    CHECK(!(kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN)); /* firmware still took the button */

    /* The first RELEASE ends the power-up hold. It is not the end of a short
     * press, so it must not cycle the page either. */
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_STATUS);
    CHECK(kvm.we_writes == 0u);

    /* From here every edge counts, exactly as for a button that was up at boot. */
    pb_press();   poll_step(50);
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);    /* short press -> next page  */
    CHECK(kvm.tgt_owner == 0u);

    pb_press();   poll_step(50);                 /* long hold -> hand to DUT  */
    poll_step(900);
    CHECK(kvm.tgt_owner == 1u);
    CHECK(kvm.we_writes == 1u);
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);    /* consumed, no extra cycle  */
}

/* Held at power-up with the DUT already owning the panel: the gate covers the
 * DUT-owns branch too (a held press must not ask for the panel back), and the
 * first press after the release does. */
static void test_pb_held_at_powerup_dut_owner(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    kvm_pose(CLCDKVM_OWNER_DUT, CLCDKVM_OWNER_DUT, 0);
    pb_press();
    clcd_init();

    for (int i = 0; i < 400; i++) poll_step(5);  /* 2 s held */
    CHECK(clcd_test_relinquished());
    CHECK(kvm.we_writes == 0u);                  /* no "give it back" request */
    CHECK(kvm.tgt_owner == 1u);

    pb_release(); poll_step(50);
    CHECK(kvm.we_writes == 0u);                  /* the release is not a press */
    pb_press();   poll_step(5);
    CHECK(kvm.tgt_owner == 0u);                  /* first real press: back    */
    CHECK(kvm.we_writes == 1u);
}

/* A short blip at power-up that is released before the service's first pass is
 * no press at all -- and the button is armed from the first pass that sees it
 * up, so the very next press works (no extra release needed). */
static void test_pb_up_at_start_is_armed_at_once(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    clcd_init();
    poll_step(5);                                /* first pass: PB up -> armed */
    pb_press();   poll_step(50);
    pb_release(); poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);
    CHECK(kvm.we_writes == 0u);
}

/* ==========================================================================
 * 11. ONE-TIME KVM SETUP WHEN THE DUT OWNS THE PANEL AT THE FIRST POLL.
 *     The setup (clcd_kvm_init() + the pb_en clear) used to live in ST_RESET. If
 *     the DUT already owned the panel on the first clcd_poll() -- a hardware PB1
 *     toggle before the firmware ran, or a harness restart while the DUT held it
 *     -- the driver relinquished at once, never reached ST_RESET, and on regain
 *     clcd_regain() jumped straight to ST_INIT. The setup NEVER ran: pb_en stayed
 *     at its reset 1, so the hardware toggled ownership on every PB1 press AND
 *     pb_service() acted on the same press. Now the setup is the first thing
 *     kvm_service() does, on every path, exactly once.
 * ========================================================================== */

/* A PB1 press as the RTL sees it: the debounced level rises and, IF pb_en is set
 * (and nothing masks it -- the RP is healthy here), the hardware toggles the
 * TARGET owner itself (clcd_kvm.sv priority 3). hw_toggles counts that action,
 * so "hardware and firmware both acted" is directly observable. */
static void hw_pb_press(void)
{
    if (!(kvm.status & CLCDKVM_STATUS_PB_LEVEL) &&
        (kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN)) {
        kvm.tgt_owner ^= 1u;
        kvm.hw_toggles++;
    }
    kvm.status |= CLCDKVM_STATUS_PB_LEVEL;
}

static void test_setup_runs_once_when_dut_owns_at_first_poll(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN);      /* the RTL reset value   */

    /* The DUT took the panel before this firmware ran; the KVM latched the
     * handover's events. PB1 is up. */
    kvm_pose(CLCDKVM_OWNER_DUT, CLCDKVM_OWNER_DUT, 0);
    kvm.event = CLCDKVM_EVENT_PANEL_RESET_DONE | CLCDKVM_EVENT_DUT_GAINED |
                CLCDKVM_EVENT_HARNESS_LOST;
    clcd_init();

    /* First pass: THE PATH -- relinquished at once, render FSM never left
     * ST_RESET. The setup ran anyway. */
    poll_step(5);
    CHECK(clcd_test_relinquished());
    CHECK(clcd_test_state() == CLCD_ST_RESET);         /* ST_RESET never ran    */
    CHECK(kvm.init_writes == 1u);                      /* clcd_kvm_init() ran   */
    CHECK(kvm.ctrl_persist & CLCDKVM_CTRL_BL_RST_SRC); /* BL/RST handed over    */
    CHECK(!(kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN));   /* firmware took PB1     */
    CHECK(kvm.tgt_owner == 1u);                        /* setup moved nothing   */
    CHECK(kvm.we_writes == 0u);

    /* ...and ONCE: a thousand more relinquished passes do not repeat it. */
    for (int i = 0; i < 1000; i++) poll_step(5);
    CHECK(kvm.init_writes == 1u);
    CHECK(!(kvm.ctrl_persist & CLCDKVM_CTRL_PB_EN));

    /* A PB1 press while the DUT owns the panel: exactly ONE actor. The hardware
     * must not toggle (pb_en is clear); the firmware asks for the panel back. */
    hw_pb_press();
    poll_step(5);
    CHECK(kvm.hw_toggles == 0u);                       /* hardware did nothing  */
    CHECK(kvm.we_writes == 1u);                        /* firmware acted, once  */
    CHECK(kvm.tgt_owner == 0u);                        /* -> harness            */
    pb_release(); poll_step(50);

    /* The KVM hands the panel back: regain -> ST_INIT (not ST_RESET), full
     * repaint. The setup must not run a second time. */
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    kvm.event |= CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE;
    clcdfifo.total = 0;
    poll_step(5);
    CHECK(!clcd_test_relinquished());
    CHECK(clcd_test_state() == CLCD_ST_INIT || clcd_test_state() == CLCD_ST_INIT_WAIT);
    drive_full_repaint();
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcdfifo.total >= full_screen_bytes());
    CHECK(kvm.init_writes == 1u);

    /* Harness owns it now. A short press cycles the page -- the firmware's
     * action only; the hardware still does not touch ownership. */
    hw_pb_press();  poll_step(50);
    pb_release();   poll_step(50);
    CHECK(clcd_page_get() == CLCD_PAGE_APPS);
    CHECK(kvm.hw_toggles == 0u);
    CHECK(kvm.tgt_owner == 0u);
    CHECK(kvm.we_writes == 1u);                        /* no ownership request  */

    /* A long hold hands it to the DUT -- again the firmware alone. */
    hw_pb_press();  poll_step(50);
    poll_step(900);
    CHECK(kvm.tgt_owner == 1u);
    CHECK(kvm.we_writes == 2u);
    CHECK(kvm.hw_toggles == 0u);
    pb_release();   poll_step(50);
    CHECK(kvm.init_writes == 1u);
}

/* ==========================================================================
 * THE KVM OWNER RULE under the aligned theme (HM R4): a theme switch, a page
 * change and a forced refresh while the DUT owns the panel push NOTHING -- the
 * new colours wait for the regain, whose full repaint then draws them.
 * ========================================================================== */
static void test_aligned_theme_waits_for_the_panel(void)
{
    mock_regs_reset(); kvm_reset_model(); seed_healthy();
    s_theme = &clcd_theme_today;
    clcd_init();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    bring_to_idle();
    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_DUT, 0);  poll_step(5);
    kvm_pose(CLCDKVM_OWNER_DUT,     CLCDKVM_OWNER_DUT, 0);  poll_step(5);
    CHECK(clcd_test_relinquished());

    clcdfifo.total = 0;
    s_theme = &clcd_theme_aligned;
    clcd_page_set(CLCD_PAGE_APPS);
    clcd_test_force_reformat();
    for (int i = 0; i < 2000; i++) poll_step(1);
    CHECK(clcd_test_relinquished());
    CHECK(clcdfifo.total == 0u);                     /* never drew under the DUT */

    kvm_pose(CLCDKVM_OWNER_HARNESS, CLCDKVM_OWNER_HARNESS, 0);
    kvm.event |= CLCDKVM_EVENT_HARNESS_GAINED | CLCDKVM_EVENT_PANEL_RESET_DONE;
    poll_step(5);
    CHECK(!clcd_test_relinquished());
    drive_full_repaint();
    CHECK(clcd_test_state() == CLCD_ST_IDLE);
    CHECK(clcdfifo.total >= full_screen_bytes());    /* the regain paints it all */
    clcd_panel_state_t ps;
    clcd_panel_state(&ps);
    CHECK(strcmp(ps.theme, "aligned") == 0 && ps.page == CLCD_PAGE_APPS);
    s_theme = &clcd_theme_today;
    clcd_page_set(CLCD_PAGE_STATUS);
}

int main(void)
{
    test_event_w1c();
    test_kvm_init_one_write();
    test_ctrl_rmw_never_clobbers_owner();
    test_request_owner_moves_via_we();
    test_lose_paints_banner_then_relinquishes();
    test_regain_reinits_and_repaints_all();
    test_reset_without_gain_still_repaints();
    test_steady_state_is_a_diff();
    test_pb_en_cleared_at_init();
    test_pb_short_press_cycles_page();
    test_pb_long_press_hands_to_dut();
    test_pb_press_returns_from_dut();
    test_pb_held_at_powerup_is_ignored();
    test_pb_held_at_powerup_dut_owner();
    test_pb_up_at_start_is_armed_at_once();
    test_setup_runs_once_when_dut_owns_at_first_poll();
    test_aligned_theme_waits_for_the_panel();
    printf("test_clcd_kvm: %d checks passed\n", s_checks);
    return 0;
}
