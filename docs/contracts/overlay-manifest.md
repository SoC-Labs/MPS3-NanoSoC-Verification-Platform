# Contract: overlay artefact-set schema + A/B slot store

**Version:** v0.5 (2026-09-30). Produced by the DFX flow (A2), consumed by the
host pusher/pyverify (A4) and the shell overlay-store firmware (A3).

> **Changelog v0.5 (2026-09-30 — additive, `schema` stays 1):** a required
> top-level `ip_class` (`"open"` | `"arm-aaa"`), copied from
> `RM_LIB(<rm>,ip_class)` in `fpga/dfx/rm_list.tcl`. See "`ip_class` — who may
> redistribute the overlay". Every consumer reads named keys and ignores the
> rest, so older readers are unaffected.

> **Changelog v0.4 (2026-09-29 — the contract catches up with D13; the manifest
> schema is unchanged, `schema` stays 1):** the A/B slot store is on the **user
> microSD**, not the QSPI flash, and clearings live in RAM. D16 had handed the
> SST26 to the DUT and D13 (2026-09-23) replaced the pad-less `axi_quad_spi_0` at
> `0x44A4` with `usd_spi`; nothing in the shell firmware issues a flash opcode
> (`firmware/overlay_store/overlay_store.h`); this document still described the
> QSPI layout and its STAGE/CACHE clearing regions. Rewritten: "A/B slot store".
> The v0.1–v0.3 QSPI layout is in this file's git history.

> **Changelog v0.3 (2026-09-23 — additive):** the optional `ltx` field now has
> normative semantics (see "`ltx` — RM-internal ILA probes"), plus two optional
> companions `ltx_crc32` / `ltx_uuids` and the build-dir sidecar
> `config_<rm_key>.ltx.json`. No existing field changed; `schema` stays 1.
>
> **Changelog v0.2 (2026-07-06, A6 reconcile — additive):** the `static_id`
> derivation scheme is now normative (see "`static_id` scheme" below) —
> previously shown by example only. v0.1 (2026-07-04): I2 resolution, I12
> len units, I13 CRC-32 variant.

## `static_id` scheme (v0.2, closes the "by example only" gap)
`static_id` = the standard **zlib/IEEE-802.3 CRC-32 over the raw bytes of
the routed, routing-locked, RP-black-boxed static checkpoint**
(`static_routed_locked.dcp` — the exact artefact every RM in the run is
implemented against), rendered `0x%08X` and emitted by the DFX flow to
`<out>/static_id.txt` (consumed by `gen_manifest.py --static-id-file` and
baked into the shell firmware image at shell build time via the
`mps3_shell_static_id()` seam). Properties, by design:
- **Toolchain-free to re-derive** from the checkpoint file
  (`zlib.crc32(dcp_bytes)`) — same CRC-32 primitive as I13, no new code.
- **A shell rebuild mints a new `static_id` even for a logically identical
  shell** (a `.dcp` embeds timestamps). This is intentional and exactly the
  invalidation rule below demands: partials are valid only against the
  literal locked static they were routed into.
- 32 bits — drops into the wire header's and slot header's `u32 static_id`.

Rendering: `static_id.txt` and manifests may use uppercase hex digits; the
control channel reports it lowercase (`net-protocol.md` identity rendering).
Consumers compare the **u32 value**, never the string.

## Why a *set*, not a file (UltraScale-mandatory)
On UltraScale a partial cannot simply overwrite the previous RM: the currently
loaded RM's **clearing bitstream** must be sent first. So an overlay is always
a triple, and the store/host must carry and sequence pairs — this is the single
biggest delta from a 7-series/Zynq DFX flow and the top correctness risk.

**I2 RESOLVED — the shell owns clearing sequencing.** The coordinator keeps the
*currently-loaded* RM's clearing bitstream resident (the greybox's at boot;
thereafter the incoming RM's, cached from its pair after each swap). The host
therefore pushes only the **incoming** RM's `{clearing, partial}`; it never
tracks what is running. See `net-protocol.md` swap sequence.

**Units + CRC (I12/I13):** `len` fields below are **bytes** (multiple of 4);
the wire header's `len_words = len/4`. All `crc32` are zlib/IEEE CRC-32 over the
raw `.bin` payload.

