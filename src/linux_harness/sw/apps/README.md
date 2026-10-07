# sw/apps — M5 application layer (CLCD display, boot-status surface, overlay-store read tooling)

Date: 2026-07-17. OFF-BOARD work: everything below is proven on the host with
**mocked registers / faked sysroots only**. Nothing here has touched a board,
a UIO node, or a real MTD device. Companion docs: `../../DRIVER_MATRIX.md`
(§2.9 CLCD/KVM rows), `../../SERVICE_DISPOSITION.md` (§3.10, §3.5), and the
main repo's `docs/CLCD_PANEL_FACTS.md` (READ-ONLY; the silicon-proven panel
config this port reuses).

## Deliverables

| Binary | Install | What it is |
|---|---|---|
| `mps3-clcdd` | `/usr/sbin` | CLCD status-display daemon: UIO node `clcd` (DT `clcd@44ac0000`), HX8347-D init table + 40x15 dirty-cell text renderer ported from the lit-on-silicon firmware driver. Exits 0 when the block is absent (QEMU). `--preview` renders one frame as ASCII with no hardware. Also refreshes `/run/mps3/status.json`. |
| `mps3-status` | `/usr/bin` | One-shot boot-status surface: the same snapshot as one flat JSON line (stdout or `-o` file), or `--text` for the panel frame. Local surface only — the frozen `:6900` telemetry/diag wire shapes stay in `mps3-ctrld`. |
| `mps3-ovlstore` | `/usr/bin` | **READ-ONLY** overlay-store inspector: `info` (header/slots) and `verify` (payload CRC32 vs descriptors) over a flash image file or `/dev/mtd*`. **No write flows exist in the source** — the D16 shared-flash embargo is structural here, and `tests/run_tests.sh` greps to keep it that way. |

Image delivery: `br2_external/package/mps3-apps/` (+`Config.in` hook,
`BR2_PACKAGE_MPS3_APPS=y` in `configs/mbv_harness_defconfig`), init script
`rootfs_overlay/etc/init.d/S91mps3clcd`. `build.sh` re-copies the defconfig
every run, so the package rides the Wave-3 **S1** rebuild with no extra step.

## What ports verbatim vs what deliberately changed

Verbatim from the firmware (`firmware/clcd/*`, `firmware/overlay_store/*`,
main repo, read-only — files copied with provenance intact):
`hx8347_init.{h,c}` (every init byte + `CLCD_ROTATE_180=1`), `font8x16.h`,
`ovlstore_codec.{h,c}` (frozen little-endian 74-byte header). Ported
faithfully in `clcd_core.c`: the cell stream (window regs + RAMWR + RGB565
**MSB-first**, 273 B/cell), dirty-cell diff, FIFO discipline (writes DROPPED
on full by the block ⇒ poll `STATUS.fifo_full` before every write, bounded
per-pass budget), the proven TIMING 4/4/2 and CTRL reset sequence
(BL active-high, RST active-low), the rm_id-v2 design-half keying and the
row-2 field-overflow bounds.

Documented deviations (all deliberate):
1. **Resident-RM source**: bare metal read DFXCTL CSRs live. Under Linux that
   page belongs to `mps3_dfx.ko`; the display reads the driver's sysfs
   (`/sys/class/misc/mps3dfx/{state,rm_id,icap_bytes}`) instead — `rm_id` is
   the **last VERIFIED** id, qualified only in the settled states
   (IDLE/DONE). A JTAG/ICAP side-load can therefore go stale on the glass
   (the exact staleness the firmware fixed by reading the CSR); carried as an
   honest gap until the driver exposes a live-qualified view.
   No driver ⇒ the glass says `NO DFX DRIVER`, never a guess.
2. **Row 7** shows the kernel identity instead of CLKRST clk/reset bits —
   this app touches neither CLKRST nor DFXCTL (ownership discipline,
   SERVICE_DISPOSITION §3.6). Open item below.
