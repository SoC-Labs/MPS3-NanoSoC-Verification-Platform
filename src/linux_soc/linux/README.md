# rv32 Linux for `linux_soc` (AMD MicroBlaze V, Sv32) — kernel, userland, board DTB

Builds a RISC-V 32-bit (RV32IMAC, Sv32, glibc) Linux for the `linux_soc` MicroBlaze V
SoC, proves the kernel + userland by booting them to a login shell on
`qemu-system-riscv32 -M virt`, and produces the real-SoC device tree.

```
./build.sh        # toolchain wrapper -> Buildroot -> DTB -> artifacts/   (~30-60 min)
./boot_qemu.sh    # boot artifacts on the Buildroot-built QEMU; writes qemu_boot.log
```

| Thing | Where |
|---|---|
| Buildroot defconfig | `configs/mbv_linux_defconfig` |
| Kernel config fragment | `configs/kernel_fragment.config` |
| Board device tree | `mbv_soc.dts` → `mbv_soc.dtb` |
| Artifacts (Image, rootfs, dtb, OpenSBI) | `artifacts/` |
| QEMU boot proof | `qemu_boot.log` |
| Toolchain wrapper generator | `mk_toolchain_wrapper.sh` |

Tracked: configs, DTS, scripts, this README, the boot log. Ignored (regenerable):
`buildroot/`, `device-tree-xlnx/`, `toolchain_wrapper/`, `artifacts/`, `logs/`.

---

## 1. Toolchain — the vendor toolchain lies about its own defaults

No toolchain is built from source. Buildroot consumes AMD's **prebuilt** rv32 glibc
toolchain as `BR2_TOOLCHAIN_EXTERNAL`. Facts **measured off the toolchain**, not assumed:

| | |
|---|---|
| gcc | 13.4.0 |
| glibc | **2.39** |
| kernel headers | 6.6 |
| sysroot ISA | `rv32imac_zicbom_zba_zbb_zbs` |
| sysroot ABI | **`ilp32` — SOFT float** (`ld-linux-riscv32-ilp32.so.1`) |

**The trap:** the vendor `gcc`'s built-in default is `-march=rv32imafdc -mabi=ilp32d`
(*hard* float). That default is wrong twice over:

1. this core has `C_USE_FPU=0` — there is no FPU; and
2. it cannot even compile hello-world **against its own sysroot** (the shipped glibc is
   soft-float `ilp32`). Verified: with no flags, `gcc hello.c` fails; with
   `-march=rv32imac -mabi=ilp32` it links fine (multilib dir `rv32imac/ilp32`).

Buildroot probes the compiler **with no ABI flags** in places
(`check_toolchain_ssp`, called from `pkg-toolchain-external.mk:607`), hits that broken
default, and dies with a *misleading* `SSP support not available in this toolchain`.

So `BR2_TOOLCHAIN_EXTERNAL_PATH` points at **`toolchain_wrapper/`** — three generated
driver scripts (`gcc`/`g++`/`cpp`) that call the vendor's *real* compiler binaries with
`--sysroot=<vendor glibc sysroot>` and a **correct default** `-march`/`-mabi`. Flags go
*before* `"$@"`, so an explicit `-march` from Buildroot still wins. Regenerate with
`mk_toolchain_wrapper.sh`; nothing under `<EDA-install>` is written.

### The F/D landmine (this one would have shipped)

`BR2_RISCV_ISA_RVF`/`RVD` are **`select`ed by `BR2_riscv_g`**, which is the *default*
choice for "Target Architecture Variant". A `select` **cannot** be undone with
`# BR2_RISCV_ISA_RVF is not set`. Leaving the default gives
`-march=rv32ima`**`fd`**`c`: `-mabi=ilp32` only makes the *calling convention*
soft-float — gcc still emits **real F/D instructions**, which **illegal-instruction-trap
on this FPU-less core**… while booting perfectly under qemu-virt, which *has* an FPU.

That is a silent, **board-only** failure that the QEMU proof would happily pass.
`BR2_riscv_custom=y` is what actually turns F/D off. `build.sh` asserts the final
`-march` before it will start.

Also: `BR2_TOOLCHAIN_EXTERNAL_INET_RPC` must be **off** — glibc 2.39 removed sunrpc.

