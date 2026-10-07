# `rp/eth_ss` — the rm_eth_ss Reconfigurable Module (RM) wrapper

**Status: FILLED, v1 (2026-07-07, A1).** The real standalone **AHB-MAC + PTP
subsystem** (`ethmac_subsystem_ahb`: OpenCores ethmac + HA1588 PTP +
eth_rx_cksum, from the read-only
`~/SoCLabs/nanoSoC-refactor/ethernet-subsystem-ahb` checkout) is
instantiated and wired to the partition-pin boundary as the platform's second
DUT (IMPLEMENTATION_PLAN.md Phase 5.5). `rm_id` is the real, permanent
constant `32'h0000_0002` (matches `fpga/dfx/rm_list.tcl`'s
`RM_LIB(rm_eth_ss,rm_id)`).

**OOC synth: PROVEN board-free.** Vivado 2024.1, `synth_design -mode
out_of_context -top rp_eth_ss_wrapper -part xcku115-flvb1760-1-c`: **0
errors, 0 critical warnings**, checkpoint + utilization report emitted — see
"D7 sizing datapoint" below.

Unlike `rm_nanosoc`, this DUT **actually has a MAC**: the contract's
RMII + MDIO group is REAL here, and `irq_out` carries a real interrupt.
v1 scope is a structurally-complete, OOC-synthesizable RM whose MAC comes
out of reset *configured and receiving* (see "AHB decision") — full
software-driven operation (TX descriptors, PTP timestamp draining, cksum
offload config) is a later phase.

## Layout

```
fpga/rp/eth_ss/
├── README.md               (this file)
├── rp_eth_ss_wrapper.sv     RM top: subsystem + rmii_to_mii + DMA SRAM + bring-up FSM
├── eth_ss_bringup.sv        AHB-Lite constant-programmer FSM (the "AHB decision") + 1 Hz PAUSE-frame TX beacon
├── eth_ss_ooc.xdc           socketed-XDC OOC timing constraints (partition-timing.md)
├── eth_ss_rm.xdc            RM-internal exceptions for reapply at DFX link (partition-timing.md)
├── XDC_VALIDATION.txt       before/after OOC timing evidence for the XDCs
├── filelist.tcl             OOC-synth file list (parses the source repo's own flist)
├── ooc_synth.tcl            OOC synth driver (reads eth_ss_ooc.xdc if present)
├── .gitignore               build/ + Vivado side files
└── build/                   (untracked) rm_eth_ss_synth.dcp, util_rm_eth_ss.rpt, logs
```

## Which checkout and why (source-repo survey)

Two candidate trees exist in this lab; **they are NOT interchangeable**:

| | `~/SoCLabs/nanoSoC-refactor/ethernet-subsystem-ahb` (**CHOSEN**) | `~/SoCLabs/ethernet-mac-ahb` (standalone) |
|---|---|---|
| Identity | `origin = soclabs/ethernet-subsystem-ahb.git`, HEAD `a7de238` (2026-05-30) — **the repo `rm_list.tcl` names** ("standalone AHB-MAC + PTP subsystem (ethernet-subsystem-ahb)") | `origin = soclabs/ethernet-mac-ahb.git`, HEAD `3246c90` (2026-04-23) — the MAC-only sub-project |
| `ethmac_subsystem_ahb.v` (the AHB slave wrapper this RM instantiates) | **Present** (`src/rtl/`) | **Absent** — the AHB subsystem wrapper only exists at the eth-ss level |
| `ethernet-mac-ahb` content | Submodule @ `037ac83` (**newer**): has `eth_rx_cksum.v`, patched `ptp_parser.v`, `rmii_to_mii` with `mode_speed` + `RMII_RX_ERR_FROM_DIBIT` | Older: **no `eth_rx_cksum.v`** — its `ethmac_subsystem_apb.v` is port-incompatible with the chosen wrapper (no `cksum_int_o`) and cannot build this RM |
| Proven FPGA precedent | `fpga/vivado_ip/eth_ss_vivado_wrapper.v` — the exact `rmii_to_mii` + subsystem wiring this RM mirrors | (n/a) |