3. **diag counters** on row 8 are the honest Linux substitutes
   (netdev `rx_dropped`/`tx_errors`) — schema-of-the-glass, not the frozen
   `diag` verb (which stays in `mps3-ctrld`).
4. **KVM**: presence-gated and NOT serviced. The CLCDKVM block has never been
   built (Wave-4); the daemon logs if the UIO node exists and never touches
   the page (MBV DECERR = real S-mode fault). The firmware's
   lose/regain/banner logic is NOT ported yet — port it with the KVM wave.

## Host test suite (`make test` ⇒ `tests/run_tests.sh`)

All with mocked registers / faked sysroots, per the tasking:

- `test_clcd_core` (~210k checks): behavioural CLCD mock (drop-on-full FIFO,
  depth 128, concurrent-drain model) + an HX8347 GRAM model that decodes the
  accepted byte stream back into a 320x240 framebuffer. Proves: reset/CTRL/
  TIMING sequence; init table streamed byte-exact before any GRAM traffic;
  **zero** drops ever (including a 200-pass stalled-FIFO phase + clean
  recovery); per-pass budget; first paint == init + exactly 600×273 bytes;
  the decoded framebuffer matches the shadow text glyph-for-glyph, healthy
  screen contains no red pixel, banner rows render white-on-red; no-driver /
  unprovisioned / failed-swap honesty; rm name/caps/version bounds over all
  65536 design ids; formatter edge cases.
- `test_ovlstore`: codec golden bytes at the frozen LE offsets; CRC verify
  pass; single-bit corruption ⇒ `ERR_CRC` with computed values; bad magic /
  version / short header; out-of-range descriptor rejected unread; invalid
  slot never CRC'd; whole flow on a 0400 read-only file.
- `test_status_linux`: collector against a faked sysroot (populated, mid-swap
  ⇒ rm_id unqualified, and empty ⇒ everything degrades honestly, never falls
  through to the live system); JSON shape.
- `run_tests.sh` extras: CRC32 vs the IEEE check vector + python `binascii`;
  JSON parsed by `json.load`; `--text`/`--preview` no-hardware paths;
  cross-language store image (python packer ⇒ C reader); the D16
  no-write-paths grep.

Result at delivery: `M5 APPS HOST SUITE: PASS` (run
`make -C src/linux_harness/sw/apps test`, plain host `cc`).

## What is NOT proven (needs bench / Wave-3+)

- The panel itself: this port emits the byte-identical stream the lit panel
  consumes, but no Linux process has ever driven the real FIFO. Bench check
  rides Wave-4/B-series (and the RGB-vs-BGR red-fill question from
  CLCD_PANEL_FACTS §7.1 is still open — hardware fact, not software).
- In-image build: the Buildroot package is wired but the image build itself
  is Wave-3 **S1** (dead build, WAVE2_STATUS §3). The package build was
  emulated off-tree (rsync'd source dir + `UIO_SRC` override + host cc).
- `mps3-ovlstore` against a real `/dev/mtd*`: the OVLSTORE SPI node is
  `disabled` on this baseline (QSPI v0.2); the mtdchar path is the same
  `pread()` code path as the image file, but that equivalence is asserted,
  not proven.
- mps3dfx sysfs semantics: `rm_id`-validity licence (settled-states rule) is
  this module's interpretation of the driver's attrs; re-check when the
  driver grows an explicit validity attr.

## Open items

1. CLKRST row (DUT clk-alive / MMCM lock / reset bits) — needs an agreed
   read-only surface that doesn't race the DFX driver's ownership.
2. KVM handover service (port firmware `clcd_lose/regain` + OSD banner) —
   with the Wave-4 KVM static.
3. Live-qualified rm_id (driver attr) to close the side-load staleness gap.
4. `mps3-status` daemon-liveness fields (ctrld/pushd pidfile checks) if the
   fleet wants them in status.json.
