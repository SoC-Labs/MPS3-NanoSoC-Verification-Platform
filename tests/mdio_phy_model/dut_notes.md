# DUT notes — `mdio_phy_model`

RTL: `fpga/ethernet/mdio_phy_model/mdio_phy_model.sv`, module `mdio_phy_model`
(params `C_S_AXI_ADDR_WIDTH=12`, `C_S_AXI_DATA_WIDTH=32`, `C_PHY_ADDR=5'd1`).
Real port list (confirmed by reading the RTL, 2026-07-04):

- `s_axi_*` — standard AXI4-Lite slave (VPHY regmap: `PHY_STATE`@0x00,
  `PHY_ID`@0x04, `LINK_EVENT`@0x08 — see `tests/common/regmap.py`).
- `mdc_i`, `mdio_o_i`, `mdio_oe_i` (I, sampled from the DUT MAC) /
  `mdio_i_o` (O, this model's reply) — partition-pins.md's MDIO group,
  shell's view. In this isolated bench, the testbench plays the DUT-MAC
  (MDIO master) role via `tests/common/mdio_master.py`'s `MDIOMaster`.

Status as of writing: **RTL exists but is a Phase-0 stub** — the AXI-Lite
write/read channel FSMs and the Clause-22 frame engine are `// TODO(A1)`;
`mdio_i_o` is currently tied to `1'b1`. `tests/common/dut_presence.rtl_ready()`
gates this bench on that TODO marker being removed, not just file presence —
see `tests/common/dut_presence.py` docstring for why (a stub would hang, not
fail cleanly).

Register table this bench checks (from the block's own README, "Two register
spaces" table): BMCR(0)/BMSR(1)/PHYID1(2)/PHYID2(3)/ANAR(4)/ANLPAR(5).
Link-up defaults per the same README are a **documented starting point, not
final** ("TODO(A1): pick and document an actual PHYID value with A3/A6") —
this bench's PHYID test writes `PHY_ID` via AXI-Lite first rather than
asserting an undecided reset default.
