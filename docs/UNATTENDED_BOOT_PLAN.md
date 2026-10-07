# Unattended boot for the MBV-Linux harness — route evaluation and plan

> **Status: HISTORICAL** — a record of route evaluation for unattended boot of the MBV-Linux harness as of 2026-07-27.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.
> The step-0 question it names below — "characterise MCC configuration reliability" — is now a
> measurement with a tool and a runbook: [docs/BOOT_RATE.md](BOOT_RATE.md). The "about 1 time in 4"
> in §1 is the four-attempt note as it stood; it is history, not a rate, and BOOT_RATE.md says why.
> Cites `BOARD_HANDOFF_NOTES.md`, `PLATFORM_LIVE_STATUS.md`, `QSPI_CLEARING_CACHE_HW_FINDINGS.md`, `SIGNOFF_CHECKLIST.md` — internal notes, not in the public tree.

**Status 2026-07-24: PLANNED, not started. One finding reframes the whole problem — read §1 first.**

Today every harness boot needs a workstation: `xsdb` programs the bitstream, then `dow`s four payloads
into DDR and sets `a0`/`a1`/`pc`. Nothing survives a reset. For a platform others are meant to use, that is
a hard limit — and it is the largest remaining engineering gap before sign-off.

## 1. The gate nobody can engineer around

**The MCC must configure the FPGA from the SD card, and on this board that works about 1 time in 4.**

`BOARD_HANDOFF_NOTES.md` records four controlled MCC-reset attempts with identical inputs: one clean
boot, two dark, one partial (`shell_id 0`), and concludes *"MCC-config-from-SD is FLAKY … JTAG config is
100% reliable; MCC-SD config is not … **NOT software-fixable**."*

Every unattended route inherits this, because none of them can configure the FPGA themselves (§2.3). No
payload work, loader, or storage medium changes it. **Characterising and fixing MCC configuration
reliability is therefore step 0 of any unattended-boot effort**, and if it cannot be made reliable,
unattended boot is dead at layer 1 regardless of everything below.

There is a second-order hazard worth stating: an *unattended MCC reload mid-campaign* reprograms the FPGA
with the SD-resident classic shell (`static_id 0xD84A2E7A`, unstamped) underneath a running session.
`dfx_preflight.sh` catches it — that is exactly the foreign-image case it was built for — but it means
making the MCC path more automatic also makes that failure more likely.

## 2. Routes evaluated

### 2.1 QSPI / SST26 flash — **BLOCKED** (two independent fatal blockers)

*No wires.* The shell's `axi_quad_spi_0` exists (`shell_linux_bd.tcl:933`) and the CPU can reach its
registers at `0x44A4_0000`, but its `SPI_0` master interface **is never made external** — the six flash
IOBUFs in `shell_linux_top.sv:272-279` are driven only from the RP side. This was deliberate: commit
`541a513` *"relinquish OVLSTORE pads"* handed the flash to the DUT at the D16 boundary freeze. The device
tree says so outright — *"Enabling it would probe a bus with no wires"* (`shell_linux.dts:426-429`). The RP
cannot relay it either: there is no shell↔DUT bus, only scalars and an AXI-Stream byte channel
(`partition-pins.md:11`).

*No room.* The part is an SST26VF064B — **8 MiB**. The payload is 43,196,013 B (**5.15×**). Gzipped and with
the rootfs removed entirely it is still 8,872,338 B — **over by 484 KB**.

Also: the shell-side QSPI path has never worked on silicon (*"first light FAILS"*,
`QSPI_CLEARING_CACHE_HW_FINDINGS.md`), and `PLATFORM_LIVE_STATUS.md:519` already advises **"Do not use the
QSPI"** for boot media.

### 2.2 MCC-SMC memory preload — feasible on paper, **not recommended**

The mechanism is real and the ordering is favourable: MPS3 TRM §3.3 puts the preload at step 10 — *after*
FPGA configuration (so the MIG is up) and *before* the CPU-release at step 11. Files that are not
`.axf`/`.elf` load as flat binaries. Size is viable.

