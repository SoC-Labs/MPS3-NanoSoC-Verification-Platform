# BRAM `INIT` audit — full content diff, Vivado baseline vs Synplify (IICE=1 / IICE=0)

Status: **PASS — all three builds proven byte-identical, and proven correct
against external ground truth.** Executed 2026-07-29.

This supersedes the R2 **sentinel** (`EXPECT_IMEM_INIT_00` in the Makefile),
which asserted only that one known-good `INIT_00` payload appears on *some*
IMEM RAMB. That check is necessary but nowhere near sufficient, and README.md
flagged it as such ("the R2 gate is a sentinel, not a whole-memory diff ...
that diff has not been done"). This document is that diff.

---

## 1. Why the obvious method does not work

The two toolchains name and pack memories differently, so a cell-by-cell
name match is meaningless:

| memory | Vivado baseline | Synplify |
|---|---|---|
| IMEM (16 KiB) | `.../u_sram/mem_reg_0 .. mem_reg_3` | `.../u_sram/mem_mem_0_0`, `mem_1_mem_1_0_0`, `mem_2_mem_2_0_0`, `mem_3_mem_3_0_0` |
| bootrom (2 KiB) | `.../u_bootrom/out_data_reg` | `.../u_bootrom/rdata_0_0` |

Worse, **the lane suffix in the cell name is each tool's private convention and
carries no authoritative lane index.** `sl_fpga_rom_word.v` declares exactly
*one* array — `reg [31:0] mem [0:DEPTH-1]` — and the 4-way byte-lane split is
the synthesiser's byte-write-enable inference. So `mem_reg_0` and
`mem_mem_0_0` are *not* known to be the same byte lane just because both end in
`0`. (They are not: see §5.)

So the audit is done on **content**, and where content alone is not enough, on
**reconstructed memory images checked against external ground truth**.

---

## 2. Method

1. Dump **every** `INIT_xx` / `INITP_xx` string on **every** BRAM cell of each
   checkpoint (`get_cells -hier -filter {REF_NAME =~ RAMB*}`, then
   `list_property` filtered to `INIT_*`/`INITP_*` — so no property is missed by
   guessing a name range).
2. Establish a completeness precondition: `INIT_FILE` is `NONE` on every cell
   in all three DCPs, so the `INIT_*`/`INITP_*` strings are a **complete**
   description of memory content — nothing is loaded from an external file at
   implementation time. `INIT_A`/`INIT_B` (output-register reset values, not
   memory) are zero everywhere and are excluded from content comparison.
3. Normalise payload spelling. Two forms occur and must not create a phantom
   diff: `256'hFDFB…` (Vivado, and most of Synplify) and bare `FDFB…`
   (64 of Synplify's payloads). Every content payload in all three DCPs is
   256 bits / 64 nibbles — verified, not assumed.
4. Build the multiset of **non-zero** payloads per side and diff it. This is
   naming- and packing-independent.
5. Where the multiset diff cannot settle it, reconstruct the actual memory
   image and check it against ground truth **per side, independently**:
   - IMEM → `fpga/rp/nanosoc/hello_image.hex`, the word-format `$readmemh`
     file the RTL is given (`sl_fpga_rom_word.v:78`).
   - bootrom → the case statement in `bootrom.sv`, the literal RTL both
     synthesisers read.

Resolving each side against an *external* reference is what makes the result
non-circular: no lane order or bit layout is ever chosen to make the two sides
agree.

### Inputs

| tag | checkpoint |
|---|---|
| `vivado` | `fpga/dfx/build/rm_nanosoc_synth/rm_nanosoc_synth.dcp` (rebuilt in this worktree) |
| `syn_iice` | `fpga/rp/nanosoc_iice/build/rm_nanosoc_synth.dcp` (Synplify, IICE=1) |
| `syn_noiice` | `fpga/rp/nanosoc_iice/build_noiice/rm_nanosoc_synth.dcp` (Synplify, IICE=0) |

Ground truth: `hello_image.hex` md5 `e0cf6827b01bcc484f093b5666581b12`
(identical in this worktree and the primary tree);
`bootrom.sv` md5 `93fcbeb7b5efbce9c114b1435008055b`.

The Vivado baseline was re-synthesised part-way through this work (the
`HD.CLK_SRC` fix to `nanosoc_ooc.xdc`). Both the pre- and post-change
checkpoints were dumped and their BRAM content is **byte-identical**
(`CELL`+`INIT` lines md5 `fd49e4ba4b152aa1c1e67328fafe25f3` either side), which
is expected — `HD.CLK_SRC` is a timing property applied after `synth_design` —
but it was checked rather than assumed, so the numbers below describe the
checkpoint currently on disk.

---

## 3. Census

| | `vivado` | `syn_iice` | `syn_noiice` |
|---|---|---|---|
| BRAM cells | 25 | 28 | 26 |
| — `RAMB18E2` / `RAMB36E2` | 9 / 16 | 9 / 19 | 9 / 17 |
| content properties (`INIT_xx`+`INITP_xx`) | 2 952 | 3 448 | 3 160 |
| **non-zero** content properties | **194** | **189** | **189** |
| cells carrying any non-zero content | 5 | 5 | 5 |
| cells that are entirely zero | 20 | 23 | 21 |

**Only two memories in this RM carry initialised content** — the IMEM (4 cells)
and the bootrom (1 cell). Everything else (DMEM, SRAM regions 0/1, the QSPI
cache RAMs, and the IICE trace buffer) is all-zero on every side. Verified
exhaustively, not sampled.

Cell-count deltas are fully explained and benign:

- `syn_noiice` = `vivado` + 1: the QSPI cache **tag** RAM
  (`u_way0_cache_ram/tag_ram_0_i/mem_mem_0_0`) becomes a BRAM under Synplify;
  Vivado implements it in distributed/LUT RAM. Zero-initialised either way, so
  no content consequence.
- `syn_iice` = `syn_noiice` + 2: the Identify trace buffer
  (`u_core/ident_coreinst/IICE_CPU_INST/b3_SoW/b3_SoW/b3_SoW_b3_SoW_0_{0,1}`),
  two `RAMB36E2`, zero-initialised. This is the IICE's own sample memory and is
  expected.

---

## 4. Naive multiset diff — the honest partial result

| scope | `vivado` vs `syn_noiice` | `vivado` vs `syn_iice` | `syn_noiice` vs `syn_iice` |
|---|---|---|---|
| IMEM only (128 payloads/side) | **MULTISET EQUAL** | **MULTISET EQUAL** | **MULTISET EQUAL** |
| bootrom only | DIFFERS (66 vs 61) | DIFFERS (66 vs 61) | MULTISET EQUAL |
| all cells | DIFFERS (66 vs 61) | DIFFERS (66 vs 61) | MULTISET EQUAL |

So:

- The **two Synplify builds are bit-identical** on every BRAM payload —
  turning the IICE on changes no memory content anywhere. Proven by multiset
  equality alone; nothing further needed.
- The **IMEM** multiset matches the Vivado baseline exactly (128 non-zero
  payloads each side, zero on either side only).
- The **bootrom** does **not** match by payload, and *cannot* be settled by a
  multiset diff, because the two tools use genuinely different bit layouts
  inside the RAMB18 (§6). This is exactly the trap that a payload diff cannot
  see through, so §5 and §6 do the real work.

---

## 5. IMEM — PROVEN byte-exact (and the lane order is reversed)

Both tools pack the 16 KiB IMEM the same way: **4 × `RAMB36E2` at width 9**
(8 data bits/address in `INIT_*`, 1 parity bit/address in `INITP_*`, 4096
addresses). So the feared "one Vivado 256-bit `INIT_00` spread across four
Synplify cells" mismatch **does not arise here** — Vivado byte-lane packs too.

Lane→byte-position resolved **per side, independently, against
`hello_image.hex`**, each match unique:

| byte position | `vivado` cell | Synplify cell (both builds) |
|---|---|---|
| `bits[7:0]`   | `mem_reg_0` | `mem_3_mem_3_0_0` |
| `bits[15:8]`  | `mem_reg_1` | `mem_2_mem_2_0_0` |
| `bits[23:16]` | `mem_reg_2` | `mem_1_mem_1_0_0` |
| `bits[31:24]` | `mem_reg_3` | `mem_mem_0_0` |

> **Trap, recorded:** Synplify's lane numbering is **reversed** relative to
> Vivado's. A by-name comparison (`mem_reg_0` vs `mem_mem_0_0`) would have
> compared byte lane 0 against byte lane 3 and reported a spurious mismatch —
> and, worse, a "fix" that trusted the names would have concluded the image was
> byte-swapped. It is not. The R2 sentinel got the right answer here only
> because it searched *all* IMEM cells for the payload instead of trusting a
> name.

Reconstruction:

- 4 lanes × 4096 bytes = **16 384 B**, matching the IMEM size
  (`RAM_ADDR_W=14` → `DEPTH = 1 << 12` words).
- `INITP_*` is zero on all 12 IMEM cells; no spare `INIT` bits are set. So the
  cells hold the image **and nothing else**.
- Reconstructed image sha256
  `4797b481303649128527034abaa454d83979fa2d8ff057ec38db46100d40bee3`
  on **all three** sides, equal to `hello_image.hex` byte-for-byte
  (1 032 of 4 096 words populated; remainder zero-filled by the RTL's
  `initial` loop, as designed).

**Verdict: IMEM content identical across all three builds, and correct.**

---

## 6. bootrom — PROVEN byte-exact despite different bit layouts

The bootrom is a pure 512 × 32 case-ROM (`bootrom.sv`, `word_addr[8:0]`,
`out_data[31:0]`), inferred by both tools into **one `RAMB18E2`, TDP width
18/18** = 1024 rows × 18 bits (16 data bits from `INIT_*` + 2 parity bits from
`INITP_*` per row). Both tools use both ports to read 32 bits per cycle, but
they *place the bits differently*:

| | non-zero `INIT` props | non-zero `INITP` props | popcount `INIT` | popcount `INITP` | total |
|---|---|---|---|---|---|
| `vivado` | 62 | 4 | 5 538 | 332 | **5 870** |
| Synplify (both) | 61 | 0 | 5 870 | 0 | **5 870** |

The equal total popcount (and it equals the ground-truth ROM's popcount, 5 870)
was the first hint that the same information is present in a different
arrangement. That hint is not a proof, so the layout was **solved empirically**:
for each ROM word-bit `b` (0..31), search the entire 1024×18 store for a
`(row_offset, row_stride, bit_in_row)` triple whose extracted 512-bit sequence
over word addresses `a = 0..511` exactly equals the ground-truth sequence of
bit `b`. No layout was assumed; a 512-bit exact match makes coincidence
negligible.

All 32 bits located on all three sides. The resolved layouts:

- **Vivado** — `row_stride = 1`, two regions:
  - rows `0..511` → word bits `[17:0]` — `INIT` bits 0..15 **plus ROM bits 16
    and 17 stored in the PARITY array** (`INITP` bits 16..17 of the row).
    This is why `INITP_00..03` are non-zero and `INITP_04..07` are zero.
  - rows `512..1023` → word bits `[31:18]` (14 of the row's 18 slots used).
- **Synplify** (both builds) — `row_stride = 2`, interleaved:
  - row `2a` → word bits `[15:0]`, row `2a+1` → word bits `[31:16]`.
  - the parity array is **entirely unused**.

Exhaustiveness check (closes the "is there anything *else* in the BRAM?"
loophole) — for every side:

- the mapping is **injective**: 512 × 32 = **16 384 distinct** row/bit slots
  claimed, no slot claimed twice;
- of the 1024 × 18 = 18 432 slots, the 2 048 unclaimed ones are **all zero**;
- the reconstruction equals `bootrom.sv` exactly.

Reconstructed ROM sha256
`ea3876dc672e372faff14f1c5aa615499c87d7978ca0274c75d07213a20a9f28`
on **all three** sides.

**Verdict: bootrom content identical across all three builds, and correct —
the payload-level difference is purely a tool bit-packing choice, carrying the
same 2 KiB of ROM.**

---

## 7. Verdict

| claim | status |
|---|---|
| `syn_iice` vs `syn_noiice`, all BRAM payloads | **IDENTICAL** (multiset equality, no reconstruction needed) |
| IMEM content, all three builds | **IDENTICAL**, and byte-exact vs `hello_image.hex` |
| bootrom content, all three builds | **IDENTICAL**, and byte-exact vs `bootrom.sv` |
| every other BRAM (DMEM, SRAM 0/1, QSPI cache, IICE buffer) | all-zero on all three builds |
| union of initialised memory content | **IDENTICAL** across all three |

The Synplify+Identify path does not perturb a single bit of initialised memory
content, and both toolchains produce the *correct* content, not merely matching
content. The audit compared **3 027 / 3 532 / 3 238** properties per side
(2 952 / 3 448 / 3 160 of them memory content) — no sampling.

Enabling the IICE costs two zero-initialised `RAMB36E2` for its trace buffer
and changes nothing else.

## 8. What this does NOT prove

- Nothing here is about **placement, routing or timing** — content only.
- It says nothing about whether `hello_image.hex` is the *desired* firmware,
  only that all three builds bake in exactly the image the RTL was handed.
  A firmware change must still update the R2 sentinel string.
- The QSPI cache **tag** RAM moving from LUT RAM (Vivado) to BRAM (Synplify) is
  reported here as a resource difference. Both are zero-initialised, so it is
  content-neutral, but it is a real utilisation difference and is not analysed
  further in this document.
- The audit reads the **synthesis** checkpoints. It does not re-verify that
  `updatemem`/bitstream generation preserves these payloads.

## 9. Reproducing

Evidence is script-driven, not hand-transcribed. The dump is a small Tcl
(`get_cells -hier -filter {REF_NAME =~ RAMB*}` + `list_property`) run over each
DCP; the analysis is pure Python over those dumps. Re-running needs only the
three checkpoints plus the two ground-truth files named in §2. Note
`fpga/rp/nanosoc_iice/build*/` is gitignored, so regenerate the Synplify
checkpoints before re-auditing:

```
make -C fpga/rp/nanosoc_iice synth dcp                                  # IICE=1
make -C fpga/rp/nanosoc_iice IICE=0 OUT=$PWD/fpga/rp/nanosoc_iice/build_noiice synth dcp
make -C fpga/dfx rm-nanosoc-dcp                                         # Vivado baseline
```
