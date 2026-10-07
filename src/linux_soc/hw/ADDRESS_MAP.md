# `linux_soc` — address map, CPU contract, and device-tree facts

**This file is the contract.** The device tree, OpenSBI, the kernel and Buildroot
all build against the numbers here. They are not aspirational: they are set in
[`mbv_soc.tcl`](mbv_soc.tcl) §0 and **read back off the built block design** into
`build/address_map.txt` by `make bd`. If the two ever disagree, `build/address_map.txt`
is the truth and this document is a bug.

- **Part:** `xcku115-flvb1760-1-c` (Arm MPS3, Kintex UltraScale KU115)
- **Block design:** `linux_soc` → wrapper `linux_soc_wrapper`
- **Toolchain:** Vivado **2026.1** (`$XILINX_VIVADO/Vivado/bin/vivado` — note the nested path)
- **Provenance:** evolved from the proven `poc/ddr4_mbv` spike (built, WNS +0.174 ns,
  DDR4 BFM calibration sim passed). Rebuild with `make bd`.

---

## 1. Address map

All addresses are **physical**, as seen by the MicroBlaze V.

| Region | Base | Size | Master space | Notes |
|---|---|---|---|---|
| LMB boot BRAM | `0x0000_0000` | 128 KiB | ILMB + DLMB | **reset vector lives here** |
| `axi_uartlite` | `0x4060_0000` | 64 KiB | M_AXI_DP | console, 115200 8N1 |
| `axi_intc` | `0x4120_0000` | 64 KiB | M_AXI_DP | interrupt controller |
| `axi_timer` | `0x41C0_0000` | 64 KiB | M_AXI_DP | kernel timebase, 2 counters |
| DDR4 | `0x8000_0000` | **1 GiB** | M_AXI_IC + M_AXI_DC | Linux system RAM |

Peripheral bases are the canonical Xilinx defaults, which is what every Xilinx DTS
generator and MicroBlaze reference DTS already assumes.

**Cacheable window = exactly the DDR aperture:** `0x8000_0000 … 0xBFFF_FFFF`
(`C_I/DCACHE_BASEADDR/HIGHADDR`). It is *derived* from the DDR constants in the Tcl,
never retyped — a cacheable window that fails to cover RAM is a silent ~10× slowdown.

### Why DDR is 1 GiB and not 2 GiB

Read this before "fixing" it:

- The fitted SO-DIMM is **4 GiB**. The MicroBlaze V AXI address is **32-bit**, so at
  most **2 GiB** is reachable at base `0x8000_0000` (the spike mapped 2 GiB).
- **rv32 Linux maps all of lowmem linearly from `PAGE_OFFSET` (`0xC000_0000` on 32-bit
  RISC-V) to the top of the address space — 1 GiB — and the RISC-V port has no
  highmem.** RAM beyond 1 GiB is simply unusable by the kernel.
- Mapping exactly what Linux can use keeps **silicon == device tree**, with no
  "declared but unreachable" tail.

To grow it you must change **both** `MAP_DDR_RANGE` in `mbv_soc.tcl` **and** the
`memory@` node below, together. That is a deliberate contract change, not a tweak.

---

## 2. Reset vector

```
reset vector = 0x0000_0000     (microblaze_riscv C_BASE_VECTORS)
```

This is both the **reset fetch address and the trap-vector base**. It points at the
**LMB BRAM**, so the core boots from block RAM — never from uncalibrated DRAM.

The reset sequencing is what makes this safe, and it is worth knowing:
`ddr4_0/c0_init_calib_complete` (active-high) is wired straight into
`proc_sys_reset_0/aux_reset_in` (active-low), so **the CPU is held in reset until DDR4
calibration completes**. The core cannot execute a single instruction until the DRAM
it will eventually jump into is live. A boot stub in BRAM may therefore assume DDR is
already calibrated.

---

## 3. CPU contract

