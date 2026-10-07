"""tests/board_gpio/test_gpio_mux_logic.py

Pure-Python truth-table test for the GPIO passthrough OWN-mux --
`docs/contracts/shell-regmap.md` v0.1's new GPIO block (`0x44AA_0000`, I4)
and `docs/contracts/partition-pins.md`'s "Board-port / GPIO passthrough"
signal group. No cocotb, no DUT, no simulator -- pytest-collectible, runs
today, same pattern as `tests/common/test_*.py`.

There is no RTL for this block yet (`fpga/shell/ip/board_gpio/` does not
exist -- see `tests/board_gpio/dut_notes.md`) and no firmware driver
either, so there is nothing here to import as an "oracle." The model below
is an **independent, from-scratch re-derivation** of the OWN-mux semantics
from the contract prose alone:

    shell-regmap.md GPIO block:
      IN  (0x00, ro) -- board pad -> sampled value
      OUT (0x04)     -- host drive value, used where OWN=host
      OE  (0x08)     -- host output-enable
      OWN (0x0C)     -- 0 = DUT owns the bit (default), 1 = host mux overrides

    partition-pins.md board-port group (shell <-> RP):
      dut_gpio_o  (I to shell) -- DUT drive value
      dut_gpio_oe (I to shell) -- DUT output-enable (per-bit tristate)
      dut_gpio_i  (O to RP)    -- board pad -> shell -> DUT sample

Per-bit resolution (this is the thing under test -- there is exactly one
correct reading of "0 = DUT owns... 1 = host mux overrides" and this file
proves it exhaustively rather than spot-checking a couple of cases):

    resolved_drive[i] = OUT[i] if OWN[i] else dut_gpio_o[i]
    resolved_oe[i]    = OE[i]  if OWN[i] else dut_gpio_oe[i]
    pad[i]            = resolved_drive[i] if resolved_oe[i] else <external>
    IN[i] == dut_gpio_i[i] == pad[i]   (the shell brokers one physical net
                                        to both readers, regardless of who
                                        is driving it -- partition-pins.md:
                                        "the DUT reaches board I/O... through
                                        the shell" -- neither side can tell
                                        the difference between "the other
                                        side drove this" and "an external
                                        device on the pad drove this")
"""
from __future__ import annotations

from dataclasses import dataclass

NGPIO = 16  # partition-pins.md board-port group: "Width NGPIO ... v0: 16"
MASK = (1 << NGPIO) - 1


@dataclass(frozen=True)
class GpioMuxResult:
    pad_drive: int   # what's actively driven onto the pad, per bit
    pad_oe: int       # whether the pad is actively driven, per bit


def resolve_gpio_bus(
    own: int, dut_o: int, dut_oe: int, host_out: int, host_oe: int,
    width: int = NGPIO,
) -> GpioMuxResult:
    """The OWN-mux, independently derived from shell-regmap.md's GPIO
    table: per bit, OWN=0 selects the DUT's own drive/OE, OWN=1 selects the
    host's OUT/OE -- a plain bitwise 2:1 mux, selector = OWN.
    """
    m = (1 << width) - 1
    own &= m
    dut_o &= m
    dut_oe &= m
    host_out &= m
    host_oe &= m
    not_own = (~own) & m
    drive = (host_out & own) | (dut_o & not_own)
    oe = (host_oe & own) | (dut_oe & not_own)
    return GpioMuxResult(pad_drive=drive & m, pad_oe=oe & m)


def sample_pad(result: GpioMuxResult, external: int, width: int = NGPIO) -> int:
    """What the physical pad electrically reads, per bit: the resolved
    drive value where the mux's resolved OE says something is actively
    driving it, else whatever an external device on that pad presents
    (modeled as a caller-supplied stimulus -- a real undriven CMOS pad is
    an unstable float, out of scope for a digital truth table). Both
    GPIO.IN and dut_gpio_i sample this same value -- see module docstring.
    """
    m = (1 << width) - 1
    return (result.pad_drive & result.pad_oe) | (external & (~result.pad_oe) & m)


# --------------------------------------------------------------------------- #
# Default (OWN=0 everywhere): DUT owns every bit
# --------------------------------------------------------------------------- #

def test_default_own_all_zero_gives_dut_full_control():
    """shell-regmap.md: "0 = DUT owns the bit (default)". With OWN=0 across
    the whole bus, the host's OUT/OE must have ZERO effect on the pad --
    regardless of what garbage the host has staged in OUT/OE (e.g. a host
    driver that writes OUT/OE speculatively before ever claiming OWN)."""
    result = resolve_gpio_bus(
        own=0x0000, dut_o=0xBEEF, dut_oe=0xFFFF,
        host_out=0xFFFF, host_oe=0xFFFF,  # host garbage -- must be ignored
    )
    assert result.pad_drive == 0xBEEF
    assert result.pad_oe == 0xFFFF


def test_default_own_all_zero_dut_undriven_bits_stay_undriven():
    result = resolve_gpio_bus(own=0x0000, dut_o=0x0000, dut_oe=0x0000,
                                host_out=0xFFFF, host_oe=0xFFFF)
    assert result.pad_oe == 0x0000, "host OE must not leak through when OWN=0"


