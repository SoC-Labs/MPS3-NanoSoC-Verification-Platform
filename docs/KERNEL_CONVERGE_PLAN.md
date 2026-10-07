# Harness Kernel Converge Plan

> **Status: HISTORICAL** — a record of the MicroBlaze-V Linux harness kernel convergence plan as of 2026-07-22.
> Superseded by / current state in [docs/STATUS.md](STATUS.md).
> Kept for provenance; do not update.

**Scope:** Prove that the MicroBlaze-V Linux harness kernel (Linux 6.18.7,
Buildroot) reproduces its silicon-proven state from a **clean extract + patch
apply** — i.e. every silicon workaround now living in the already-extracted
build tree is fully captured by the committed patch series, so a
from-scratch `linux-rebuild` converges to the same source.

- **Verified:** 2026-07-22 (read-only; no board, no build, no file edits).
- **Method:** pristine `linux-6.18.7` extracted to a `/tmp` scratch tree, the
  patch series dry-run + sequentially applied, then the result diffed
  byte-for-byte against the live harness build tree.
- **Verdict:** **CONVERGES CLEAN.** All 7 linux patches apply with zero fuzz,
  zero offset, in Buildroot lexical order, and the patched pristine tree is
  **identical** to the built tree for every source file. No stray hand-edits
  remain in the build tree.

---

## 1. Patch set Buildroot applies to the kernel

The harness defconfig
(`src/linux_harness/sw/configs/mbv_harness_defconfig`) sets a **single**
patch dir:

```
BR2_GLOBAL_PATCH_DIR = "…/src/linux_harness/sw/patches"
```

Buildroot applies, per package, the patches in `<BR2_GLOBAL_PATCH_DIR>/<pkg>/`.
For the `linux` package that is **`src/linux_harness/sw/patches/linux/`** only.

**The `src/linux_soc/linux/patches/` series does NOT apply to the harness
build.** That path is the *linux_soc* defconfig's `BR2_GLOBAL_PATCH_DIR`
(`mbv_linux_defconfig`), a different image. The harness patch dir is a
superset/evolution of it (0001–0004 are the same lineage; the harness adds
0005–0007 and upgrades 0002/0003). Only the harness dir is in scope here.
The `…/patches/opensbi/` patch targets the OpenSBI package, not the kernel —
out of scope for the kernel converge (noted only for completeness).

No `series` file exists in the linux patch dir, so Buildroot applies in
**lexical filename order** (confirmed):

| Order | Patch | Role | Target files |
|------:|-------|------|--------------|
| 0001 | `serial-uartlite-optional-irqless-rx-tx-poll-mode` | uartlite poll-mode (DT-gated, inert) | `drivers/tty/serial/uartlite.c` |
| 0002 | `riscv-idle-configurable-nowfi-mbv-coma-workaround` | **no-wfi idle** (`CONFIG_RISCV_SOCLABS_NOWFI_IDLE`, default n) | `arch/riscv/Kconfig`, `arch/riscv/include/asm/cpuidle.h` |
| 0003 | `clocksource-soclabs-mbv-axi-timer-clockevent-pland` | **AXI-timer clocksource/clockevent** (creates driver) | `drivers/clocksource/{Kconfig,Makefile}`, **new** `drivers/clocksource/timer-soclabs-mbv.c`, **new** `Documentation/devicetree/bindings/timer/soclabs,mbv-timer.yaml` |
| 0004 | `serial-uartlite-fix-in1-edge-level-race` | uartlite IRQ edge/level race fix | `drivers/tty/serial/uartlite.c` |
| 0005 | `DIAG-smsc911x-enable-USE_DEBUG-tracing` | diag tracing toggle | `drivers/net/ethernet/smsc/smsc911x.h` |
| 0006 | `smsc911x-disable-init-phy-loopback-selftest` | skip PHY loopback selftest | `drivers/net/ethernet/smsc/smsc911x.c` |
| 0007 | `riscv-mbv-route-busy-delays-off-frozen-rdtime-to-axi-timer` | **route `__delay` off frozen rdtime onto AXI-timer** | `arch/riscv/lib/delay.c`, `arch/riscv/include/asm/delay.h`, `drivers/clocksource/timer-soclabs-mbv.c` |

---

## 2. Dry-run apply result (the converge proof)