| Property | Value | IP parameter |
|---|---|---|
| Core | MicroBlaze V (`microblaze_riscv:1.0`) | — |
| XLEN | 32 | `C_DATA_SIZE=32`, `C_ADDR_SIZE=32` |
| MMU | **Sv32, supervisor mode** | `C_USE_MMU=3` |
| ISA | **`rv32imac_zba_zbb_zbs`** | see below |
| Caches | 8 KiB I + 8 KiB D | `C_USE_ICACHE/DCACHE=1` |
| Optimisation | PERFORMANCE (0) | `C_OPTIMIZATION=0` |
| CPU / AXI clock | **100 MHz** | `clk_wiz_0` |
| DDR UI clock | 200 MHz | DDR4 `ui_clk` (= CK/4, 1600 MT/s) |

### The ISA is a hard constraint, not a preference

The RISC-V **glibc** toolchain shipped with this Vivado
(`gnu/riscv/linux_toolchain/lin32/bin/riscv32-amd-linux-gnu-gcc`) reports this default
sysroot (measured with `gcc -print-sysroot`):

```
.../riscv32imac_zicbom_zba_zbb_zbs-amd-linux
```

The **prebuilt glibc is compiled for `rv32imac` + Zicbom + Zba + Zbb + Zbs.** Any
extension glibc actually emits that the core lacks is an illegal-instruction trap in
userspace. GCC emits Zba/Zbb/Zbs freely in optimised code (glibc's string routines lean
on Zbb), and every `rv32imac` binary is dense with compressed instructions. The core
therefore enables **M, A, C, Zba, Zbb, Zbs**.

> The `poc/ddr4_mbv` spike ran **`rv32im` with `C_USE_COMPRESSION=0`** — correct for its
> hand-written bare-metal memtest, and **fatal for a glibc userland**. This is the single
> biggest functional change from the spike.

### ⚠ Zicbom: the vendor's own sysroot asks for an extension this core does not have

`microblaze_riscv` has **no Zicbom parameter** — verified against the complete `CONFIG.*`
space of the 2026.1 IP; no `C_*CBO*` / `C_*ZIC*` parameter exists. The IP does not
implement `cbo.clean` / `cbo.flush` / `cbo.inval`. Consequences for downstream:

- **Do not** advertise `zicbom` in the DTS `riscv,isa` string. The kernel only emits
  `cbo.*` when the ISA string permits it, so a correct DTS keeps it away from them.
- Userland glibc does **not** emit `cbo.*` (they are cache-maintenance ops), so the
  **prebuilt sysroot remains usable**.
- A **from-source Buildroot toolchain must target `rv32imac_zba_zbb_zbs`**, *not*
  `..._zicbom_...`.

### Deliberate performance cap (a finding, not an accident)

`C_OPTIMIZATION=2` (FREQUENCY) is **illegal together with `C_USE_MMU=3`** — the IP's xgui
clamps the MMU range to `{0,1}` when optimisation is FREQUENCY. Sv32 supervisor mode is
non-negotiable for Linux, so FREQUENCY optimisation is unavailable and this core's
achievable Fmax is capped accordingly. Carried over from the spike and re-confirmed.

### No Early-Access flag at 2026.1

The spike had to export the undocumented `AMD_VIVADO_MICROBLAZE_V_EA` because Vivado
2024.1 gated `C_USE_MMU=3` behind it (and *silently refused* the value without it).
**Measured on this 2026.1 install with the variable unset: `C_USE_MMU=3` is accepted and
reads back `3`.** An MMU-enabled MicroBlaze V is a **production** configuration at 2026.1.
Do **not** set that variable; `build.tcl` warns if it is set.

Because this IP's documented failure mode is *silently keeping the old value*, every
Linux-critical parameter is **read back and asserted** in `mbv_soc.tcl` (`_assert_cfg`).
A core that is not the contracted core fails the build instead of reaching a bench.

---

## 4. Interrupt map

`axi_timer` and `axi_uartlite` → `xlconcat` → `axi_intc/intr[1:0]` → `axi_intc/interrupt`
→ MicroBlaze V `INTERRUPT` bus interface.

| INTC input | Source |
|---|---|
| **0** | `axi_timer` |
| **1** | `axi_uartlite` |

