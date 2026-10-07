# MicroPython REPL demo in the MPS3 harness — status + bench runbook

**What this is:** a live MicroPython REPL running on the real single-core NanoSoC
(Cortex-M0), delivered as a conformant DFX reconfigurable module in the existing
MPS3 harness. Type Python at a prompt; poke registers; light the board LEDs.

**Status when written (2026-07-15): proven in simulation, ready to bitstream** —
the interpreter boots to a REPL and evaluates Python on the real SoC RTL.
**The "not yet on silicon" caveat is retired:** MicroPython now cold-boots on
the real board from on-board QSPI flash with an empty instruction memory
(`docs/STATUS.md`, "Flash boot"). The simulation evidence below still stands
as the bring-up record; the bench steps are kept for reproducing the run.

All work is in the working tree, **no commits** — stage for review per the repo's
"never `git add -A`" rule (the CLCD-KVM track shares this tree).

---

## What is proven, and how

| Claim | Proof | Where |
|---|---|---|
| MicroPython boots to a REPL on the real NanoSoC RTL | full-SoC cocotb co-sim, banner decoded off the UART2 pad | `tests/micropython_boot` (`TESTS=1 PASS=1`) |
| ...and evaluates Python | `print(1+1)` driven into RXD → `2` on TXD | `make -C tests/micropython_boot UPY_GATE2=1` |
| The RM drops into the LIVE harness at zero cost | `pin_check` 9/9 — boundary-identical to the frozen contract | `make -C fpga/dfx pin-check` |
| `static_id` unchanged, all 8 overlays stay valid | same 30-signal boundary; no shell rebuild | boundary is `rp_nanosoc_wrapper` verbatim |
| The RM synthesises | OOC synth clean, 60.5 BRAM (fat IMEM+DMEM) | `make -C fpga/dfx rm-nanosoc-upy-dcp` |
| rm_id is unique + consistent | `0x01000005`, wrapper/list/manifest agree | `check_rm_id_encoding.py` |

The banner, decoded byte-for-byte off `p1_out[5]` (UART2 TXD, 8N1, 651 dut_clk/bit):

```
~BQqI=18010000,0000CBC1 V=1800FC00,08000189 M=00000001 W=18010000,0000CBC1 RMicroPython 532428cc6d on 2026-07-14; SoC Labs NanoSoC (Cortex-M0) with cortex-m0
>>> print(1+1)
2
>>>
```

The `~BQqI=…M=…W=…R` prefix is stage-0's own boot telemetry and it corroborates
the boot: `I=18010000,0000CBC1` is the bootrom reading the image's SP and reset
vector; `M=00000001` is REMAP taking; `R` precedes the jump. Then MicroPython runs.

---

## The pieces

- **Interpreter**: `firmware/micropython/` — ports/minimal retargeted to armv6-m /
  Cortex-M0. 65.3 KB text. Polled UART2 driver; real `machine.mem8/16/32` (so the
  register/LED demo needs no custom code). Rebuild: `make -C firmware/micropython`
  → `build/micropython_word.hex`.
- **The RM**: `fpga/rp/nanosoc_upy/` — a thin wrapper that INSTANTIATES the stock
  `rp_nanosoc_wrapper` with `IMEM_RAM_ADDR_W=17` (128 KB) / `DMEM_RAM_ADDR_W=16`
  (64 KB) and bakes the image in. Registered in `fpga/dfx/rm_list.tcl` as
  `rm_nanosoc_upy` (design_id 0x0005).
- **Boot proof bench**: `tests/micropython_boot/`.

### This is a SCAFFOLD, not the product

The 128 KB IMEM is fat BRAM — an FPGA-only convenience (44 extra tiles of 2160).
Silicon cannot reproduce it. The **product** path for code that does not fit in
16 KB is XiP from external QSPI flash (`qspi_mem` @ 0x7000_0000), which keeps IMEM
at 16 KB on sim/FPGA/ASIC alike and is proven at the SoC level in `tests/qspi_xip`.
The MicroPython port carries over to the product RM unchanged — only the linker
script moves code from IMEM to the XiP aperture. Retire this scaffold once the
QSPI RM boots. See the one-config roadmap.

The QSPI controller IS present in this RM (the SoC instantiates it unconditionally
now — every nanosoc build pulls the ahb_qspi + CG092 IP); it simply has no external
device on the boundary yet, so its pads are tied off and this RM boots from IMEM.

---

## To take it to the board (the step I could not do — no board access)

1. **Build the overlay bitstream.** The scaffold's OOC checkpoint is staged
   (`make -C fpga/dfx rm-nanosoc-upy-dcp`). Add it to the locked static WITHOUT
   re-minting `static_id`, then generate the overlay — mirror the
   `add-rm-…`/`overlays` flow the other RMs use (an `add-rm-nanosoc-upy` target
   still needs writing; the OOC dcp is the input). Multi-hour P&R.
2. **Load it** over the existing swap path (JTAG/XVC or OTW), same as any overlay.
   The shell reads back `rm_id == 0x01000005` to confirm the right RM landed.
3. **Console → mpremote.** In the DFX flow the DUT console is UART AXI-Stream →
   `uart_bridge` → hub TCP, NOT a native tty. Bridge it to a pty:
   `socat pty,link=/dev/ttyUPY,raw,echo=0 TCP:<hub>:<uart-port>` then
   `mpremote connect /dev/ttyUPY`. (The monolithic flow would give a native
   FT4232 tty, but that is outside the harness.)
4. **Demo**:
   ```python
   >>> from machine import mem32
   >>> mem32[0x40010010] = 0xFF     # GPIO0 OUTENSET: P0[7:0] outputs
   >>> mem32[0x40010004] = 0x55     # DATAOUT -> alternating board LEDs
   >>> (mem32[0x40010000] >> 8) & 0xFF   # read the DIP switches
   ```
   A register write typed at a Python prompt, and a physical LED changes —
   through the RP boundary and the board_gpio pad mux.

Baud note: the console is **76800 8N1** (BAUDDIV 651 @ the shell's 50 MHz dut_clk),
matched to `uart_axis_shim`. Not 115200.

---

## Reproduce the sim proof

```sh
source set_env.sh
make -C firmware/micropython                 # build the image (if not present)
make -C tests/micropython_boot               # GATE 1: boots to "MicroPython"
make -C tests/micropython_boot UPY_GATE2=1   # + GATE 2: print(1+1) -> 2
```

Slow by nature — each UART byte is 130 us of sim; the banner is ~14 ms sim
(~3 min wall). The bench streams every decoded byte with its sim-time and reports
exactly how far it got if the banner is not reached, rather than a bare fail.