But **none of the hardware exists**: the flown static has no `SMBM_*` ports, no SMC→AXI bridge, and Arm's
recommended `microToAhb.v` is not on this filesystem. `IMAGE0ADDRESS` is an address in *MCC* space with a
64 MB per-chip-select window, so the current load map (75.4 MiB span) would have to be re-laid out. The MBV
also takes reset from `USER_nPB0`, not `CB_nRST`, so today it would start *during* the preload.

That is a lot of new RTL, a re-key (§4), and a boundary change — to land on a path still capped by §1.

### 2.3 FPGA self-configuration from flash — **not available**

`CONFIG_MODE` is `S_SELECTMAP` (*slave* SelectMAP): an external master clocks the bitstream in. The 8 MB
SST26 is user NVM, **not** the configuration flash — an earlier claim otherwise was retracted on hardware
evidence (`OPEN_ISSUES.md:125-135`). The bitstream is 12.8 MB anyway, 1.53× the part. **The MCC configures
the FPGA no matter what we do**, which is why §1 is a gate and not a preference.

### 2.4 User microSD + a BRAM first stage — **most promising**

The MPS3's *user* microSD is FPGA-wired and separate from the MCC's configuration card (`USD_CLK`/`USD_CMD`/
`USD_DAT[3:0]`, LVCMOS33). It has no 8 MB ceiling, no 64 MB window, and no dependence on undocumented MCC
firmware behaviour.

Two pieces of scaffolding already exist and are proven:

* **A 128 KiB LMB BRAM sits at the MBV reset vector** (`shell_linux_bd.tcl:130-131,156`), and the mechanism
  to preload it is working today: `src/linux_soc/hw/fw_dbg/bootstub.c` is baked in post-bitstream via
  `updatemem` + `.mmi` and *runs automatically on FPGA config — no debugger, no `dow`*. That is the natural
  home for a first stage.
* **The CPU is already held in reset until `c0_init_calib_complete`** (`[DEV-4]`), so DDR is guaranteed
  usable when a stage-0 runs — a genuinely favourable difference from the nanosoc precedent.

What transfers from the silicon-proven nanosoc flash boot is the *shape* (copy-from-storage, CRC, jump).
What does **not** transfer is XiP: the shell's `axi_quad_spi` is `C_SPI_MODE {0}` — a plain shift register
with no memory aperture — and RISC-V Linux has no self-decompressing head.

## 3. Verified facts that constrain any route

**The kernel is 104 KiB from overwriting the DTB.** `build.sh` step 6/6 is a mandatory gate computing
exactly this, using the Image header's `image_size` (which **includes BSS**). Load `0x80400000` +
`0x01DE6000` ends at `0x821E6000`; the DTB sits at `0x82200000`. Margin **106,496 B on a 30 MiB budget —
0.3%**. Any added driver breaks the build.

> A conflicting figure of 428 KB was reported during this analysis. It is wrong: it measures the *file*
> size and ignores BSS, which is zeroed at runtime and would clobber the DTB. The gate's own measure is
> authoritative.

**`Image.gz` is NOT a free win.** It is tempting (31,028,736 → 8,593,277) but **nothing in this flow
decompresses** — `fw_jump` simply jumps to the payload. Flipping `BR2_LINUX_KERNEL_IMAGEGZ` today would
break the boot. It only becomes valuable *after* a first stage with gunzip exists.

### Step 1 result — BUILT AND MEASURED 2026-07-24 (not yet board-validated)

`configs/kernel_fragment_harness_slim.config` + a `STRICT_KERNEL_RWX` off build produced
`artifacts/Image_slim`. It does **not** displace `Image`; the proven artifact is untouched.

| | file | `image_size` (BSS-incl.) | end | margin to DTB |
|---|---:|---:|---:|---:|
| `Image` (proven) | 31,028,736 B | 31,350,784 B | `0x821E6000` | **104 KiB** |
| `Image_slim` | **16,700,928 B** | 17,022,976 B | `0x8143C000` | **14,096 KiB** |

**−14,327,808 B (−46.2%), and the DTB cliff goes from 0.3% of budget to 13.8 MiB — 135× more
headroom.** The predicted saving was −14,319,392 B; measured −14,327,808 B.

