# fpga/dfx — DFX (Dynamic Function eXchange) flow for the MPS3 (KU115) platform

**Owner:** A2 (dfx-flow). **Status:** PRODUCTION FLOW RUNS ON THE **REAL
SHELL** — **build of record (SUPERSEDED; `rm_list.tcl` carries TEN RMs today, not
four — and docs/FIELDED_SHELL.md has the shell that is actually on the board) was
against the then-current 256 KiB-LMB shell (2026-07-07),
`static_id 0xECCEDBF3`, FOUR swappable RMs at that date** (greybox, led, nanosoc **and
now the eth_ss second DUT**). `build_dfx.tcl` runs the full N-config loop
(greybox → led → real nanosoc) against A1's
`build/shell_proj_256k/shell_static_synth.dcp` (`rp_inst=u_rp_dut`, stub
carved out in-flow), then `rm_eth_ss` (the standalone AHB-MAC + PTP
subsystem, real RMII/MDIO + a DMA SRAM) is folded in **incrementally**
against the same locked static (`make add-rm-eth-ss` — keeps `static_id`
stable so already-shipped partials stay valid). `pr_verify` COMPATIBLE on
every non-reference RM incl. eth_ss (97 partition pins compared), timing
met on all four configs (WNS **+2.337 ns**, static-dominated — tighter than
the 128 KiB shell's +3.135 ns because the 256 KiB local RAM densifies the
static, still 0 failing endpoints), `.bit`+`.bin` partial/clearing pairs per
RM (COMPRESSed, so per-RM sizes differ), verified four `overlay/<rm_name>/`
triples out of `gen_manifest.py`, and the generated firmware override
`overlay/mps3_shell_static_id.c` returning `0xECCEDBF3UL` (consumed by the
ELF/firmware agent; see "static_id scheme"). **eth_ss D7:** Pblock 1,626
LUTs (3.8%) / 2 BRAM tiles (4 RAMB18E2, 1.4%) — the MAC's DMA SRAM fits with
large margin.

**Evidence history** (newest first): `prod_results_2026-07-07-256k/`
(256 KiB shell, `0xECCEDBF3`, CURRENT) · `prod_results_2026-07-06-realshell/`
(128 KiB shell, `0x628E2D0D`) · `prod_results_2026-07-06/` (proof-static
stand-in, `0x2F458F06`). One-command repro: the `Makefile` here (see "Build
+ overlay quickstart"). No A1-dependent `TODO(A2)` markers remain — what's
left is the hardware-only I18 residue.

Known cross-tree fallout (host-owned, not fixed from A2): `host/pyverify/
tests/test_e2e_deploy.py` still hardcodes an OLD `REAL_STATIC_ID`
(`0x2F458F06`) + stale `REAL_NANOSOC_SIZES`, so its 6 real-overlay tests
fail against the regenerated `0xECCEDBF3` manifests until the host owner
rekeys them to `0xECCEDBF3` + sizes `(117684, 1648200)` (a read-only
pyverify `Overlay.load()+validate` cross-check confirms the triples
themselves are correct — details in
`prod_results_2026-07-07-256k/ARTIFACTS.txt`).

This directory is the MPS3/UltraScale port of the **PROVEN** Zynq-7 DFX probe
(`nanosoc-multicore-system/future-work/dfx-pynq/dfx_impl_probe.tcl`,
feasibility closed 2026-07-02, see `docs/DFX_BACKGROUND.md`). The Z2 probe
established that "static shell + whole-SoC reconfigurable partition, boundary
= slow scalars only" is a clean DFX split. **This port keeps that structure
and swaps in the UltraScale-specific semantics that the Z2 probe gets wrong
(or skips) for 7-series** — see the delta table at the bottom.

## Methodology

### Static shell + RP relationship
- **Static shell** (`fpga/shell/`, owned by A1): MicroBlaze, lwIP, LAN9220
  I/F, bridge, AXI HWICAP → ICAPE3, DFX Decoupler + AXI Shutdown Manager,
  Debug Bridge, clock/reset unit (DRP MMCM + 3 resets), VPHY. Programmed
  once via the MCC from SD; must survive every RP teardown/reload.
- **Reconfigurable Partition (RP)** (`fpga/rp/<rm>/`, boundary defined by
  `docs/contracts/partition-pins.md`): holds exactly one RM at a time. RMs
  are built and swapped independently of the shell.
- The RP↔shell boundary is **slow scalars only** (clocks/resets, SWD, RMII+
  MDIO, UART/SWO AXI-Stream, `rm_id`/`dut_lockup`/`irq_out`) — no shell↔DUT
  AXI in v0. This is the same property that made the Z2 split trivial (no
  bus decoupling protocol needed, just a signal decoupler), and it is a
  precondition assumed everywhere in this flow.

### Building RMs OOC against the locked static (the "non-project" DFX flow)
Standard Xilinx multi-RM DFX pattern (UG909), the same shape as the Z2 probe
but extended from 2 configs to N RM configs:

1. **Config 1** = static shell (OOC-linked, or read from `fpga/shell/`'s
   locked synth checkpoint) + **RM #1** (the *reference* RM — `rm_greybox`,
   see `rm_list.tcl`). Mark the RP cell `HD.RECONFIGURABLE`, floorplan the
   Pblock (`dfx_floorplan.xdc`), `opt/place/phys_opt/route`.
