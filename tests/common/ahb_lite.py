"""ahb_lite.py — a self-contained AHB-Lite master BFM + protocol monitor.

This is the AHB-Lite counterpart to `regmap.py`'s `AxiLiteMaster`: same house
conventions (an explicit signal-handle constructor, a `from_dut()` binder, and
`async def read(addr)` / `async def write(addr, data)` on top), no
`cocotbext-*` dependency, cocotb-2.x-safe phase handling.

It exists because every *shell* block on this platform is AXI4-Lite, but every
*DUT-side* block is AHB-Lite (nanosoc is a Cortex-M0 SoC). The first consumer
is the `nanosoc_exp` socket (`fpga/rp/nanosoc_exp/README.md`, the "hole" at
`0x6000_0000`); every future DUT-side peripheral needs the same thing.


Why AHB-Lite needs a real BFM (and not a handshake loop)
--------------------------------------------------------
AXI4-Lite is a set of independent VALID/READY handshakes — you can bang one out
inline. AHB-Lite is a **two-stage pipeline** with a *shared* ready signal, and
almost every naive BFM gets it wrong in one of four ways. This one is written
to get all four right, because a BFM that quietly skips them makes every bench
built on it lie:

1. **Address phase / data phase are different cycles.** In cycle *N* the master
   drives `HSEL/HADDR/HTRANS/HWRITE/HSIZE/HBURST/HPROT`. In cycle *N+1* it
   drives `HWDATA` (write) or samples `HRDATA` (read). A BFM that drives
   HADDR and HWDATA in the same cycle will pass against a sloppy slave and fail
   against a correct one.

2. **The pipeline really overlaps.** Back-to-back transfers put transfer *n+1*'s
   address phase in the *same cycle* as transfer *n*'s data phase. A BFM that
   does address-then-data-then-idle never produces that overlap, so it can never
   catch the bug it exists to catch (a slave that latches HADDR in the data
   phase, or that lets a new address clobber the in-flight one). This BFM runs a
   continuous background driver over a transaction queue, so `burst_write()` /
   `pipeline()` emit genuinely gapless transfers.

3. **A wait state freezes *both* phases.** The slave holds `HREADYOUT` low; the
   master must hold the address phase (HADDR/HTRANS/... unchanged) *and* hold
   HWDATA, and the *next* address phase must not advance.

4. **ERROR is a two-cycle response.** The slave drives `HRESP=1, HREADYOUT=0`
   and then `HRESP=1, HREADYOUT=1`. Because the bus is pipelined, by the time
   the error appears the master has *already* broadcast the next transfer's
   address — so the spec requires the master to drive `HTRANS=IDLE` during the
   second cycle to cancel it. This BFM does exactly that, and then re-presents
   the cancelled transfer, so the caller's `await` still means what it says.
   (See `_driver()`.)


Sampling model (identical to `regmap.py`'s, and the reason it is safe)
----------------------------------------------------------------------
Everything in this file follows one rule, the same one `AxiLiteMaster` follows::

    await RisingEdge(clk)   # -> we are in a WRITABLE phase.  Signals written
                            #    here settle during the cycle that just began,
                            #    and are sampled by the DUT at the NEXT edge.
    ...drive...
    await ReadOnly()        # -> settled values FOR THE CYCLE THAT JUST BEGAN.
    ...sample...
    await RisingEdge(clk)   # -> the DUT consumes what we drove; loop.

Writing a signal while parked in `ReadOnly` raises "Attempting setting a value
during the ReadOnly phase" under cocotb 2.0.1 *and* 1.7.2 (see the long note at
the top of `regmap.py`). Every drive below happens in a writable phase.

Because HREADYOUT may legally be combinational off HSEL/HTRANS (it must *not*
be combinational off HREADY — AMBA IHI0033), sampling it in `ReadOnly` after
our own drives have settled is the correct thing to do: we see the ready the
slave is presenting *for the address phase we are currently driving*.


Wiring: what to connect `hready` to
-----------------------------------
AHB-Lite's `HREADY` (an *input* to a slave) is the *global* bus ready — in a
single-slave system it is, by definition, that slave's own `HREADYOUT`. The
BFM **samples** HREADY; it never invents it. Two supported wirings:

* **Preferred — tie it in a harness.** Give the bench a small top module that
  instantiates the DUT and does `assign hready = hreadyout;`, exposing the AHB
  signals as top-level ports. This is what `tests/qspi_xip/qspi_xip_harness.sv`
  does, and it makes it *impossible* for the bench to lie to the slave about
  readiness. Bind with `AhbLiteMaster.from_dut(dut)`.

* **Convenience — let the BFM mirror it.** If your `TOPLEVEL` is the raw slave
  (so its `hready` input is floating), pass `hready_drive=dut.hready` and the
  BFM starts a combinational mirror task (`hready <= hreadyout`, delta-delayed,
  no cycle delay). Legal precisely because HREADYOUT cannot depend on HREADY.
  Both wirings are proven by the self-test.


Usage
-----
::

    from ahb_lite import AhbLiteMaster, AhbLiteMonitor, AhbError, HSIZE_BYTE

    cocotb.start_soon(Clock(dut.hclk, 20, unit="ns").start())
    ahb = AhbLiteMaster.from_dut(dut)          # binds hclk/hresetn/h* by name
    mon = AhbLiteMonitor.from_dut(dut)         # optional; asserts bus legality
    mon.start()

    await ahb.reset()                          # drives hresetn low, bus IDLE

    await ahb.write(0x6000_0004, 0xDEAD_BEEF)  # single word write
    data, resp = await ahb.read(0x6000_0004)   # -> (0xDEADBEEF, 0)

    # gapless pipelined transfers (address phase of n+1 overlaps data phase of n)
    await ahb.burst_write(0x6000_0000, [0x11, 0x22, 0x33, 0x44])
    words = await ahb.burst_read(0x6000_0000, 4)

    await ahb.idle(3)                          # 3 explicit IDLE cycles
    await ahb.write(0x6000_0000, 0xAA, size=HSIZE_BYTE)   # byte-lane placed

    try:                                       # ERROR surfaces as an exception
        await ahb.read(0x6000_1000)
    except AhbError as e:
        ...
    # ...or as a status, if you would rather assert on it:
    ahb.raise_on_error = False
    data, resp = await ahb.read(0x6000_1000)   # resp == HRESP_ERROR

Ports it binds by default (the frozen `nanosoc_exp_socket` port list,
`fpga/rp/nanosoc_exp/README.md` §2): `hclk`, `hresetn`, `hsel`, `haddr`,
`htrans`, `hwrite`, `hsize`, `hburst`, `hprot`, `hmastlock`, `hwdata`,
`hready`, `hrdata`, `hreadyout`, `hresp`. `from_dut()` also accepts a `prefix`
(e.g. `prefix="exp_"`) and falls back to UPPERCASE names (the Arm/CMSDK style,
`HADDR`/`HTRANS`/...) automatically, so it binds Arm IP too.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Iterable, List, Optional, Sequence, Tuple

import cocotb
from cocotb.handle import HierarchyObject
from cocotb.triggers import Event, ReadOnly, RisingEdge, ValueChange

# --------------------------------------------------------------------------- #
# AHB-Lite encodings (AMBA 5 AHB, IHI0033)
# --------------------------------------------------------------------------- #
HTRANS_IDLE = 0b00
HTRANS_BUSY = 0b01
HTRANS_NONSEQ = 0b10
HTRANS_SEQ = 0b11

HBURST_SINGLE = 0b000
HBURST_INCR = 0b001
HBURST_WRAP4 = 0b010
HBURST_INCR4 = 0b011
HBURST_WRAP8 = 0b100
HBURST_INCR8 = 0b101
HBURST_WRAP16 = 0b110
HBURST_INCR16 = 0b111

HSIZE_BYTE = 0b000   # 8 bit
HSIZE_HALF = 0b001   # 16 bit
HSIZE_WORD = 0b010   # 32 bit

HRESP_OKAY = 0
HRESP_ERROR = 1

# Cortex-M0 drives HPROT = 4'b0011 for a privileged, non-bufferable,
# non-cacheable DATA access (bit0=data/opcode, bit1=privileged, bit2=bufferable,
# bit3=cacheable). Instruction fetch is 4'b0010. This is the BFM default because
# it is what the real bus master in nanosoc emits for a load/store.
HPROT_DATA = 0b0011
HPROT_OPCODE = 0b0010

_TRANS_NAME = {0: "IDLE", 1: "BUSY", 2: "NONSEQ", 3: "SEQ"}


class AhbError(Exception):
    """Raised when a slave answers a transfer with `HRESP = ERROR` and the
    master was constructed with `raise_on_error=True` (the default)."""

    def __init__(self, addr: int, write: bool, msg: str = ""):
        self.addr = addr
        self.write = write
        super().__init__(
            f"AHB-Lite ERROR response to {'write' if write else 'read'} "
            f"@ 0x{addr:08X}{(': ' + msg) if msg else ''}"
        )


class AhbProtocolError(Exception):
    """Raised by `AhbLiteMonitor` when the bus violates AHB-Lite."""


def _res(sig, default: Optional[int] = None) -> Optional[int]:
    """Resolve a handle to an int, or return `default` if it holds x/z.

    cocotb 2.x raises `ValueError` from `int(LogicArray)` when any bit is not
    0/1 — which is exactly what a bus looks like before reset. Callers decide
    whether an unresolvable value is benign (bus idle) or fatal.
    """
    try:
        return int(sig.value)
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# Transaction
# --------------------------------------------------------------------------- #
class AhbTransfer:
    """One queued AHB-Lite transfer. Returned by the non-blocking
    `init_read()` / `init_write()` entry points so a bench can keep several in
    flight and `await` them later — that is how you get a genuinely pipelined
    bus without hand-writing the state machine.

    `IDLE` and `BUSY` cycles are represented as *markers*: they occupy an
    address phase (and, for BUSY, the following slot in the pipeline) but carry
    no data and complete with no response, exactly as the spec requires a slave
    to treat them.
    """

    __slots__ = ("addr", "wdata", "write", "size", "trans", "burst", "prot",
                 "mastlock", "rdata", "resp", "marker", "_done")

    def __init__(self, addr: int, *, write: bool, wdata: int = 0,
                 size: int = HSIZE_WORD, trans: int = HTRANS_NONSEQ,
                 burst: int = HBURST_SINGLE, prot: int = HPROT_DATA,
                 mastlock: int = 0, marker: bool = False):
        self.addr = addr
        self.wdata = wdata
        self.write = write
        self.size = size
        self.trans = trans
        self.burst = burst
        self.prot = prot
        self.mastlock = mastlock
        self.marker = marker
        self.rdata: Optional[int] = None   # raw bus word (all 32 bits)
        self.resp: Optional[int] = None
        self._done = Event()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def _complete(self, resp: int, rdata: Optional[int]) -> None:
        self.resp = resp
        self.rdata = rdata
        self._done.set()

    async def wait(self) -> "AhbTransfer":
        """Block until this transfer's data phase has retired."""
        if not self._done.is_set():
            await self._done.wait()
        return self

    @property
    def value(self) -> int:
        """Read data, extracted from the byte lane implied by addr/size."""
        assert self.rdata is not None, "transfer has not completed"
        shift, mask = _lane(self.addr, self.size)
        return (self.rdata >> shift) & mask

    def __repr__(self) -> str:
        if self.marker:
            return f"<AHB {_TRANS_NAME[self.trans]}>"
        kind = "WR" if self.write else "RD"
        return (f"<AHB {kind} {_TRANS_NAME[self.trans]} 0x{self.addr:08X} "
                f"size={self.size} resp={self.resp}>")