## Manifest (per RM) — `overlay/<rm>/manifest.json`
```json
{
  "schema": 1,
  "static_id": "0xA1B2C3D4",     // must match the running shell; else reject
  "rm_id":     "0x0000_0001",    // read back from the RM's rm_id pin to verify
  "rm_name":   "nanosoc",
  "ip_class":  "arm-aaa",        // "open" | "arm-aaa", from rm_list.tcl (v0.5)
  "clearing":  { "file": "nanosoc_clear.bin", "len": 393216, "crc32": "0x..." },
  "partial":   { "file": "nanosoc.bin",       "len": 2097152, "crc32": "0x..." },
  "ltx":       "nanosoc.ltx",     // optional, for host ILA re-attach
  "ltx_crc32": "0x...",           // optional, with ltx (v0.3)
  "ltx_uuids": ["8214FADE..."],   // optional, with ltx (v0.3)
  "fw":        "nanosoc_app.bin", // optional baked/loaded DUT firmware
  "built":     "2026-07-04",
  "vivado":    "2024.1"
}
```
Rules:
- `clearing` + `partial` always ship together; a manifest missing either is invalid.
- The **greybox** RM ships with the shell; its clearing bitstream is the
  post-power-on "currently loaded" state the coordinator assumes at boot.
- Partials are keyed to `static_id`. A shell rebuild changes `static_id` and
  **invalidates every stored partial** — the pusher must refuse a mismatch and
  the freshness gate flags it.

