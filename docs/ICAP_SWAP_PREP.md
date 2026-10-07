# ICAP DFX Swap Prep — MicroBlaze-V Linux Harness (first real swap)

> **Status: HISTORICAL** — a record of preparation for the first real ICAP DFX swap on the Linux shell as of 2026-07-22.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

Prep for the **first real ICAP DFX swap** on the MPS3/XCKU115 Linux shell
(static_id `0x2B082E1B`). Investigation + DT prep only — no board, no builds.

Scope of change in this task:
- Edited: `src/linux_harness/shell_linux.dts` (dropped `generic-uio` from two nodes).
- Added: this doc.
- **Not** touched: anything under `src/linux_harness/sw/` (build.sh, configs,
  `dfx.conf`, `static_id`, daemons — owned by the WireGuard agent) and no
  `sw/artifacts/` `.dtb` was overwritten (compile check went to `/tmp` only).

---

## 1. How `mps3_dfx.ko` maps the ICAP + dfx_ctl for `mock=0`

**Model: raw `ioremap()` of fixed physical addresses. No fpga-manager, no UIO,
no `request_mem_region()`.**

Evidence — `src/linux_harness/drivers/icap/mps3_dfx_drv.c`:

- `dfx_init()` `mock=0` branch, **L787–800**: for each of the three blocks it
  calls `d->bases[i] = ioremap(phys[i], MPS3_BLOCK_SPAN)` and nothing else.
  There is **no** `request_mem_region()` / `devm_request_mem_region()` anywhere
  in the file. `grep -n request_mem_region` on the driver tree returns nothing.
- The device is a **misc chardev** `/dev/mps3dfx` (`misc_register`, L809–813),
  instantiated once at `module_init` — it does **not** bind to a DT node and
  does **not** use the `platform_driver`/OF match machinery. The phys addresses
  come from module params (`hwicap_phys`/`dfxctl_phys`/`clkrst_phys`, L61–66),
  defaulting to the constants in `mps3_icap_regs.h`.
- Register access goes through the ops vtable `drv_rd`/`drv_wr` (L99–116),
  which do `readl/writel(d->bases[blk] + off)` on real HW (or the in-kernel
  mock when `mock=1`).
- The **fpga-manager / fpga-bridge veneer** (L608–750) is under
  `#if IS_ENABLED(CONFIG_FPGA)` / `CONFIG_FPGA_BRIDGE`. **Correction (verified in
  the built `.config`): the harness 6.18.7 kernel has `CONFIG_FPGA=y` and
  `CONFIG_FPGA_REGION=y`, so the veneer IS compiled in** — but it is registered
  programmatically against the **misc device** (`dfx_fpga_register()` →
  `fpga_mgr_register_full(d->misc.this_device, …)`, L678), **not** against the
  `hwicap` DT node. So it does not claim `hwicap@44a20000`, and removing
  `generic-uio` from that node leaves it unbound (harmless — the driver
  ioremaps the phys address directly). The chardev path is the product path;
  the veneer stays dormant unless the fpga-manager framework is invoked (the
  swap daemon uses `/dev/mps3dfx` ioctls, not the framework).
  Board-window note: watch the boot log for any `fpga_region` node that
  references `fpga-mgr = <&hwicap>` — with no manager on that node it may log a
  probe-defer, which is benign (the real manager lives on `/dev/mps3dfx`).

**Consequence for the `generic-uio` concern.** Because the driver bare-ioremaps
and never reserves the region, there is **no hard mapping failure** if
`hwicap@44a20000` / `dfx-ctl@44a10000` are also bound to `generic-uio` — two
mappings of the same page coexist silently. `uio_pdrv_genirq` likewise does not
`request_mem_region` (it maps lazily on `mmap`). So the hazard is **not** a
probe conflict; it is a **correctness race**: `/dev/uioN` would let any
userspace process `mmap` and poke the *live* ICAP write-FIFO / DFXCTL decouple
register concurrently with the kernel swap engine — precisely a torn config
stream or a mid-swap decouple flip. Nothing in the kernel prevents it, so the
interlock has to be "don't publish the UIO node."

---

## 2. Device-tree binding decision

**Decision: DROP `"generic-uio"` from `hwicap@44a20000` and `dfx-ctl@44a10000`.
KEEP it on `clkrst@44a00000` and on every other harness CSR node.** (Done.)

Rationale, with DTS evidence (`src/linux_harness/shell_linux.dts`):

- The DTS already stated the rule: the fabric comment (was ~L515–520) says
  "*remove `generic-uio` from these two nodes when the kernel drivers land, so
  both cannot race*." This task **is** landing the kernel swap driver
  (`mps3_dfx.ko mock=0`) for the first real swap, so the condition is met.
- Given §1 (bare ioremap, no region reservation), removing the UIO node is the
  **only** mechanism that enforces single-owner access to the ICAP + decouple
  registers. It is necessary, not cosmetic.