`filelist.tcl` therefore resolves **everything** from the eth-ss checkout
(`ETH_SS_HOME`) and its **internal submodule** (`ETHMAC_AHB_HOME =
$ETH_SS_HOME/ethernet-mac-ahb`), never the standalone sibling. Vendor IP
(OpenCores EthMAC/HA1588, Arm CMSDK BP210) resolves read-only from
`$ARM_IP_LIBRARY_PATH` (set in tools.env; no default) — same env vars and
layout as the source repo's own `set_env.sh`.

**What was deliberately NOT wrapped:** the repo's full `ethernet_ss_ahb` SoC
(generated `build_soc/rtl/ethernet_ss_ahb.sv`: Cortex-M0+/M0 + bootrom +
IMEM/SRAM regions + UART + the MAC subsystem). This RM wants the **MAC in
operation, no CPU** — wrapping the SoC would drag in Arm core IP, a
generated boot ROM (build artifact), and a debug story the v0.1 boundary
doesn't need. The `ethmac_subsystem_ahb` level is exactly the MAC+PTP+cksum
cut the rm_list.tcl entry describes.

### Top module survey (`ethmac_subsystem_ahb`, src/rtl/ethmac_subsystem_ahb.v)

- **AHB-Lite slave** (register window): `HSEL/HADDR[31:0]/HTRANS/HWRITE/
  HSIZE/HBURST/HPROT/HWDATA/HMASTLOCK/HREADY` in, `HRDATA/HREADYOUT/HRESP`
  out → `cmsdk_ahb_to_apb` (ADDRWIDTH=16, 64 KB window) →
  `cmsdk_apb_slave_mux` on `paddr[15:12]`:
  `0x0000` OpenCores ethmac (regs `0x000-0x3FF`, BD SRAM `0x400-0x7FF`),
  `0x1000` HA1588 PTP, `0x2000` eth_rx_cksum.
- **AHB-Lite master** (frame DMA): the MAC's Wishbone master via
  `wb_to_ahb3lite` (`SWAP_DMA_BYTES` passthrough).
- **MII PHY interface** (4-bit 25 MHz TX+RX + mcoll/mcrs) — **the core is
  MII, not RMII**; see "Clocking" for the bridge.
- **MDIO** master (`md_pad_i/mdc_pad_o/md_pad_o/md_padoe_o`,
  active-high output-enable).
- **Interrupts**: `int_o` (MAC, per INT_MASK), `cksum_int_o` (level, all
  enables reset to 0 → benign unconfigured).
- **PTP**: `rtc_clk` input + `rtc_time_ptp_ns/sec`, `rtc_time_one_pps`
  bond-outs; HA1588 hardware-servo group (needs an external PHC — none
  here, disabled inert).

## Directions — mirror image of the shell's view

Identical port list (names/widths/directions **and order**) to
`rm_greybox.sv`, `rm_led.sv` and `rp_nanosoc_wrapper.sv` — mechanically
diffed against both `rm_greybox` and `rp_nanosoc_wrapper` (30 ports +
`parameter int NGPIO = 16`): exact match. See
`fpga/rp/nanosoc/README.md` "Directions — mirror image of the shell's view"
for the line-by-line derivation from `partition-pins.md` v0.1.

## The AHB decision — who drives the AHB slave port?

The partition boundary carries **no AHB** (contract line 8), so nothing
outside this RM can ever program the MAC. Options considered:

- **(a) Tie the slave port idle** (HREADY=1, IDLE): structurally valid,
  functionally dead — the MAC powers up with `MODER.RXEN/TXEN=0` and the
  platform's "MAC-in-operation" purpose is deferred entirely.
- **(b) RM-internal one-shot constant-programmer FSM** (**CHOSEN**):
  `eth_ss_bringup.sv`, a ~100-cell AHB-Lite write engine that walks a fixed
  13-write bring-up sequence once after reset, then re-writes one register
  once a second to make the MAC transmit (see "Transmit beacon" below). The register map was clear
  enough to commit to (b): offsets/fields read from
  `ethernet-mac-ahb/src/rdl/ethmac_regs.rdl` + `ha1588_patches/reg.v`, and
  the RX-BD flag bits cross-checked against `eth_wishbone.v` itself
  (`ram_do[15]`=EMPTY, `[14]`=IRQ, `[13]`=WRAP).