### zicbom: named, but not used (checked, not assumed)

The sysroot is *named* `...zicbom...`, but the core has no zicbom parameter. I
disassembled the shipped `libc.so.6`, `libm.so.6` and the dynamic loader: **zero**
`cbo.*` instructions and **zero** F/D instructions. So the prebuilt userland is safe on
this core. **Do not** advertise `zicbom` in `riscv,isa` — the *kernel* will emit `cbo.*`
if you do.

---

## 2. The board timer — the contract's "open risk #1" is RESOLVED

The contract flagged: *no CLINT, no mtime, no PLIC — must confirm the kernel gets a
timebase.* Resolved, and **not** the way the contract guessed.

Established facts:

* **There is no CLINT and no PLIC.** The Vivado 2026.1 IP catalog contains neither, and
  `microblaze_riscv` has no mtime/mtimecmp parameter.
* **Mainline has no Xilinx AXI-timer clocksource driver.** `drivers/clocksource` in
  6.18.7 contains *zero* xilinx files (`include/clocksource/timer-xilinx.h` exists only
  because `drivers/pwm/pwm-xilinx.c` shares the register defs). Neither does AMD's own
  `linux-xlnx`. So the `axi_timer` **cannot** be the kernel's timebase.

That looks fatal, and isn't — because the **core** provides both halves itself:

| Need | Provided by | IP parameter | Default |
|---|---|---|---|
| clocksource | `rdtime` (Zicntr) | `C_USE_COUNTERS` | **1** |
| clockevent | `stimecmp` (**Sstc**) | `C_USE_SSTC` | **1** |

`drivers/clocksource/timer-riscv.c` writes `CSR_STIMECMP` directly when
`riscv_sstc_available`, and only falls back to `sbi_set_timer()` (which *would* need a
CLINT) when it is not. `C_USE_SSTC` is a hidden IP param — *"Enable RISC-V Supervisor
Time Compare (SSTC)… only available when supervisor mode is enabled"* — and supervisor
mode **is** enabled (`C_USE_MMU=3`). AMD's own BSP confirms `rdtime` is real: `xiltimer`
`microblaze_riscv_sleep.c` calls `rdtime()` scaled by `Xil_GetRISCVFrequency()`, which is
why `timebase-frequency = 100 MHz` (the CPU clock).

**`riscv_sstc_available` is set from the device tree.** Drop `"sstc"` from
`riscv,isa-extensions` and the kernel silently falls back to an SBI timer this hardware
cannot provide → no clockevent → no boot. It is in `mbv_soc.dts`. Leave it there.

### → Action for the HW track

`mbv_soc.tcl` does **not** currently read-back-assert `C_USE_SSTC` or `C_USE_COUNTERS`;
it relies on their defaults. Given this IP's documented failure mode is *silently keeping
the old value* (the HW agent already got burned by `C_NUM_INTR_INPUTS`), **assert both
`== 1`**. They are now load-bearing for boot.

Interrupts are fine as designed: `irq-xilinx-intc.c` supports being a **chained**
controller, so `axi_intc` hangs off `riscv,cpu-intc` at hwirq **9** (S-mode external).
`xlnx,kind-of-intr` **must** equal the hardware's `C_KIND_OF_INTR` (`0x0`, all level) —
the driver picks the edge-vs-level `irq_chip` straight off that word, and a mismatch
gives lost/stuck interrupts with no error message.

**~~Still open (firmware, not hardware)~~ — CLOSED, see §5.4.** This section used to warn
that OpenSBI's `generic` platform "expects a CLINT" and that we would likely need a custom
OpenSBI platform or an SBI shim in the boot BRAM — *"the biggest remaining risk to
boot-to-shell on silicon."* **That was wrong.** Reading the OpenSBI 1.6 source rather than
assuming: `fdt_ipi_init()` and `fdt_timer_init()` *explicitly* return success when no
device matches — the code comments are literally *"On some single-hart system there is no
need for IPIs"* and *"Systems with Sstc might not provide any node in the FDT"*. The
`generic` platform is **designed** for a single-hart, no-CLINT, no-PLIC, Sstc machine, and
it ships a uartlite console driver. Stock `platform=generic` is the right firmware. No shim
needed.

---