- **`hwicap`** (now `compatible = "soclabs,mps3-hwicap-fpga-mgr",
  "xlnx,axi-hwicap-3.0"`): no in-tree driver matches `soclabs,...`; the node
  sits unbound and `mps3_dfx.ko` maps it by phys addr. **CONFIRM-AT-INTEGRATION:**
  the stock `xilinx_hwicap` driver matches `xlnx,axi-hwicap-3.0` — verify
  `CONFIG_XILINX_HWICAP` is unset in the harness kernel (it is absent from the
  QEMU 6.18.7 config) or it will claim `/dev/icap` and race the same page. I
  left the `xlnx` string in place (documented intent, harmless while the config
  omits the driver); flag it rather than silently strip it.
- **`dfx-ctl`** (now `compatible = "soclabs,dfx-ctl-1.0"`): unbound, mapped by
  phys addr. **Dependency:** the DTS notes DFXCTL.RM_STATUS is the source of the
  telem daemon's "lockup" field. With UIO gone, any daemon needing DFXCTL.STATUS
  / RM_STATUS must read it through the driver's `MPS3_DFX_IOC_GET_STATUS`
  (`dfxctl_status`, `mps3_dfx_drv.c` L447) instead of a raw UIO mmap. That is a
  daemon-side migration owned by the `sw/` tree — noted here, not changed.
- **`clkrst` keeps `generic-uio` deliberately.** Split ownership
  (SERVICE_DISPOSITION §3.6, DTS L553–559): the swap engine only co-maps
  `rp_resetn`; the `dut`/`dbg` resets serve the :6900 `reset` verb and OpenOCD
  srst via the UIO daemon. That daemon still needs `/dev/uioN` for clkrst, so it
  stays. This is why the original rule said *two* nodes, not three.
- `fpga-region0` (DTS, root) is `status = "disabled"` and only holds phandles
  (`fpga-mgr = <&hwicap>`, `fpga-bridges = <&dfx_ctl>`) as design intent; the
  phandles remain valid after the compatible edits. The interim swap path does
  not use the region.
- The `uio_pdrv_genirq.of_id=generic-uio` cmdline (DTS L109) and all other
  `generic-uio` nodes are unaffected — they still bind and produce `/dev/uioN`.

**Compile check:** `dtc -I dts -O dtb -p 4096 -o /tmp/shell_linux_check.dtb
src/linux_harness/shell_linux.dts` → exit 0, no errors.

---

## 3. The board-window swap steps

### 3.0 Provisioning (before any swap)

1. **`mock=0`.** `/etc/mps3/dfx.conf` (in `sw/`, ships `DFX_MODULE_ARGS="mock=1"`)
   must be set to `DFX_MODULE_ARGS="mock=0"` at board provisioning. `S10mps3dfx`
   reads it to insmod `mps3_dfx.ko`.
   - **Defaults are correct — do NOT copy dfx.conf's example override verbatim.**
     `mps3_icap_regs.h` (authoritative, matches the DTB `reg` addrs): HWICAP =
     `0x44A20000`, DFXCTL = `0x44A10000`, CLKRST = `0x44A00000`. The example
     override *comment* in `dfx.conf` L10 lists `hwicap_phys=0x44A00000 …
     clkrst_phys=0x44A20000`, which has **HWICAP and CLKRST swapped**. With
     bare `mock=0` (no overrides) the header defaults are used and this is fine;
     only a verbatim copy of that example would mis-map. Flagged for the `sw/`
     owner — I cannot edit `dfx.conf`.
2. **static_id = `0x2B082E1B`.** Bake it in via `MPS3_STATIC_ID=0x2B082E1B
   ./build.sh` (post-build hook `mps3_provision.sh` writes line 1 of
   `/etc/mps3/static_id`; the overlay ships `0x00000000` = unprovisioned). The
   daemons load it (`mps3_ctrld.c` `load_static_id`, L584) and reject any spooled
   pair whose header `static_id` mismatches (`swap_worker.c` L186–191). Optional
   future home: a `soclabs,static-id` root DT prop + a boot hook (not wired; out
   of scope).
3. **Rebuild the DTB** from the edited `shell_linux.dts` through the normal
   `sw/` DTB step (do not hand-place a `.dtb`), and load the LED + greybox
   partials from
   `mps3-nanosoc-platform/fpga/dfx/build_linux/prod/`
   (`config_rm_led_pblock_rp_dut_partial.bit`, `config_rm_greybox_…partial`
   + `_clear`) onto the target (spool dir, default `/run/mps3`).

### 3.1 Swap path (spool + daemon, NOT direct dev poking)

The swap is orchestrated, not driven by hand:

- **:6900 `mps3-ctrld`** receives the `swap` verb; the reply is **HELD** until
  the swap FSM settles (`mps3_ctrld.c` L19–21). It forks **`swap_worker.c`**.