Paired with (b), the MAC's **frame DMA lands in an RM-internal 8 KB AHB
SRAM** (`sl_ahb_sram` from the source repo's fpga_lib = `cmsdk_ahb_to_sram`
+ `cmsdk_fpga_sram`) so received frames genuinely complete: descriptor
recycles (WRAP), `INT_SOURCE.RXB` sets, `int_o` → `irq_out` fires. Result:
after a swap, the shell's virtual PHY can send a frame at the DUT and
observe a real `irq_out` edge — no software anywhere.

### The bring-up write sequence (all 32-bit writes, offsets in the 64 KB window)

| # | Addr | Value | Register | Why |
|---|------|-------|----------|-----|
| 0 | `0x0020` | `0x0000_0001` | TX_BD_NUM | 1 TX BD (BD0 @0x400); RX ring starts BD1 @0x408. Satisfies both MODER gates (RXEN needs <0x80, TXEN needs >0) |
| 1 | `0x000C` | `0x0000_0015` | IPGT | recommended back-to-back IPG for full-duplex |
| 2 | `0x0040` | `MAC_ADDR0_VAL` (`0x454C_5302`) | MAC_ADDR0 | station address octets 1-4 (see note below) |
| 3 | `0x0044` | `MAC_ADDR1_VAL` (`0x0000_3253`) | MAC_ADDR1 | station address octets 5-6 |
| 4 | `0x0008` | `0x0000_001F` | INT_MASK | unmask TXB\|TXE\|RXB\|RXE\|BUSY → real `irq_out` activity |
| 5 | `0x040C` | `0x0000_0000` | RX BD1 word1 | frame-buffer pointer = DMA SRAM offset 0 (pointer BEFORE handing the BD to the MAC) |
| 6 | `0x0408` | `0x0000_E000` | RX BD1 word0 | EMPTY\|IRQ\|WRAP — one self-recycling RX descriptor |
| 7 | `0x1020` | `RTC_PERIOD_NS` (40) | PTP RTC period (int ns) | 40 ns ⇒ assumes 25 MHz `rtc_clk` (= `dut_clk`) — **A6 flag #1** |
| 8 | `0x1024` | `RTC_PERIOD_FRAC` (0) | PTP RTC period (frac ns) | 8.32 fixed-point with #7 |
| 9 | `0x1000` | `0x0000_0004` | PTP RTC ctrl | `period_ld` (bit2) — load strobes are rising-edge-detected after a 3-FF CDC, hence set… |
| 10 | `0x1000` | `0x0000_0000` | PTP RTC ctrl | …then clear (inter-write gap ≥ 8 cycles guarantees the level crosses) |
| 11 | `0x0000` | `0x0001_A423` | MODER | RECSMALL\|PAD\|CRCEN\|FULLD\|PRO\|TXEN\|RXEN — enable after every configuration register, MAC wakes fully configured |
| 12 | `0x0024` | `0x0000_0004` | CTRLMODER | TXFLOW only (2026-09-23): arms the MAC's PAUSE-frame transmitter. RXFLOW/PASSALL stay 0, so received PAUSE frames are still ignored |
| then | `0x0050` | `0x0001_0000 \| seq` | TXCTRL | TXPAUSERQ \| pause-time = seq (1, 2, 3 …): one PAUSE frame per write. Written at once, then every `REARM_CYCLES` (50,000,000 = 1 s at 50 MHz `dut_clk`) |

MODER bit derivation (RDL bit order, LSB up): RXEN(0)+TXEN(1)+PRO(5)+
FULLD(10)+CRCEN(13)+PAD(15)+RECSMALL(16) = `0x1A423`. Promiscuous because
v1 has no host to program a filter for; full-duplex because the shell's
virtual PHY is a 100M FD RMII endpoint (no collisions). PTP after config,
before MODER: the RTC free-runs from 0 at the programmed rate
(`rtc_time_one_pps` ticks 1 Hz — RM-internal in v0.1).

