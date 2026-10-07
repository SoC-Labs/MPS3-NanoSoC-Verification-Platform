"""kvm_models.py — bench-side models of the KVM's TWO sources.

`clcd_kvm` arbitrates one physical HX8347-D 8080 bus between two independent
masters (`fpga/shell/ip/clcd_kvm/README.md` §1):

    SOURCE A — the harness `clcd_0`, SYNCHRONOUS to s_axi_aclk, reaching the KVM
               on the h_* ports (h_pd_o/h_cs_n/h_wr_n/h_rd_n/h_rs/h_bl/h_rst_n
               + the two new quiescence taps h_busy / h_fifo_empty).
    SOURCE B — a DUT accelerator, ASYNCHRONOUS (dut_clk, 50 MHz shipped),
               reaching the KVM tunnelled over dut_gpio_o[15:8] /
               dut_gpio_oe[15:8] (docs/contracts/dut-display-tunnel.md §2).

This module models both as *drivers* (the panel side is watched by
`tests/clcd/clcd_panel_model.py`, reused as-is — it watches only the pads, so it
is bus-agnostic).

WHY MODEL THE SOURCES AT ALL, rather than wiggling the ports by hand: the
headline property of this block ("no truncated 8080 cycle across a switch") is
only meaningful against a source that runs a real, multi-phase, strobed cycle
and that keeps running while the KVM takes the pads away from it. A source model
that stops when asked cannot expose a truncation. Both models below therefore
run FREELY: they have no idea they are being switched, exactly like the real
`clcd.sv` FSM and the real accelerator.

Two deliberate modelling decisions, both load-bearing:

  1. **Clock-to-Q.** Both models apply their outputs `CLK_TO_Q_NS` after the
     driving clock edge, never *at* it. `clcd.sv` registers its pads, and the
     KVM's mux to the pads is combinational, so a bench that drove h_* in the
     same delta as the panel monitor's ReadOnly sample would race the monitor.
     The 1 ns skew makes the model behave like the registered RTL it stands in
     for, and removes the race.

  2. **Bit skew (the DUT side only).** `DutTunnelSource(skew_ns=...)` applies a
     *subset* of the changed tunnel bits, then the rest `skew_ns` later. That is
     a faithful model of what the KVM's 2-FF synchronisers actually do to an
     async vector — different bits resolve on different shell cycles — and it is
     the ONLY way a cocotb bench can exercise the 2-cycle stability filter
     (README §12), because a whole-vector `.value =` assignment is atomic and
     therefore cannot tear. With the filter present the pads never see a
     half-updated vector; without it they see one for a cycle, and
     `test_cdc_*` catches it.
"""
from __future__ import annotations

import random
from collections import deque

import cocotb
from cocotb.triggers import RisingEdge, Timer

# --------------------------------------------------------------------------- #
# The tunnel bit map — docs/contracts/dut-display-tunnel.md §2, FROZEN.
# Strobes are carried ACTIVE-HIGH (that doc's §3: it is a safety property, so
# that the decoupler's DECOUPLED_VALUE 0x0 means "all strobes idle"). The KVM
# inverts them to the pads' active-low sense, and nowhere else does.
# --------------------------------------------------------------------------- #
T_CS = 8        # 1 = chip select ASSERTED
T_WR = 9        # 1 = write strobe ASSERTED
T_RS = 10       # 0 = command, 1 = data (not a strobe; no inversion)
T_RD = 11       # RESERVED — drive 0
T_PD_OE = 12    # RESERVED — drive 0
T_BUSY = 13     # 1 = display engine has a byte in flight OR queued
T_REQ = 14      # 1 = the DUT requests the panel (sampled as an EDGE)
T_SPARE = 15    # RESERVED — drive 0
PD_SHIFT = 8    # dut_gpio_o[15:8] = PD[7:0]

# RS encodings, shared with tests/clcd/clcd_panel_model.py.
RS_CMD = 0
RS_DATA = 1

CLK_TO_Q_NS = 1

# Harness FSM phases — the clcd.sv shape (README §6: CS is asserted ONLY in the
# three non-IDLE states, and the FSM returns through IDLE for >=1 cycle, so a
# quiescent point exists per BYTE, not per burst).
H_IDLE = 0
H_SETUP = 1
H_STROBE_LO = 2
H_STROBE_HI = 3
H_PHASE_NAMES = {H_IDLE: "IDLE", H_SETUP: "SETUP",
                 H_STROBE_LO: "STROBE_LO", H_STROBE_HI: "STROBE_HI"}