- The incoming pair (clearing + partial) is published into the shared spool by
  the config/push daemon; `swap_worker.c` `wait_for_pair()` (L72) blocks until
  both `SPOOL_CLEARING` and `SPOOL_PARTIAL` are staged.
- `swap_worker.c` `do_swap()` (L156+) opens `/dev/mps3dfx` (single-owner,
  `O_RDWR`, L166) and runs the frozen sequence:
  1. re-check both spool metas' `static_id == cfg->static_id` (L186–191).
  2. `ioctl(SWAP_BEGIN)` → decouple + shutdown + RP reset, confirm-polled (L224).
  3. `push_file(... KIND_CLEARING, staged)` → `PUSH_BEGIN`/`write()`×N/`PUSH_END`;
     CRC is gated **before** any ICAP write (L233).
  4. `push_file(SPOOL_PARTIAL, KIND_PARTIAL, STREAM_DIRECT)` → the incoming LED
     partial, packed MSB-first and pushed to the ICAP write-FIFO as bytes land
     (L239–241).
  5. `ioctl(SWAP_FINISH, &res)` → hostio4 hook (stub today), release, **RM_ID
     verify with the RP connected, then commit-or-re-isolate** (L250).
- On success the incoming clearing is promoted to the resident cache
  (L287–288); on any failure the engine parks the RP decoupled + in-reset and
  the worker reports the bare-metal `swap failed` shape.

### 3.2 Loading the LED partial + reading back rm_id

For this first swap the incoming partial is
`config_rm_led_pblock_rp_dut_partial.bit`. After `SWAP_FINISH`:

- `struct mps3_dfx_result` (`mps3_dfx_uapi.h` L46) returns `ok`, `verified`,
  and **`rm_id`** — the LED RM's `rm_id` read back from **DFXCTL.RM_ID**
  (`DFXCTL_RM_ID` = offset `0x10`, RO, RP partition-pin readback,
  `mps3_icap_regs.h` L64) with the RP connected, gated on
  `DFXCTL_RM_STATUS.RM_ID_VALID` (bit0, offset `0x14`). `verified==1` means the
  read-back `rm_id` matched the wire-header target; a mismatch re-isolates and
  fails closed.
- Live observability mid-swap: sysfs `rm_id` / `state` / `icap_bytes` /
  `eos_status` (`mps3_dfx_drv.c` L532–558) and `MPS3_DFX_IOC_GET_STATUS`
  (`dfxctl_status` = raw DFXCTL.STATUS, L447). `sysfs rm_id` is the **last
  verified** id, never a transient live register read.

### 3.3 Where `0x2B082E1B` fits (static_id vs rm_id — don't conflate)

- **`0x2B082E1B` is the SHELL `static_id`** — it identifies the Linux static
  design, is provisioned into `/etc/mps3/static_id`, echoed by the :6900
  `ping`/`shell_id` (`mps3_ctrld.c` L130), and matched against every spooled
  partial's header (`swap_worker.c` L187). It is **not** read from DFXCTL.
- **DFXCTL.RM_ID returns the RM's `rm_id`** (the LED module's id), a different
  value. Pre-swap check: confirm `ping.shell_id == 0x2B082E1B`. Post-swap check:
  confirm `result.verified==1` and `result.rm_id` == the LED partial's expected
  `rm_id` from its wire header.

---

## Key file references

| Item | Path | Line(s) |
|------|------|---------|
| `mock=0` bare ioremap (no request_mem_region) | `src/linux_harness/drivers/icap/mps3_dfx_drv.c` | 787–800 |
| chardev `/dev/mps3dfx` register | same | 809–813 |
| fpga-mgr/bridge veneer (compiled in, registered on misc device) | same | 608–750 |
| GET_STATUS → dfxctl_status | same | 430–451 |
| phys defaults / block map | `src/linux_harness/drivers/icap/mps3_icap_regs.h` | 30–41, 64 |
| swap chardev ABI + sequence | `src/linux_harness/drivers/icap/mps3_dfx_uapi.h` | 1–24, 46–51 |
| swap worker (do_swap) | `src/linux_harness/sw/daemons/swap_worker.c` | 72, 156–301 |
| :6900 held-swap verb | `src/linux_harness/sw/daemons/mps3_ctrld.c` | 19–21, 130, 584 |
| dfx.conf (mock arg; addr-swap caveat) | `src/linux_harness/sw/br2_external/rootfs_overlay/etc/mps3/dfx.conf` | 8, 10, 12 |
| static_id file (ships 0x0) | `src/linux_harness/sw/br2_external/rootfs_overlay/etc/mps3/static_id` | 1 |
| hwicap / dfx-ctl / clkrst nodes | `src/linux_harness/shell_linux.dts` | (edited) hwicap, dfx-ctl; clkrst keeps UIO |
| fpga-region0 (disabled, design intent) | `src/linux_harness/shell_linux.dts` | `fpga-region0` |