2. **Extract + lock static**: `update_design -cell $rp_cell -black_box` +
   `lock_design -level routing` on the routed config-1 checkpoint →
   `static_routed_locked.dcp`. This checkpoint is reused, unchanged, for
   every other RM — it is what makes RM builds independent of each other.
3. **Config N** (one per remaining RM in `rm_list.tcl`): read the locked
   static checkpoint, add the RM's own OOC synth checkpoint, `link_design`,
   `place/route`.
4. **`pr_verify`** the reference config against every other config —
   confirms all RMs present compatible partition-pin interfaces against the
   *same* static image. A `pr_verify` failure here is the actionable signal
   that an RM's partition-pin wrapper drifted from `partition-pins.md`.
5. **`write_bitstream`** per config → see the artefact set below.

`build_dfx.tcl` drives steps 1–5 for the RM set in `rm_list.tcl`.
`dfx_floorplan.xdc` is step 1's Pblock/HD.RECONFIGURABLE/SNAPPING_MODE
declaration, `source`d (not merely `read_xdc`'d) so it can see the caller's
`$rp_inst` variable.

### The clearing-bitstream rule (UltraScale-mandatory)
On 7-series, overwriting an RP with a new partial bitstream is enough — the
new partial's frames simply replace the old ones. **UltraScale does not work
this way**: the RP must be returned to a known/cleared configuration state
before the *next* partial is loaded, or the load can corrupt state / fail.
Vivado encodes this directly in `write_bitstream`: for an UltraScale RM,
`write_bitstream` emits **both** `<top>_<pblock>_partial.bit` (the RM) *and*
`<top>_<pblock>_partial_clear.bit` (the matching clearing bitstream) — this
is not optional and not a flag to pass, it is how UltraScale DFX bitstreams
are structured. Every overlay this flow produces is therefore a
`{clearing, partial}` **pair**, matching `docs/contracts/overlay-manifest.md`
exactly ("why a set, not a file"). The runtime sequencing rule (send the
*currently-loaded* RM's clearing bitstream before the *new* RM's partial) is
the shell coordinator's job (A3, `net-protocol.md` §"Swap sequence") — **A2's
job stops at emitting a correctly-paired, correctly-named artefact set**; A2
does not decide swap order at runtime.

### SLR constraint (KU115 is a 2-SLR SSI device)
`xcku115-flvb1760-1-c` is split across two Super Logic Regions with an SLR
crossing (interposer) that costs extra routing delay and consumes dedicated
Laguna/SLL resources. Crossing an RP over an SLR boundary would mean a
partial reconfiguration event that changes logic straddling that boundary —
avoid the whole class of problem by keeping the **RP Pblock entirely within
one SLR** (`dfx_floorplan.xdc`). Keep the **ICAP (ICAPE3) in the same SLR as
the RP** too: ICAP is a static-shell resource (A1 owns its placement), but
its configuration-frame delivery path to the RP is shorter and simpler to
time-close if it doesn't also cross the SLR boundary — flag this as an
A1↔A2 coordination point, not something `fpga/dfx/` can enforce unilaterally.

### `BITSTREAM.CONFIG.PERSIST` and `SNAPPING_MODE`
- `BITSTREAM.CONFIG.PERSIST` **must be OFF**. PERSIST keeps unprogrammed I/O
  driving their last state across a *full* device reconfiguration — it has
  nothing to offer an ICAP-driven partial flow and is documented as mutually
  exclusive with ICAP use. `build_dfx.tcl` sets this explicitly rather than
  relying on the (usually-off) default, because a shell rebuild that
  silently flips it would break partial delivery in a way that's hard to
  root-cause on hardware.
- `SNAPPING_MODE ON` stays (it is not 7-series-only) — it lets the Pblock
  snap to legal RP boundaries at finer-than-clock-region granularity, which
  matters for fitting the RP inside a single SLR without waste.

### What's dropped from the Z2 probe
`RESET_AFTER_RECONFIG` is **not** carried over — see the delta table. Instead
we rely on UltraScale's automatic post-configuration GSR pulse, which is
correct *only* when the clearing bitstream is loaded first (another way of
stating why the clearing-bitstream rule is load-bearing, not a nicety).

## Artefact set → `overlay-manifest.md` mapping

Per RM build (`build_dfx.tcl`, one invocation builds the whole RM set):

| Build artefact (this directory's output) | overlay-manifest.md field |
|---|---|
| `<out>/config_<rm_key>_routed.dcp`, `static_routed_locked.dcp` | (build-only; not shipped in the overlay) |
| `<out>/pr_verify_<rm_key>.rpt` | (build-only gate; a clean run is required before shipping) |
| `<out>/config_<rm_key>_<pblock>_partial.bit` + `.bin` | `partial.file` (`<rm>.bin`), `partial.len`, `partial.crc32` |
| `<out>/config_<rm_key>_<pblock>_partial_clear.bit` + `.bin` | `clearing.file` (`<rm>_clear.bin`), `clearing.len`, `clearing.crc32` |
| `<out>/config_<rm_key>.ltx` — written by `write_debug_probes -force -cell u_rp_dut` from the routed config open for that RM's bitstreams, **only** for an RM `rm_list.tcl` declares `RM_LIB(<rm>,debug) 1` (see "RM-internal ILAs" below) | `ltx` (optional) |
| `<out>/config_<rm_key>.ltx.json` — sidecar: ILA UUIDs + crc32 of the `.ltx` and of its partial (`tools/ltx_sidecar.py`) | `ltx_crc32`, `ltx_uuids` |
| `<out>/debug_core_<rm_key>.rpt` — `report_debug_core` of that config (debug RMs only) | (build-only; the UUID cross-check) |
| `<out>/drc_hdpr_<rm_key>_prelink.rpt`, `<out>/drc_<rm_key>.rpt` | (build-only **gate**: `scripts/harness_gates/check_hdpr_reports.py`) |
| — (A3/firmware-owned) | `fw` (optional, DUT firmware baked/loaded separately) |
| `<out>/static_id.txt` — computed from the locked static checkpoint (scheme below) | `static_id` |
| `rm_id` constant assigned in `rm_list.tcl`, driven by the RM wrapper | `rm_id` |
| `<out>/overlay_inputs.txt` — one line per built RM: `rm_key rm_name rm_id <partial.bin> <clearing.bin>`, paths **relative to `<out>`** | (hand-off record driving `make overlays`) |
| `<out>/mint.json` — the machine-readable record of the run (sources, checksums, firmware flags, what was reused) | (provenance; `tools/MINT_RECORD_SCHEMA.md`) |

Partial/clearing filenames are **normalised across all RMs** to the
`config_<rm_key>_<pblock>_partial[_clear].{bit,bin}` pattern: the reference
RM gets them for free from its full-device `write_bitstream` (Vivado appends
the pblock suffix itself), and every other RM's `-cell` write is given that
same root explicitly (Vivado's `-cell` form names the partial exactly as told
plus `_clear` — proven in `proof/`). `gen_manifest.py --copy` (driven by
`make overlays` off `overlay_inputs.txt` + `static_id.txt`) renames them into
the contract's bare `overlay/<rm_name>/{<rm>.bin, <rm>_clear.bin}` layout.

## RM-internal ILAs, and the per-config gates (2026-09-23)

An RM may carry its own mode-1 debug bridge (hub) and ILAs, reached over XVC →
`debug_bridge_0` (mode 2, `C_NUM_BS_MASTER 1`) → the RP's `dbg_bscan_*` legs
(`docs/planning/HANDOVER_RM_ILA_OVER_XVC.md`). The flow's side of it:

1. **Declare it.** `set RM_LIB(<rm>,debug) 1` in `rm_list.tcl` (absent = 0).
2. **The `.ltx`.** For every config — reference, others, and the incremental
   `add-rm` path, plus `tools/finish_partials.tcl` — `build_dfx.tcl` calls
   `write_rm_debug_probes` (`tools/debug_probes.tcl`) with the routed config
   still open after `write_bitstream`. If the RP holds a debug core it writes
   `config_<rm_key>.ltx` with `-cell u_rp_dut`, deletes the extras Vivado drops
   beside it (`_clear.ltx`, `_<pblock>_partial*.ltx`), checks every core it
   names is under `u_rp_dut`, and writes `debug_core_<rm_key>.rpt`.
3. **The gate, both ways.** `debug 1` and no debug core in the routed RP, or a
   debug core in a `debug 0` RM, is a hard Tcl error printing
   `DFX_LTX_GATE_FAILED`. The Makefile greps for it (Vivado exits 0 on a Tcl
   error), then runs `tools/ltx_sidecar.py gate` — a second derivation of the
   same rule from `DEBUG_RM_KEYS` (sed over `rm_list.tcl`) that parses the `.ltx`
   as JSON, cross-checks the ILA UUIDs against `report_debug_core`, and writes
   the sidecar `config_<rm_key>.ltx.json`. `make overlays` re-runs it with
   `--check` and passes `gen_manifest.py --ltx … --ltx-sidecar …` for every RM
   that has one; there is **no sixth column** in `overlay_inputs.txt`.
4. **It travels with its partial.** Stage 8's hub copy and the fielding set
   (`fielded/README.md`) carry each `.ltx`, its sidecar and its partial pair.
5. **DRC is a gate.** `check_hdpr_reports.py` runs on `drc_hdpr_<rm>_prelink.rpt`
   (before `opt_design`, so a bad RM stops the mint in minutes) and on
   `drc_<rm>.rpt` (routed): HDPR-16/-18/-50 at any severity, or any DRC `Error`,
   fails with `DFX_HDPR_GATE_FAILED`. Warnings (RTSTAT-10, PDCN-1569, CFGBVS-1 on
   a debug RM; REQP-1934, BUFC-1 on every config) pass.
6. **The RP boundary is counted.** After link the RP cell's pin count must equal
   `boundary.yaml` `totals.bits`, and after `opt_design` it must not have moved
   (`DFX_RP_PIN_GATE_FAILED`): Vivado silently punches `sl_iport0/sl_oport0`
   ports into the RP when a static-side ILA/VIO exists (spike 2026-09-23). None
   exists today; this is a tripwire.

Stage 6 of the mint also runs `firmware/platform/verify_shell_image.py
--expect-xvc-target dbgbr` on the baked image: only a Debug-Bridge firmware can
reach an RM's ILAs.

## `static_id` scheme (RESOLVED 2026-07-06 — A6 to fold into the contract)

`static_id` **=** `zlib`/IEEE-802.3 **CRC-32 over the raw bytes of
`static_routed_locked.dcp`** (the routed, routing-locked, RP-black-boxed
static checkpoint every RM in the run is implemented against), formatted
`0x%08X` (upper-case, 8 hex digits). Computed by `build_dfx.tcl` immediately
after `lock_design`/`write_checkpoint` (proc `file_crc32`, Tcl's built-in
`zlib crc32`, chunked) and written to `<out>/static_id.txt`, which
`gen_manifest.py --static-id-file` consumes.

Why this scheme:

- **Deterministic + toolchain-free to re-derive**: anyone holding the locked
  checkpoint can recompute it with `python3 -c "import zlib,sys;
  print(f'0x{zlib.crc32(open(sys.argv[1],'rb').read())&0xffffffff:08X}')"` —
  no Vivado in the loop; same CRC-32 the manifest/wire-header fields already
  use (contract I13), so host/firmware need no new primitive.
- **Changes whenever the static changes** — including on a rebuild of a
  logically identical shell (a `.dcp` is a zip with embedded timestamps).
  Deliberately conservative in exactly the direction
  `overlay-manifest.md` demands: *"a shell rebuild changes static_id and
  invalidates every stored partial"*. Partials are only valid against the
  literal locked static they were routed into, so keying on the file identity
  (not a semantic netlist hash) is correct, not just convenient.
- **32 bits** — drops straight into the wire header's `u32 static_id`
  (`net-protocol.md` "Bitstream framing") and the QSPI slot header's
  `u32 static_id`.

**Handoff to firmware (A3's `mps3_shell_static_id()` seam) — artifact now
GENERATED (2026-07-06 realshell run):** the shell firmware exposes its
built-against static_id via a weak-default function
(`firmware/coordinator/coordinator.c`) expecting a strong override
generated at build time. `make -C fpga/dfx overlays` now emits exactly
that override, from the same `static_id.txt` the manifests are keyed to:

- **`overlay/mps3_shell_static_id.c`** — a strong (non-weak)
  `uint32_t mps3_shell_static_id(void)` returning the run's static_id
  (`0xECCEDBF3UL` was the then-current 256 KiB-LMB build's id when this was written -- the fielded shell is recorded in `docs/FIELDED_SHELL.md`; compiles clean
  with `gcc -Wall -Wextra -Werror`). It sits in `overlay/` beside the
  manifests it must agree with, is regenerated by every `make overlays`,
  and — like the manifests — is build evidence, not stable source.
- **A3's remaining half:** add this file to the Vitis/firmware build's
  source list (it is deliberately NOT copied into `firmware/` from A2's
  side — firmware/ is out of A2 write scope). Linking it in makes the
  running shell report the static_id its overlays were generated
  against, which is what the pusher checks on ping (the alternative
  `CFLAGS += -DMPS3_SHELL_STATIC_ID=$(cat static_id.txt)UL` define route
  remains possible but is now second-best to the generated source).

The value is the same 10-char `0x%08X` literal everywhere (manifest, wire
header, firmware), so no reformatting is needed.

**Flag for A6 (do not edit `docs/contracts/` from A2):**
`overlay-manifest.md` and `net-protocol.md` currently show `static_id` by
example only (`"0xA1B2C3D4"`). The contract wording should state the
derivation: *CRC-32 (zlib) of `static_routed_locked.dcp`, formatted
`0x%08X`; recomputed on every static extraction; the shell reports the
`static_id` it was built with (baked into the shell firmware image at shell
build time) and the pusher refuses on mismatch.* Note the runtime half of
that sentence (how the running shell learns its own static_id) is an
A1/A3-side decision this scheme enables but does not implement.

## I18 (software half): `.bit` vs `.bin` anatomy, and streaming into HWICAP

Byte-level findings from the artefacts this flow emits (`write_bitstream
-bin_file`, Vivado 2024.1, xcku115; verified identical structure on full,
partial and clearing images for greybox/led/nanosoc):

- **The `.bin` is exactly the `.bit` minus the ASCII metadata container.**
  A `.bit` is a sequence of tagged records: `a` = design string
  (`rp_shell_top;UserID=0XFFFFFFFF;PARTIAL=TRUE;Version=2024.1`, clearing
  images additionally carry `;CLEAR=TRUE`), `b` = part, `c` = date,
  `d` = time, `e` = `u32 length` + raw configuration data. The `.bin` equals
  the `e` record's payload **byte-for-byte** (verified), with **no trailer**.
  Header overhead is **variable length** (117 B full / 130 B partial / 141 B
  clearing in this build — it tracks the design-string length), so never
  strip a fixed offset from a `.bit`; ship/consume the `.bin`, or parse the
  records if only a `.bit` is available.
- **Payload layout (identical full/partial/clearing):** 16 dummy words
  `0xFFFFFFFF`, bus-width autodetect (`0x000000BB`, `0x11220044`), 2 more
  dummies, **SYNC `0xAA995566` at word 20 (byte 80)**, then the UltraScale
  config packets, ending in a Type-1 `CMD=DESYNC` (`0x30008001 0x0000000D`)
  and NOOP (`0x20000000`) padding.
- **Byte/word order:** configuration words are stored **big-endian** in the
  file (the sync word appears literally as bytes `AA 99 55 66`). No
  SelectMAP-style bit-swapping is present in the `.bin` (a bit-swapped sync
  would read `55 99 AA 66`). Length is always a multiple of 4
  (`len_words = len/4` — contract I12 holds for every emitted artefact).
- **What A3's config agent must therefore do** (PG134 AXI HWICAP, `WF`
  register): stream the `.bin` from offset 0 — including the leading dummy
  pad and sync, which ICAP requires — assembling **each 4 file bytes
  MSB-first into one 32-bit word** and writing one word per `WF` push
  (i.e. `word = (b0<<24)|(b1<<16)|(b2<<8)|b3`); throttle on `SR`/FIFO
  occupancy. Do **not** bit-swap and do **not** little-endian-load words from
  the byte stream. This matches the Xilinx `xhwicap` driver's handling of
  `-bin_file` output for UltraScale (its per-family byte-swap flag is "no
  swap needed" for 7-series+/UltraScale on AXI).
- **Hardware-verifiable only (the remaining I18 residue):** (1) confirmation
  on the real MicroBlaze+HWICAP path that no per-word byte swap is needed
  end-to-end (the AXI data-path endianness between our CPU, interconnect and
  HWICAP is the one link the file analysis cannot see); (2) the
  clearing-then-partial load actually clearing/GSR-ing the RP on silicon;
  (3) post-`DESYNC` behaviour (`EOS`/status readback). Everything else about
  the artefact format is now pinned by the analysis above.

## Build + overlay quickstart (`Makefile`)

**The whole flow is one target.** `make -C fpga/dfx mint BUILD=... SHELL_PROJ=...
RM_SET="..." SHELL_TOUCH=0|1 TOUCH=0|1` runs shell -> RM checkpoints -> prod ->
overlays -> firmware -> `mint.json` -> hub copy, as eight file targets, so a
re-run repeats only what is missing or stale and `make -n` prints the plan.
The runbook is **[docs/BUILD_AND_MINT.md](../../docs/BUILD_AND_MINT.md)**; read
it before firing one. The targets below are its pieces, and remain useful on
their own.

```sh
cd fpga/dfx
make help             # the entry point, its stages, and the pieces
make rm-nanosoc-dcp   # real-nanosoc OOC synth -> staged rm_nanosoc_synth.dcp (~7 min)
make prod             # PRODUCTION build_dfx.tcl vs the REAL shell: N-config
                      #   loop, pr_verify, pairs, static_id -> build/prod/
                      #   (~26 min measured on the 256 KiB-shell run)
make overlays         # gen_manifest.py build+verify -> overlay/{greybox,led,nanosoc}/
                      #   + overlay/mps3_shell_static_id.c (A3 firmware override)
make verify           # re-check every overlay/*/manifest.json round-trip

make proof            # (optional) legacy greybox<->led proof; emits
                      #   build/proof/static_synth.dcp, the flow-debug stand-in
```

### Preserving a mint

`build/prod/static_routed_locked.dcp` is the **only** input from which a new
overlay can ever be added to a shell once it is fielded, and it lives in a
gitignored scratch tree (`fpga/dfx/build*/`). Until 2026-09-09 the fielded one
existed in exactly one copy, on one workstation: a `make clean`, a full disk or a
tidied machine would have closed that shell permanently, and nothing in the repo
said so. `fielded/<static_id>/` is where a copy is preserved with checksums;
`MINT_HUB` (see `tools.env.example`) is where the mint's stage 8 puts a second
one, off this box, every time instead of when someone remembers. There is **no
default destination** — a hostname baked into a Makefile outlives the machine —
so that stage is a no-op until you set one, and it says so.

`scripts/dfx_scratch_report.sh` classifies every scratch tree LIVE / PROVENANCE /
DEAD so that decision is a safe one to take.

### Per-RM registration

`rm_list.tcl` records, per RM, whether its OOC checkpoint is synthesised
`inline` by `build_dfx.tcl` (a single dependency-free `.sv`) or must arrive
`prebuilt` from `make rm-<name>-dcp`. That used to be a path match — "anything
under `fpga/dfx/rms/` is self-contained" — which was true of the five demo RMs
and false for `rm_socscope`, whose wrapper lives there and whose sources come
from `$SOCSCOPE_HOME`. The Makefile registers the four values each prebuilt RM
needs (synth script, log marker, environment, optional reuse path);
`tests/dfx_flow/test_mint_record.py` fails if the two lists drift.

### Row paths in `overlay_inputs.txt`

Rows are **relative to the build dir** (`tools/overlay_inputs.tcl`): the
artefacts sit next to the file that lists them, and that is the only way the
record travels with them. Rows written before 2026-09-10 are absolute —
`fielded/0xA8C1C535/overlay_inputs.txt` is ten of them, rooted at one
workstation's home directory — and are still accepted by `make overlays`, so an
older prod tree can be re-keyed without a re-mint.

`make prod` defaulted, when this was written, to the then-current 256 KiB-LMB shell (`STATIC_DCP =
../../build/shell_proj_256k/shell_static_synth.dcp`); the fielded shell is 1 MiB LMB (`docs/FIELDED_SHELL.md`), so pass `STATIC_DCP` explicitly rather than trust the default (A1's `build_shell.tcl`
output; regenerate if absent) and `RP_INST=u_rp_dut` (the RP cell path in
every shell variant and the proof stand-in — `shell_top`/`rp_shell_top` is
the netlist top, so no `u_top/` prefix appears in Vivado cell paths).
Against an earlier shell or the proof stand-in for flow debugging:
`make prod STATIC_DCP=$(pwd)/build/shell_proj_a1/shell_static_synth.dcp`
(128 KiB, `0x628E2D0D`) or `.../build/proof/static_synth.dcp`.
Vivado work dirs stay under `fpga/dfx/build/` (gitignored). **Commit
policy for `overlay/`**: payloads (`*.bin`) are gitignored (large,
regenerated); the small `manifest.json`s (and the generated
`mps3_shell_static_id.c`) are deliberately left trackable as build
evidence — with the caveat that on a fresh clone `make verify` fails until
`make prod && make overlays` regenerates the payloads (and a re-run
produces a NEW static_id, so regenerated manifests will differ — they are
evidence of a build, not stable source).

## Z2 lessons carried over (still true on UltraScale)

- **Manual top clock at checkpoint-link time.** OOC synthesis checkpoints
  carry no IP-generated XDC. On Z2 this meant `create_clock` on the PS7
  `FCLKCLK[0]` pin had to be re-created by hand *before* `read_xdc`'ing the
  generated timing XDC, or downstream clock-relative constraints silently
  failed to resolve. **Resolved naturally for the real shell (2026-07-06
  dry-run + prod run):** A1's `shell_static_synth.dcp` is a project-mode
  post-synth checkpoint whose baked-in `mps3_harness.xdc` carries the
  `create_clock` on `OSCCLK1` (50 MHz board primary, legacy-provenance name
  `dut_clk`), and Vivado auto-derives the `clk_wiz_shell`/`clk_wiz_dut` MMCM
  output clocks from it at link — no manual `create_clock` needed. The
  clock guard in `build_config` (refuse to implement a clockless link)
  stays as the tripwire for any future shell whose DCP loses that property.
- **Pin-facing IOB-packed registers belong in the static shell, not the RM**
  (Z2 HDPR-29). On Z2 this was discovered the hard way (RMII TX IOB packing
  failed inside the RP; production fix = move the re-register stage static-
  side). `docs/contracts/partition-pins.md` already designs this in from the
  start for MPS3 — no `IOB FALSE` workaround should be needed here because no
  pin-facing register should ever be instantiated inside `fpga/rp/*`.

## Delta table: Z2 (7-series) probe → MPS3 (UltraScale) flow

| Aspect | Z2 probe (`dfx_impl_probe.tcl`) | This flow (`fpga/dfx/`) |
|---|---|---|
| Part | `xc7z020clg400-1` | `xcku115-flvb1760-1-c` |
| Clearing bitstream | None — 7-series overwrites the RP directly | **Mandatory**; every overlay is a `{clearing,partial}` pair |
| Post-reconfig reset | `RESET_AFTER_RECONFIG true` on the pblock | **Dropped** — automatic GSR on UltraScale, contingent on the clearing bitstream having run |
| `SNAPPING_MODE` | `ON` | `ON` (unchanged) |
| Device floorplan constraint | Clock-region rows only (single-SLR device) | RP Pblock **must** fit in one SLR (KU115 = 2-SLR SSI); ICAP co-located |
| `BITSTREAM.CONFIG.PERSIST` | Not touched (not ICAP-relevant on this Zynq flow) | Explicitly set **OFF** (mutually exclusive with ICAP) |
| IOB packing in the RP | Workaround (`IOB FALSE`) accepted for the probe; production moves regs to static | Designed out from the start — `partition-pins.md` keeps all pin-facing regs static |
| Config delivery | Linux `fpga_manager` (full) / PYNQ `pr_download` (partial), `.bit`→`.bin` via `bit2bin.py` | MicroBlaze **AXI HWICAP** → ICAPE3 (`.bit`→`.bin` via `write_bitstream -bin_file`; word order resolved — see "I18" above, hardware confirmation pending) |
| Number of RM configs built | 2 (`nanosoc`, `blank`) | N, from `rm_list.tcl` (`rm_greybox`, `rm_nanosoc`, `rm_eth_ss`, …) |
| Static extraction | Same technique (`black_box` + `lock_design -level routing`) | Same technique, reused |

## Open items for A6 to confirm

- **RP instance path** (`rp_inst` in `build_dfx.tcl`/`dfx_floorplan.xdc`):
  **RESOLVED against the real shell (2026-07-06)** — the default is now
  **`u_rp_dut`**. The I4 contract's `u_top` is realised as the netlist top
  module itself (`fpga/shell/shell_top.sv`), so Vivado cell paths carry no
  literal `u_top/` prefix; the earlier `u_top/u_rp_dut` default was based
  on reading I4's notation as a path. Confirmed mechanically by the
  realshell dry-run (`prod_results_2026-07-06-realshell/dryrun_rp_pins.txt`:
  top-level cell `u_rp_dut`, exact partition-pins.md v0.1 boundary). The
  old shell_bd.tcl `RP_nanosoc` skeleton drift flag is obsolete — A1's real
  BD keeps the RP outside the BD entirely, as `shell_top.sv`'s sibling cell.
- **Greybox generation (I19)**: **resolved** — hand-authored tie-off wrapper
  (`fpga/dfx/rms/rm_greybox/rm_greybox.sv`), not Vivado `buffer_ports`. Full
  reasoning in that file's module header; short version in
  `fpga/dfx/rms/README.md`.
- **Greybox↔LED-counter pr_verify path (Phase 1.1/1.2)**: **RUN ON THE REAL
  SHELL** (2026-07-06 realshell rerun; first run same day on the proof
  stand-in). `build_dfx.tcl` synthesizes greybox/led inline (no separate
  synth step); readiness filter defaults to {greybox, led} + nanosoc when its
  pre-staged OOC checkpoint exists (`make rm-nanosoc-dcp`); explicit override:
  4th `-tclargs`. No stand-in blocker remains. See `fpga/dfx/rms/README.md`.
- **`rm_nanosoc` port-list drift — RESOLVED, then FILLED (2026-07-04)**: the
  wrapper was first conformed to `partition-pins.md` v0.1 (I4 `dut_gpio_*`
  group), then filled with the **real single-core nanosoc** (D2b resolved:
  read-only `nanosoc_m0_soc` checkout) driving the real
  `rm_id = 0x0000_0001`. Proven board-free: OOC synth 7,935 LUTs; DFX
  config-3 `pr_verify` COMPATIBLE vs the greybox-locked static, WNS
  +6.05 ns, partial+clearing emitted (`proof/build_rm_nanosoc.tcl`,
  `proof/proof_results_2026-07-04/`).
- **SLR choice**: **CONFIRMED on the live device (2026-07-06 dry-run,
  `prod_results_2026-07-06-realshell/dryrun_slr.txt`)** — the KU115 splits
  as SLR0 = clock regions `X0Y0..X5Y4`, SLR1 = `X0Y5..X5Y9`; the RP Pblock
  regions `X2Y0/X3Y0/X2Y1/X3Y1` are ALL in **SLR0**, and the primary ICAP
  site (`CONFIG_SITE_X0Y0`, clock region X5Y1) is also in **SLR0** — the
  RP/ICAP co-location goal holds. 17 shell board pins (USER_SW/nLED/nPB0)
  sit in RP clock region X2Y0's IOB column, which is legal (the Pblock
  carries only SLICE/DSP/RAMB site ranges, no IOB sites).
- **Pblock sizing (D7)**: **measured on the real shell for all four DUTs**
  (2026-07-07 256 KiB-shell run, `prod_results_2026-07-07-256k/util_*.rpt`).
  Largest is nanosoc: **7,902 / 42,824 Pblock LUTs (18.5%)**, **16.5 / 144
  Block-RAM tiles (11.5%)** (identical to the 128 KiB run — the RP is the
  same netlist; the LMB doubling is in the STATIC, outside the Pblock).
  eth_ss (the real MAC+PTP DUT, the BRAM-fit worry because of its DMA SRAM)
  lands at **1,626 LUTs (3.8%)** and just **2 BRAM tiles = 4 RAMB18E2
  (1.4%)** — no fit issue. greybox/led are trivial. So the 4-clock-region
  SLR0 Pblock keeps ~5x LUT and ~9x BRAM headroom over the biggest real DUT.
  With both DUTs' numbers now in hand and SLR confirmed (above), the current
  range stands; `dfx_floorplan.xdc`'s TODO is only about a future DUT that
  outgrows it (e.g. multicore nanosoc).
- **`.bin` word order for AXI HWICAP**: software half **RESOLVED** — full
  byte-level analysis in "I18" above (`.bin` = `.bit` minus variable-length
  header; big-endian config words; sync at word 20; no bit-swap). What
  remains is hardware-only (listed in that section) and moves to the
  bring-up checklist (I17/I20+), not this flow.
- **`static_id`**: **RESOLVED** — see "`static_id` scheme" above. A6 owns
  the contract-wording update in `overlay-manifest.md`/`net-protocol.md`
  (A2 does not edit `docs/contracts/`).
- **Static shell checkpoint path**: **RESOLVED (2026-07-06 realshell run)**
  — `build_dfx.tcl` still takes it as a required argument, and the Makefile
  now defaults to A1's real `build/shell_proj_a1/shell_static_synth.dcp`
  with `RP_INST=u_rp_dut`. The formerly-TODO shell wiring landed as:
  (a) an RP **stub carve-out** in `build_config` (`update_design
  -black_box` when the RP cell arrives with A1's DONT_TOUCH'd
  `rp_dut_stub` contents instead of a black box); (b) `read_xdc` of the
  impl-only `fpga/shell/constraints/mps3_harness_timing.xdc` at config-1
  link, keyed on the `u_shell` cell (proof stand-in unaffected); (c) NO
  manual dut_clk `create_clock` — see the Z2-lessons bullet above. The
  clockless-link guard remains as a tripwire.