class HarnessSource:
    """SOURCE A — a faithful stand-in for `clcd.sv`'s 8080 FSM + FIFO.

    Drives the KVM's h_* inputs in the s_axi_aclk domain. Phase widths are in
    whole s_axi_aclk cycles and match clcd.sv's TIMING convention exactly
    (proven in tests/clcd/: WR-low width == wr_lo, CS-setup interval ==
    cs_setup, back-to-back period == wr_lo + wr_hi + cs_setup + 1).

    Quiescence (README §6): `quiet = fifo_empty && !busy`, where busy == the FSM
    is not in ST_IDLE. Both are published to the KVM on the two NEW clcd.sv pins
    (h_busy / h_fifo_empty), which is the whole reason those pins exist.
    """

    def __init__(self, dut, cs_setup=2, wr_lo=4, wr_hi=4):
        self.dut = dut
        self.cs_setup = cs_setup
        self.wr_lo = wr_lo
        self.wr_hi = wr_hi
        self.queue = deque()
        self.emitted = []          # {RS,byte} pairs whose WR RISING edge completed
        self.phase = H_IDLE
        self.hung = False          # never go quiescent (the hung-owner test)
        self._stop = False
        self._cnt = 0
        self._cur = None
        # Steady-state BL/RST as the shipped firmware leaves them: panel lit and
        # out of reset (clcd.sv CTRL[1]=1, CTRL[2]=1). CLCDKVM.CTRL.bl_rst_src=0
        # at reset means these pass straight through — the drop-in property
        # (README §10) that keeps the shipped ELF lighting the panel.
        self.bl = 1
        self.rst_n = 1

    # ---- results ---------------------------------------------------------- #
    @property
    def period(self):
        """Back-to-back byte period, in s_axi_aclk cycles."""
        return self.cs_setup + self.wr_lo + self.wr_hi + 1

    @property
    def busy(self):
        return 1 if (self.hung or self.phase != H_IDLE) else 0

    @property
    def fifo_empty(self):
        return 0 if (self.hung or self.queue) else 1

    @property
    def quiet(self):
        return self.fifo_empty and not self.busy

    def enqueue(self, pairs):
        self.queue.extend(pairs)

    def clear_emitted(self):
        self.emitted = []

    def stop(self):
        self._stop = True

    # ---- driver ----------------------------------------------------------- #
    def _apply(self, cs_n, wr_n, rs, pd):
        d = self.dut
        d.h_cs_n.value = cs_n
        d.h_wr_n.value = wr_n
        d.h_rd_n.value = 1              # READ_PATH=0 ships: RD never asserts
        d.h_rs.value = rs
        d.h_pd_o.value = pd
        d.h_pd_oe.value = 1             # const 1 at READ_PATH=0 (clcd.sv:505)
        d.h_busy.value = self.busy
        d.h_fifo_empty.value = self.fifo_empty
        d.h_bl.value = self.bl
        d.h_rst_n.value = self.rst_n

    def idle_now(self):
        """Park every h_* input at its idle value (call before the clock runs)."""
        self.phase = H_IDLE
        self._apply(cs_n=1, wr_n=1, rs=0, pd=0)

    async def run(self):
        d = self.dut
        rs, pd = 0, 0
        while not self._stop:
            await RisingEdge(d.s_axi_aclk)

            # ---- next-state (evaluated on the edge, applied after clk-to-Q) --
            if self.hung:
                # A wedged clcd_0: pads parked idle, but busy/fifo_empty never
                # clear, so the KVM's drain gate can never be satisfied. Parking
                # the pads idle is deliberate — it isolates the TIMEOUT property
                # from the truncation property (a hung owner that also held CS
                # would confound the two).
                self.phase = H_IDLE
                self._apply(cs_n=1, wr_n=1, rs=rs, pd=pd)
                continue

            if self.phase == H_IDLE:
                if self.queue:
                    self._cur = self.queue.popleft()
                    rs, pd = self._cur
                    self.phase = H_SETUP
                    self._cnt = self.cs_setup
            elif self.phase == H_SETUP:
                self._cnt -= 1
                if self._cnt == 0:
                    self.phase = H_STROBE_LO
                    self._cnt = self.wr_lo
            elif self.phase == H_STROBE_LO:
                self._cnt -= 1
                if self._cnt == 0:
                    # WR rising edge -> the panel latches {RS, byte} HERE. This
                    # is the instant the source considers the byte "sent"; if it
                    # did not reach the pads whole, the KVM truncated it.
                    self.emitted.append((rs, pd))
                    self.phase = H_STROBE_HI
                    self._cnt = self.wr_hi
            elif self.phase == H_STROBE_HI:
                self._cnt -= 1
                if self._cnt == 0:
                    self.phase = H_IDLE     # >=1 IDLE cycle: CS deasserts

            cs_n = 0 if self.phase in (H_SETUP, H_STROBE_LO, H_STROBE_HI) else 1
            wr_n = 0 if self.phase == H_STROBE_LO else 1

            await Timer(CLK_TO_Q_NS, units="ns")
            self._apply(cs_n=cs_n, wr_n=wr_n, rs=rs, pd=pd)

    # ---- bench helpers ---------------------------------------------------- #
    async def wait_phase(self, phase, timeout_cycles=2000):
        for _ in range(timeout_cycles):
            await RisingEdge(self.dut.s_axi_aclk)
            if self.phase == phase:
                return
        raise AssertionError(
            f"harness source never reached phase {H_PHASE_NAMES[phase]}")

    async def wait_idle(self, timeout_cycles=20000):
        for _ in range(timeout_cycles):
            await RisingEdge(self.dut.s_axi_aclk)
            if self.quiet:
                return
        raise AssertionError("harness source never drained")


