# uart_over_eth/

Relays the DUT's UART0 (boot monitor), UART1 (application), and SWO/ITM
trace AXI-Stream taps at the partition boundary to three raw TCP sockets
(6930/6931/6932) — ARCHITECTURE_SPEC.md §11.

## Design

nanosoc's UART is `cmsdk_apb_usrt` with AXI-Stream byte TX/RX already (per
§11 "FPGA side"), so this module does not implement a UART peripheral
itself — it is a byte-shovel between an AXI-Stream FIFO (RX: DUT->shell,
TX: shell->DUT) and a TCP socket, kept in the static shell so console
sessions "survive DUT swaps (streams simply re-establish)" per §11.
SWO/ITM is handled identically (raw byte relay; §11 explicitly allows
leaving ITM undecoded and pushing decode to host-side tooling — this
skeleton does not decode ITM).

Three near-identical instances (`uart_over_eth_uart0`, `_uart1`, `_swo`)
share one relay implementation parameterized by port + stream register
base, rather than three copies of the same logic.

## Register block — RESOLVED (shell-regmap.md v0.1, I7)

`shell-regmap.md` v0.1 adds ONE UARTBR block (`MPS3_UARTBR_BASE` =
0x44A9_0000) covering all three streams as sub-offsets —
`UARTBR_U0_TXRX`/`UARTBR_U1_TXRX`/`UARTBR_SWO_RX` (each `[7:0]` data +
`[8]` valid, `UARTBR_DATA_MASK`/`UARTBR_VALID`) plus `UARTBR_FIFO_STATUS`
— rather than three separate 64 KiB pages as the earlier
AMBIGUITY(A6) #3 placeholders (`MPS3_UART0_STRM_BASE` etc.) assumed.
`uart_over_eth_stream_state_t` now carries a per-stream `data_off` into
that one block instead of a per-stream base address; see
`platform_regs.h`'s UARTBR section for the exact bit layout (the
`FIFO_STATUS` per-stream tx_full/rx_empty bit positions are still a
firmware-chosen layout, not contract-fixed — confirm against
`fpga/shell/ip/uart_bridge` once it exists).

## Gating during a swap

`g_shell_state.uart_gated` is set by `coordinator/swap_fsm.c`'s
`SWAP_GATE`; `uart_over_eth_poll()` should stop draining/pushing FIFOs
while gated (the RP is decoupled — nothing coherent to read/write) but
should NOT close the TCP sockets, per §6.3 "streams simply re-establish"
(the *client* reconnects if needed, but the FPGA-side socket surviving the
swap is what makes "simply re-establish" cheap rather than requiring a
fresh TCP handshake every time).

## Linux policy (`-DMPS3_UART_LINUX_POLICY`, mps3-harnessd only)

The bare-metal image is frozen at v0.11 and is built without this flag: it
relays exactly as described above, and `test_uart_over_eth` pins it. Two
silicon findings from the ILA mint (2026-09-24, #16/#17) made the Linux
harness choose a different policy:

| | bare metal (v0.11) | mps3-harnessd |
|---|---|---|
| No client on a port | FIFO not read: the DUT's UART back-pressures (`tready=0`, seen for 60 s on the ILA) and the backlog waits for the next client | **drain and drop**: ≤ 256 pops per poll per stream, counted; the first drop of a no-client period and the total at the next connect are logged |
| Bytes in the bridge at a swap | carried across: the next client read 16 bytes of `rm_uart_echo rea…` before nanoSoC's boot line | **flushed** on the first poll after `uart_gated` falls: `rx_hold` + the DUT→host FIFO, all three streams, counted and logged |
| Host→DUT pacing | none | optional `--uart-pace-ms N` (default 0 = off): ≥ N ms between bytes on U0/U1 |

The flush cannot eat the NEW RM's first bytes: the swap FSM holds
`dut_resetn` from DECOUPLE_ASSERT onwards and only the host's `reset dut`
(after the swap has answered) releases it, and an RM's console runs on
`dut_resetn`. An RM that printed on `rp_resetn` would lose what it emitted
between RELEASE and DONE (a few ms).

No wire key carries the counts (net_proto.c is shared and frozen);
`uart_over_eth_dropped()` / `_flushed()` hold them and the harness log
records them. Tests: `firmware/test/test_uart_over_eth_linux.c` (built with
and without the flag: the bare-metal build reproduces both failures), and
harnessd's e2e `test_uart_*`.
