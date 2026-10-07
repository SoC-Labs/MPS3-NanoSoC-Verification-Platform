# DUT notes — `uart_axis_shim`

RTL: `fpga/rp/nanosoc/uart_axis_shim.sv`, module `uart_axis_shim`
(params `CLK_HZ`, `BAUD`). NEW leaf bench (A5 verification-confidence pass) —
this real sequential logic (two 8N1 UART state machines + baud generators)
had **zero** verification before this bench.

Port list (confirmed by reading the RTL):
- `clk`, `resetn` — dut_clk domain, active-low reset.
- `dut_txd_i` (I) / `dut_rxd_o` (O) — the raw serial pins wired to nanosoc's
  CMSDK UART2 GPIO (P1[5]=TXD out of the DUT, P1[4]=RXD into the DUT).
- `uart_tx_tdata[7:0]`/`tvalid`/`tready` (O/O/I) — DUT->host console bytes,
  i.e. the shim's UART **receiver** output (partition-pins.md console group).
- `uart_rx_tdata[7:0]`/`tvalid`/`tready` (I/I/O) — host->DUT console bytes,
  i.e. the shim's UART **transmitter** input.

## Bench notes

- **Baud divisor override.** `DIV = CLK_HZ/BAUD`. The Makefile overrides
  `CLK_HZ=800000`/`BAUD=100000` so `DIV=8` clk cycles per bit; the real
  25 MHz/115200 default is `DIV=217` (~2170 cycles/byte — too slow to sim).
  `DIV` in `test_uart_axis_shim.py` MUST track that override.
- No AXI-Lite here, so no SVA protocol checker is bound; the Makefile still
  `include`s `../common/bench_common.mk` for the `COVERAGE=1` toggle.
- The round-trip test loops `dut_rxd_o` back into `dut_txd_i` in the bench so
  a single byte exercises the serializer AND the deserializer end-to-end, and
  holds `uart_tx_tready` low to prove the single held byte survives
  back-pressure (the shim has one rx_byte register, no RX FIFO — a second
  byte arriving un-consumed would overwrite it, so back-pressure is tested
  one byte at a time).

## Flagged for the RTL owner / A6

- The RX path accepts a byte regardless of the stop-bit value (no
  framing-error signalling) — documented "bring-up scope" in the RTL header,
  not a bug, but a real UART would surface it. Not asserted here.
- `CLK_HZ`/`BAUD` are still open (PLATFORM_MAPPING §"Gaps/decisions for A6"
  item 8: 100 MHz vs 25 MHz vs 50 MHz dut_clk) — the shim's AXIS handshake is
  correct at any DIV, but the real divisor must be fixed before hardware.
