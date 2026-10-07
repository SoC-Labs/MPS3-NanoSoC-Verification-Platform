# host/readback — configuration readback capture (XAPP1230)

Recovering **every register in the device** from configuration memory, with no
instrumentation, no probe selection and no re-synthesis. The complement to the
IICE trace path:

| | IICE trace | readback capture |
|---|---|---|
| coverage | 69 bits, chosen at build time | **6,554 registers in the RP**, all of them |
| depth | 923 samples ≈ 20.5 µs (measured) | **one cycle** |
| readout | ~340 bit/s (soft TAP) → 22 min | ~570 KB/s (config TAP) → 63 frames = **0.11 s** |
| cost to add a signal | re-instrument → Synplify → P&R → new partial | **zero** |
| needs Synopsys | yes (Synplify + Identify seats) | **no — pure Vivado** |

## Status: the `.ll` → readback mapping is SOLVED (2026-07-31)

`decode_readback.py` maps a `.rdbk` capture back to named nets. The mapping is
established, not assumed — `--verify` re-proves it from a Vivado mask file on
every run and exits non-zero if it does not hold exactly.

### The mapping

A `.ll` line is `Bit <offset> <frameaddr> <frameoffset> <SLRname> <SLRnum> …`.
The ASCII readback is 32 characters + `\n` per line, one 32-bit configuration
word per line, after an optional text header. Then, **for SLR0**:

```
word_index   = 133 + offset // 32
char_column  =  31 - offset  % 32          # column 0 = leftmost character
byte_in_file = header_len + word_index*33 + char_column
```

Four measured facts behind it:

1. **`offset == k*3936 + frameoffset` exactly**, for all 10,483,797 `Bit` lines of
   the full-device `.ll` (0 exceptions). 3936 = 123 words × 32 bits = one
   UltraScale frame, so `k = offset // 3936` is a **dense** frame ordinal that
   counts every frame of the SLR. `k` is monotonic in `(blocktype, row, column,
   minor)` decoded from `<frameaddr>` — verified in both SLRs — and block-type-1
   (block RAM content) columns are 128 frames apart, also verified.
   *This is what makes the FAR enumeration unnecessary: the ordinal is already in
   the `offset` column.*
2. **`offset` is SLR-relative.** SLR0 offsets run 0…191,460,447 and SLR1 offsets
   **also start at 0**; `frameaddr → k` is the same function in both. The `.ll`
   does not carry the SLR's base in the stream.
3. **133 pad words** precede SLR0 frame 0 (= one 123-word dummy frame + 10
   pipeline words). Uniquely determined; see the table below.
4. Inside a word, `offset % 32` is an **LSB-first** bit number while the line is
   printed **MSB-first**, hence `31 - offset%32`.

### How it was proven — three independent tests, all board-free

**(a) Block RAM content vs the mask, over 10,436,736 bits.** `write_bitstream
-mask_file` emits `.msd`, same geometry as the readback. Block-RAM content frames
are entirely `mask=1`, which pins the *frame* mapping without depending on the
column rule:

| word pad | `mask=1` fraction at all `.ll` block-RAM bits |
|---|---|
| 131 | 0.9843014 |
| 132 | 0.9921507 |
| **133** | **1.0000000** (0 off-grid) |
| 134 | 0.9923501 |
| 135 | 0.9847001 |
| 256 (=133+123) | 0.9995215 |

Exactly 1.0 at one pad and nowhere else.

**(b) LUTRAM/SRL vs the mask, over 30,912 bits — this is what pins the column
rule.** Whole-word-aligned RAM runs cannot discriminate the column rule (both
candidates land inside the same fully-masked word). The discriminating case is
**188 words in which Vivado lists only `Ram=` residues 0…15 while masking columns
16…31** — a 16-bit distributed RAM / SRL16 in half a word:

| column rule | `mask=1` fraction, 30,912 LUTRAM/SRL bits |
|---|---|
| `column = offset % 32` (identity) | 0.915114 |
| **`column = 31 - offset % 32`** | **1.000000** |
| `(offset % 32) ^ 16` | 1.000000 |
| `(offset % 32) ^ 24` (byte-swap) | 1.000000 |
| word-reversed within the frame | 0.024845 |

So the mask rules out identity and frame-word reversal but leaves three
candidates; test (c) is what separates them.

**(c) Semantic ground truth: a real firmware ROM comes back as readable bytes.**
Reconstructing `RAMB18_X10Y30` from `full.rbd` with `B:BIT<n>` packed LSB-first
per byte yields, in correct order:

