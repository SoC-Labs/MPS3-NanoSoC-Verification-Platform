# MPS3 config-SD bundles — nanoSoC on HBI0309 A / B / C

The MPS3's Arm **MCC** (Motherboard Configuration Controller) reads the config
microSD on every power-on and programs the FPGA from it *before* releasing
reset. This directory is a **flow** that assembles a shippable SD-card bundle —
one per HBI0309 board variant (A / B / C) — carrying **our nanoSoC** config and
bitstream. The layout mirrors the Arm MPS3 "Boardfiles" bundle structure (used
here as a structural template only; the bundle contains no Arm application-note
content).

```
fpga/mps3_sd/
├── assemble_sd.sh          # the flow: stamps a bundle for a variant + a .bit
├── README.md               # this file
├── .gitignore              # keeps the generated bundle/ out of git
├── templates/              # source-of-truth config templates (edit these)
│   ├── config.txt          # SD-root MCC config (shared, variant-independent)
│   ├── board.txt           # per-variant motherboard config (@BOARD@ placeholder)
│   ├── nanosoc.txt         # nanoSoC FPGA app config (OSC0:25.0 + SMB/LAN toggle)
│   └── images.txt          # OPTIONAL software-preload config (--with-images)
└── bundle/                 # GENERATED output (git-ignored) — copy onto the SD
```

## What each file is

| File | Role |
|------|------|
| `templates/config.txt`  | Top-level MCC config at the SD root. Read first on every power-on. `AUTORUN`, oscillator/board flags. Variant-independent — the MCC auto-selects the matching `MB/HBI0309<rev>/` subtree. |
| `templates/board.txt`   | Per-variant motherboard config. Points the MCC at the MB BIOS (`mbb_v141.ebf`, stock) and at our app config (`Nanosoc\nanosoc.txt`). `@BOARD@` is stamped to `HBI0309A/B/C` by the flow. |
| `templates/nanosoc.txt` | The nanoSoC FPGA application-note config (AN522-derived). Names the bitstream (`F0FILE: nanosoc.bit`), sets the oscillators (incl. the **Ethernet clock**), and the peripheral-support flags. Variant-independent. |
| `templates/images.txt`  | Optional. Preload an `.axf`/`.bin` into the SoC at program time. Ships disabled (`TOTALIMAGES: 0`); only staged with `--with-images`. |
| `nanosoc.bit`           | **Your** full nanoSoC bitstream, copied in by the flow. Not stored in git. |

## Build a bundle

```bash
# one variant (C is the hardware-proven Kintex UltraScale rev):
./assemble_sd.sh C /path/to/nanosoc.bit

# a single SD tree containing ALL of A/B/C (the MCC auto-selects by board rev):
./assemble_sd.sh ALL /path/to/nanosoc.bit

# no bitstream yet — writes a clearly-named placeholder + instructions:
./assemble_sd.sh B
```

Output lands in `bundle/HBI0309<V>/` (or `bundle/all/` for `ALL`):

```
bundle/HBI0309C/
├── config.txt
└── MB/HBI0309C/
    ├── board.txt
    └── Nanosoc/
        ├── nanosoc.txt
        ├── nanosoc.bit          # (or nanosoc.bit.PLACEHOLDER if no .bit given)
        └── images.txt           # only with --with-images
```

Options / knobs:

- `--out DIR` — write the bundle somewhere other than `./bundle`.
- `--with-images` — also stage `images.txt` under `Nanosoc/`.
- `ETH_SMB=0 ./assemble_sd.sh …` — stamp `FPGA_SMB`/`FPGA_LAN` **FALSE** in
  `nanosoc.txt` (see the Ethernet toggle below).
- Legacy form `./assemble_sd.sh /path/to/x.bit` (a path-like first arg) still
  works and defaults to variant **C**.

## Which bitstream?

The SD only ever carries a **full** bitstream (the MCC cannot load partials):

- `nanosoc_mps3_top.bit` — `fpga/monolithic/build_monolithic.tcl` — the non-DFX
  full SoC. Simplest "board + SoC alive, UART prints hello" check.
- `config_greybox.bit` — `fpga/dfx/proof/build_proof.tcl` — the DFX static shell
  with the greybox RM baked in; JTAG-swap the RP afterwards (see
  `docs/BOARD_BRINGUP.md`).