Pristine source: `src/linux_soc/linux/buildroot/dl/linux/linux-6.18.7.tar.xz`
(the Buildroot download tarball; 154 MB, present) extracted fresh to
`/tmp/kcp2/linux-6.18.7`.

Buildroot applies patches with `patch -F0 -g0 -p1 --no-backup-if-mismatch -t -N`
(`support/scripts/apply-patches.sh`). **`-F0` = fuzz factor 0 — Buildroot
requires a zero-fuzz apply or the build aborts.** The proof below uses
`--fuzz=0` to match.

### Sequential apply in Buildroot order (fuzz=0), fresh pristine tree

| Order | Patch | Result | Fuzz | Offset |
|------:|-------|--------|------|--------|
| 0001 | uartlite poll-mode | **APPLIES CLEAN** | 0 | 0 |
| 0002 | no-wfi idle | **APPLIES CLEAN** | 0 | 0 |
| 0003 | AXI-timer clocksource | **APPLIES CLEAN** | 0 | 0 |
| 0004 | uartlite race fix | **APPLIES CLEAN** | 0 | 0 |
| 0005 | smsc911x diag | **APPLIES CLEAN** | 0 | 0 |
| 0006 | smsc911x selftest | **APPLIES CLEAN** | 0 | 0 |
| 0007 | delay → AXI-timer | **APPLIES CLEAN** | 0 | 0 |

**All 7 apply clean, zero fuzz, zero offset, in lexical order.** This is the
order Buildroot uses and it satisfies every dependency (below).

### Ordering dependencies (why patches must be applied in-sequence, not in isolation)

A naive "each patch dry-run against *pristine* independently" gives two false
FAILs — both are ordering dependencies satisfied by the lexical sequence, not
real defects:

- **0004 depends on 0001.** Both edit `uartlite.c`; 0004's hunks land on
  context that 0001 introduces. Against pristine (no 0001), 3 of 4 hunks fail;
  applied *after* 0001, it is clean.
- **0007 depends on 0003.** 0007 patches `drivers/clocksource/timer-soclabs-mbv.c`,
  which **does not exist** until 0003 creates it. Against pristine, `patch`
  reports "can't find file to patch"; applied *after* 0003, it is clean.

Lexical order (0003 < 0004 < 0007) places every dependency before its
dependent, so the Buildroot-native ordering is correct. **No re-ordering is
required.**

---

## 3. Timer-patch coherence (0003 ↔ 0007)

The two timer fixes are **coherent and non-conflicting**:

- **File sets are disjoint except the driver file:**
  - 0003 → `drivers/clocksource/{Kconfig,Makefile}`, **creates**
    `timer-soclabs-mbv.c`, **creates** the DT binding yaml.
  - 0007 → `arch/riscv/lib/delay.c`, `arch/riscv/include/asm/delay.h`, and a
    small additive edit to `timer-soclabs-mbv.c`.
  - The only shared file is `timer-soclabs-mbv.c`. 0003 creates the whole file;
    0007 then patches three `@@` regions of the now-existing file (an
    `#include <linux/delay.h>`, a new `mbv_delay_read_cycles()` reader of
    `TCR1`, and one `riscv_register_delay_cycles(...)` call in the clocksource
    init after `TCR1` is enabled). **No hunk overlap** — 0007's additions sit
    between existing 0003 lines, not on top of them.

- **Symbol wiring closes end-to-end:**
  - 0007 **defines** `riscv_register_delay_cycles()` + `EXPORT_SYMBOL` in
    `arch/riscv/lib/delay.c`, and **declares** it in
    `arch/riscv/include/asm/delay.h`.
  - 0007's edit to `timer-soclabs-mbv.c` `#include <linux/delay.h>` and calls
    that exported symbol, registering `TCR1` as the busy-delay counter.
  - `__delay()` (0007, `delay.c`) reads the registered fn when present and
    falls back to `get_cycles()` (rdtime) during early boot before the timer
    probes. Both counters run at 100 MHz on this SoC, so no cycle rescaling.
  - The two patches are **complementary, not redundant**: 0003 fixes
    clocksource/clockevent (the frozen-rdtime *timekeeping* path); 0007 fixes
    the arch busy-delay (`udelay`/`ndelay`/`mdelay`) path, reusing 0003's
    counter. Consistent silicon rationale (rdtime freezes in a tight
    busy-spin; the AXI fabric counter does not).

**No conflict, no overlap, one required ordering dependency (0003 before
0007) — satisfied by lexical order.**

---