### `ip_class` — who may redistribute the overlay (v0.5, 2026-09-30)
Required. `"arm-aaa"` when the RM's sources pull in Arm-licensed IP
(Cortex-M0/M0+, CMSDK, SoC-400, anything reached through `CMSDK_DIR`,
`ARM_IP_LIBRARY_PATH`), so the overlay ships only in the
private bundle; `"open"` when nothing Arm-licensed is in the partial.
- **One source.** Decided per RM in `fpga/dfx/rm_list.tcl`
  (`RM_LIB(<rm>,ip_class)`, with the evidence in the entry's comment).
  `gen_manifest.py build` copies it, looked up by `rm_name`; for a registered
  RM `--ip-class` may only repeat it. An RM rm_list.tcl does not register gets
  `--ip-class`, else `"arm-aaa"` with a warning (the gate then refuses it in
  the tree).
- **Checked.** `gen_manifest.py verify` refuses a missing value, any value
  other than the two, or one that differs from rm_list.tcl.
  `scripts/harness_gates/check_overlay_ip_class.py` (`make check` stage 2)
  holds every `fpga/dfx/overlay{,_linux}/*/manifest.json` to rm_list.tcl.
- **Published trees** built before v0.5 gain the key in place with
  `fpga/dfx/tools/stamp_ip_class.py` (one key, backed up, nothing else moves).
- Harness Manager reads it and shows `unknown` for a missing or other value.

### `ltx` — RM-internal ILA probes (v0.3, 2026-09-23, additive)
Present only for an RM that carries its own debug hub + ILA(s), i.e. one
`fpga/dfx/rm_list.tcl` declares `RM_LIB(<rm>,debug) 1`. Absent for every other
RM; a consumer must not assume it.
- **Same routed config as the partial.** The DFX flow writes it with
  `write_debug_probes -force -cell u_rp_dut config_<rm_key>.ltx` from the routed
  config that is open for that RM's `write_bitstream`, in the same Vivado
  session. An `.ltx` from any other build of the RM mislabels every probe and
  nothing on the wire can tell; a rebuilt RM gets a new `.ltx` or none at all.
- **Partial-scoped.** It names only debug cores under `u_rp_dut` (the RM's own
  XSDB hub and its ILAs); the flow refuses one that names a static-side core.
  The empty `<f>_clear.ltx` Vivado writes beside it is never staged.
- **Declared, both ways.** `debug 1` without an `.ltx`, or an `.ltx` from a
  `debug 0` RM, fails the build (`build_dfx.tcl` marker `DFX_LTX_GATE_FAILED`,
  then `fpga/dfx/tools/ltx_sidecar.py gate`).
- **The sidecar.** Beside each `config_<rm_key>.ltx` in the build dir the flow
  writes `config_<rm_key>.ltx.json`: `rm_key`, `rp_inst`, the ILA UUIDs (from
  the `.ltx`, cross-checked against `report_debug_core`), and the crc32 of both
  the `.ltx` and the partial `.bin`. `gen_manifest.py --ltx-sidecar` refuses a
  partial or `.ltx` whose crc32 differs from it, and records `ltx_crc32` +
  `ltx_uuids` in the manifest; `gen_manifest.py verify` checks `ltx_crc32`.
- **It travels with its partial.** Every copy of a debug RM's partial (the
  mint's hub back-up, the fielding set, `fielded/<id>/MANIFEST.md5`) carries its
  `.ltx` and sidecar too.
- The shell never reads `ltx`; it is for the host (pyverify's re-attach after a
  swap loads it as the XVC target's `PROBES.FILE`). `ltx_uuids` lets a host
  confirm the ILAs Vivado enumerates are the ones the file describes.

## A/B slot store — the user microSD (D13)
The last committed DUT overlay (its clearing + partial) lives in an A/B pair of
slots on the **user** microSD card. **Not** the MCC config card (`V2M_MPS3`),
which is not FPGA-wired, and **not** the QSPI flash, which belongs to the DUT.
`commit` writes the **inactive** slot, reads it back and checks both CRCs, and
only then flips the header — an interrupted commit leaves the previous default
intact.

```
Card LBA 0: MBR. The store is the first partition entry of TYPE 0xDA, found by
            type, not position; it must start at LBA >= 3 (LBA 1-2 are stage0's
            boot-select copies). No 0xDA entry => "foreign": never written,
            except by `usd` format's explicit rules (net-protocol.md).
Inside that partition (partition-relative LBAs, 512-byte blocks):
  LBA 0      header copy 0  \  ping-pong: [0..73] the packed header below,
  LBA 1      header copy 1  /  [74..77] u32le seq, [78..81] u32le CRC-32 of
                               [0..77]. Readers take the newest valid copy;
                               writers overwrite the OTHER one.
  +1 MiB     slot A (8 MiB)    clearing at slot offset 0, then the partial at
  +9 MiB     slot B (8 MiB)    the next 512-byte boundary
  >= 17 MiB  end of layout     (a smaller partition is refused)

Packed header, 74 bytes, little-endian (the QSPI store's codec, unchanged):
  magic "OVLS" | u16 ver | u8 active_slot(0/1) | u8 flags
  per slot: { u32 static_id, u32 rm_id, u32 clear_off, u32 clear_len, u32 clear_crc,
              u32 part_off,  u32 part_len,  u32 part_crc, u8 valid }
```
Authorities: the card layout `firmware/overlay_store/ovlstore_sd.h`, the header
codec `firmware/overlay_store/ovlstore_codec.h`, the wire (`usd`, `commit`, the
power-on load) `net-protocol.md` "User microSD (`usd`) and `commit` — v0.13",
the design `docs/planning/HANDOVER_USD_OVERLAY_STORE.md`. A slot's stored
`clearing` is **that RM's own** clearing bitstream (used when swapping *away*
from it), paired with its partial; a pair larger than one 8 MiB slot is refused.
A static built before D13 has no working store: its `0x44A4` page is an
`axi_quad_spi_0` with no pads.

**Clearings live in RAM.** The QSPI STAGE/CACHE regions are gone with the SST26
backend. The swap FSM keeps the running RM's clearing in one RAM arena sized for
the largest clearing (`MPS3_SWAP_CLEARING_ARENA_BYTES`: 262,144 B bare metal,
`firmware/platform/Makefile`; 1 MiB in harnessd, its `Makefile`;
`firmware/coordinator/swap_fsm.c`). It copies the incoming clearing in only
after the new RM verifies, so a failed swap never overwrites the still-valid
one; a clearing that does not fit is never copied and the next swap-away fails
closed. The greybox clearing comes from the image (bare metal: a baked blob;
Linux: `/etc/mps3/greybox_clear.bin`); large partials stream ICAP-direct.

Boot: the RP holds the **greybox** (from the shell image), so the coordinator's
"currently-loaded clearing" starts as the greybox's. Once per FPGA
configuration, after the network is up, the store's power-on hook loads the
default through the ordinary swap FSM (internal source `usd`) — greybox
clearing, then the slot's partial — but only if the card is ready, the store is
`valid`, the active slot's `static_id` is the shell's, both CRCs pass in full
before any byte reaches ICAP, and PB1 is not held. Otherwise the board stays on
greybox. `commit` (net-protocol 6900) persists a new default over Ethernet.

> Runtime "current clearing" (RAM) ≠ the A/B *default* store. The A/B slots
> persist the boot default; the coordinator additionally holds the running RM's
> clearing so any `swap` — even to an RM not in the default store — can clear
> the outgoing RM. The greybox clearing is the boot seed of that clearing.

A library of more than one stored DUT is future work.

## Directory layout (host side)
```
overlay/
  <rm_name>/
    manifest.json
    <rm>.bin           # partial
    <rm>_clear.bin     # clearing
    <rm>.ltx           # optional
    <rm>_app.bin       # optional DUT firmware
```
The pusher validates `manifest.json` (both files present, static_id match, CRCs)
before any push; `pyverify` reads it to drive swap + re-attach.