**MAC address note:** `0x454C_5302`/`0x0000_3253` was meant to encode
`02:53:4C:45:53:32` per the **RDL's** convention (ADDR0 low byte = first
octet). **Settled 2026-09-23 by simulation, and the RDL reading is wrong for
this MAC:** the PAUSE frames this RM now sends carry the source address
`32:53:45:4C:53:02` — `{MAC_ADDR1[15:0], MAC_ADDR0}` sent most significant
byte first, as `eth_transmitcontrol.v` does (`MAC[47:40]` first), and read
back byte for byte at DUTEGR in `tests/eth_ss_bringup` `ARM=tx`. It is still a
valid unicast, locally-administered address (first octet `0x32`: bit 0 = 0,
bit 1 = 1), so it was left as is. Under `PRO=1` it matters only as that
source address; re-derive the parameters `MAC_ADDR0_VAL`/`MAC_ADDR1_VAL` from
the OpenCores packing before any non-promiscuous use.

### Transmit beacon (2026-09-23)

After the bring-up the FSM does not park. It writes `TXCTRL` =
`TXPAUSERQ | seq` once, then again every `REARM_CYCLES` clocks (default
50,000,000: 1.0 s at the shell's 50 MHz `dut_clk`; 1.33 s if `dut_clk` is ever
retuned to 37.5 MHz, and so on — the period is in cycles, not seconds). Each
write makes the MAC send exactly one IEEE 802.3x PAUSE frame, and the MAC
clears the request bit itself once the frame is queued:

| field | bytes | value |
|---|---|---|
| dst | 0-5 | `01:80:C2:00:00:01` (MAC-control multicast) |
| src | 6-11 | `32:53:45:4C:53:02` (the station address, see the MAC address note) |
| EtherType | 12-13 | `0x8808` (MAC control) |
| opcode | 14-15 | `0x0001` (PAUSE) |
| pause time | 16-17 | `seq`, big-endian: 1, 2, 3 … (16 bits, wraps) |
| pad | 18-59 | 42 × `0x00` (MODER.PAD) |
| FCS | 60-63 | CRC-32 (MODER.CRCEN) |

64 bytes with FCS, 72 on the wire with preamble.

**Why a PAUSE frame and not a frame of our own.** A normal frame needs a
ready TX buffer descriptor pointing at the frame's bytes in memory the MAC's
TX DMA reads. That memory is the 8 KB `sl_ahb_sram` on the MAC's DMA master
bus, and the FSM's only bus goes to the subsystem's register slave port,
which cannot reach it. Loading the SRAM would need a wrapper change (an
AHB mux on the DMA bus, or an initialised SRAM). The PAUSE generator
(`eth_transmitcontrol.v`) builds its frame from registers, so it is the one
frame this MAC can send with no CPU and no memory. The pause time carries the
sequence number, so consecutive frames differ and a stale frame cannot pass
for a new one.

**Where it goes.** The shell's `eth_bridge_3port` floods a multicast
destination to every port except the one it came in on: the management
port, which is `DUTEGR`'s capture FIFO, and the LAN9220 uplink, which the
shell safe-ties (drained, it reaches no wire). `link_partner_mac` does not act
on PAUSE frames, so the non-zero pause time pauses nothing in the shell.
**If the uplink is ever connected to the LAN9220**, these frames must be
dropped at it (802.1D: `01:80:C2:00:00:0x` is link-local and a bridge must
not forward it), or the lab switch will honour a pause of up to 65,535 quanta
(0.34 s at 100 Mb/s) once a second.

**What else changed and what did not.** `INT_MASK` is still `0x1F`: the
PAUSE-sent interrupt (`TXC`, bit 5) is masked, so `irq_out` still reports RX
activity only and `RM_STATUS[2]` keeps its meaning. RX is unchanged: full
duplex, RXFLOW = 0. `TX_BEACON = 0` restores the old park-after-bring-up
FSM exactly. Proof: `tests/eth_ss_bringup`, `make` (the bus schedule) and
`make ARM=tx` (the whole RM into the shell's virtual PHY, bridge and DUTEGR,
three frames read back byte for byte). Both are seen to fail against the
previous FSM.

Not programmed (documented TODO for the software phase): TX BD0 +
TX frame injection from memory, non-promiscuous filtering, HA1588 TSU queue enables
(`0x1040`/`0x1060` — timestamp capture queues), eth_rx_cksum config
(`0x2000` window), MIIM PHY management transactions (MDIO pins are wired
and mastered by the MAC, but no MIICOMMAND is issued by the FSM).

