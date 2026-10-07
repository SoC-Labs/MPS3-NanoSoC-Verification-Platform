# MDIO interop — CLOSED. One real model bug, one bench bug of mine.

The MDIO SUT originally reported that `mdio_phy_model` was "not MDIO-interoperable with
the real OpenCores `eth_miim`": reads came back **right-shifted by one bit** and writes
**never committed**. Both halves are now resolved — and they had **different causes**.

`tests/lan8720_sut` now runs **VERDICT=PASS, unexplained_failures=0**.

## Half 1 — READS shifted by one bit: a REAL model bug (fixed)

`eth_miim` is the reference: its frame is textbook Clause-22 (bit counter → TA at bit-times
46-47, DATA at 48-63) and it interoperates with real LAN8720-class PHYs. The model drove its
read data one MDC period late, so every read came back `value >> 1` (PHYID1 `0x0007`→`0x0003`,
PHYID2 `0xC0F1`→`0x6078`).

**Fixed in `mdio_phy_model`** (commit `0ea0a1b`, "MDIO PHY model drove read data one MDC
period late"). Reads now return `PHYID1=0x0007`, `PHYID2=0xC0F1`, `ANAR=0x01E1` — correct.

## Half 2 — WRITES never committed: MY BENCH, not the model

This one was mine. `MIICOMMAND`'s bits are **not** what an obvious reading suggests
(`eth_registers.v:939-941`):

```verilog
assign r_WCtrlData = MIICOMMANDOut[2];   // WRITE is bit 2
assign r_RStat     = MIICOMMANDOut[1];   // READ  is bit 1
assign r_ScanStat  = MIICOMMANDOut[0];   // SCAN  is bit 0
```

The bench pulsed **bit 0** to trigger a write — which is **ScanStat**. So `eth_miim` was told
to start a continuous *scan*, and **never emitted a write frame at all**. Instrumenting the
model confirmed it: every decoded MDIO header was `OP=10` (read); an `OP=01` (write) frame
never arrived. Reads worked only because `0x2` (bit 1 = RStat) happened to be correct.

**The RDL, the generated header and the C driver all had this right** —
`ETHMAC_REGS_MIICOMMAND_WCTRLDATA_Pos = 2`, and `ethmac.c` writes `MIICOMMAND = 0x4U` for a
write and `0x2U` for a read. Only this bench was wrong. Fixed.

With `MIICOMMAND = 0x4`, ANAR reads back `0x0181` — exactly the value written.

## What the SUT now proves (both board findings, through the real DUT)

| board finding | SUT result |
|---|---|
| **#2 — MDIO *write* works** (FPGA→PHY direction is fine) | ✅ a write issued while inbound is dead still lands: `ANAR = 0x0181` |
| **#3 — MDIO *read* is dead** (PHY→FPGA broken) | ✅ `BMSR = 0xFFFF` at all 32 PHY addresses |

That asymmetry — outbound healthy, inbound dead — is precisely the MPS3 board's signature,
and it now reproduces end-to-end against the real `eth_miim` MDIO master through the shield
model. Reads recover the moment the inbound path is restored.

## Method note (worth keeping)

Both of my false starts here shared a shape: **I assumed a register field layout instead of
reading the RTL.** `MIICOMMAND` bit 0 is not "write", and (separately) `PACKETLEN` is
`{MINFL[31:16], MAXFL[15:0]}`, not the reverse — see `FINDING_TX_LENGTH.md`, where the same
mistake produced a bogus "TX-length bug". When a bench and a DUT disagree, check the bench's
register assumptions against the RTL **first**.