## 3. Device tree — hand-authored, because the vendor flow cannot do it

The brief preferred XSA + `xsct` + `Xilinx/device-tree-xlnx`. **That flow cannot
generate this DTS.** device-tree-xlnx master (`239f035`, 2026-03-05) has **zero**
references to `riscv`/`mbv`, and its CPU handler declares:

```
cpu/data/cpu.mdd:21:  OPTION supported_peripherals = (microblaze);
```

— classic MicroBlaze only. There is no MicroBlaze V DT generator, and PetaLinux (which
would otherwise ship one) is not installed. So `mbv_soc.dts` is hand-authored.

It was *originally* written against `hw/ADDRESS_MAP.md` (prose) alone. **It is now audited
node-by-node against the real hardware handoff `hw/build/linux_soc.xsa`** — the XSA exists,
and while `device-tree-xlnx` still cannot *generate* from it, it is perfectly readable as
ground truth (it is a zip; the facts are in `linux_soc.hwh`). Every base address, IRQ line,
clock and ISA bit in the DTS is now traced to an XSA parameter or netlist connection. **See
§5 — that audit is the real deliverable, and it found four defects the address map could
never have caught.**

The address map itself checks out exactly: RAM `0x8000_0000` + 1 GiB, uartlite
`0x4060_0000` (console, 115200 8N1, INTC input 1), `axi_intc` `0x4120_0000`, `axi_timer`
`0x41C0_0000` (INTC input 0 — described, but **unused by Linux**, see §2).

---

## 3b. Vendor-toolchain gotchas that actually broke the build

All four were hit for real; each is fixed in-tree. Listed so nobody re-debugs them.

| Symptom | Real cause | Fix |
|---|---|---|
| `SSP support not available in this toolchain` | Buildroot probes the compiler **with no ABI flags** (`pkg-toolchain-external.mk:607`); the vendor default ISA can't compile against its own sysroot | `toolchain_wrapper/` gives the toolchain a correct **default** `-march`/`-mabi` |
| `RPC support not available in C library` | glibc 2.39 dropped sunrpc | `# BR2_TOOLCHAIN_EXTERNAL_INET_RPC is not set` |
| busybox `Error 127`, `gcc-ar: No such file or directory` | vendor tool scripts locate their payload with **`dirname $0`**, and `$0` is the *invocation* path — Buildroot symlinks them into `host/bin`, so the relative hop breaks | wrapper emits **absolute-path** scripts for *every* tool, not symlinks |
| `ERROR: we shouldn't have a /etc/ld.so.conf file` | the vendor sysroot ships one, and it rsyncs into the target | `TARGET_FINALIZE_HOOK` in `br2_external/` — **not** a post-build script (hooks run at Makefile:759, the check is at :788, post-build only at :827, i.e. too late) |

And the one that actually panicked the kernel:

> **`Failed to execute /sbin/init (error -2)` → `Kernel panic - No working init found`**, with
> `/sbin/init` plainly present. `-2` is ENOENT *on the ELF interpreter*, not on init. The AMD
> sysroot is **merged-/usr** — it has no `/lib` at all, libc and the loader live only in
> `usr/lib` — so Buildroot copied them to `target/usr/lib`, while every binary's INTERP is
> `/lib/ld-linux-riscv32-ilp32.so.1`. A non-merged skeleton leaves `/lib` a real, loader-less
> directory. Fix: **`BR2_ROOTFS_MERGED_USR=y`**, which makes `/lib` a symlink to `/usr/lib`.
> Without it the kernel boots perfectly and userland never starts.

---

## 4. What the QEMU proof does and does not cover

`boot_qemu.sh` boots the artifacts on the Buildroot-built `qemu-system-riscv32 -M virt`,
logs in as root and runs commands. **Result: PASS** — see `qemu_boot.log`.

```
Linux version 6.18.7 (riscv32-amd-linux-gcc.real (GCC) 13.4.0) ... 
MicroBlaze-V rv32 Linux (SoCLabs linux_soc)
mbv-linux login: root
# uname -a
Linux mbv-linux 6.18.7 #1 SMP riscv32 GNU/Linux
# echo SHELL_PROOF_$((6*7))
SHELL_PROOF_42
```

