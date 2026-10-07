# tests/rmii_speed — does the shipped RMII bridge run at the speed the PHY negotiates?

**Verdict: no.** The multicore integration clocks `rmii_to_mii` for **10 Mb/s** while the
firmware makes the PHY negotiate **100 Mb/s**. Reproduced here in simulation, with a
positive control.

Pure-VCS SystemVerilog bench (no cocotb), same shape as `tests/rmii_conformance/`.
Not wired into repo-root `make check` — run it explicitly.

```
source set_env.sh
make -C tests/rmii_speed          # positive control + defect test
make -C tests/rmii_speed result   # regenerate RESULT.txt
```

## What it does

Drives a real MII/RMII frame — 7× `0x55` preamble, `0xD5` SFD, then a 16-byte payload —
into the bridge's RMII RX port at **100 Mb/s** (one di-bit per 50 MHz `REF_CLK`), and
checks what emerges on its MII RX port. The receiver hunts for the SFD nibble pair
exactly as a MAC does, so a one-nibble offset cannot silently corrupt the result.

## Results (VCS 2022.06-SP2, bridge md5 `5e38c43c`)

| `mode_speed` | `mrx_clk` period | nibbles seen | SFD found | payload recovered | verdict |
|---|---|---|---|---|---|
| `1` (correct for 100 Mb/s) | **40.0 ns** (25 MHz) | 48 | yes, at 16 | **16/16** | PASS |
| `0` (what the bitstream ships) | **400.0 ns** (2.5 MHz) | **5** | never | **0/16** | FAIL |

At the 10 Mb/s divider the bridge samples about a tenth of the stream and never
synchronises. That is the defect.

## This is a falsification test

`make defect` **expects** `mode_speed=0` to fail. If it ever passes, the reasoning in
`docs/LAN8720_BRINGUP_WORK_ITEM.md` §5 is wrong, and the Makefile says so and exits
non-zero rather than quietly agreeing with itself.

`make pos` is the control: if `mode_speed=1` cannot recover the frame, the *bench* is
broken and nothing else it says can be trusted. (It was, on the first run — the
stimulus had no preamble, so the receiver had no way to align. The control caught it.)

## Why the existing bench cannot answer this

There are at least three divergent copies of `rmii_to_mii.v` in this lab, and they are
**not interchangeable**:

| md5 | `mode_speed` port? | who compiles it |
|---|---|---|
| `5e38c43c` | **yes** | what `nanosoc-multicore-system` **ships** — this bench |
| `5dd9b16f` | no — *"Removed the mode_speed input — hard-wired to 100 Mbps operation"* | `tests/rmii_conformance/` |
| `91e33182` | no | other trees |

So `tests/rmii_conformance` — which closed I22 — compiles a bridge that **cannot** exhibit
this defect. Its bit/nibble-order conformance result stands; it simply never touched the
speed divider. This Makefile **refuses to run** against a copy lacking the port, and prints
the expected md5, because benching an orphan copy is how you get a green result for RTL
nobody builds.

## Evidence from the shipped build (not from this bench)

- `WARNING: [Synth 8-7071] port 'mode_speed' of module 'rmii_to_mii' is unconnected for
  instance 'u_rmii_bridge'` —
  `nanosoc_multicore_project.runs/..._ip_0_0_synth_1/runme.log:130`
- `speed_div_reg[3:0]` survives into the **routed** netlist. It could not, had `mode_speed`
  resolved to `1`: `rmii_to_mii.v:145` would hold the counter at zero and Vivado would
  optimise it away. So Vivado tied the undriven input to `0`.
- `rmii_to_mii.v:152` — `tick = mode_speed | (speed_div == 4'd0)`; header: *"1 = 100 Mbps,
  0 = 10 Mbps"*.
- Firmware writes `ANAR = 0x0181` (`ptp_slave/main.c:178`) = 100BASE-TX + 100BASE-TX-FD.
  **10BASE-T is not advertised**, despite the code comment saying it is. So the PHY
  negotiates 100 Mb/s.

## What this bench does NOT prove

- Nothing about the **MPS3** failure. That is a separate, already-diagnosed fault: the SH0
  level shifter passes FPGA→PHY but is dead PHY→FPGA. See
  `docs/LAN8720_BRINGUP_WORK_ITEM.md` §2. This bench is about the *multicore integration*,
  which would still be wrong once the board fault is fixed.
- Nothing about the **standalone** subsystem, which wires `mode_speed` correctly via
  `ethmac_ahb_rmii.v:54,87`. That is the path the working PYNQ-Z2 result comes through.

## Fix

Instantiate `ethmac_ahb_rmii` instead of `rmii_to_mii` directly, or drive `mode_speed` from
the MAC's negotiated speed (`eth_speed_ctrl.v` exists for this). Then re-run — `make defect`
should be retired and replaced with a sweep that passes at both speeds.