# --------------------------------------------------------------------------- #
# SOURCE B — the DUT, over the tunnel. ASYNC.
# --------------------------------------------------------------------------- #
class DutTunnelSource:
    """A student accelerator's display engine, seen through the tunnel.

    Runs on its OWN clock (`period_ns`, 20 ns = 50 MHz shipped), asynchronous to
    s_axi_aclk by construction: it is paced with Timer, from a start phase that
    is deliberately not a multiple of the shell period, so its edges land in the
    middle of shell cycles.

    Phase widths are in dut_clk cycles and default to the reference
    accelerator's CS_SETUP = WR_LO = WR_HI = 8 (dut-display-tunnel.md §5), which
    honours the NORMATIVE timing floor: every phase >= 8 dut_clk cycles, PD/RS
    stable >= 4 dut_clk cycles either side of `wr`. At 50 MHz that is 160 ns per
    phase against the KVM's <=20 ns filter latency.

    `skew_ns` (see the module docstring) staggers the changed bits of the tunnel
    vector to model per-bit synchroniser resolution skew. It must be < the
    guard band, or the DUT itself is violating the floor.
    """

    def __init__(self, dut, period_ns=20.0, cs_setup=8, wr_lo=8, wr_hi=8,
                 start_ns=3.0, skew_ns=0.0, seed=1):
        self.dut = dut
        self.period_ns = period_ns
        self.cs_setup = cs_setup
        self.wr_lo = wr_lo
        self.wr_hi = wr_hi
        self.start_ns = start_ns
        self.skew_ns = skew_ns
        self.rng = random.Random(seed)
        self.queue = deque()
        self.emitted = []
        self.phase = H_IDLE
        self.hung = False
        self.req = 0
        # dut_gpio_*[7:0] are the LED path and are NOT ours (tunnel doc §2:
        # "dut_gpio_o[7:0] and dut_gpio_oe[7:0] are UNTOUCHED"). The models
        # drive a recognisable pattern there so a KVM that accidentally decoded
        # the LOW half would show up as garbage bytes at the panel.
        self.led_o = 0xA5
        self.led_oe = 0x5A
        self._stop = False
        self._cur = None
        self._o = 0
        self._oe = 0

    @property
    def period(self):
        """Back-to-back byte period, in dut_clk cycles."""
        return self.cs_setup + self.wr_lo + self.wr_hi + 1

    @property
    def busy(self):
        """The tunnel's `busy` bit — NORMATIVELY 'any byte in flight OR queued'
        (dut-display-tunnel.md §2), i.e. the exact analogue of the harness's
        !(fifo_empty && !busy)."""
        return 1 if (self.hung or self.phase != H_IDLE or self.queue) else 0

    @property
    def cs(self):
        return 1 if self.phase in (H_SETUP, H_STROBE_LO, H_STROBE_HI) else 0

    @property
    def quiet(self):
        return (not self.cs) and (not self.busy)

    def enqueue(self, pairs):
        self.queue.extend(pairs)

    def clear_emitted(self):
        self.emitted = []

    def stop(self):
        self._stop = True

    # ---- driver ----------------------------------------------------------- #
    def _vec(self, cs, wr, rs, pd):
        o = ((pd & 0xFF) << PD_SHIFT) | self.led_o
        oe = self.led_oe
        oe |= (cs & 1) << T_CS
        oe |= (wr & 1) << T_WR
        oe |= (rs & 1) << T_RS
        # T_RD / T_PD_OE / T_SPARE: RESERVED — every RM drives them 0.
        oe |= (self.busy & 1) << T_BUSY
        oe |= (self.req & 1) << T_REQ
        return o, oe

    def _write(self, o, oe):
        self.dut.dut_gpio_o_i.value = o
        self.dut.dut_gpio_oe_i.value = oe

    async def _drive(self, o, oe):
        """Apply the tunnel vector, optionally with per-bit skew."""
        if self.skew_ns <= 0:
            self._o, self._oe = o, oe
            self._write(o, oe)
            return
        changed_o = (o ^ self._o) & 0xFFFF
        changed_oe = (oe ^ self._oe) & 0xFFFF
        if not (changed_o or changed_oe):
            return
        # Split the changed bits into an "early" and a "late" group: the early
        # ones resolve on shell cycle N, the late ones on N+1. That is exactly
        # the tear the 2-cycle stability filter exists to hide.
        mask_o = self.rng.getrandbits(16) & changed_o
        mask_oe = self.rng.getrandbits(16) & changed_oe
        mid_o = (self._o & ~mask_o) | (o & mask_o)
        mid_oe = (self._oe & ~mask_oe) | (oe & mask_oe)
        self._write(mid_o, mid_oe)
        await Timer(self.skew_ns, units="ns")
        self._o, self._oe = o, oe
        self._write(o, oe)

    def idle_now(self):
        """Park the tunnel at the value a decoupled / display-less RM presents:
        ALL ZERO. tunnel doc §3/§4 — 0x0 decodes to 'all strobes idle, nothing
        requested, nothing in flight', by construction."""
        self.phase = H_IDLE
        self._o = self._oe = 0
        self._write(0, 0)

    def clamp(self):
        """What the dfx_decoupler physically does: DECOUPLED_VALUE 0x0 on both
        vectors (shell_bd.tcl:626, IDs 13/14)."""
        self._o = self._oe = 0
        self._write(0, 0)

    async def run(self):
        await Timer(self.start_ns, units="ns")    # async phase w.r.t. s_axi_aclk
        rs, pd = 0, 0
        cnt = 0
        while not self._stop:
            if self.hung:
                # A hung accelerator: `busy` stuck high forever, pads idle. The
                # KVM must preempt it on TIMEOUT rather than wait for ever.
                o, oe = self._vec(cs=0, wr=0, rs=rs, pd=pd)
                await self._drive(o, oe)
                await Timer(self.period_ns, units="ns")
                continue

            if self.phase == H_IDLE:
                if self.queue:
                    self._cur = self.queue.popleft()
                    rs, pd = self._cur
                    self.phase = H_SETUP
                    cnt = self.cs_setup
            elif self.phase == H_SETUP:
                cnt -= 1
                if cnt == 0:
                    self.phase = H_STROBE_LO
                    cnt = self.wr_lo
            elif self.phase == H_STROBE_LO:
                cnt -= 1
                if cnt == 0:
                    self.emitted.append((rs, pd))
                    self.phase = H_STROBE_HI
                    cnt = self.wr_hi
            elif self.phase == H_STROBE_HI:
                cnt -= 1
                if cnt == 0:
                    self.phase = H_IDLE

            o, oe = self._vec(cs=self.cs,
                              wr=1 if self.phase == H_STROBE_LO else 0,
                              rs=rs, pd=pd)
            await self._drive(o, oe)
            await Timer(self.period_ns, units="ns")

    # ---- bench helpers ---------------------------------------------------- #
    async def wait_phase(self, phase, timeout_ns=200_000):
        t = 0.0
        while t < timeout_ns:
            await Timer(self.period_ns / 4, units="ns")
            t += self.period_ns / 4
            if self.phase == phase:
                return
        raise AssertionError(
            f"DUT source never reached phase {H_PHASE_NAMES[phase]}")

    async def wait_idle(self, timeout_ns=400_000):
        t = 0.0
        while t < timeout_ns:
            await Timer(self.period_ns / 2, units="ns")
            t += self.period_ns / 2
            if self.quiet:
                return
        raise AssertionError("DUT source never drained")

    async def pulse_req(self, high=True, hold_ns=200.0):
        """Drive the tunnel's `req` bit. Sampled as an EDGE by the KVM: rising =
        'I want the panel', falling = 'you can have it back'."""
        self.req = 1 if high else 0
        await Timer(hold_ns, units="ns")