**It proves:** the kernel builds and boots at RV32/Sv32; the glibc userland built with
the AMD vendor toolchain actually executes (a dynamically-linked shell ran commands);
init, syslogd, klogd, crond all come up; clean poweroff. That is the whole software stack
minus the board.

Asserted on the shipped binaries (not assumed): busybox, libc.so.6 and vmlinux each
contain **0** F/D instructions and **0** `cbo.*` instructions, and are soft-float `ilp32`.
Note `/proc/cpuinfo` under qemu-virt reports `rv32ima`**`fdc`**`h_…` — QEMU's CPU *has* an
FPU. That is exactly why the F/D landmine in §1 is dangerous: a userland with F/D would
pass this very boot and still die on the real core.

**It does NOT prove the board.** `-M virt` and `linux_soc` differ in every I/O detail:

| | qemu-virt (proved) | linux_soc (not yet run) |
|---|---|---|
| console | ns16550 `ttyS0` | **uartlite `ttyUL0`** @ `0x4060_0000` |
| interrupts | PLIC | **axi_intc** chained to `riscv,cpu-intc` |
| timer | CLINT + SBI | **`rdtime` + `stimecmp` (Sstc)** |
| RAM base | `0x8000_0000` | `0x8000_0000` (same) |
| DTB | QEMU generates it | **`mbv_soc.dtb`** |
| rootfs | virtio-blk ext2, `root=/dev/vda` | **initramfs only** — no block device exists |
| M-mode fw | OpenSBI `generic` | OpenSBI `generic` — **resolved**, see §5.3 |

The **rootfs** row is the one that bit us: the QEMU run mounted an ext2 disk over
virtio-blk, so it never exercised the initramfs path — and `linux_soc` has no block
device of any kind. See §5.2.

One kernel `Image` serves both: `configs/kernel_fragment.config` compiles in
`SERIAL_UARTLITE` + `XILINX_INTC` alongside the virt drivers, and the DTB decides what
probes. One rootfs serves both: getty runs on `console` (**not** `ttyS0`), so
`/dev/console` follows whatever `console=` the kernel got.

**Next step on silicon** is not "boot Linux" — it is: bring up an SBI/M-mode story (§2),
then `earlycon=uartlite,mmio32,0x40600000`, which is the first thing that will print.

---

## 5. The DTS, audited against the real XSA

The device tree was previously written against `hw/ADDRESS_MAP.md` (prose). It is now
cross-checked, node by node, against the actual hardware handoff
`hw/build/linux_soc.xsa` — a zip; the truth is in `linux_soc.hwh`. Where the doc and the
XSA could disagree, the XSA wins.

**Every address in the XSA agrees with `ADDRESS_MAP.md`.** The map is not the problem.
The problems were all in what the DTS *did not say*.

### 5.1 Verified against `linux_soc.hwh` (no change needed)

| DTS | XSA fact | |
|---|---|---|
| `memory@80000000 reg = <0x80000000 0x40000000>` | `ddr4_0` `C0_DDR4_ADDRESS_BLOCK` `0x8000_0000..0xBFFF_FFFF` on IC **and** DC | ✅ |
| `serial@40600000 reg = <… 0x10000>` | `C_BASEADDR 0x40600000` / `C_HIGHADDR 0x4060FFFF` | ✅ |
| `interrupt-controller@41200000` | `C_BASEADDR 0x41200000` / `0x4120FFFF` | ✅ |
| `timer@41c00000` | `C_BASEADDR 0x41C00000` / `0x41C0FFFF` | ✅ |
| `current-speed = <115200>` | `C_BAUDRATE = 115200` | ✅ |
| `xlnx,data-bits = <8>`, `use-parity = <0>` | `C_DATA_BITS=8`, `C_USE_PARITY=0` | ✅ |
| `xlnx,kind-of-intr = <0x0>` | `C_KIND_OF_INTR = 0x00000000` (all **level**) | ✅ |
| `xlnx,num-intr-inputs = <2>` | `C_NUM_INTR_INPUTS = 2` — propagation **did** widen the read-only default of 1 | ✅ |
| uartlite `interrupts = <1 …>` | netlist: `axi_uartlite_0/interrupt → intr_concat/In1` | ✅ |
| timer `interrupts = <0 …>` | netlist: `axi_timer_0/interrupt → intr_concat/In0` | ✅ |
| `xlnx,one-timer-only = <0>` | `C_ONE_TIMER_ONLY=0`, `enable_timer2=1` | ✅ |
| `mmu-type = "riscv,sv32"` | `C_USE_MMU = 3` | ✅ |
| `clock-frequency = <100000000>` | `C_FREQ = 100000000` | ✅ |
| isa `m`,`a`,`c` | `C_USE_MULDIV/ATOMIC/COMPRESSION = 1` | ✅ |
| isa `zba`,`zbb`,`zbs` — and **no `zbc`** | `C_USE_BITMAN_A/B/S = 1`, **`C_USE_BITMAN_C = 0`** | ✅ |
| isa `zicntr` | `C_USE_COUNTERS = 1` | ✅ |
| isa **`sstc`** | **`C_USE_SSTC = 1`** — the load-bearing one | ✅ |
| no `zicbom` | the IP has no such parameter; the core lacks `cbo.*` | ✅ |