def _lane(addr: int, size: int) -> Tuple[int, int]:
    """Byte-lane shift and mask for a transfer on a 32-bit AHB data bus.

    AHB-Lite is *not* byte-strobed like AXI: a sub-word transfer's data must be
    placed on the byte lane selected by the low address bits (little-endian
    here — nanosoc is little-endian), and the slave decodes the lane from
    HADDR[1:0] + HSIZE. Getting this wrong is the classic sub-word bug, so the
    BFM does the placement/extraction rather than leaving it to each bench.
    """
    if size == HSIZE_WORD:
        return 0, 0xFFFF_FFFF
    if size == HSIZE_HALF:
        return 8 * (addr & 0x2), 0xFFFF
    if size == HSIZE_BYTE:
        return 8 * (addr & 0x3), 0xFF
    raise ValueError(f"unsupported HSIZE {size} (this BFM is 32-bit: "
                     f"HSIZE_BYTE/HALF/WORD only)")


def _size_bytes(size: int) -> int:
    return 1 << size


# --------------------------------------------------------------------------- #
# Master
# --------------------------------------------------------------------------- #
class AhbLiteMaster:
    """A cocotb AHB-Lite **master** BFM with a real two-stage pipeline.

    A single background task (`_driver`) owns the bus: every clock cycle it
    drives one address phase and one data phase, popping transfers from an
    internal queue. Blocking `read()`/`write()` enqueue one transfer and await
    it; `burst_*()` / `pipeline()` enqueue several, so their address phases go
    out back-to-back with no idle bubble.

    Args:
        clk:        HCLK handle.
        haddr, htrans, hwrite, hwdata: master-driven handles (required).
        hrdata, hready: slave-driven handles the master samples (required).
                    `hready` is the *global* bus ready — see the module
                    docstring on how to wire it.
        hsel, hsize, hburst, hprot, hmastlock, hresp: optional handles; omit
                    any the DUT does not have (`hresp` omitted ⇒ always OKAY).
        hresetn:    optional active-low reset. If given, the driver parks the
                    bus in IDLE and issues nothing while it is asserted, and
                    `reset()` becomes available.
        hready_drive: optional handle for the slave's `hready` *input* when
                    there is no harness to tie it to `hreadyout`. See module
                    docstring.
        raise_on_error: True (default) ⇒ an ERROR response raises `AhbError`.
                    Set False to have `read()`/`write()` return the HRESP value
                    for the test to assert on.
        deselect_on_idle: True (default) ⇒ HSEL is deasserted on IDLE cycles,
                    as a real address decoder would do once the address leaves
                    the region. Set False to hold HSEL asserted through IDLE —
                    which pins the property that a slave must not act on
                    `HTRANS=IDLE` even while selected.
        name:       log-name suffix (`cocotb.<name>`).
    """

    def __init__(self, clk, haddr, htrans, hwrite, hwdata, hrdata, hready,
                 hsel=None, hsize=None, hburst=None, hprot=None,
                 hmastlock=None, hresp=None, hresetn=None, hready_drive=None,
                 raise_on_error: bool = True, deselect_on_idle: bool = True,
                 name: str = "ahb_lite"):
        self.clk = clk
        self.haddr, self.htrans, self.hwrite = haddr, htrans, hwrite
        self.hwdata, self.hrdata, self.hready = hwdata, hrdata, hready
        self.hsel, self.hsize, self.hburst = hsel, hsize, hburst
        self.hprot, self.hmastlock, self.hresp = hprot, hmastlock, hresp
        self.hresetn = hresetn
        self.hready_drive = hready_drive
        self.raise_on_error = raise_on_error
        self.deselect_on_idle = deselect_on_idle
        self.log = logging.getLogger(f"cocotb.{name}")

        self._q: deque = deque()
        self._force_idle = False
        self._driver_task = None
        self._mirror_task = None

        # Counters — cheap, and they are what a bench asserts on to prove the
        # pipeline actually overlapped (see `overlapped_cycles`).
        self.n_write = 0
        self.n_read = 0
        self.n_error = 0
        self.overlapped_cycles = 0   # cycles with BOTH an addr phase and a
                                     # data phase live = real pipelining
        self.idle_cycles = 0

        self._park()
        self.start()

    # ---- binding ---------------------------------------------------------- #
    @classmethod
    def from_dut(cls, dut: HierarchyObject, prefix: str = "", **kwargs
                 ) -> "AhbLiteMaster":
        """Bind to a DUT by AHB-Lite signal name.

        Tries `<prefix>haddr` then `<prefix>HADDR` for each signal, so it binds
        both the frozen lowercase `nanosoc_exp_socket` port list and the
        UPPERCASE Arm/CMSDK style. Optional signals absent from the DUT are
        simply left unbound. Any handle can be overridden by keyword
        (e.g. ``from_dut(dut, hready=dut.tb_hready)``).
        """
        def get(base: str, required: bool = False):
            for nm in (f"{prefix}{base}", f"{prefix}{base.upper()}"):
                try:
                    return getattr(dut, nm)
                except AttributeError:
                    continue
            if required:
                raise AttributeError(
                    f"AhbLiteMaster.from_dut: no handle "
                    f"'{prefix}{base}' / '{prefix}{base.upper()}' on {dut!r}")
            return None

        # HREADY: prefer a real global-ready net; otherwise the slave's own
        # HREADYOUT (single-slave AHB-Lite: they are the same signal).
        # NB: a cocotb 2.x handle raises TypeError on `bool(handle)`, so the
        # fallback cannot be written `get("hready") or get("hreadyout")`.
        hready = kwargs.pop("hready", None)
        if hready is None:
            hready = get("hready")
        if hready is None:
            hready = get("hreadyout", required=True)

        binding = dict(
            clk=get("hclk", required=True),
            haddr=get("haddr", required=True),
            htrans=get("htrans", required=True),
            hwrite=get("hwrite", required=True),
            hwdata=get("hwdata", required=True),
            hrdata=get("hrdata", required=True),
            hready=hready,
            hsel=get("hsel"),
            hsize=get("hsize"),
            hburst=get("hburst"),
            hprot=get("hprot"),
            hmastlock=get("hmastlock"),
            hresp=get("hresp"),
            hresetn=get("hresetn"),
        )
        binding.update(kwargs)
        return cls(**binding)

    # ---- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        """Start (or restart) the bus driver. Called by `__init__`."""
        if self._driver_task is None or self._driver_task.done():
            self._driver_task = cocotb.start_soon(self._driver())
        if self.hready_drive is not None and (
                self._mirror_task is None or self._mirror_task.done()):
            self._mirror_task = cocotb.start_soon(self._hready_mirror())

    def stop(self) -> None:
        """Kill the bus driver and park the bus in IDLE."""
        for t in (self._driver_task, self._mirror_task):
            if t is not None and not t.done():
                t.kill()
        self._driver_task = self._mirror_task = None
        self._park()

    async def reset(self, cycles: int = 4, post_cycles: int = 2) -> None:
        """Assert `hresetn` low for `cycles` clocks with the bus parked in
        IDLE, release it, then wait `post_cycles`. Requires an `hresetn`
        handle. `cycles`/`post_cycles` are configurable because DUTs differ in
        how long they need (nanosoc's peripherals want a few; a bare register
        file wants one).
        """
        if self.hresetn is None:
            raise RuntimeError("AhbLiteMaster.reset(): no hresetn handle bound")
        self._q.clear()
        self._force_idle = False
        self._park()
        self.hresetn.value = 0
        for _ in range(max(1, cycles)):
            await RisingEdge(self.clk)
        self.hresetn.value = 1
        for _ in range(max(0, post_cycles)):
            await RisingEdge(self.clk)

    # ---- blocking register API (mirrors regmap.AxiLiteMaster) ------------- #
    async def write(self, addr: int, data: int, size: int = HSIZE_WORD,
                    prot: int = HPROT_DATA) -> int:
        """Single word (or byte/halfword) write. Returns HRESP."""
        txn = self.init_write(addr, data, size=size, prot=prot)
        await txn.wait()
        return self._finish(txn)

    async def read(self, addr: int, size: int = HSIZE_WORD,
                   prot: int = HPROT_DATA) -> Tuple[int, int]:
        """Single word (or byte/halfword) read. Returns `(data, hresp)` —
        the same shape as `regmap.AxiLiteMaster.read()`. `data` is already
        extracted from the byte lane implied by `addr`/`size`."""
        txn = self.init_read(addr, size=size, prot=prot)
        await txn.wait()
        resp = self._finish(txn)
        return txn.value, resp

    def _finish(self, txn: AhbTransfer) -> int:
        if txn.resp == HRESP_ERROR:
            self.n_error += 1
            if self.raise_on_error:
                raise AhbError(txn.addr, txn.write)
        return txn.resp if txn.resp is not None else HRESP_OKAY

    # ---- non-blocking / pipelined API ------------------------------------- #
    def init_write(self, addr: int, data: int, size: int = HSIZE_WORD,
                   trans: int = HTRANS_NONSEQ, burst: int = HBURST_SINGLE,
                   prot: int = HPROT_DATA, mastlock: int = 0) -> AhbTransfer:
        """Queue a write without waiting. Returns the `AhbTransfer` to await."""
        self._check_align(addr, size)
        shift, mask = _lane(addr, size)
        txn = AhbTransfer(addr, write=True, wdata=(data & mask) << shift,
                          size=size, trans=trans, burst=burst, prot=prot,
                          mastlock=mastlock)
        self._q.append(txn)
        self.n_write += 1
        return txn

    def init_read(self, addr: int, size: int = HSIZE_WORD,
                  trans: int = HTRANS_NONSEQ, burst: int = HBURST_SINGLE,
                  prot: int = HPROT_DATA, mastlock: int = 0) -> AhbTransfer:
        """Queue a read without waiting. Returns the `AhbTransfer` to await."""
        self._check_align(addr, size)
        txn = AhbTransfer(addr, write=False, size=size, trans=trans,
                          burst=burst, prot=prot, mastlock=mastlock)
        self._q.append(txn)
        self.n_read += 1
        return txn

    def _check_align(self, addr: int, size: int) -> None:
        n = _size_bytes(size)
        if addr & (n - 1):
            raise ValueError(f"AHB-Lite: address 0x{addr:08X} is not "
                             f"{n}-byte aligned for HSIZE={size}")

    async def idle(self, cycles: int = 1) -> None:
        """Insert exactly `cycles` `HTRANS=IDLE` address phases, in order,
        after everything already queued. (Just *not calling* the BFM also
        idles the bus, but only if the queue happens to be empty — this is the
        deterministic version, for benches that pin idle-cycle behaviour.)"""
        last = None
        for _ in range(max(0, cycles)):
            last = AhbTransfer(0, write=False, trans=HTRANS_IDLE, marker=True)
            self._q.append(last)
        if last is not None:
            await last.wait()

    async def pipeline(self, transfers: Sequence[AhbTransfer]) -> List[AhbTransfer]:
        """Await a list of already-queued transfers (from `init_*`). They were
        emitted back-to-back; this just blocks until the last retires and
        raises on the first ERROR (when `raise_on_error`)."""
        for t in transfers:
            await t.wait()
        for t in transfers:
            self._finish(t)
        return list(transfers)

    async def burst_write(self, addr: int, data: Iterable[int],
                          size: int = HSIZE_WORD, burst: int = HBURST_INCR,
                          busy: int = 0, prot: int = HPROT_DATA) -> None:
        """Incrementing burst write: `NONSEQ` then `SEQ` for each subsequent
        beat, address phases back-to-back (transfer *n+1*'s address phase
        overlaps transfer *n*'s data phase — the thing a non-pipelined BFM can
        never produce).

        `busy`: insert this many `HTRANS=BUSY` cycles before each `SEQ` beat.
        BUSY is a master-side stall *inside* a burst; a slave must ignore it
        (respond OKAY, zero wait states, start no transfer). Slaves that treat
        BUSY as a real transfer double-write — `busy>0` is how you catch that.
        """
        words = list(data)
        step = _size_bytes(size)
        queued: List[AhbTransfer] = []
        for i, w in enumerate(words):
            a = addr + i * step
            if i and busy:
                for _ in range(busy):
                    m = AhbTransfer(a, write=True, trans=HTRANS_BUSY,
                                    burst=burst, marker=True)
                    self._q.append(m)
                    queued.append(m)
            queued.append(self.init_write(
                a, w, size=size, burst=burst, prot=prot,
                trans=HTRANS_NONSEQ if i == 0 else HTRANS_SEQ))
        await self.pipeline(queued)

    async def burst_read(self, addr: int, count: int, size: int = HSIZE_WORD,
                         burst: int = HBURST_INCR, busy: int = 0,
                         prot: int = HPROT_DATA) -> List[int]:
        """Incrementing burst read. Returns the list of values (lane-extracted).
        See `burst_write` for `busy`."""
        step = _size_bytes(size)
        queued: List[AhbTransfer] = []
        beats: List[AhbTransfer] = []
        for i in range(count):
            a = addr + i * step
            if i and busy:
                for _ in range(busy):
                    m = AhbTransfer(a, write=False, trans=HTRANS_BUSY,
                                    burst=burst, marker=True)
                    self._q.append(m)
                    queued.append(m)
            t = self.init_read(a, size=size, burst=burst, prot=prot,
                               trans=HTRANS_NONSEQ if i == 0 else HTRANS_SEQ)
            queued.append(t)
            beats.append(t)
        await self.pipeline(queued)
        return [t.value for t in beats]

    # ---- the bus driver --------------------------------------------------- #
    def _park(self) -> None:
        """Drive the bus to its inert state (a writable-phase operation)."""
        self.htrans.value = HTRANS_IDLE
        self.haddr.value = 0
        self.hwrite.value = 0
        self.hwdata.value = 0
        if self.hsel is not None:
            self.hsel.value = 0
        if self.hsize is not None:
            self.hsize.value = HSIZE_WORD
        if self.hburst is not None:
            self.hburst.value = HBURST_SINGLE
        if self.hprot is not None:
            self.hprot.value = HPROT_DATA
        if self.hmastlock is not None:
            self.hmastlock.value = 0

    def _drive_addr(self, txn: Optional[AhbTransfer]) -> None:
        """Drive the address phase for this cycle. `None` (or an IDLE marker)
        ⇒ IDLE.

        Re-driving the *same* txn on a wait state is what holds HADDR/HTRANS
        stable, which is exactly what the spec demands (and what a monitor
        checks)."""
        if txn is None or txn.trans == HTRANS_IDLE:
            self.htrans.value = HTRANS_IDLE
            if self.hsel is not None and self.deselect_on_idle:
                self.hsel.value = 0
            return
        self.htrans.value = txn.trans
        self.haddr.value = txn.addr
        self.hwrite.value = 1 if txn.write else 0
        if self.hsel is not None:
            self.hsel.value = 1
        if self.hsize is not None:
            self.hsize.value = txn.size
        if self.hburst is not None:
            self.hburst.value = txn.burst
        if self.hprot is not None:
            self.hprot.value = txn.prot
        if self.hmastlock is not None:
            self.hmastlock.value = txn.mastlock

    def _drive_data(self, txn: Optional[AhbTransfer]) -> None:
        """Drive HWDATA for this cycle's data phase. Held across wait states by
        virtue of being re-driven every cycle with the same txn."""
        if txn is not None and txn.write and not txn.marker:
            self.hwdata.value = txn.wdata

    async def _hready_mirror(self) -> None:
        """`assign hready = hreadyout;` in Python — for benches whose TOPLEVEL
        is the raw slave (no harness to tie it). Delta-delayed, not cycle-
        delayed, so the slave samples the correct value at the next edge.
        Sound only because AHB-Lite forbids HREADYOUT from depending
        combinationally on HREADY (IHI0033)."""
        self.hready_drive.value = _res(self.hready, 1)
        while True:
            await ValueChange(self.hready)
            v = _res(self.hready)
            if v is not None:
                self.hready_drive.value = v

    async def _driver(self) -> None:
        """The whole point of this file. One cycle per iteration:

            drive(addr phase N) + drive(data phase N)   [writable]
            sample HREADY / HRESP / HRDATA for cycle N  [ReadOnly]
            edge N+1 -> resolve

        `addr_txn` is the transfer whose *address* phase is on the bus this
        cycle; `data_txn` is the one whose *data* phase is. When HREADY is
        sampled high, both advance in the same edge — which is precisely the
        pipeline overlap.
        """
        addr_txn: Optional[AhbTransfer] = None
        data_txn: Optional[AhbTransfer] = None

        while True:
            # --- in reset: park, issue nothing ---------------------------- #
            if self.hresetn is not None and _res(self.hresetn, 0) != 1:
                self._park()
                addr_txn = data_txn = None
                self._force_idle = False
                await RisingEdge(self.clk)
                continue

            # --- writable phase: choose + drive cycle N -------------------- #
            if self._force_idle:
                # Second cycle of a two-cycle ERROR response: the spec requires
                # HTRANS=IDLE here so the already-broadcast next address is
                # cancelled. Do NOT pop a new transfer.
                self._force_idle = False
                addr_txn = None
            elif addr_txn is None and self._q:
                addr_txn = self._q.popleft()

            self._drive_addr(addr_txn)
            self._drive_data(data_txn)

            if addr_txn is None and data_txn is None:
                self.idle_cycles += 1
                await RisingEdge(self.clk)
                continue
            # A cycle carrying BOTH a live address phase and a live data phase
            # is the pipeline overlap. Benches assert on this counter to prove
            # the BFM really did emit gapless transfers (markers don't count).
            if (addr_txn is not None and data_txn is not None
                    and addr_txn.trans in (HTRANS_NONSEQ, HTRANS_SEQ)
                    and not data_txn.marker):
                self.overlapped_cycles += 1

            # --- ReadOnly: settled slave outputs for cycle N --------------- #
            await ReadOnly()
            ready = _res(self.hready)
            resp = _res(self.hresp, HRESP_OKAY) if self.hresp is not None \
                else HRESP_OKAY
            rdata = _res(self.hrdata, 0)
            if ready is None:
                raise AhbProtocolError(
                    "HREADY is x/z while a transfer is in flight "
                    f"(addr_phase={addr_txn!r}, data_phase={data_txn!r}) — the "
                    "slave's hready input is probably unconnected: tie it to "
                    "hreadyout in a harness, or pass hready_drive= to the BFM")

            # --- edge N+1: the DUT consumes cycle N; resolve --------------- #
            await RisingEdge(self.clk)

            if ready:
                # Data phase retires AND the address phase is accepted, in the
                # same edge. This is the pipeline.
                if data_txn is not None:
                    # Markers (IDLE/BUSY) complete too — `idle()` and the BUSY
                    # beats inside a burst are awaited by the caller, so a
                    # marker that never completes deadlocks the bench.
                    data_txn._complete(
                        resp, rdata if not data_txn.write else None)
                    if resp == HRESP_ERROR and not data_txn.marker:
                        self.log.debug("ERROR response: %r", data_txn)
                data_txn = addr_txn
                addr_txn = None
            elif resp == HRESP_ERROR and data_txn is not None:
                # First cycle of the two-cycle ERROR response. The next
                # transfer's address is ALREADY on the bus and the spec
                # requires the master to cancel it by driving HTRANS=IDLE in
                # the second cycle. Re-present it afterwards, so the caller's
                # await still means what it says.
                self._cancel_addr_phase(addr_txn)
                addr_txn = None
                self._force_idle = True
            # else: plain wait state — hold both phases, re-driven next cycle.

    def _cancel_addr_phase(self, txn: Optional[AhbTransfer]) -> None:
        """Undo the address phase an ERROR response forces us to cancel.

        The error broke the burst, so whatever resumes must restart it:
          * a cancelled BUSY marker is *dropped* (BUSY is only legal inside a
            burst, and this one no longer is) — but completed, so anything
            awaiting it does not deadlock;
          * the transfer that resumes is demoted `SEQ -> NONSEQ`. Re-presenting
            it as SEQ after the mandatory IDLE would be a protocol violation
            (and `AhbLiteMonitor` check M6 would rightly fire on it).
        """
        if txn is not None:
            if txn.marker:
                txn._complete(HRESP_OKAY, None)
            else:
                self._q.appendleft(txn)
                self.log.debug("ERROR cancelled address phase %r — will "
                               "re-present as NONSEQ", txn)
        # Drop any BUSY markers now stranded at the head of the queue.
        while self._q and self._q[0].marker and self._q[0].trans == HTRANS_BUSY:
            self._q.popleft()._complete(HRESP_OKAY, None)
        if self._q and self._q[0].trans == HTRANS_SEQ:
            self._q[0].trans = HTRANS_NONSEQ