**All inputs are LEVEL-sensitive:** `C_KIND_OF_INTR = 0x0000_0000`.

The encoding is confirmed from the IP's own xgui tooltip
(`axi_intc_v4_1.tcl:117`): **"0 = Level, 1 = Edge"**. Both sources are level —
`axi_timer` holds its interrupt until `T0INT` is written back, and `axi_uartlite`'s
follows the FIFO state.

> This value **must** equal the DTS property `xlnx,kind-of-intr`. Linux's
> `irq-xilinx-intc` driver selects the edge vs level `irq_chip` straight off that word,
> so a mismatch means lost or permanently-stuck interrupts.

### `C_NUM_INTR_INPUTS` is read-only and *derived* — a trap worth knowing

On `axi_intc:4.1` this parameter **defaults to 1** and is **read-only**:

```
report_property [get_bd_cells axi_intc_0]
  CONFIG.C_NUM_INTR_INPUTS   string   true(read-only)   1
```

`set_property` on it is a **silent no-op** — Vivado returns success and the value stays
`1`. The width is instead derived from the net driving `intr` (our 2-bit `xlconcat`)
during `validate_bd_design`'s parameter-propagation pass. `mbv_soc.tcl` therefore
asserts it is `2` **after** validate.

This matters: had propagation not happened, the INTC would physically have **one** input,
the uartlite interrupt would be wired into nothing, and the DTS's `interrupts = <1 2>`
would name an input that does not exist — with nothing else in the flow complaining.

---

## 5. Device tree

### Compatible strings (what the kernel actually matches on)

| Node | `compatible` | Linux driver |
|---|---|---|
| Console | `xlnx,xps-uartlite-1.00.a` | `drivers/tty/serial/uartlite.c` |
| Interrupt controller | `xlnx,xps-intc-1.00.a` | `drivers/irqchip/irq-xilinx-intc.c` |
| Timer | `xlnx,xps-timer-1.00.a` | Xilinx AXI timer clocksource/clockevent |
| RAM | node name `memory@80000000` | — |

### Skeleton

Facts below that come from *this hardware* are authoritative. Items marked
**OPEN** are **not** determined by the hardware — see §6 before trusting them.

```dts
/dts-v1/;
/ {
    #address-cells = <1>;
    #size-cells    = <1>;
    model          = "SoCLabs linux_soc - MicroBlaze V on Arm MPS3 (KU115)";

    chosen {
        bootargs    = "console=ttyUL0,115200 earlycon";
        stdout-path = "serial0:115200n8";
    };

    aliases { serial0 = &uartlite; };

    cpus {
        #address-cells = <1>;
        #size-cells    = <0>;
        timebase-frequency = <100000000>;   /* 100 MHz - see OPEN item in section 6 */

        cpu@0 {
            device_type     = "cpu";
            reg             = <0>;
            compatible      = "amd,mbv32", "riscv";
            riscv,isa       = "rv32imac_zba_zbb_zbs";   /* NO zicbom - core lacks it */
            mmu-type        = "riscv,sv32";
            clock-frequency = <100000000>;

            cpu_intc: interrupt-controller {
                compatible          = "riscv,cpu-intc";
                #interrupt-cells    = <1>;
                interrupt-controller;
            };
        };
    };

    /* 1 GiB at 0x8000_0000. MUST match MAP_DDR_RANGE in mbv_soc.tcl. */
    memory@80000000 {
        device_type = "memory";
        reg         = <0x80000000 0x40000000>;
    };

    soc {
        compatible     = "simple-bus";
        #address-cells = <1>;
        #size-cells    = <1>;
        ranges;

        intc: interrupt-controller@41200000 {
            compatible          = "xlnx,xps-intc-1.00.a";
            reg                 = <0x41200000 0x10000>;
            interrupt-controller;
            #interrupt-cells    = <2>;
            xlnx,kind-of-intr   = <0x0>;    /* all LEVEL - must match C_KIND_OF_INTR */
            xlnx,num-intr-inputs = <2>;

            interrupt-parent = <&cpu_intc>;
            interrupts       = <9>;         /* OPEN: 9 = S-mode ext, 11 = M-mode ext */
        };

        timer@41c00000 {
            compatible      = "xlnx,xps-timer-1.00.a";
            reg             = <0x41c00000 0x10000>;
            interrupt-parent = <&intc>;
            interrupts      = <0 2>;        /* INTC input 0, level-high */
            clock-frequency = <100000000>;  /* s_axi_aclk */
            xlnx,one-timer-only = <0>;      /* BOTH counters present */
        };

        uartlite: serial@40600000 {
            compatible      = "xlnx,xps-uartlite-1.00.a";
            reg             = <0x40600000 0x10000>;
            interrupt-parent = <&intc>;
            interrupts      = <1 2>;        /* INTC input 1, level-high */
            current-speed   = <115200>;
            xlnx,data-bits  = <8>;
            xlnx,use-parity = <0>;
            clock-frequency = <100000000>;
        };
    };
};
```

