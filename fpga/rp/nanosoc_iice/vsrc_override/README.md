# `vsrc_override/` — local source overrides for the Synplify front end

**Standing project rule:** never modify anything under the Arm IP library (`$ARM_IP_LIBRARY_PATH/**`)
or any read-only upstream checkout. If a fix appears to need an IP-library
change, copy the affected file into the project tree, point the file list at
the local copy, and document the deviation here. That is what this directory
is.

The substitution is mechanical and it **fails loudly**: `gen_prj.tcl` builds a
`basename -> local path` map from this directory, walks the flattened nanosoc
file list, and replaces each matching entry. If a `basename` in this directory
does not appear exactly once in the upstream list, project generation **errors
out** — so a rename or removal upstream cannot silently leave a stale override
in the build (or, worse, leave the upstream file in the build while the
override is quietly ignored).

Nothing here is read by any other flow: `fpga/rp/nanosoc/ooc_synth.tcl`
(Vivado) and every simulation flow keep reading the upstream files unchanged.

---

## 1. `sl_fpga_rom_word.v`

Upstream: `$SOCLABS_NANOSOC_SOC_DIR/src/rtl/fpga_lib/rom/sl_fpga_rom_word.v`
(read-only checkout; a vendored copy of the same file also exists in
`ethernet-subsystem-ahb`).

This module **is instantiated** and **is load-bearing** — it is the IMEM/DMEM
BRAM, `$readmemh`-preloaded with the firmware image. Two deviations:

| # | Upstream | Local | Why |
|---|---|---|---|
| 1 | `(* ram_style = "block" *)` only | keeps it, **adds** `/* synthesis syn_ramstyle="block_ram" */` | `ram_style` is Vivado dialect. Synplify's equivalent is `syn_ramstyle`. Both are retained (they say the same thing), so the file stays readable by Vivado, VCS and Synplify unchanged. Pinning it matters: an instrumented build must not silently spill a 16 KiB memory into LUTRAM and blow the RP pblock's LUT budget. |
| 2 | one `initial` block containing a zero-fill `for` loop **and** `$readmemh` on the same array | `$readmemh` only | The Synopsys VCS-based front end rejects two initialisations of one memory as `Error-[MULTI-MEM-INIT-SAME-HIERSIG]` ("Multiple memory initializations for same hierarchical signal"). **This is a real, previously-hit failure**, fixed the same way and validated at `nanosoc-ethernet-chiplet/fpga/haps-sx-pc/vsrc_override/sl_fpga_rom_word.v:63-80` (2026-07-24, ProtoCompiler UC flow). The zero-fill exists only to keep unwritten cells out of X in **simulation**; an inferred BRAM initialises to 0 by construction, so on a synthesis path a lone `$readmemh` is behaviourally identical. |

**Do not "restore" the zero-fill loop.** It buys nothing here and it is a hard
elaboration error.

**Open risk this does NOT fix — R2 in the plan.** Whether `$readmemh` content
survives as `INIT_*` strings on the `RAMB36E2` cells through the *Synplify*
EDIF is **unverified**. The silent failure mode is a DUT that boots from
garbage. Gate it by diffing `INIT_*` on the RAMB cells against the Vivado-synth
baseline before trusting any bitstream from this flow (`make dcp` writes the
checkpoint you need for that diff).

---

## 2. `cmsdk_fpga_rom.v`

Upstream: `$ARM_IP_LIBRARY_PATH/latest/Corstone-101/logical/models/memories/cmsdk_fpga_rom.v`
(Arm CMSDK r1p1-00rel0, **read-only**).

This module is **compiled but never instantiated** in any synthesis
configuration. `sl_ahb_rom.v` picks it only in the `else` arm of
`` `ifdef RAM_PRELOAD ``, and `pynq/filelist.tcl` defines `RAM_PRELOAD`
unconditionally. `sl_ahb_rom.v`'s own comment says to "keep it out of any
synthesis configuration".

The local copy is **not a copy of the Arm source**. It is a synthesis-only
stand-in with the identical module name, parameters and ports, carrying only
the byte-lane BRAM. What it drops is the upstream `initial` block, which is
hostile to a synthesis front end in three independent ways:

1. a **runtime-conditional** memory init — `if (filename != "") $readmemh(...)`;
2. an **inter-array copy loop** — scratch array read, four byte-lane arrays
   written cell-by-cell, all inside the same `initial`;
3. a `2**AW`-byte **scratch array** (`fileimage`) that exists only to be read
   during that copy — 64 KiB of registers for `AW=16`, in a module whose output
   is discarded.

Upstream carries **no** `//synthesis translate_off` guard. It is the only file
in the 240-file nanosoc compile set with an unguarded, unsynthesisable
`initial` block:

- `cm0_tarmac.v` (the Cortex-M0 trace monitor — `$fopen`/`$fwrite`/`$finish`/
  `$timeformat`/`wait`/`#2`) **is** guarded, `//synthesis translate_off` at
  line 177 to `translate_on` at 435. **It therefore needs no override**, which
  is why it is absent from this directory even though a keyword scan flags it.
- `cmsdk_fpga_sram.v`'s zero-fill loop is under `` `ifdef ARM_ASSERT_ON ``,
  which this flow does not define.

Vivado tolerates all three with warnings, which is why the baseline
`fpga/rp/nanosoc/ooc_synth.tcl` never had to care. Synplify is not known to,
and the two outcomes are a hard error on a file that contributes nothing, or
minutes of elaboration burned on a dead 64 KiB array.

**Honesty about status:** deviation 1.2 is a *reproduced* failure elsewhere.
Deviation 2 is a *reasoned* risk — no Synplify run has yet been executed
against this file set, so it is not proven that Synplify rejects the upstream
`initial`. Removing it is nevertheless safe by construction, because the module
is dead in this configuration.

---

## Residual source risks with NO override (deliberately)

| Risk | File(s) | Why no override |
|---|---|---|
| Nested **unpacked** struct as a module port — `output nanosoc_build_info_pkg::nanosoc_build_info__out_t hwif_out` (139 nested typedefs across 3 PeakRDL regblocks) | `nanosoc_build_info.sv`, `nanosoc_ahb_interconnect_discovery.sv`, `nanosoc_cpu_ss_ahb_interconnect_discovery.sv` | Plan R4. Already elaborated cleanly through Synopsys HDL Compiler in the chiplet tree. There is no known fix to pre-apply, and inventing one (flatten or black-box) before seeing a real error would be speculation. If Synplify balks: these are APB read-only discovery registers, functionally inert — black-box them. |
| Headers found by "search the compiled file's own directory" (e.g. CG092's `p_flash_cache_f0_gen_const_pkg.vh`, which sits beside its `.v` files) | CG092 flash cache | Not a content problem. `gen_prj.tcl` adds every source directory as an include path, the same fix `collect_filelist.tcl` already applies for VCS. |
| Unconnected inputs — Vivado ties low silently, Synplify may not | 10x `[Synth 8-6104]` + 3x `[Synth 8-3848]` recorded at `fpga/rp/nanosoc/ooc_synth.tcl:32-34` | Plan R6. Belongs in `rp_nanosoc_wrapper.sv` (not owned here) if it bites. |