## 4. Built-tree ↔ patch-series reconciliation (the sign-off concern)

The sign-off risk was that today's fixes live **both** as patches **and** as
direct edits in the already-extracted build tree
(`src/linux_harness/sw/build/buildroot/output/build/linux-6.18.7/`), so nobody
had proven a clean extract reproduces them.

**Resolved.** The patched pristine tree (`/tmp/kcp2`) was diffed against the
live build tree:

- **Every one of the 11 patch-touched files is byte-for-byte identical**
  between `pristine+patches` and the build tree (uartlite.c, riscv Kconfig,
  cpuidle.h, clocksource Kconfig/Makefile, timer-soclabs-mbv.c, the DT yaml,
  smsc911x.{c,h}, delay.c, delay.h).
- **A full source-file diff** (`.c/.h/.S/.dts/.yaml/Kconfig/Makefile`,
  excluding generated dirs) found **zero** other source files differing
  between the two trees.
- The only files present solely in the build tree are **standard kernel
  build-generated artifacts** (kallsyms `.S`, ASN.1-compiler output,
  raid6 `int*.c`, radeon `*_reg_safe.h`, `crc32table.h`, apparmor
  `*_names.h`, etc.) and object/config output — **no hand edits**.

Conclusion: the build tree contains **nothing that the patch series does not
reproduce**. The "direct edits also in the build tree" are exactly the patch
series, already applied. A clean extract + patch apply reconstructs the
silicon-proven kernel source.

---

## 5. Converge checklist

For `make -C sw … linux-rebuild` from a clean extract to reproduce the
silicon-proven kernel, all of the following must hold — all are **TRUE** as of
this verification:

- [x] `BR2_GLOBAL_PATCH_DIR` points at `src/linux_harness/sw/patches` (harness
      dir), so `linux/0001..0007` are the applied set. *(defconfig, verified)*
- [x] Kernel version pinned to `6.18.7`; download tarball present at
      `src/linux_soc/linux/buildroot/dl/linux/linux-6.18.7.tar.xz`.
- [x] No `series` file in `patches/linux/` → lexical order 0001→0007, which
      satisfies both ordering deps (0001 before 0004; 0003 before 0007).
- [x] All 7 patches apply at **fuzz 0** (matches Buildroot's `patch -F0`);
      none rely on fuzz or offset.
- [x] 0003 creates `timer-soclabs-mbv.c`; 0007 patches it → 0003 must precede
      0007 (it does).
- [x] 0001 modifies `uartlite.c` context that 0004 patches → 0001 must precede
      0004 (it does).
- [x] Timer patches 0003/0007 are non-overlapping and symbol-complete
      (`riscv_register_delay_cycles` defined + declared + called).
- [x] Config fragments referenced by the defconfig exist:
      `src/linux_soc/linux/configs/kernel_fragment.config` and
      `src/linux_harness/sw/configs/kernel_fragment_harness.config`. The
      no-wfi workaround is **CONFIG-gated** (`RISCV_SOCLABS_NOWFI_IDLE`,
      default n); build.sh flips it for the `Image_nowfi` artifact.
- [x] Built tree == pristine+patches for all source (no un-captured edits).

### To make the converge self-evident (recommendations, not blockers)

- The current build tree is a **stateful** artifact. A truly clean proof is
  `rm -rf output/build/linux-6.18.7 && make linux-rebuild` (or
  `linux-dirclean`), so Buildroot re-extracts and re-applies from the tarball.
  This verification proved that path *equivalent* by reconstructing it in
  `/tmp` — it did not run the Buildroot build (out of scope).
- Nothing here depends on files another agent owns
  (`sw/configs/*`, `sw/build.sh`); those were read-only inputs.

---

## Appendix — commands used (all read-only, scratch in /tmp)

```
# pristine extract
tar -C /tmp/kcp2 -xf .../dl/linux/linux-6.18.7.tar.xz
# sequential apply, Buildroot order, fuzz 0
for p in patches/linux/*.patch; do patch -p1 --dry-run --fuzz=0 < $p; patch -p1 --fuzz=0 < $p; done
# reconcile against build tree
diff -rq /tmp/kcp2/linux-6.18.7 .../output/build/linux-6.18.7   # (filtered to source files)
```

Buildroot apply line (for reference):
`patch -F0 -g0 -p1 --no-backup-if-mismatch -t -N` — `support/scripts/apply-patches.sh`.