*Safety checked before building:* every driver the harness needs is already built **in**, so no
module trim can break it — `SMSC911X=y` (eth0), `UIO=y`, `UIO_PDRV_GENIRQ=y`, `SERIAL_UARTLITE=y`,
`XILINX_INTC=y`, `RISCV_TIMER=y`, and — relevant to §2.4 — `MMC_SPI=y` and `SPI_XILINX=y` are
already present. `CONFIG_MODULES` stays on so the out-of-tree `mps3_dfx.ko` still loads.

*Trade-off, stated plainly:* this gives up kernel W^X. Defensible on a lab harness on a private
network, but it is a security-posture decision, not a free win.

**Still owed before it can ship:** a board window to boot `Image_slim` and re-run the swap
campaign. Until then `Image` remains the default in `board_image.tcl`'s companion scripts.

### Step 1b — rootfs trim, measured (config verified, full rebuild still owed)

The rootfs carries 199 stock modules / 21 MB because the base defconfig is upstream's
general-purpose `rv32`. Measured by rebuilding the cpio from the real target tree —
**method calibrated first**: reproducing the untrimmed archive landed within 0.035% of the
shipped `rootfs.cpio.gz`, so the trimmed figure is trustworthy.

| | bytes |
|---|---:|
| `rootfs.cpio.gz` as shipped | 11,888,216 |
| with stock modules removed (keeping `updates/mps3_dfx.ko`) | **4,445,232** |
| saving | **−7,442,984 (−62.6%)** |

`kernel_fragment_harness_slim.config` was then applied to the real kernel config and run
through `olddefconfig` to prove it does what it claims: **`=m` symbols 207 → 114** (93
modules eliminated), with `SMSC911X`, `UIO`, `UIO_PDRV_GENIRQ`, `SERIAL_UARTLITE`,
`XILINX_INTC`, `MODULES`, `MMC_SPI` and `SPI_XILINX` all still `=y`. The config was then
restored and verified byte-identical to the proven snapshot; no rebuild was triggered.

That check earned its keep: `# CONFIG_SCSI is not set` **silently did not take** —
`drivers/ata/Kconfig:18` does `select SCSI` and `CONFIG_ATA=y`. The fragment now disables
`ATA`/`SCSI`/`BLK_DEV_SD` together.

### Step 1c — BUILT. Real artefacts, from the build system

A full Buildroot pass with the fragment applied, followed by an automatic restore pass.
Both artefacts came out of the build system — nothing hand-assembled.

| | shipped | slim | |
|---|---:|---:|---|
| `fw_jump.bin` | 269,072 | 269,072 | unchanged |
| kernel | 31,028,736 | **13,897,216** | `Image_slim` |
| `shell_linux.dtb` | 9,989 | 9,989 | unchanged |
| rootfs | 11,888,216 | **5,476,082** | `rootfs_slim.cpio.gz` |
| **total** | **43,196,013** | **19,652,359** | **−23,543,654 B (−54.5%)** |

The kernel came out *smaller* than step 1's 16,700,928 B, because the fragment also strips
built-in subsystems (`DRM`, `ATA`/`SCSI`, `USB`, `MEDIA`) rather than only changing section
alignment. **Margin to the DTB: 104 KiB → 16,860 KiB.**

*Integrity checked, not assumed:*
* The slim rootfs still carries the harness — `updates/mps3_dfx.ko`, `S10mps3dfx`, `S90mps3d`,
  `dropbear`, `etc/mps3/dfx.conf`, `etc/mps3/static_id`, `usr/sbin/ip`, `wg` all present.
* A full entry-by-entry diff against the shipped rootfs shows the trim removed **only**
  module files and their directories — 102 modules remain (the fragment is deliberately less
  aggressive than deleting all 199), and nothing else changed.
* The five proven artefacts were **md5-verified unchanged** after the whole exercise.

