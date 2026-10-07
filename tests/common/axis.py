"""axis.py — generic byte-wide AXI-Stream driver/monitor.

Binds by explicit signal handles (no hardcoded port names), so the same
pair serves every AXIS interface these benches touch:
  - partition-pins.md console tap: `uart_tx_t*` / `uart_rx_t*` (no tlast/tuser)
  - gen_checker.sv: `gen_m_t*` (tlast, no tuser) / `chk_s_t*` (tlast + tuser
    frame-error flag)
  - eth_bridge_3port.sv: `mgmt_*`/`dut_mac_*`/`uplink_*` (tlast; dut_mac_s
    also carries tuser)
  - link_partner_mac.sv: `m_axis_rx_*` (tlast + tuser) / `s_axis_tx_*` (tlast)

Self-contained (no cocotbext-axi dependency) — cocotbext-axi's
AxiStreamSource/AxiStreamSink are a reference/optional accelerant if
installed in the sim environment.
"""
from __future__ import annotations

from cocotb.triggers import RisingEdge, ReadOnly

# Beat-timing note (2026-07-06, W-RTL-ETH — supersedes the W-SIM port note
# that used to live here): the original 2.x port sampled tready/tvalid in
# the ReadOnly phase of the SAME timestep as the awaited edge, i.e. it read
# POST-edge values. That mis-times both classes against real synchronous
# RTL, exactly as tests/uart_bridge/dut_notes.md flagged ("a beat must be
# sampled in the ReadOnly window BEFORE the committing edge — post-edge
# sampling sees the FWFT FIFO's post-pop state and drops the first byte"):
#   * Monitor: with an always-ready sink, an egress that advances its data
#     register every cycle is sampled one beat late — the first byte of
#     every packet is dropped and the state seen is the post-advance one.
#   * Driver: on a tready 0->1 turnaround (e.g. eth_bridge_3port re-opening
#     its single-frame buffer after a forward), post-edge sampling observes
#     the NEW ready, concludes the still-pending beat was accepted, and
#     either loses that byte or re-commits the held final byte as a phantom
#     1-byte packet once ready returns.
# Fix (this file, both classes): AXI-Stream semantics sample a beat as
# committed AT a rising edge iff tvalid && tready held immediately BEFORE
# that edge. Each loop iteration therefore samples in ReadOnly of the
# *current* timestep (values that will be latched by the next edge), THEN
# awaits the edge, and only then acts on the sampled values / writes the
# next beat (post-edge callback = writable phase, legal under cocotb 2.0.1).
# Note the values sampled at that ReadOnly are stable up to the edge for
# the RTL these benches drive (ready/valid are registered outputs; no other
# testbench task wiggles them mid-cycle). Proven under VCS 2022.06-SP2 +
# cocotb 2.0.1 by tests/{bridge,gen_checker} (this wave) and regression of
# tests/uart_bridge (AxisByteDriver consumer, still green).


class AxisByteDriver:
    """`tlast`/`tuser` are optional (pass `None` if the interface doesn't
    have one, e.g. the UART tap)."""

    def __init__(self, clk, tdata, tvalid, tready, tlast=None, tuser=None):
        self.clk = clk
        self.tdata = tdata
        self.tvalid = tvalid
        self.tready = tready
        self.tlast = tlast
        self.tuser = tuser
        self.tvalid.value = 0

    async def write(self, data: bytes, user: int = 0):
        """Drives `data` as one packet: `tlast` (if present) pulses on the
        final byte; `tuser` (if present) is held at `user` for the whole
        packet (matches e.g. link_partner_mac's per-frame error flag).
        Each beat is held until the edge at which tvalid&&tready committed
        it (pre-edge sampling — see the module timing note)."""
        # Phase alignment: land the first beat's writes strictly AFTER a
        # clock edge (post-edge callback = writable, mid-cycle). Without
        # this, a caller whose Timer awaits happen to land ON an edge
        # timestep (e.g. tests/bridge's 50+20 ns bring-up against a 10 ns
        # clock) races that edge: the DUT can sample the first beat at the
        # very edge the loop below doesn't count, double-committing byte 0
        # (seen under VCS). Subsequent beats are always written in post-edge
        # callbacks, so only the first needs aligning.
        await RisingEdge(self.clk)
        for i, b in enumerate(data):
            self.tdata.value = b
            self.tvalid.value = 1
            if self.tlast is not None:
                self.tlast.value = 1 if i == len(data) - 1 else 0
            if self.tuser is not None:
                self.tuser.value = user
            while True:
                await ReadOnly()
                accepted = bool(int(self.tready.value))
                await RisingEdge(self.clk)
                if accepted:
                    break  # beat committed at this edge
            # post-edge callback = writable phase: next beat (or the final
            # deassert below) is driven here, in time for the next edge.
        self.tvalid.value = 0
        if self.tlast is not None:
            self.tlast.value = 0


class AxisByteMonitor:
    def __init__(self, clk, tdata, tvalid, tready, tlast=None, tuser=None):
        self.clk = clk
        self.tdata = tdata
        self.tvalid = tvalid
        self.tready = tready
        self.tlast = tlast
        self.tuser = tuser
        self.tready.value = 1

    async def _beat(self):
        """Samples one clock: returns (data, last, user) if a beat committed
        at the awaited edge, else None. Pre-edge sampling — see module
        timing note."""
        await ReadOnly()
        committed = bool(int(self.tvalid.value)) and bool(int(self.tready.value))
        data = last = user = None
        if committed:
            data = int(self.tdata.value)
            last = int(self.tlast.value) if self.tlast is not None else 0
            user = int(self.tuser.value) if self.tuser is not None else None
        await RisingEdge(self.clk)
        if committed:
            return data, last, user
        return None

    async def read_bytes(self, count: int) -> bytes:
        """Reads exactly `count` bytes, ignoring tlast/tuser (use
        `read_packet()` when the interface has tlast and packet boundaries
        matter)."""
        out = bytearray()
        while len(out) < count:
            beat = await self._beat()
            if beat is not None:
                out.append(beat[0])
        return bytes(out)

    async def read_packet(self, timeout_cycles: int = 10000):
        """Reads one tlast-delimited packet. Returns (data, user) where
        `user` is the tuser value sampled on the final (tlast) beat, or
        `(data, None)` if this interface has no tuser. Requires tlast to
        have been provided to the constructor."""
        assert self.tlast is not None, "read_packet() requires a tlast signal"
        out = bytearray()
        for _ in range(timeout_cycles):
            beat = await self._beat()
            if beat is not None:
                data, last, user = beat
                out.append(data)
                if last:
                    return bytes(out), user
        raise TimeoutError(f"read_packet(): no tlast within {timeout_cycles} cycles")