Bus fabric for all of this: two independent single-master/single-slave
AHB-Lite point links (HSEL=1, HREADY = own HREADYOUT loop) — bring-up FSM →
subsystem slave, and subsystem DMA master → SRAM. No interconnect, no
arbitration, nothing shared. `HRESP` errors are latched into an `errored`
telemetry flag; no retry logic in v1.

## Wiring decisions (dispositions table)

| Contract pin(s) | Disposition |
|---|---|
| `dut_clk` | REAL — subsystem `HCLK`, bring-up FSM clk, DMA SRAM `HCLK`, **and PTP `rtc_clk`** (same choice as upstream `eth_ss_vivado_wrapper.v`'s `sys_fclk`) |
| `dut_resetn`, `rp_resetn` | REAL — ANDed → `HRESETn` (and `rmii_to_mii.RESETn`); either asserting resets the whole DUT |
| `dbg_resetn` | **UNUSED — deliberately** (differs from rm_nanosoc): this RM has no debug logic, and an OpenOCD SRST pulse must not kill a MAC mid-frame. A6 flag #2 |
| `swd_clk`, `swd_dio_oe` | unused (no SW-DP — no Cortex-M anywhere in this RM) |
| `swd_dio_o` → `swd_dio_i` | `swd_dio_i = swd_dio_o` — permanent reflection of the shell's own drive (the nanosoc wrapper's shared-wire mux idiom, degenerate case: the DUT never drives) |
| `phy_rmii_ref_clk`, `phy_rmii_crs_dv`, `phy_rmii_rxd[1:0]` | **REAL** — into `rmii_to_mii` (source repo's own proven bridge) |
| `phy_rmii_txd[1:0]`, `phy_rmii_tx_en` | **REAL** — from `rmii_to_mii`; leave the RM as plain fabric signals (registered in the bridge, no IOB attributes — HDPR-29: pad re-registering is shell-side) |
| `mdc`, `mdio_o`, `mdio_oe`, `mdio_i` | **REAL** — MAC's `eth_miim` MDIO master, direct; `md_padoe_o` is active-high drive-enable, matching the contract's `mdio_oe` sense |
| `uart_tx_*` | tied inert (`tvalid=0`, `tdata=0`) — no console UART in this DUT |
| `uart_rx_*` | swallowed (`tready=1`) — host bytes accepted-and-dropped so the shell FIFO can't back up |
| `swo` | `0` — no Cortex-M ⇒ no ITM/SWO (structurally N/A) |
| `rm_id[31:0]` | **REAL constant `32'h0000_0002`** (`RM_ID_ETH_SS` localparam; matches rm_list.tcl) |
| `dut_lockup` | `0` — no CPU to lock up (structurally N/A) |
| `irq_out` | **REAL** — `int_o \| cksum_int_o` (MAC interrupt, unmasked by bring-up write #4; cksum line benign-low until configured) |
| `dut_gpio_o/oe[15:0]` | `0` — no GPIO source in this DUT; `oe=0` so the shell never drives a pad on our behalf |
| `dut_gpio_i[15:0]` | unused |

RM-internal signals with **no v0.1 partition pin** (left internal, A6 flag
#3): `bringup_done`/`bringup_errored` (FSM telemetry), `rtc_time_one_pps` +
PTP time bond-outs, HA1588 servo group (disabled — needs the external
tidelink/PHC block this platform doesn't place in the RM).

## Clocking

- `dut_clk` drives everything bus-side (subsystem, FSM, SRAM) **and**
  `rtc_clk`. `RTC_PERIOD_NS`/`RTC_PERIOD_FRAC` wrapper parameters (default
  40 ns / 0 = 25 MHz) must track the shell's real `dut_clk` — same
  not-yet-fixed-by-A6 class as rm_nanosoc's `UART_CLK_HZ`.
- `phy_rmii_ref_clk` (50 MHz, shell-sourced) clocks only the `rmii_to_mii`
  bridge. The bridge **derives ÷2 MII clocks (`mtx_clk`/`mrx_clk`) as
  toggled registers** consumed by the MAC's MII domain — RM-internal
  clock generation in the letter-of-the-law sense. Disposition: the
  contract's "no clock generation inside the RM" rule is read as a
  *boundary* rule (no MMCM/PLL, no clocks exported — and indeed synth used
  0 BUFG/MMCM/PLL); this ÷2 topology is the source repo's own proven
  FPGA arrangement (`eth_ss_vivado_wrapper.v`, hardware-proven on PYNQ).
  Flagged for A6 confirmation (flag #4) rather than silently assumed.
- CDC: MAC-internal (OpenCores eth_fifo/eth_top handle WB↔MII crossing);
  `rmii_to_mii` has its own 3-stage reset synchronizer + 2-stage RX data
  sync; ha1588 `reg.v` CDCs its ctrl strobes into `rtc_clk`. Nothing for
  the shell beyond the contract's existing static-side rules.

## OOC timing constraints (socketed XDC — `partition-timing.md`)

Per `docs/contracts/partition-timing.md` v0.1 (the "socketed-XDC" clock
contract, sibling to `partition-pins.md`), this RM ships **two** XDCs — it is
the platform's worked example of the full two-file delivery because it has real
internal clock domains:

- **`eth_ss_ooc.xdc`** (the `<rm>_ooc.xdc` half) — read additively by
  `ooc_synth.tcl` (guarded) for a **standalone OOC timing sign-off**.
- **`eth_ss_rm.xdc`** (the `<rm>_rm.xdc` half) — the RM-internal subset
  (generated MII clocks + the WB↔MII async group) that `build_dfx.tcl` should
  reapply **scoped to the RP cell** at DFX link (that integration is a
  documented follow-up in `partition-timing.md` — NOT wired here).

**Clocks defined** (partition-pin periods copied byte-for-byte from the built
shell, `partition-timing.md` table):

| Clock | Period | Freq | Kind |
|---|---|---|---|
| `dut_clk` | 20.000 ns `{0.000 10.000}` | 50 MHz | primary — AHB/APB slave fabric, bring-up FSM, DMA SRAM (HCLK), MAC Wishbone/host domain, **and PTP `rtc_clk`** (all one domain) |
| `phy_rmii_ref_clk` | 20.000 ns `{0.000 10.000}` | 50 MHz | primary — RMII reference, drives only `rmii_to_mii` |
| `mii_tx_clk` / `mii_rx_clk` | 40.000 ns `{0.000 20.000}` | 25 MHz | **generated** ÷2 of `phy_rmii_ref_clk` (the `rmii_to_mii` toggle-register MII clocks) |

**Internal exceptions:** the one genuine internal async crossing is
`set_clock_groups -asynchronous` between `dut_clk` and the RMII+MII group — the
OpenCores MAC's async-FIFO'd Wishbone↔MII boundary. `dut_clk`↔`rtc_clk` and
`dut_clk`↔DMA-SRAM are **single-domain** here (both = `dut_clk`), so they need
no exception. The MII generated clocks stay phase-related to the RMII reference,
so the ref↔MII÷2 paths time as synchronous (not false-pathed).

**Validated (REAL OOC synth, Vivado 2024.1 — see `XDC_VALIDATION.txt`):**

| | before (clockless) | after `eth_ss_ooc.xdc` |
|---|---|---|
| clocks constrained | **0** ("no user specified timing constraints") | **4** (2 primary + 2 generated MII) |
| setup | 0 analyzed / 6375 unconstrained endpoints | **WNS +17.333 ns, 0 failing / 4765** |
| WB↔MII crossing | invisible | declared async + **verified** (clock-interaction: `dut_clk↔mii_*` = "Ignored / Asynchronous Groups") |

Standalone OOC timing went from meaningless to a real, clean-setup sign-off
across all three domains. One honest surface: 41 endpoints show pre-route hold
slack on the ÷2 MII toggle-clock crossing — not meaningful at unplaced OOC
synth (place/route resolves it), but now visible for the first time; watch it at
the full DFX implementation where `eth_ss_rm.xdc` reapplies the same generated
clocks + async group. Details + reproduction: `XDC_VALIDATION.txt`.

## D7 sizing datapoint (OOC synth, Vivado 2024.1, xcku115-flvb1760-1-c)

`0 errors, 0 critical warnings` (241 unique warnings — all vendor-IP noise
classes; **no** multi-driven nets, latches, combinational loops or black
boxes). From `build/util_rm_eth_ss.rpt`:

| Resource | Used | Notes |
|---|---|---|
| CLB LUTs | **1,769** (0.27%) | 1,609 logic + 160 LUTRAM (MAC BD RAM / MIIM shift) |
| CLB Registers | **2,178** | all FF, no latches |
| Block RAM tiles | **2** (4 × RAMB18E2) | the 8 KB internal DMA frame SRAM |
| CARRY8 / F7MUX | 16 / 151 | |
| DSPs | 0 | |
| BUFG / MMCM / PLL | **0 / 0 / 0** | no clock resources — RM-internal ÷2 clocks are plain FFs at synth |

Context: **~22% of rm_nanosoc's 7,935 LUTs** — comfortably inside the
existing `pblock_rp_dut` (which already fits nanosoc), so no floorplan
pressure from this RM. Cell counts by instance (synth hierarchy table):
`u_eth_ss` 3,931, `u_bringup` 103, `u_dma_ram` 88, `u_rmii_bridge` 68.

Artifacts (untracked, `.gitignore`d): `build/rm_eth_ss_synth.dcp`,
`build/util_rm_eth_ss.rpt`, `build/ooc_synth.{log,jou}`. Rebuild with:

```
vivado -mode batch -source fpga/rp/eth_ss/ooc_synth.tcl \
       -journal fpga/rp/eth_ss/build/ooc_synth.jou \
       -log     fpga/rp/eth_ss/build/ooc_synth.log
```
(all env vars default to this lab's layout; override `OUT_DIR` /
`ETH_SS_HOME` / `ETHMAC_AHB_HOME` / `ETHMAC_IP_DIR` / `HA1588_IP_DIR` /
`CMSDK_DIR` as needed — see `filelist.tcl` header.)

## Verifying without Vivado

- `eth_ss_bringup.sv` lints clean standalone:
  `verilator --lint-only -Wall -sv fpga/rp/eth_ss/eth_ss_bringup.sv`
  (exit 0, silent).
- `rp_eth_ss_wrapper.sv` can't lint fully standalone (instantiates the
  external subsystem — by design, not vendored). It was checked against
  black-box stubs carrying the exact current port headers of
  `ethmac_subsystem_ahb` / `sl_ahb_sram` / `rmii_to_mii` (copied verbatim
  from the read-only checkout into the scratch verification area, not part
  of this deliverable): **zero port mismatches**, plain lint silent; under
  `-Wall` only the documented-expected classes (`PINCONNECTEMPTY` on the
  deliberately-open PTP/servo bond-outs, `UNUSED` on the single-slave DMA
  link's ignored sideband) — same classes `fpga/dfx/rms/README.md` already
  expects for partially-tied wrappers.
- Port-boundary conformance was diffed mechanically against `rm_greybox.sv`
  and `rp_nanosoc_wrapper.sv`: identical (30 ports, same order).
- `filelist.tcl` dry-runs under plain `tclsh fpga/rp/eth_ss/filelist.tcl`
  (no Vivado): 46 .v + 3 .sv files, 4 include dirs, `ETH_WISHBONE_B3`,
  every file existence-checked.

## Flags for A2 (dfx-flow)

1. **rm_list.tcl `synth_recipe` for rm_eth_ss is stale** (still "TODO(A2)"):
   it can now say "PROVEN: OOC synth via fpga/rp/eth_ss/ooc_synth.tcl
   (sources = fpga/rp/eth_ss/filelist.tcl: wrapper + bringup FSM +
   ethmac_subsystem_apb.flist from the read-only ethernet-subsystem-ahb
   checkout + Arm IP library vendor IP, NOT vendored)". Not edited here —
   rm_list.tcl is A2-owned.
2. **Pre-built-checkpoint path**: stage `build/rm_eth_ss_synth.dcp` as
   `<out_dir>/rm_eth_ss_synth.dcp` per `fpga/dfx/rms/README.md` "Build
   recipe" (this RM is multi-file/externally-sourced — keep it off
   build_dfx.tcl's per-invocation re-synth path, like rm_nanosoc).
3. The wrapper passes the `dut_gpio_o` readiness filter (boundary identical
   to greybox — verified above), so default RM auto-selection will pick it
   up once the DCP is staged. The DFX link + `pr_verify` config (analogous
   to `fpga/dfx/proof/build_rm_nanosoc.tcl`'s config-3) has **not** been
   run — that's A2's flow, and `fpga/dfx/` is out of this task's scope.
4. **Socketed timing XDC (new — `partition-timing.md`):** this RM now ships
   `eth_ss_rm.xdc` — RM-internal generated MII clocks + the WB↔MII async
   group — that `build_dfx.tcl` SHOULD reapply **scoped to the RP cell** after
   `read_checkpoint -cell`, or `route_design` mistimes the MAC's WB↔MII FIFO
   boundary. The exact guarded `read_xdc -cell` recipe + placement/ordering
   requirements are in `docs/contracts/partition-timing.md` ("How
   build_dfx.tcl SHOULD consume these"). Deliberately NOT wired into
   `build_dfx.tcl` here (that file + the DFX flow are the parallel DFX-flow
   agent's scope — this is the coordination handshake).

## Flags for A6 (integrator)

1. **`dut_clk` frequency** still unfixed (existing open point) — now also
   binds this RM's `RTC_PERIOD_NS` (=40 ⇒ 25 MHz assumption). One knob,
   wrapper-parameter override, no file edit.
2. **`dbg_resetn` semantics for CPU-less RMs**: this wrapper deliberately
   ignores it (nanosoc folds it into the core reset). If A6 prefers uniform
   all-three-ANDed behaviour across RMs, it's a one-line change.
3. **No v0.1 pin** for bring-up done/error status, `rtc_time_one_pps`, or
   PTP time readback. Candidates for a future contract rev (or a designated
   `dut_gpio_o` bit if A6 prefers zero contract churn).
4. **RM-internal ÷2 MII clocks** inside `rmii_to_mii` (see "Clocking") —
   upstream-proven topology, but confirm the contract's "no clock
   generation" rule is intentionally boundary-scoped.
5. **MAC address byte-packing ambiguity** (RDL vs OpenCores doc) — moot
   under promiscuous v1; resolve before non-promiscuous use.

## Reproducibility — and why this RM is not affected

The standalone eth_ss FPGA image (the fielded "mainfix" build, driven by
`$ETH_SS_HOME/fpga/Makefile`, *not* by this repo) carried a long-standing report
that it could not be rebuilt: a clean rebuild produced a dead Cortex-M0 while the
fielded image ran. Root-caused board-free on 2026-09-10 — it is a **silent
compiler-flag clobber**, not non-determinism: `$ETH_SS_HOME/fpga/Makefile:230-232`
passes `GNU_CC_EXTRA_FLAGS` on the make command line, where it overrides the `:=`
in every sub-makefile, so the FPGA flow *replaces* the bootloader's
`-Os -flto -mthumb-interwork` with the board `-D` defines instead of appending
them. Two entry points, two different binaries, identical source.

Evidence and fix: `docs/planning/ETHSS_REPRODUCIBILITY.md`,
`fpga/rp/local_overrides/eth_ss/` (patches + a non-clobbering build driver),
`tests/eth_ss_repro/` (the gate, with a control that is seen to fail).

**`rm_eth_ss` as built here is untouched by it.** This RM has no CPU —
`eth_ss_bringup.sv` is the bus master (see `filelist.tcl`'s "minus everything
CPU/bootrom/region-related") — so it compiles no firmware, generates no bootrom,
and `fpga/dfx/Makefile`'s `rm-eth-ss-dcp` / `add-rm-eth-ss` rules need no new
knob. The defect is confined to the standalone subsystem flow in `$ETH_SS_HOME`.

**Corollary (2026-09-23): this RM transmits one PAUSE frame a second, and
nothing else.** Until 2026-09-23 it never transmitted: the bring-up FSM set
`TX_BD_NUM=1` and never readied BD0, which is why 2026-09-22 saw reception
proven and `DUTEGR RX_FRAMES = 0`. From the RM built after that date, the FSM
writes `TXCTRL` once a second (see "Transmit beacon" above), so with
`rm_eth_ss` resident `DUTEGR RX_FRAMES` should climb by about 1 per second.
If it stays at 0 on that RM, the DUT→host return path is broken; there is
still no M0 in this RM to blame. On an RM built before that date, 0 is the
designed result. See `docs/planning/ETHSS_REPRODUCIBILITY.md` § 2026-09-23.