```
SoCLabs NanoSoC'25 ARM-CM0+ADP+   FT1+U38400   EXTIO-DMA    20250412
QSPI:PRESENT   QSPI:NO-CHIP   QSPI:BAD-TBL   QSPI:ID=
```

This is the decisive test, because it is sensitive to the *whole* composition
(pad, frame ordinal, column rule, byte packing). Every near-miss returns the same
bytes **scrambled**, which is exactly how a wrong permutation fails:

| column rule | LUTRAM mask | reconstructed text from `RAMB18_X10Y30` |
|---|---|---|
| identity `r` | 0.915114 | `dkmkmkmkmkmk…` (junk) |
| `r ^ 16` (half-swap) | 1.000000 | `naN sbaLCoS` (right bytes, reversed) |
| `r ^ 24` (byte-swap) | 1.000000 | `an NbsLaoC` (right bytes, pairs swapped) |
| **`31 - r`** | **1.000000** | **`SoCLabs NanoSoC'25 ARM-CM0+ADP+`** |

**(d) Class consistency.** At pad 133 the mask covers `.ll` classes at exactly
1.0 / 1.0 / 0.0 for block RAM / LUTRAM / CLB flops. See the next section for why
0.0 is the *correct* answer for flops.

### Refuted models

| model | result | verdict |
|---|---|---|
| absolute `<offset>`, `(off//32)*33 + off%32`, no pad | mask 0.0375 | REFUTED (below chance) |
| same, sweeping the word pad over −500…+1200 | best mask **0.0327** | REFUTED |
| `<frameaddr>` ordinal from the sorted `.ll` frame set × 123 + `<frameoffset>` | mask 0.0660 | REFUTED — the `.ll` names only 5,265 of the SLR's frames, so a sorted ordinal is not readback order. Superseded: `offset // 3936` already *is* the dense ordinal |
| chance level | 0.0838 | — |
| `column = offset % 32` (identity) | LUTRAM mask 0.915114, scrambled ROM text | REFUTED |
| `column = (offset % 32) ^ 16` / `^ 24` | LUTRAM mask 1.0 but ROM text out of order | REFUTED |
| frame-word reversal (`122 - fo//32`) | mask 0.0255 | REFUTED |
| per-frame fingerprint containment against `mask=1` | 100s of hits at every phase | INCONCLUSIVE — the flop lattice is too coarse and the mask has large all-1 regions. Abandoned in favour of (a)–(c) |

### Two corrections to earlier framing

**1. `mask=1` is NOT "the FF/RAM content bits".** Measured at the solved pad,
over the full-device `.ll`:

| `.ll` class | n (SLR0) | `mask=1` fraction |
|---|---|---|
| block RAM content (`RAM=B:BIT*` / `B:PARBIT*`) | 10,436,736 | **1.0000000** |
| LUTRAM / SRL (`Ram=A:*` on a SLICE) | 30,912 | **1.0000000** |
| CLB flip-flop (`Latch=AQ…HQ2`) | 14,733 | **0.0000000** |
| block RAM output register (`Latch=DO*`) | 859 | 0.0000000 |

(all four measured with `off_grid = 0`, i.e. every single bit landed on a real
data character, never on a line terminator — itself a geometry check)

Vivado's mask covers **RAM content only**; it does not mask CLB flop content,
because a readback *without* capture returns a flop cell's INIT value, which is
predictable. So "what fraction of flop bits land on `mask=1`" is not a mapping
test — the correct answer is 0.0, and `verify_mapping()` asserts that.
The 8.385% global mask density is real but is not the flop set.

**2. `Latch=DOAL0` / `DOAU3` / `DOBL7` / `DOPAU0` are block RAM OUTPUT
REGISTERS, not SLICE flip-flops.** They are 859 of the 15,592 SLR0 `Latch=`
lines and they live in BRAM tiles. SLICE flops sit at `frameoffset % 4 == 0`
without exception; the `DO*` entries do not. Treating them as CLB flops (which
the previous version of this tool did) is what made the flop statistics look
self-contradictory. `classify()` separates them and only `CLB_FF` inverts.

## The silicon capture: capture DID populate the cells

`readback_hw_device -capture` on mps3_01 (XCKU115, cable `210249B86C47`)
produced `snap.rdbk`, 398,034,450 B, 12,061,650 words, no header.