The cacheable window (`C_I/DCACHE_BASEADDR..HIGHADDR = 0x8000_0000..0xBFFF_FFFF`) is
*exactly* the DDR aperture, and `C_BASE_VECTORS = 0x0` confirms the reset vector.

### 5.2 What was actually wrong

1. **No rootfs. (fatal)** The DTS had no `linux,initrd-start` / `linux,initrd-end`. This
   SoC has **no block device at all** — the BD is DDR + uartlite + intc + timer; there is
   no virtio, no SD, no flash on the AXI map — and the kernel is built
   `CONFIG_INITRAMFS_SOURCE=""`, so nothing is baked in. An external initramfs is the
   *only* rootfs path, and the DT is the only way to point at one. As shipped, the kernel
   would have unpacked nothing, found no `/init`, and panicked
   `VFS: Unable to mount root fs on unknown-block(0,0)`. **The QEMU proof hid this
   completely** — it mounted an ext2 disk over virtio-blk (`root=/dev/vda`).
2. **`root=/dev/ram0 rw` named a device the kernel cannot create.** `CONFIG_BLK_DEV_RAM`
   is **not set**. (Also `init=/sbin/init` was pointless — the cpio has `/init`.) Both are
   gone; `bootargs` is now just `console=` + `earlycon=`.