Copy whichever you built in as the bitstream arg; the flow renames it to
`nanosoc.bit` (the name `nanosoc.txt`'s `F0FILE` expects).

## Per-variant A / B / C differences

Across the three revisions, **the only difference is the `BOARD:` line in
`board.txt`** (`HBI0309A` / `HBI0309B` / `HBI0309C`). Everything else — the
nanoSoC FPGA config, the bitstream, the MB BIOS reference, `images.txt` — is
identical for all three. (This matches the Arm MPS3 reference bundle, where the
A/B/C `board.txt` files are byte-identical apart from `BOARD:` and the app-note
`.bit`/`.txt` are shared across revisions.) That is why a single parameterised
`board.txt` template plus a variant-independent `nanosoc.txt` covers all three.

> Board revision is silkscreened near the power connector. HBI0309**C** is the
> Kintex UltraScale XCKU115 rev this platform is hardware-proven on. A/B are
> earlier revs — use the matching `board.txt` (or ship `ALL` and let the MCC pick).

## Two config facts that bite

1. **Ethernet reference clock — `OSC0: 25.0` (REQUIRED).** The LAN9220 PHY
   reference is `OSC0`. It **must** be `25.0` MHz. An earlier nanoSoC SD shipped
   `OSC0: 24.0` — a ~4% error on the PHY reference that breaks Ethernet. The
   template already sets `25.0`; do not change it. (The working Arm AN543
   Ethernet reference also uses `25.0` here.)

2. **`FPGA_SMB` / `FPGA_LAN` toggle.** The working Ethernet reference sets both
   `TRUE` so the MCC brings up the LAN9220 over its SMB (+ SCC) sideband — this
   **may be needed for the LAN9220 SMB path**. The template defaults them `TRUE`.
   They are *independent* of the `OSC0:25.0` clock fix. If the board fails to
   program with them `TRUE` (MCC hangs on SMB), stamp them `FALSE`:
   `ETH_SMB=0 ./assemble_sd.sh …` — the SoC + Ethernet datapath still run off the
   `OSC0` clock.

3. **`APPFILE` uses a DOS `\` separator**: `APPFILE: Nanosoc\nanosoc.txt`. A `/`
   silently fails → FPGA unprogrammed → dead PBON LED, no UART. The template
   already uses `\`.

## Flash the generated tree onto the MPS3 config SD

The MPS3 config microSD is a small vfat volume (usually labelled **V2M_MPS3**),
exposed as a USB mass-storage device when the board's DBG USB is connected.

1. **Mount** the config SD (`V2M_MPS3`).
2. **Copy the bundle contents to the SD root** — merge, do **not** wipe the SD:

   ```bash
   cp -r bundle/HBI0309C/config.txt bundle/HBI0309C/MB  /media/<you>/V2M_MPS3/
   #        └── or bundle/all/* for the all-variants tree
   ```

   This drops `config.txt` at the root and `MB/HBI0309C/{board.txt,Nanosoc/…}`
   under it. **Do not delete the SD's stock Arm files** — especially
   `MB/HBI0309<rev>/mbb_v141.ebf` (the MB BIOS our `board.txt` references) and
   any MCC firmware blobs. If a stock `board.txt` already exists for your rev,
   our `board.txt` replaces it (it is complete, not a fragment).
3. **Auto-boot.** `config.txt` sets `AUTORUN: TRUE`, so on the next power-on the
   MCC programs the FPGA and releases the SoC hands-free (a `~3 s` window lets a
   serial key-press halt it). Set `AUTORUN: FALSE` in `templates/config.txt` and
   rebuild if you want an interactive/manual boot instead.
4. **Reboot the board**: eject the SD, power-cycle the MPS3 — or, since
   `USB_REMOTE: TRUE`, drop a `reboot.txt` on the SD root to reboot over USB.

### Expected power-on sequence

MCC LEDs cycle while it reads the SD → FPGA programs → `USER_nLED[0]` lights when
reset is released, then UART traffic. **Dead PBON / no UART** = the MCC did not
find/parse the config: re-check the `\` in `board.txt`'s `APPFILE`, that the
files are under `MB/HBI0309<your rev>/`, and that `nanosoc.bit` is present.

## fpgahub alternative

fpgahub's `sd_install` + `mps3_mcc_reboot` plugins can automate the copy+reboot
(mount the USB-MSD SD, atomically write `MB/HBI0309<rev>/Nanosoc/`, reboot over
the MCC serial). Point it at a `bundle/HBI0309<rev>/` produced here. Until that
board node is wired up (tender step), use `assemble_sd.sh` + a manual copy.
