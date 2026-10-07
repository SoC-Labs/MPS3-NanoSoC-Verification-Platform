# smsc911x/ — bare-metal LAN9220 driver (W-SMSC)

**Code is here now** (`smsc911x.h`/`smsc911x.c`): the LAN9220
host-interface driver, **register sequences ported from Zephyr's
`drivers/ethernet/eth_smsc911x.c` (Apache-2.0)** and re-expressed against
this repo's HAL — no Zephyr code copied verbatim, no Zephyr device-model /
`net_pkt` / IRQ dependency retained. **Licensing-audit note (A6): the
Apache-2.0 derivative provenance applies to `smsc911x.{h,c}`** and is
recorded in both file headers.

What's real: BYTE_TEST/ID_REV sanity, soft reset, PMT/E2P readiness, FIFO
sizing + AFC, MAC-CSR indirection, internal-PHY MII access (reset, autoneg
restart, link status), MAC-address programming, **promiscuous** + TX/RX
enable (§8.3), single-segment TX command framing, RX status/data draining
with fail-closed drop paths (error frame / frame-larger-than-buffer both
consume-and-drop, never truncate). Poll model (interrupts masked) per
point 5 below. Exercised by `firmware/test/test_smsc911x.c` against
`firmware/test/fake_lan9220.c`'s register-level chip model.

Still seamed: the register **base** is an `smsc911x_init()` runtime
argument (an A1 BD/`xparameters.h` detail — point 1 below), and the
**pbuf glue** (the lwIP `ethernetif` wrapper mapping pbuf chains onto
`smsc911x_tx_frame`/`smsc911x_rx_frame`) is the same deliberately-unwritten
one-thin-file TODO as `common/net_if.h`'s lwIP backend.

The porting rationale/notes below are kept as originally written — they
were the porting spec, and points 1–5 describe what the port did.

## Why Zephyr over Linux (ARCHITECTURE_SPEC §12 reuse table / §12 licensing)

Three reference drivers exist for the LAN9220 (`smsc911x`):
Zephyr's `eth_smsc911x.c`, the Arm MPS3 SMM's own driver, and Linux's
`smsc911x`. §12 explicitly prefers the Apache-2.0 Zephyr driver over
GPL-licensed Linux for this reason: this firmware is a small, standalone,
non-copyleft bare-metal image with no other GPL dependency anywhere in the
stack (lwIP is permissive, the Vitis BSP is Xilinx's own license, HWICAP/
DFX/AXI Quad SPI are Vivado IP) — pulling in a GPL driver would put the
whole firmware image under GPL's linking obligations for no benefit, when
a permissively-licensed equivalent already exists. The Arm MPS3 SMM driver
is also a reasonable reference (same board family) but is Arm-licensed
reference code, not a clean permissive base to port from directly.

## Porting shape (bare-metal MicroBlaze, no Zephyr device model)

Zephyr's `eth_smsc911x.c` is written against Zephyr's `device`/`net_if`
abstractions (devicetree-bound register base, Zephyr's net_pkt buffers,
Zephyr's IRQ API). None of that exists on bare-metal MicroBlaze + lwIP.
The port strips those layers down to:

1. **Register access** — replace Zephyr's devicetree-derived base address
   + `sys_read32`/`sys_write32` with a MicroBlaze-local base (from the
   Vitis-generated `xparameters.h`, wherever A1's block design maps the
   AXI EMC / SMC master onto the LAN9220's static-memory-bus interface —
   *not* one of the `common/platform_regs.h` blocks, since LAN9220 is a
   board peripheral behind the static-memory bus per ARCHITECTURE_SPEC
   §4.1 "LAN9220 host I/F," not an AXI4-Lite shell block; confirm the exact
   base with A1 once the BD exists).
2. **Init sequence** — LAN9220 chip-ID readback, PHY reset/bring-up (the
   LAN9220 has its own internal PHY; separate from this project's *virtual*
   PHY on the DUT-facing RMII side, §8), MAC address programming, TX/RX
   FIFO configuration — this sequence carries over near-verbatim from
   Zephyr's driver logic, just re-expressed against raw register writes
   instead of `sys_write32(dev_cfg->base + REG, val)`.
3. **TX/RX path** — Zephyr's `net_pkt` scatter/gather becomes a plain
   byte-buffer copy into/out of lwIP's `pbuf` chain, driven from
   `coordinator_main_loop()`'s `xemacif_input()` call (RX) and lwIP's
   `netif->linkoutput` callback (TX) — i.e. this becomes the bottom half
   of a standard lwIP `ethernetif.c`, with the LAN9220-specific TX/RX FIFO
   push/pop copied in from the Zephyr driver's equivalent functions.
4. **Promiscuous mode** — ARCHITECTURE_SPEC §8.3: "LAN9220 must be
   promiscuous to carry the DUT's MAC address" (two MACs share one PHY
   port — the management stack's and the DUT's, learned upstream). Zephyr's
   driver has a promiscuous-mode config bit; confirm it's exposed and set
   it at init, since normal Zephyr usage may not exercise that path.
5. **IRQ vs poll** — Zephyr's driver is interrupt-driven; a first bare-metal
   pass may poll the LAN9220's interrupt-status register from the main loop
   instead of wiring a real MicroBlaze interrupt controller entry, to keep
   the first bring-up simpler (matches this firmware's "everything is a
   polled `_poll()`" shape elsewhere) — revisit if polling proves too slow
   for 10/100 line rate.

## Not addressed here

No AXI-EMC/SMC timing parameters, no register offset table, no MAC address
source (burned-in EEPROM on LAN9220 vs firmware-assigned) — these need the
actual Zephyr source file in hand during the real port, plus the LAN9220
datasheet for register offsets, neither reproduced here.