3. **uartlite's clock never bound.** `uartlite.c` calls `devm_clk_get(dev, "s_axi_aclk")` —
   a *named* lookup — and the node had `clocks` but no `clock-names`, so it silently got
   `-ENOENT`. Benign (uartlite's baud is fixed in hardware, and the clock is optional), but
   wrong. Added `clock-names = "s_axi_aclk"`.
4. **IRQ type cells were `0` = `IRQ_TYPE_NONE`.** The hardware is level, active-high
   (`C_KIND_OF_INTR=0x0`, `C_IRQ_ACTIVE=0x1`, `Sense_of_IRQ_Level_Type=Active_High`), so
   both lines are now `4` = `IRQ_TYPE_LEVEL_HIGH`. In practice cosmetic — `irq-xilinx-intc`
   has no `.irq_set_type` and picks edge-vs-level from `xlnx,kind-of-intr` — but the DT
   should describe the hardware. *(`ADDRESS_MAP.md`'s skeleton says `<0 2>`, which is
   `IRQ_TYPE_EDGE_FALLING` and contradicts its own "level-high" comment — a doc bug in
   `hw/`, reported not fixed.)*

### 5.3 The boot map is **not** a free choice — and it is tight

`ADDRESS_MAP.md` §6.2 says "DTB + initramfs placed by the loader". That is **not true**.
OpenSBI 1.6 `fw_jump` is built `FW_PIC` for `platform=generic`, and its next-stage
addresses are baked in PC-relative (read out of the binary with `objdump`):

```
fw_next_addr = _fw_start + 0x0040_0000   -> kernel
fw_next_arg1 = _fw_start + 0x0220_0000   -> DTB      (a1 is FORCED to this)
fw_next_mode = 1                          -> PRV_S    (drops to S-mode. correct.)
```

Loading OpenSBI at the DDR base therefore *fixes* the kernel at `0x8040_0000` — which
happily is both 4 MiB-aligned (rv32 Sv32 superpage) and exactly `DDR_BASE + text_offset`
(the Image header's `text_offset` is `0x400000`) — and the DTB at `0x8220_0000`.

> **The squeeze:** the kernel's *effective* size (Image header `image_size`, which
> **includes bss**) is `0x1DE4000` = 29.9 MiB, so it ends at `0x821E_4000` — **112 KiB**
> below the forced DTB slot. A few more drivers and the kernel's bss clear silently eats
> the DTB. `mk_dtb.sh` now **fails the build** if that margin goes negative.

Two more traps, both silent:

- **Do not `elf load` `fw_jump.elf`.** It is linked at vaddr `0x0` — which on this SoC is
  the **128 KiB LMB BRAM** — and it is 276 KiB. It would overrun the BRAM. Load the raw
  **`fw_jump.bin`** at `0x8000_0000`.
- **`a1` must be set at entry.** OpenSBI's `fw_prev_arg1` returns 0, so it takes its *own*
  fdt pointer from whatever the previous stage left in `a1`. Garbage there =
  `fdt_check_header failed` = dead port.

`./mk_dtb.sh` builds the DTB, stamps `linux,initrd-end` from the real `rootfs.cpio.gz`
size, asserts all of the above, and prints the XSDB load recipe.

### 5.4 OPEN RISK #1 (`ADDRESS_MAP.md` §6.1) — now closed

The contract flagged `interrupts = <9>` on the AXI INTC as "the **S-mode assumption**, not
a measured fact", and it was right to: `microblaze_riscv` has a single `Interrupt` pin, and
the bare-metal BSP treats it as **machine** external (`xil_exception.c` sets `MIE_MEIE`,
bit 11; `XIL_INTERRUPT_ID_MACHINE_EXTERNAL = 0x800000B`). That is M-mode, not S-mode.

Settled from the IP's own changelog:

```
2025.1.1 (Rev.6): Feature Enhancement: Implement supervisor mode delegation
2025.2   (Rev.7): Feature Enhancement: Include support for Supervisor External Interrupt
2025.2   (Rev.7): Feature Enhancement: Implement Supervisor Timer Register extension (Sstc)
```

This build is **2026.1 / Rev. 9**, so the core implements `seip` and `mideleg`. `<9>`
(`IRQ_S_EXT`) is correct for an S-mode kernel. **Watch for it anyway at bring-up:** the
failure is specific and quiet — the box boots and prints fine (both earlycon and OpenSBI
*poll* the uartlite, no IRQ needed) and then wedges the instant anything waits on a UART RX
interrupt, i.e. at the login prompt. `cat /proc/interrupts` showing 0 on the uartlite line
is the tell. That would be a **hardware** finding, not a software one.

The rest of §6.1 is closed too: OpenSBI `generic` is *explicitly* built to tolerate a
single-hart, no-CLINT, no-PLIC, Sstc machine — `fdt_ipi_init()` ("*no need for IPIs*") and
`fdt_timer_init()` ("*Systems with Sstc might not provide any node in the FDT*") both
return success with no device. It detects Sstc by **trap-probing `CSR_STIMECMP`** (hardware
truth, not the DT) and then sets `menvcfg.STCE`, which is what lets S-mode Linux write
`stimecmp` at all. It has a real uartlite console driver
(`fdt_serial_xlnx_uartlite`, matching `xlnx,xps-uartlite-1.00.a`), resolved via
`stdout-path` → `/aliases`. And `fdt_reserved_memory_fixup()` appends `/reserved-memory`
covering its own PMP-protected firmware, so `memory@80000000` can honestly start at the DDR
base. (That fixup *grows the FDT in place by 1 KiB* — which is why the DTB is now built
padded, `dtc -p 4096`; the old one had **0 bytes** of slack.)

### 5.5 Still unproven

- **`timebase-frequency = <100000000>`.** The XSA fixes the CPU clock at 100 MHz
  (`C_FREQ`), but "`rdtime` ticks once per CPU clock" is inferred from AMD's BSP
  (`xiltimer` scales `rdtime()` by `Xil_GetRISCVFrequency()`), not measured. If it is wrong
  the box still boots — it just keeps bad time. Check `date` against a stopwatch.
- **The DTB has never been executed.** Nothing here has run on silicon.