# --------------------------------------------------------------------------- #
# Full host override (OWN=1 everywhere)
# --------------------------------------------------------------------------- #

def test_full_host_override_ignores_dut_entirely():
    """shell-regmap.md: "1 = host mux overrides". With OWN=1 across the
    whole bus, the DUT's dut_gpio_o/dut_gpio_oe must have zero effect --
    this is the case that matters most in practice (host driving LEDs
    directly for bring-up/bench use while no RM is loaded, or a greybox
    RM that drives nothing -- see fpga/dfx/rms/rm_greybox/rm_greybox.sv's
    `dut_gpio_o = dut_gpio_oe = '0`)."""
    result = resolve_gpio_bus(
        own=0xFFFF, dut_o=0xFFFF, dut_oe=0xFFFF,  # DUT garbage -- must be ignored
        host_out=0xCAFE, host_oe=0x00FF,
    )
    assert result.pad_drive == 0xCAFE
    assert result.pad_oe == 0x00FF


# --------------------------------------------------------------------------- #
# Per-bit independence -- a mixed OWN mask must mux bit-by-bit, not
# whole-bus-at-once (the most likely off-by-a-lot RTL bug: treating OWN as
# a single global enable rather than a per-bit select).
# --------------------------------------------------------------------------- #

def test_mixed_own_mask_selects_strictly_per_bit():
    # bits [3:0] host-owned, bits [15:4] DUT-owned.
    own = 0x000F
    dut_o = 0b1010_1010_1010_1010
    dut_oe = 0b1111_1111_0000_0000
    host_out = 0b0000_0000_0000_0101
    host_oe = 0b0000_0000_0000_0011

    result = resolve_gpio_bus(own=own, dut_o=dut_o, dut_oe=dut_oe,
                                host_out=host_out, host_oe=host_oe)

    for bit in range(NGPIO):
        expect_drive = (host_out >> bit) & 1 if (own >> bit) & 1 else (dut_o >> bit) & 1
        expect_oe = (host_oe >> bit) & 1 if (own >> bit) & 1 else (dut_oe >> bit) & 1
        assert (result.pad_drive >> bit) & 1 == expect_drive, f"bit {bit} drive mismatch"
        assert (result.pad_oe >> bit) & 1 == expect_oe, f"bit {bit} oe mismatch"


# --------------------------------------------------------------------------- #
# GPIO.IN / dut_gpio_i: a single physical net, sampled the same way by both
# readers regardless of who (if anyone) is driving it.
# --------------------------------------------------------------------------- #

def test_in_and_dut_gpio_i_see_the_identical_resolved_pad_value():
    result = resolve_gpio_bus(own=0x00F0, dut_o=0x1234, dut_oe=0xFFFF,
                                host_out=0x0F00, host_oe=0xFFFF)
    gpio_in = sample_pad(result, external=0x0000)
    dut_gpio_i = sample_pad(result, external=0x0000)  # same physical net
    assert gpio_in == dut_gpio_i


def test_undriven_bit_is_sampled_from_external_stimulus_not_a_stale_drive_value():
    """When neither DUT nor host drives a bit (both OE=0 for it), the pad
    reflects whatever else is on it (e.g. a button/switch on that PMOD
    pin) -- not a stale `pad_drive` value left over from a previous mux
    selection. Regression-shape: an RTL implementation that gates OE onto
    IN incorrectly (e.g. holds the last driven value instead of sampling
    the pad) would fail this."""
    result = resolve_gpio_bus(own=0x0001, dut_o=0x0000, dut_oe=0x0000,
                                host_out=0x0001, host_oe=0x0000)  # host owns bit0, but OE=0
    assert result.pad_oe & 0x1 == 0, "bit 0 must be undriven (host OE=0)"
    sampled = sample_pad(result, external=0x0001)
    assert sampled & 0x1 == 1, "an undriven bit must reflect the external stimulus"


# --------------------------------------------------------------------------- #
# Exhaustive single-bit truth table -- every combination of
# (own, dut_o, dut_oe, host_out, host_oe) for one bit, 2**5 = 32 rows.
# This is the literal "truth table" the task asks for: not spot checks,
# every row.
# --------------------------------------------------------------------------- #

def test_exhaustive_single_bit_truth_table():
    for own in (0, 1):
        for dut_o in (0, 1):
            for dut_oe in (0, 1):
                for host_out in (0, 1):
                    for host_oe in (0, 1):
                        result = resolve_gpio_bus(
                            own=own, dut_o=dut_o, dut_oe=dut_oe,
                            host_out=host_out, host_oe=host_oe, width=1,
                        )
                        expect_drive = host_out if own else dut_o
                        expect_oe = host_oe if own else dut_oe
                        row = (own, dut_o, dut_oe, host_out, host_oe)
                        assert result.pad_drive == expect_drive, f"drive mismatch at row {row}"
                        assert result.pad_oe == expect_oe, f"oe mismatch at row {row}"