# --------------------------------------------------------------------------- #
# Monitor / protocol checker
# --------------------------------------------------------------------------- #
class AhbLiteMonitor:
    """A passive AHB-Lite protocol checker. Bind it alongside the master and it
    asserts, every cycle, the properties a bench would otherwise have to trust:

      M1. **Address phase is held while `HREADY` is low** — HTRANS/HADDR/
          HWRITE/HSIZE/HBURST must not change (master-side).
      M2. **`HWDATA` is held for the whole data phase** while HREADY is low
          (master-side).
      M3. **`HREADYOUT` eventually asserts** — no transfer stalls longer than
          `max_wait` cycles (slave-side; this is what a hung DUT looks like).
      M4. **`HRESP=ERROR` is a two-cycle response** — an ERROR retiring with
          HREADY high must have been preceded by ERROR with HREADY low
          (slave-side). A one-cycle error is the single most common AHB slave
          bug and it silently corrupts the pipeline.
      M5. **`HRESP` is OKAY for IDLE/BUSY** and the slave inserts no wait state
          on them (slave-side).
      M6. **`HTRANS=SEQ` only follows an accepted NONSEQ/SEQ/BUSY**, never IDLE
          (master-side).

    Violations raise `AhbProtocolError` by default (`fatal=True`); with
    `fatal=False` they are logged and counted in `self.errors` so a bench can
    assert on the count — which is how you mutation-prove the checker itself.
    """

    def __init__(self, clk, haddr, htrans, hwrite, hwdata, hready,
                 hsize=None, hburst=None, hresp=None, hresetn=None,
                 max_wait: int = 1024, fatal: bool = True,
                 reset_grace: int = 2, name: str = "ahb_mon"):
        self.clk = clk
        self.haddr, self.htrans, self.hwrite = haddr, htrans, hwrite
        self.hwdata, self.hready = hwdata, hready
        self.hsize, self.hburst, self.hresp = hsize, hburst, hresp
        self.hresetn = hresetn
        self.max_wait = max_wait
        self.fatal = fatal
        # M5 (HREADYOUT high when idle) is a real AHB rule, but plenty of real
        # slaves — CMSDK's included — dawdle for a cycle or two after reset
        # release. Skip M5 for `reset_grace` cycles after deassertion rather
        # than fire a violation nobody can act on.
        self.reset_grace = reset_grace
        self.log = logging.getLogger(f"cocotb.{name}")
        self.errors: List[str] = []
        self._task = None

    @classmethod
    def from_dut(cls, dut: HierarchyObject, prefix: str = "", **kwargs
                 ) -> "AhbLiteMonitor":
        def get(base: str, required: bool = False):
            for nm in (f"{prefix}{base}", f"{prefix}{base.upper()}"):
                try:
                    return getattr(dut, nm)
                except AttributeError:
                    continue
            if required:
                raise AttributeError(f"AhbLiteMonitor.from_dut: no "
                                     f"'{prefix}{base}' on {dut!r}")
            return None

        # NB: a cocotb 2.x handle raises TypeError on `bool(handle)`, so the
        # fallback cannot be written `get("hready") or get("hreadyout")`.
        hready = kwargs.pop("hready", None)
        if hready is None:
            hready = get("hready")
        if hready is None:
            hready = get("hreadyout", required=True)
        binding = dict(
            clk=get("hclk", required=True),
            haddr=get("haddr", required=True),
            htrans=get("htrans", required=True),
            hwrite=get("hwrite", required=True),
            hwdata=get("hwdata", required=True),
            hready=hready,
            hsize=get("hsize"), hburst=get("hburst"), hresp=get("hresp"),
            hresetn=get("hresetn"),
        )
        binding.update(kwargs)
        return cls(**binding)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = cocotb.start_soon(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.kill()
        self._task = None

    def _fail(self, msg: str) -> None:
        self.errors.append(msg)
        self.log.error("AHB-Lite protocol violation: %s", msg)
        if self.fatal:
            raise AhbProtocolError(msg)

    def _addr_phase(self) -> tuple:
        return (
            _res(self.htrans), _res(self.haddr), _res(self.hwrite),
            _res(self.hsize) if self.hsize is not None else None,
            _res(self.hburst) if self.hburst is not None else None,
        )

    async def _run(self) -> None:
        prev_ap = None          # address phase sampled in the previous cycle
        prev_ready = 1          # HREADY in the previous cycle
        prev_resp = HRESP_OKAY
        prev_wdata = None
        stall = 0
        grace = self.reset_grace
        # data phase live this cycle = the address phase accepted last cycle
        dp_active = False
        dp_is_write = False

        while True:
            await RisingEdge(self.clk)
            if self.hresetn is not None and _res(self.hresetn, 0) != 1:
                prev_ap, prev_ready, prev_wdata = None, 1, None
                prev_resp, stall, dp_active = HRESP_OKAY, 0, False
                grace = self.reset_grace
                continue
            if grace > 0:
                grace -= 1
            await ReadOnly()

            ap = self._addr_phase()
            ready = _res(self.hready)
            resp = _res(self.hresp, HRESP_OKAY) if self.hresp is not None \
                else HRESP_OKAY
            wdata = _res(self.hwdata)
            trans = ap[0]

            if ready is None:      # bus not driven yet — nothing to check
                prev_ap, prev_ready, prev_wdata = ap, 1, wdata
                prev_resp, dp_active = HRESP_OKAY, False
                continue

            # M1 — address phase held while !HREADY (only if we were mid-stall)
            if prev_ready == 0 and prev_ap is not None and ap != prev_ap:
                # ...unless the previous cycle was the FIRST cycle of a 2-cycle
                # ERROR, in which case the master is REQUIRED to change to IDLE.
                if not (prev_resp == HRESP_ERROR and trans == HTRANS_IDLE):
                    self._fail(
                        f"address phase changed while HREADY was low: "
                        f"{prev_ap} -> {ap}")

            # M2 — HWDATA held for the whole data phase while !HREADY
            if (prev_ready == 0 and dp_active and dp_is_write
                    and prev_wdata is not None and wdata != prev_wdata):
                self._fail(f"HWDATA changed during a stalled data phase: "
                           f"0x{prev_wdata:08X} -> 0x{wdata:08X}")

            # M3 — HREADYOUT eventually asserts
            if ready == 0:
                stall += 1
                if stall > self.max_wait:
                    self._fail(f"HREADYOUT low for > {self.max_wait} cycles — "
                               f"the slave is hung (address phase {ap})")
            else:
                stall = 0

            # M4 — ERROR must be two cycles: an ERROR retiring (HREADY high)
            #      must have been preceded by ERROR with HREADY low.
            if ready == 1 and resp == HRESP_ERROR and dp_active:
                if not (prev_ready == 0 and prev_resp == HRESP_ERROR):
                    self._fail(
                        "one-cycle ERROR response: HRESP=ERROR with "
                        "HREADYOUT high was not preceded by HRESP=ERROR with "
                        "HREADYOUT low (AHB-Lite requires a 2-cycle error so "
                        "the master can cancel the pipelined address phase)")

            # M5 — no wait state and no ERROR on an IDLE/BUSY data phase
            if not dp_active and ready == 0 and grace == 0:
                self._fail("slave inserted a wait state on an IDLE/BUSY data "
                           "phase (HREADYOUT must be high when no transfer is "
                           "in its data phase)")
            if not dp_active and resp == HRESP_ERROR:
                self._fail("slave asserted HRESP=ERROR with no transfer in its "
                           "data phase (IDLE/BUSY must always be OKAY)")

            # M6 — SEQ must not follow IDLE
            if (trans == HTRANS_SEQ and prev_ap is not None
                    and prev_ready == 1 and prev_ap[0] == HTRANS_IDLE):
                self._fail("HTRANS=SEQ directly after an accepted IDLE "
                           "(a burst must start with NONSEQ)")

            # --- roll the pipeline ------------------------------------------ #
            if ready == 1:
                dp_active = trans in (HTRANS_NONSEQ, HTRANS_SEQ)
                dp_is_write = bool(ap[2]) if ap[2] is not None else False
            prev_ap, prev_ready, prev_resp, prev_wdata = ap, ready, resp, wdata