---

## 6. OPEN RISKS — read before writing the kernel/DTS

These are **not** things this hardware settles. Stating them plainly beats a
confident-looking DTS that does not boot.

### 6.1 There is no CLINT / no `mtime`, and RISC-V Linux normally requires one

The full `CONFIG.*` space of `microblaze_riscv:1.0` contains **no CLINT, no PLIC, and no
machine-timer parameter** (verified by dumping every parameter). This is why the design
uses `axi_intc` + `axi_timer`: it is the AMD-sanctioned MicroBlaze V model, and it is what
this brief specified.

But mainline RISC-V Linux normally wants:
- a `time` CSR / `mtime` for `timer-riscv` (`rdtime`), and
- `/cpus/timebase-frequency`, which `arch/riscv/kernel/time.c` **panics** without;
- an external interrupt delivered to S-mode (`interrupts = <9>`), which classically comes
  from a PLIC context.

**What the kernel agent must determine** (from AMD's MicroBlaze V Linux reference —
`meta-microblaze` / the `mbv` device trees — not from first principles):
1. Does the kernel bind the **Xilinx AXI timer** as clocksource/clockevent instead of
   `timer-riscv`, and does `timebase-frequency` still need to be present?
2. Does the `INTERRUPT` bus interface land on **`meip` (11)** or **`seip` (9)**, and how
   does OpenSBI (M-mode) forward it to an S-mode kernel? The `interrupts = <9>` in the
   skeleton above is the **S-mode assumption**, not a measured fact.

If it turns out the core cannot deliver an external interrupt to S-mode, that is a
**hardware-visible** finding and this BD must change — flag it back rather than working
around it in software.

### 6.2 Boot flow / kernel load address

The hardware fixes only the **reset vector (`0x0000_0000`, in BRAM)**. Everything else is
a software convention and is **not** pinned by this design. The natural layout, given a
4 MiB-aligned rv32 kernel:

```
0x8000_0000   OpenSBI (M-mode firmware)
0x8040_0000   Linux kernel     (4 MiB aligned - Sv32 superpage)
              DTB + initramfs  (placed by the loader)
```

The 128 KiB LMB is sized to hold a first-stage stub (jump-to-DDR, or a small SREC
loader); OpenSBI and the kernel run from DDR. For board-free bring-up, loading DDR over
JTAG/XSDB and setting the PC is sufficient — the CPU is already held in reset until DDR
calibration completes (§2).

---

## 7. Regenerating / verifying

```bash
cd src/linux_soc/hw
make bd                     # elaborate + validate_bd_design; emits build/address_map.txt
cat build/address_map.txt   # the map AS ACTUALLY ASSIGNED by Vivado - check it against section 1
make all                    # full synth+impl+reports -> build/reports/RESULT.txt
make fw                     # bare-metal DDR memtest (smoke test for the map)
```

`build/address_map.txt` is generated by walking the built BD's address spaces, not by
restating the constants — so if `assign_bd_address` ever snaps a range, the snapped value
shows up there instead of shipping a device tree that lies.