- **Alignment confirmed.** Hamming distance between `snap.rdbk` and the
  checkpoint's own `full.rbd` over a 200,000-word dense region: **0.0089 at word
  shift 0**, 0.150 at ±123, 0.156 at ±2, median 0.196 across a ±400 sweep. Sharp
  minimum at 0, so the capture shares the `.rbd` geometry and pad. Column
  reversal / byte swap / ±1-column variants of `snap` all score 0.052–0.064
  against 0.0082 for identity, so the two files also share the ASCII bit order.
- **It contains new state.** 44,561 frames are non-zero in `snap` against 20,474
  in `full.rbd`, and 20,473 of those 20,474 are a subset — the capture *adds*
  ones. 1,597 frames carry 81.8% of the 2,788,463 differing bits, and those hot
  frames occur **both inside and outside the RP's column band** (mean 1,517
  differing bits/frame for frames with RP flops, 1,585 for shell-only frames at
  column ≤200, 1,654 for shell-only frames at column >200). A greybox-vs-RM
  configuration difference would be confined to the RP band, so this is capture
  data, not a bitstream difference. Device-wide mean is 28.4 bits/frame, i.e. the
  change is concentrated in exactly the frames holding clocked logic.
- **Polarity confirmed against two opposite known reset values**, both decoded
  correctly under the same convention (`value = 1 - raw` for `CLB_FF` only):

  | net | decoded | known reset value |
  |---|---|---|
  | `…/u_apb_watchdog_frc/wdog_load` | `0xFFFFFFFF` | CMSDK `WDOGLOAD` resets to `0xFFFFFFFF` ✔ |
  | `u_shell/…/board_gpio_0/inst/oe_q` | `0x0000` | GPIO output-enable resets to 0 ✔ |
  | `u_shell/…/board_gpio_0/inst/out_q` | `0x0000` | GPIO output resets to 0 ✔ |

  So the XAPP1230 inversion is measured here, not assumed.

## What the capture says about the M0: reset state, and that is correct

Decoded against the design's own expected readback (`full.rbd`) as the INIT
reference:

| group | flop bits | differ from expected INIT |
|---|---|---|
| RP (`u_rp_dut`, DUT **held in reset**) | 6,047 | 44 = **0.73%** |
| shell (static, **running**) | 8,686 | 1,202 = **13.84%** |
| M0 architectural state only | 523 | **0** |

Every one of the 523 `u_gpr/reg_r00..r14`, `reg_msp`, `reg_psp`, `psr_*`,
`u_pfu/iaex` bits equals the design's own expected value. **The captured M0 state
is its reset/INIT state** — which is the right answer, because the capture was
deliberately taken with the DUT held in reset (`dut_clk` cannot be stopped on
this platform, see below). The 19× higher deviation in the running shell is the
positive control that the mechanism is live: this is *not* the earlier
"hypothesis 2, capture never populated the cells" — that is now refuted.

Sample values (inversion applied):

```
u_gpr/reg_msp   30'h3FFFFFFF      u_gpr/reg_r02   32'hFFFFFFFF
u_gpr/reg_psp   30'h3FFFFFFF      u_gpr/reg_r08…r11, r14  32'hFFFFFFFF
u_pfu/iaex       2'h3
```

All-ones is the expected reading, not a decode failure: Cortex-M0 GPRs and the
stack pointers are **not architecturally reset**, so they hold their FPGA INIT
value from configuration, and Vivado gave these `INIT=1`. `full.rbd` agrees bit
for bit, and the `wdog_load` / `oe_q` pair above shows the polarity is right in
both directions. Some nets print `SPARSE` because Vivado renames individual bits
(`reg_r00_16`) when it splits a bus; those bits are reported separately.

**Corollary:** to get *live* DUT state this capture must be repeated with the DUT
out of reset. The mechanism, the mapping and the decode are all now proven, so
that is a board-time-only step.

## Still unknown

- **SLR1's base in the stream.** `<offset>` is SLR-relative and only SLR0's pad
  (133) is established. SLR1 holds 109 flop bits and 448 LUTRAM bits in this
  design; solving its base from those 448 bits left many candidates
  (1,185,175 / 2,107,675 / 3,030,175 / 4,017,367 …, all at mask 1.0), because the
  constraint degenerates to frame granularity with so few bits. The tool
  **refuses** `--slr 1` rather than decode it with SLR0's pad. Fix: emit a `.ll`
  for a design with real SLR1 content, or sweep with a flop-`mask=0` negative
  constraint added.
