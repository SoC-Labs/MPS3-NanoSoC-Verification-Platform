"""``webharness.control`` — the clock and reset models. Pure data + pure logic.

Two things this module exists to get *right*, because both are easy to render
as a comfortable lie:

**1. Which resets this server can actually drive.** Exactly one: ``target="dut"``.
The shell's control channel accepts nothing else — ``coordinator_handle_reset()``
(``firmware/coordinator/coordinator.c:228-245``) answers ``"bad target"`` for
anything but ``"dut"``, because ``dbg_resetn`` belongs to the SWD/JTAG server
(it tracks OpenOCD's srst level) and ``rp_resetn`` is held by ``swap_fsm``
across a swap. The wider reset taxonomy (``mcc-reconfig``, ``usb-power``,
``dfx-swap``, …) is real but lives one layer up in the *edge/tender* — it is
listed here for reference with ``actionable: false`` and its invalidation set
from :func:`pyverify.edge.reset`, so the page can say what each would do
without growing a button that does not exist.

**2. What ``set_clk`` really does.** The retune is **real**, not a stub: the
shell looks the preset up in a fail-closed ``strcmp`` table, writes the id to
``CLKRST.DUT_CLK_SEL``, reprograms the DUT-clock MMCM over the clk_wiz AXI4-Lite
DRP (the ``MMCM_DRP`` block @ ``0x44AB_0000``), and bounded-polls
``CLKRST.STATUS.mmcm_locked`` for the relock (``firmware/clkrst/clkrst.c:126-150``).
What is *not* settled is the naming: **OPEN_ISSUES I16** — the preset names and
their ``DUT_CLK_SEL`` id mapping are not yet contract values, and
``CLKRST.DUT_CLK_SEL`` is inert on today's fabric (the DRP write is what
actually retunes). So the three presets below are the shipped firmware's table,
not a frozen interface, and the API says so in :data:`CLK_CONTRACT`.

On the Linux harness the answer is the same: ``mps3-harnessd`` runs this very
``firmware/clkrst/clkrst.c`` against the fabric through UIO
(``docs/planning/linux_lanes/HARNESSD_CONTRACT.md``), so the retune is real
there too. (The v0.7 ``mps3-ctrld`` port, whose ``set_clk``/``reset`` were
in-daemon stubs, is retired -- ``README.md`` §5.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ClkPreset", "CLK_PRESETS", "CLK_CONTRACT", "CLK_INPUT_MHZ",
    "preset_names", "find_preset",
    "RESET_TARGETS", "reset_reference", "clock_rows",
]


# --------------------------------------------------------------------------- #
# Clocks
# --------------------------------------------------------------------------- #

#: MMCM reference input. clkrst.c:19 — "Input is 50 MHz; mult/divclk = 20/1 =>
#: a fixed 1000 MHz VCO for all three presets, and clkout0_div sets the output".
CLK_INPUT_MHZ = 50.0


@dataclass(frozen=True)
class ClkPreset:
    """One row of ``clkrst_preset_table[]`` (``firmware/clkrst/clkrst.c:21-26``).

    ``divclk``/``mult``/``clkout0_div`` are the REAL clk_wiz DRP values; ``id``
    is the ``CLKRST.DUT_CLK_SEL`` code (inert on today's fabric, written anyway
    so intent is expressed — clkrst.c:141-146).
    """

    name: str
    id: int
    divclk: int
    mult: int
    clkout0_div: int

    @property
    def mhz(self) -> float:
        """Derived, not asserted: VCO = in * mult / divclk, out = VCO / O."""
        return CLK_INPUT_MHZ * self.mult / self.divclk / self.clkout0_div

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "id": self.id, "mhz": round(self.mhz, 3),
            "divclk": self.divclk, "mult": self.mult,
            "clkout0_div": self.clkout0_div,
        }


CLK_PRESETS: Tuple[ClkPreset, ...] = (
    ClkPreset(name="25mhz", id=0, divclk=1, mult=20, clkout0_div=40),
    ClkPreset(name="50mhz", id=1, divclk=1, mult=20, clkout0_div=20),
    ClkPreset(name="100mhz", id=2, divclk=1, mult=20, clkout0_div=10),
)

#: What the UI must say alongside the preset buttons. Not decoration — the
#: difference between "the retune is real" and "the names are frozen" is
#: exactly what a user of this page needs to know before scripting against it.
CLK_CONTRACT = {
    "retune": "real",
    "mechanism": "clk_wiz AXI4-Lite DRP (MMCM_DRP @ 0x44AB_0000) + bounded "
                 "CLKRST.STATUS.mmcm_locked relock poll",
    "open_issue": "I16",
    "caveat": "Preset NAMES and their CLKRST.DUT_CLK_SEL ids are not yet "
              "contract values (docs/contracts/OPEN_ISSUES.md I16); "
              "DUT_CLK_SEL is inert on today's fabric — the DRP write is what "
              "retunes. Arbitrary frequencies are not exposed: "
              "CLKRST.DUT_CLK_DRP is a placeholder window (clkrst.c:152-158).",
    "source": "firmware/clkrst/clkrst.c:21-26",
    "backend_note": "Both harness engines run the same firmware/clkrst/clkrst.c "
                    "(on Linux inside mps3-harnessd, via UIO), so the retune is "
                    "real on either; the v0.7 daemon stub is retired.",
}


def preset_names() -> Tuple[str, ...]:
    return tuple(p.name for p in CLK_PRESETS)


def find_preset(name: str) -> Optional[ClkPreset]:
    for p in CLK_PRESETS:
        if p.name == name:
            return p
    return None


def clock_rows() -> List[Dict[str, Any]]:
    return [p.to_dict() for p in CLK_PRESETS]


# --------------------------------------------------------------------------- #
# Resets
# --------------------------------------------------------------------------- #

#: The ONLY value the ``:6900`` reset verb accepts (coordinator.c:238).
RESET_TARGETS: Tuple[str, ...] = ("dut",)

#: Reset taxonomy -> how it is driven, for the ones this server cannot drive.
#: Keys are ``pyverify.edge.ResetKind`` values.
_RESET_DRIVER = {
    "dut-reset": "this server — {\"op\":\"reset\",\"target\":\"dut\"} on :6900 "
                 "(pulses CLKRST.RESET_CTRL.dut_resetn with a 1 ms hold)",
    "dfx-swap": "pyverify deploy / :6910 push + swap — RP-scoped, held by swap_fsm",
    "mcc-reconfig": "the tender (MCC USB-MSD + power cycle) — reloads the KU115 from SD",
    "usb-power": "the tender (physical power cycle)",
    "uart-soft": "not wired on this platform",
    "system": "weakest class — no separate mechanism on MPS3",
}


def reset_reference() -> List[Dict[str, Any]]:
    """The full reset taxonomy with each kind's invalidation semantics and
    whether *this server* can drive it.

    Semantics come from :func:`pyverify.edge.reset` — a pure function of the
    kind, so this needs no board. If pyverify is unavailable the reference
    degrades to the actionable row alone rather than guessing.
    """
    try:
        from pyverify.edge import ResetKind, reset as edge_reset, reset_result_to_dict
    except Exception:          # pragma: no cover - pyverify always present in check
        return [{
            "kind": "dut-reset", "actionable": True,
            "driver": _RESET_DRIVER["dut-reset"], "target": "dut",
        }]

    rows = []
    for kind in ResetKind:
        row = reset_result_to_dict(edge_reset(kind))
        actionable = kind.value == "dut-reset"
        row["actionable"] = actionable
        row["target"] = "dut" if actionable else None
        row["driver"] = _RESET_DRIVER.get(kind.value, "")
        rows.append(row)
    # actionable first: the button, then the reference.
    rows.sort(key=lambda r: (not r["actionable"], r["kind"]))
    return rows