*Restore pass verified:* `Image` back to 31,028,736 B exactly and 199 modules reinstalled.
The regenerated `rootfs.cpio.gz` differs from the shipped one by 1,515 B (0.013%) — cpio and
gzip embed timestamps, so byte-exact reproducibility is not expected; `artifacts/` holds the
authoritative proven copy and it is untouched.

**Still owed:** a board window. `Image_slim` + `rootfs_slim.cpio.gz` have never been booted.

## 7. Step 2 — first-stage loader (`src/linux_soc/hw/fw_stage0/`) BUILT

The self-booting first stage `[FIX-D]` in `shell_linux_bd.tcl` reserved. It runs from the 128 KiB LMB
BRAM at `0x0` on FPGA config, reads a boot table from storage, copies each region into DDR, CRC-verifies
it, and jumps to OpenSBI — replacing the xsdb `dow`+`rwr`+`con` recipe.

**Proven board-free:**
* The portable core (`stage0_core.c`) is **host-unit-tested** (`test/`): happy-path 4-region load, plus
  bad-magic → `S0_EMAGIC`, flipped-payload-byte → `S0_ECRC`, and truncation → `S0_EREAD`.
* The packer (`stage0_pack.py`) and the loader's `s0_crc32` **agree by construction** (cross-checked
  against a second CRC impl in the test).
* The target **builds and fits**: 1,728 B of the 131,072 B BRAM (1.3%), `_start`@`0x0`, every section in
  LMB, `fence.i`+`jr` hand-off present, nothing DDR-resident. ~126 KiB left for the SD driver + stack.

**Format** carries an explicit OpenSBI hand-off (`pc`/`a0`/`a1`) so one loader boots both the 4-region map
*and* a single `FW_PAYLOAD` blob (`num_entries=1`) — the latter being the natural pairing with the slim
boot set (§ step 1c: ~20 MB).

**Not proven (needs the board), in the order to tackle them:**
1. *DDR-staged smoke* — implement `s0_sd_read` as a memcpy from an xsdb-preloaded DDR address; exercises
   the whole loader (calib wait → core → CRC → cache writeback → OpenSBI jump → Linux) with **no SD driver**.
2. *SD driver* — `stage0_sd.c` is a deliberate **stub**; the microSD reader is the remaining
   board-dependent work (kernel already has `MMC_SPI`/`SPI_XILINX`; sketch in the file).
3. *Bake into the shipping static* — wire `stage0.coe` into the BD / `updatemem`; this re-keys the static,
   so rebuild every overlay as one set (`PLATFORM_ONE_IMPL.md`), guarded by the existing gates.

**Top validation risk:** cache coherence at the hand-off — rv32imac has no cache-maintenance CSRs; the
loader evicts via a read-thrash window + `fence.i`, which must be confirmed on silicon.

### Step 2b — packer + FW_PAYLOAD proven against REAL artefacts (board-free)

The synthetic host test proved the contract; this proves it against the actual harness bytes.

**4-region slim set, packed and loaded through the core:**

| region | dst | bytes |
|---|---|---:|
| `fw_jump.bin` | `0x80000000` | 269,072 |
| `Image_slim` | `0x80400000` | 13,897,216 |
| `shell_linux.dtb` | `0x82200000` | 9,989 |
| `rootfs_slim.cpio.gz` | `0x84000000` | 5,476,082 |
| **image** | | **19,660,530** |

All four load and CRC-verify through the same `stage0_core.c` that runs on target.

**FW_PAYLOAD single-blob, built and loaded** (`fw_stage0/mk_fw_payload.sh`, out-of-tree, board-free):
a real OpenSBI 1.6 + `Image_slim` + DTB blob — kernel's `RISCV` magic verified at payload offset
`0x400000`. Packed as a 2-region image (`fw_payload@0x80000000` + `rootfs@0x84000000`, `a1`=0 since the
FDT is embedded) and loaded clean.

| | 4-region slim | FW_PAYLOAD (2-region) |
|---|---:|---:|
| regions stage0 must place | 4 | **2** |
| `a1`/DTB hand-off | must be exact (`0x82200000`) | **irrelevant** (FDT embedded) |
| total image | 19,660,530 B | 23,572,210 B |