- **What the other ~1,000 capture-modified bits per frame are.** Under the solved
  mapping the `.ll`'s SLICE flops sit at columns ≡3 mod 4, while the bulk of the
  capture-added bits sit at columns ≡0 mod 4 (per-column differing counts in a
  dense slab: ~4,400–5,000 at ≡0 mod 4, ~3,000–3,700 at ≡2, ~1,300–1,600 at ≡1,
  ~175–450 at ≡3). The `.ll` enumerates only flops attached to user nets
  (15,701 device-wide), so most captured cells are simply not named in it. Whether
  the ≡0 mod 4 columns are unused-flop cells or another captured element is
  **not established**, and it does not affect decoding named nets. Do not read the
  earlier "capture lattice at even columns ⇒ identity column rule" inference as
  evidence — that argument over-reached from an aggregate profile which does not
  identify individual cells, and it is contradicted by (b) and (c) above.
- Whether Vivado's `-capture` issues the XAPP1230 **MSK + CTL1(`CAPTURE`, bit 23)**
  writes explicitly. It evidently achieves capture (see above), but the mechanism
  was inferred from the data, not read off a command trace.

## The real blocker for general use

`dut_clk` **cannot be stopped** on this platform — verified: `dut_clkrst`'s DRP
outputs are hardwired off, `CLKRST.DUT_CLK_SEL` is a dead scratch register, and
the `BUFGCE` inside `clk_wiz_dut` has its CE tied high with no CSR or partition
pin. Coherent whole-design capture needs the clock frozen, because capture samples
each frame as it is read.

The workaround used above — hold the DUT in **reset** so its state is static
despite the running clock — is why this experiment was possible at all without a
shell change, and is also why the decoded state is the reset state.

**Only one clock crosses the partition boundary** (`dut_clk`,
`partition-pins.md:43`), so a `BUFGCE` CE driven from a `CLKRST` bit would freeze
the entire DUT, needs **no new partition pins**, and would also give genuine
single-step. It costs a `static_id` re-mint, so it belongs in the next batched
mint.

## Usage

```sh
# re-prove the mapping against a Vivado mask (board-free, fail-closed)
python3 host/readback/decode_readback.py --ll rb.ll --mask full.msd --verify

# solve the pad from scratch if the device or geometry ever changes
python3 host/readback/decode_readback.py --ll rb.ll --mask full.msd --solve-pad 0 600

# decode named registers, with the design's own expected readback as the
# INIT reference so reset state is distinguishable from live state
python3 host/readback/decode_readback.py --ll rb.ll --rdbk snap.rdbk \
    --ref full.rbd --mask full.msd --net-filter 'u_gpr/reg_r' --json regs.json

# pull a block RAM's contents back out as bytes
python3 host/readback/decode_readback.py --ll rb.ll --rdbk snap.rdbk \
    --bram RAMB18_X10Y30 --bram-out imem.bin
```

`--verify` and `--solve-pad` **exit non-zero** rather than pick a winner when the
evidence is not exact, and passing `--mask` alongside `--rdbk` makes a decode
refuse to run unless the mapping checks out first. A confidently-wrong register
decode is worse than no decode.

### Generating the inputs (no board needed)

```tcl
open_checkpoint fpga/dfx/build/prod/config_rm_nanosoc_routed.dcp
# full-device expectation + mask, byte-comparable with a full-device capture
write_bitstream -force -mask_file -readback_file      $out/full.bit
# logic locations; add -cell [get_cells u_rp_dut] for the 64 MB subset
write_bitstream -force -logic_location_file           $out/probe.bit
```

`write_bitstream -cell [get_cells u_rp_dut] -logic_location_file` yields a 64 MB
`.ll` instead of 764 MB. Note it is **not** re-based: the offsets are in the same
whole-device coordinate system, and it still contains the shell nets that share
the RP's frames — so it is a size optimisation, not a scope change.

## Tests

Board-free and Vivado-free; they freeze the arithmetic, the class split, the byte
order and the fail-closed exits, including a negative control that provably fails
if the refuted identity column rule is swapped back in.

```sh
python3 -m pytest host/readback/tests -q          # 34 tests
python3 host/readback/tests/test_ll_mapping.py    # same, without pytest
```

Not yet wired into `make check` (the Makefile is outside this directory's
ownership); the one-line addition is `$(PY) -m pytest host/readback/tests -q`.