**Correction to an earlier claim:** FW_PAYLOAD is *not smaller* — the kernel sits at a 4 MiB payload
offset, so it costs ~+4 MB. Its value is a **simpler, less error-prone cold-boot hand-off** (one fewer
region, `a1` irrelevant), which is worth more than 4 MB for a stage-0 that has never run.

### Step 2c — TRUE single-region boot BUILT (`mk_1region.sh`)

The rootfs is now embedded into the kernel as an initramfs (`BR2_TARGET_ROOTFS_INITRAMFS`) and the whole
thing wrapped in FW_PAYLOAD — **one blob, one region, `a1` irrelevant, no separate rootfs**. Feasible only
because the slim config disabled `STRICT_KERNEL_RWX`. Verified: `Image_1region` carries a gzip stream and
cpio magic, +4,284,416 B over `Image_slim` (the real initramfs). Packed as a 1-region image and loaded
clean through `stage0_core.c`.

| layout | image | regions | `a1`/DTB |
|---|---:|:--:|---|
| 4-region slim | 19,660,530 B | 4 | must be exact (`0x82200000`) |
| 2-region FW_PAYLOAD | 23,572,210 B | 2 | irrelevant |
| **1-region (this)** | **22,380,040 B** | **1** | **irrelevant** |

The 1-region blob is ~1.2 MB *smaller* than the 2-region FW_PAYLOAD (embedding the rootfs avoids the
separate region's alignment padding) and is the simplest possible stage-0 target: copy one region to
`0x80000000`, jump. This is the recommended boot image for the stage-0 path.

**Rebuild-correctness note** (both traps bit the first attempt, are handled in `mk_1region.sh`): a plain
`make` after a config change does **not** rebuild the kernel — you must `make linux-reconfigure`; and the
rootfs cpio must be built *before* the kernel embeds it. All three of these variant builds snapshot and
restore the buildroot config; the proven default artefacts were md5-verified unchanged after each.

### Step 2d — SILICON-VALIDATED on the board (2026-07-27)

Both reduced payloads boot Linux on the MPS3 (matching DFX static, `xsdb` `dow` to DDR, lease held as
`claude-slimboot`):

| payload | dow'd | control plane up |
|---|---|---|
| slim 4-region (`Image_slim` + `rootfs_slim`) | 13.9 MB kernel + 5.48 MB rootfs | **24 s** — `ping` + `telemetry` both answered |
| **1-region FW_PAYLOAD** (`fw_payload_1region.bin`, single blob) | 22.4 MB, one region @`0x80000000`, `a1`=0 | **18 s** — the recommended stage-0 payload, booting from ONE blob |

So the −46% kernel and the single-artifact boot both **work on real hardware**, not just in the loader
model. The 1-region result is the important one: it proves OpenSBI+kernel+initramfs+DTB in one region,
jumped to at `0x80000000` with `a1` irrelevant — exactly what a stage-0 has to hand off — comes all the way
up to the harness control plane.

*Caveats, all from how these validation rootfses were built (not defects):* `shell_id=0x0` (built without
`MPS3_STATIC_ID`) and SSH unusable (built without `MPS3_ROOT_PASSWD` → dropbear rejects empty-password
root). A shippable slim/1-region image must be built through `build.sh` with those env vars set, exactly as
the stock image is. `telemetry` reports "no power sensor" — a trimmed driver; benign, worth a look before
shipping if power telemetry is wanted.

**What is still NOT validated** (needs the static rebuild, not just a board window): the stage-0 loader
running *from BRAM* — this test `dow`'d the payload to DDR the way xsdb does, which exercises the payload
and the OpenSBI hand-off but not `stage0.c` itself. That is rung 1 of the fw_stage0 bring-up ladder and
requires baking `stage0.coe` into the static (re-key).

**Measured reduction options** (all board-free, all needing a rebuild + re-validation):

| Change | Saving | Note |
|---|---:|---|
| Drop stock kernel modules from the rootfs (199 `.ko`, incl. nouveau/radeon/btrfs on a headless rv32) | **−7,580,047 B** (−63.8%) | must keep `mps3_dfx.ko` |
| `# CONFIG_STRICT_KERNEL_RWX is not set` (4 MiB Sv32 section alignment ⇒ 53.6% of the Image is zeros) | **−14,319,392 B** (−46%) | also converts the 104 KiB cliff into ~13 MiB headroom; loses kernel W^X — a policy call |
| Embed initramfs (`BR2_TARGET_ROOTFS_INITRAMFS`) | one fewer artifact | requires the row above first |
| `CONFIG_BUILTIN_DTB` | one fewer artifact | `a1` then ignored; costs the 12-variant DTB swap flexibility |

**Single-artifact boot is achievable**: OpenSBI `FW_PAYLOAD` (already available in the Buildroot config)
yields one `fw_payload.bin` ≈ 25 MB at `0x80000000`, with `a0`/`a1` irrelevant.

**Load addresses are set by the firmware build, not hardware.** `fw_jump` is FW_PIC with
`FW_JUMP_OFFSET=0x400000` and `FW_JUMP_FDT_OFFSET=0x2200000` baked in as PC-relative offsets — which also
means the `a1` written by `dfx_swap_boot.tcl` is dead code, overridden by `fw_next_arg1`.

## 4. What any of this costs

**Every route needs a static change, and a static change re-keys the platform.** A new `shell_linux_top`
implementation re-mints `BITSTREAM.CONFIG.USERID`, so `board_image.tcl`'s `BIT_USERCODE` and **every**
overlay in `fpga/dfx/overlay_linux/` must be rebuilt together, and `dfx_preflight.sh` will correctly refuse
the old ones. That is the same class of churn that destroyed the FPGA configuration twice on 2026-07-24 —
see `PLATFORM_ONE_IMPL.md`. Cheap-ish here (only 2 RMs), but it must be done as one set.

## 5. Recommended order

0. **Characterise MCC configuration reliability** — board window. True power-cycle vs `fpgahub` reboot, MCC
   console watched during config, MCC firmware (`V2M-MPS3 v1.3.2`, 2018) vs a newer `mbb_*.ebf`. **Gate on
   everything else.**
1. **Payload reduction** — board-free, wanted by every route, and independently fixes the 104 KiB cliff.
   Build as a variant; do not displace the proven artifacts until board-validated.
2. **First stage in the existing BRAM** — fork `bootstub.c`; it already auto-runs on FPGA config.
3. **Storage port** — user microSD, with the re-key done as one set (§4).

**Cheap resilience win, independent of all the above:** `WDOG_RREQ` (pin AU19) is currently tied to `1'b0`.
Wiring it converts a hung shell into an MCC reload with no host present — `PLATFORM_LIVE_STATUS.md` calls it
*"the cheapest high-value item on this page"*. It still costs a re-key, so batch it with §5.3.

## 6. Blocked on a decision

* Whether MCC flakiness is fixable at all may be a hardware/MCC-firmware question, not ours. This is the
  only genuine blocker.

### D16 is NOT a blocker for this work (corrected)

`SIGNOFF_CHECKLIST.md` describes a "D16 flash-collision embargo … until closed, all board work stays
volatile-only". Read alone that reads like a ban on all persistence. It is stale and overbroad.

`docs/contracts/OPEN_ISSUES.md:103` is authoritative and marks D16 **RESOLVED 2026-07-19 — the DUT owns
flash `0x0`-`0x50000`**, decided on silicon evidence (the low region held only a test pattern; the shell's
default firmware keeps every clearing in RAM and never touches the flash store). The surviving constraint is
narrow and specific:

> if the shell's flash-backed overlay store is ever enabled, it MUST be rebased out of `0x0`-`0x50000`
> first. `overlay_store_commit()` and A/B slot staging remain off-limits until that rebase exists.

So the live embargo covers **shell-side overlay-store flash writes**, not non-volatile work in general —
DUT-side flash programming is routine and silicon-proven (the M0 loader writes 160 KB at ~1100 B/s, and
MicroPython cold-boots from flash). None of that touches the recommended microSD route, which uses a
different device entirely.

The checklist item still deserves an owner for the narrow rebase question; it simply does not gate
unattended boot.
