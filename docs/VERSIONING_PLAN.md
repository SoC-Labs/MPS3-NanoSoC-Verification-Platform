# Versioning plan — harness (static shell) and design (RM/DUT)

**Status:** ADOPTED AND GATED. The `rm_id` encoding specified in §3.2 is enforced mechanically on every `make check` by `scripts/harness_gates/check_rm_id_encoding.py` (which cites §3.2 by name), `rm_list.tcl` derives every `rm_id` from (`design_id`, version) rather than hand-writing it, and the repo carries a `VERSION` of `1.0.0` (`README.md` §"Versioning"). Read this as the specification of a live scheme, not a proposal.
**Date:** 2026-07-14

This document defines a versioning scheme for the two independently-rebuilt
halves of the MPS3 verification platform:

- the **harness** — the static shell (`fpga/shell/` + MicroBlaze firmware), and
- the **design** — the RM (DUT) loaded into the reconfigurable partition.

It is written to be adopted concurrently by the LCD, telemetry-inventory and
fpgahub-stats efforts, so §4 fixes the exact field names and read paths.

---

## 0. Executive summary

| Question | Answer |
|---|---|
| Real RP boundary width? | **30 signals = 122 bits.** "30 pins" was a *signal* count. |
| Is `rm_id` really a 32-bit bus? | **Yes** — 32 of those 122 bits, fully plumbed to `DFXCTL.RM_ID`. |
| Can a *partial* bitstream carry `USR_ACCESS`? | **No.** Proven below — it is device-global and its only BEL sits outside the RP. |
| Can the *static* bitstream carry `USR_ACCESS`? | **Yes, for free** — a bitstream property, no RTL change, no re-key. |
| Recommended design-version mechanism | **Re-encode the existing 32-bit `rm_id`** — zero boundary cost, zero static re-key. |

**The headline:** a hardware-readable design version is **already affordable**.
`rm_id` is a genuine 32-bit hardware value with 16 bits of unused headroom, and
no RM needs more than 16 bits of identity. Widening the partition boundary — the
expensive option everyone feared — is **not necessary and is strictly dominated**.

---

## 1. Investigation #1 — the real boundary, and how `rm_id` crosses it

### 1.1 The boundary is 122 bits, not 30

`docs/contracts/partition-pins.md` v0.1 lists **30 signals**. That is a count of
named ports, not of bits. Summed by width:

| Group | Signals | Bits |
|---|---:|---:|
| Clocks & resets | 4 | 4 |
| Processor debug (SWD) | 4 | 4 |
| Ethernet (RMII + MDIO) | 9 | 11 |
| Console / trace (AXIS) | 7 | 21 |
| **Status / misc** | **3** | **34** |
| Board-port / GPIO (`NGPIO=16`) | 3 | 48 |
| **Total** | **30** | **122** |

The "Status/misc" group is `rm_id[31:0]` + `dut_lockup` + `irq_out` = 34 bits.
So **`rm_id` is a real, full-width 32-bit bus across the partition**, and the
premise that "a 32-bit `rm_id` cannot fit in 30 pins" was based on a
signals-vs-bits mix-up.

### 1.2 The path `rm_id` takes

```
RM wrapper                 assign rm_id = 32'h0000_0003;
  fpga/rp/nanosoc_multicore/rp_nanosoc_multicore_wrapper.sv (RM_ID_NANOSOC_MULTICORE)
        |  32-bit partition pin (BD pin, RP is a hierarchical cell beside the shell)
        v
shell_top.sv               wire [31:0] w_rm_id;                    (:137, :286, :393)
        |
        v
DFX decoupler              rm_id { ID 10 ... WIDTH 32 ... DECOUPLED_VALUE 0x0 }
  fpga/shell/bd/shell_bd.tcl:623      -- clamped to 0 while DECOUPLE is asserted
        |  s_rm_id_DATA
        v
dfx_ctl.sv                 input logic [31:0] rm_id_i;             (:120)
        |  2-FF ASYNC_REG sync (:340-350) + 8-cycle stability detector (:371-392)
        |  gated by rm_id_gate = ~decouple & ~decoupled & ~rp_in_reset  (:367)
        v
AXI4-Lite                  DFXCTL.RM_ID     @ 0x44A1_0010  (RO)
                           DFXCTL.RM_STATUS @ 0x44A1_0014  bit0 = rm_id_valid
                                                            bit1 = dut_lockup
```

Every one of the 32 bits survives to the CSR — this is what the parent read back
as `0x3` on silicon.

**Two properties that matter for the scheme:**

1. **`rm_id_valid` is a *gate* + stability detector, not a zero-check**
   (`dfx_ctl.sv:367-392`). Nothing in hardware treats `rm_id == 0` as special.
   The `0x00000000` = greybox convention is a *software* convention only
   (`g_shell_state.current_rm_id`, "0 = greybox"). A re-encoding is therefore
   not blocked by the valid logic.

2. **Firmware never hard-codes an expected `rm_id`.** `step_verify()`
   (`firmware/coordinator/swap_fsm.c:963-1019`) compares the CSR against
   `s_target_rm_id`, which comes from the **pushed partial's own 24-byte wire
   header**, which is generated from the manifest. So the *value* of `rm_id` is
   pure data end-to-end. Re-encoding it does not touch the verify path at all.

   The only places that map an id to a *meaning* are lookup tables:
   `clcd_rm_name()` / `clcd_rm_caps()` (`firmware/clcd/clcd.c:183-204`) and the
   host's `known_rm_names`. Those need a mask, and nothing else does.

### 1.3 DFXCTL has spare register space

Mapped: `0x00` DECOUPLE, `0x04` SHUTDOWN, `0x08` STATUS, `0x10` RM_ID, `0x14`
RM_STATUS. The block decodes a full 16-bit local address, so `0x0C`, `0x18`,
`0x1C`… are free. **Adding a DFXCTL register is free; adding a partition pin to
feed it is not.** That asymmetry is the crux of §3.

---

## 2. Investigation #2 — USR_ACCESS

Verified directly against the real part (`xcku115-flvb1760-1-c`) in Vivado
2024.1, not from documentation alone.

### 2.1 The primitive exists, and there is exactly **one** in the device

```
get_lib_cells *USR_ACCESS*        -> USR_ACCESSE2
get_sites     *USR_ACCESS*        -> (none: it is a BEL, not a site)
get_sites     CONFIG_SITE*        -> CONFIG_SITE_X0Y1  (clock region X5Y6)
                                     CONFIG_SITE_X0Y0  (clock region X5Y1)
get_bels -of  CONFIG_SITE_X0Y0    -> ... ICAP_BOT, ICAP_TOP, STARTUP,
                                        FRAME_ECC, EFUSE_USR, DNA_PORT,
                                        USR_ACCESS          <-- the only one
```

There is **one `USR_ACCESS` BEL on the whole device**: `CONFIG_SITE_X0Y0/USR_ACCESS`,
in **clock region X5Y1**.

### 2.2 That BEL is outside the reconfigurable partition

The RP pblock (`fpga/dfx/dfx_floorplan.xdc:73`) is:

```tcl
set rp_regions {X2Y0 X3Y0 X2Y1 X3Y1}
foreach site_type {SLICE DSP48E2 RAMB18 RAMB36} { ... resize_pblock -add ... }
```

— clock regions X2–X3 / Y0–Y1, and only `SLICE`/`DSP48E2`/`RAMB18`/`RAMB36` site
types are ranged. `CONFIG_SITE` is not a ranged type, and X5Y1 is not in the
region set. The XDC comment even records that the region set deliberately
"avoids the X0 config column".

**Therefore an RM physically cannot instantiate `USR_ACCESSE2`.** It is not a
policy choice; the site does not exist inside the partition.

### 2.3 The register is device-global

`BITSTREAM.CONFIG.USR_ACCESS` "writes an 8-digit hexadecimal string, or a
timestamp, into the **AXSS configuration register**" (UG908, UltraScale Bitstream
Settings). AXSS is a single device-wide configuration register — there is one per
device, not one per partition.

Verified accepted on this part:

```tcl
set_property BITSTREAM.CONFIG.USR_ACCESS 0xDEADBEEF [current_design]  ;# OK
set_property BITSTREAM.CONFIG.USR_ACCESS TIMESTAMP  [current_design]  ;# OK
set_property BITSTREAM.CONFIG.USERID     0x........ [current_design]  ;# OK (2nd 32-bit slot)
```

### 2.4 Verdict

| Question | Verdict |
|---|---|
| Stamp the **static** bitstream with USR_ACCESS? | **YES.** Pure bitstream property — **no RTL change, no netlist change, no re-key.** |
| Read it back over **JTAG**? | **Yes** (Hardware Manager reads the config registers). Exact Tcl property name must be confirmed on a board — see caveat below. |
| Read it back from the **MicroBlaze**? | Only by instantiating `USR_ACCESSE2` in the **static shell** + exposing a CSR. That *is* a static RTL change → **re-key**. Defer (§3.4). |
| Can a **partial** carry its own USR_ACCESS? | **NO.** Two independent reasons, either one fatal: (a) the RM cannot contain a `USR_ACCESSE2` to read it (§2.2); (b) AXSS is device-global — a partial writing it would **clobber the harness's own stamp**, so you could never have both. |

There is a tempting third path — have each *partial* overwrite AXSS and let the
*static's* `USR_ACCESSE2` read it. **Reject it.** It destroys the harness stamp
(you get one 32-bit slot for the whole device, not one per region), it is
undocumented and unsupported, and Vivado is not expected to emit an AXSS write in
a `write_bitstream -cell` stream at all. Option (iv) is dead.

> **Caveat, honestly flagged.** The `REGISTER.*` properties are attached to a
> `hw_device` only after `refresh_hw_device`, so the exact property name could not
> be enumerated offline. Expected form:
> `get_property REGISTER.USR_ACCESS [current_hw_device]`. Confirm on the board with
> `refresh_hw_device [current_hw_device]; list_property [current_hw_device]`.
> This is a one-line check and the only unverified claim in this document.

---

## 3. The scheme

### 3.0 Principle: two orthogonal axes. `static_id` is not replaced.

| | `static_id` | version |
|---|---|---|
| What it answers | "will this partial **fit** this fabric?" | "**which release** is this?" |
| Kind | CRC-32 fingerprint (compatibility key) | semantic version (human ordering) |
| Ordered? | No | Yes |
| Changes when | the static netlist/routing changes | anything release-worthy changes, **including firmware-only** |

They are independent. A harness *version* pins exactly one `static_id`, but one
`static_id` can serve several harness versions — a firmware-only bump changes the
`.bit` (firmware is baked in via `updatemem`) while the static routing, and hence
partial compatibility, is untouched. That is correct behaviour, not a bug.

**`static_id` stays exactly as it is.** It is load-bearing: it is checked in
firmware before any ICAP write (`config_agent.c:266`, `CFG_AGENT_ERR_STATIC_ID`),
in the host pusher (`overlay.py:216`), and by a repo gate
(`check_overlay_static_id.py`). Nothing below alters that machinery.

### 3.1 Harness version

**Source of truth: a `VERSION` file at the repo root**, one line, semver:

```
1.4.2
```

Not git tags: the build must be deterministic in a dirty tree and in CI without
tags fetched, and a version bump should be visible in the diff/review. Tag
`harness-v1.4.2` at release as a *derived* marker if desired.

**Packed 32-bit form** (`HARNESS_VER32`), chosen to be legible in hex:

```
 31           24 23           16 15            8 7             0
+---------------+---------------+---------------+---------------+
|     major     |     minor     |     patch     |     flags     |
+---------------+---------------+---------------+---------------+
flags: bit0 = dirty tree     bit1 = local (non-CI) build     bits[7:2] reserved 0
```

v1.4.2 from a clean CI tree → `0x01040200`. Same, dirty → `0x01040201`.

**Stamped in three places, from one generator:**

1. **Firmware** — `scripts/gen_version.py` emits
   `firmware/platform/generated/mps3_version.c/.h`, regenerated on every firmware
   build and gitignored, exactly mirroring the existing
   `gen_greybox_blob.py` → `generated/greybox_blob.c` pattern:

   ```c
   uint32_t    mps3_harness_version(void);      /* packed HARNESS_VER32   */
   const char *mps3_harness_version_str(void);  /* "1.4.2"                */
   const char *mps3_harness_git_sha(void);      /* "a1b2c3d4" (8 hex)     */
   bool        mps3_harness_dirty(void);
   const char *mps3_harness_build_date(void);   /* "2026-07-14"           */
   ```

   Declare them in `coordinator.h` with **weak fallbacks in `coordinator.c`**
   (the established `mps3_shell_static_id()` seam) so the host-gcc test binaries
   in `firmware/test/` keep linking with no generated file.

2. **Bitstream (free, do this now)** — in `build_dfx.tcl`, and in the monolithic
   flow:

   ```tcl
   set_property BITSTREAM.CONFIG.USR_ACCESS $HARNESS_VER32 [current_design]  ;# 0x01040200
   set_property BITSTREAM.CONFIG.USERID     0x$GIT_SHA8    [current_design]  ;# provenance
   ```

   > **Placement is critical.** These calls must go **after**
   > `write_checkpoint … static_routed_locked.dcp` (`build_dfx.tcl:582`) and
   > **before** the `write_bitstream` block (`:620-680`). Design properties are
   > captured into a DCP; setting them earlier would perturb the very bytes that
   > `file_crc32` hashes into `static_id` (`:603`), needlessly coupling the two
   > axes. Set them late and `static_id` is untouched.

3. **Manifests** — every overlay records the harness it was built against
   (`harness_version`), so the host can flag a partial built for a different
   harness release even when `static_id` still matches.

**How it is read:**

| Reader | Path |
|---|---|
| MicroBlaze firmware | `mps3_harness_version_str()` (compiled-in constant) |
| LCD | via firmware, §4.1 |
| 6900 JSON | via firmware, §4.2 |
| **JTAG, firmware not running / board wedged** | `refresh_hw_device; get_property REGISTER.USR_ACCESS [current_hw_device]` |
| Host / fpgahub | 6900 JSON, or the manifest |

The JTAG path is the one that only USR_ACCESS can provide, and it costs nothing.

### 3.2 Design (RM) version — **RECOMMENDED: re-encode `rm_id`**

`rm_id` is already a 32-bit hardware value on a fully-plumbed path to a CSR
(§1.2), and no design needs 32 bits of identity. Split it:

```
 31           24 23           16 15                            0
+---------------+---------------+-------------------------------+
|   ver major   |   ver minor   |         design_id[15:0]        |
+---------------+---------------+-------------------------------+

RM_DESIGN(x)     ((x) & 0xFFFFu)
RM_VER_MAJOR(x)  (((x) >> 24) & 0xFFu)
RM_VER_MINOR(x)  (((x) >> 16) & 0xFFu)
```

`nanosoc_multicore v1.0` → `rm_id = 0x0100_0003`.

**The low 16 bits are chosen to preserve the existing ids**, so the churn the
brief worried about is almost entirely avoided:

| RM | old `rm_id` | new `design_id` | new `rm_id` @ v1.0 | churn? |
|---|---|---|---|---|
| greybox | `0x00000000` | `0x0000` | **`0x00000000`** | none — carve-out, stays v0.0 |
| nanosoc | `0x00000001` | `0x0001` | `0x01000001` | id preserved |
| eth_ss | `0x00000002` | `0x0002` | `0x01000002` | id preserved |
| nanosoc_multicore | `0x00000003` | `0x0003` | `0x01000003` | id preserved |
| led | `0x0000001E` | `0x001E` | `0x0100001E` | id preserved |
| regdemo_a | `0x000000A1` | `0x00A1` | `0x010000A1` | id preserved |
| regdemo_b | `0x000000B2` | `0x00B2` | `0x010000B2` | id preserved |
| uart_echo | `0x4543484F` | `0x0004` | `0x01000004` | **re-numbered** |

Only `uart_echo` (ASCII `"ECHO"`, which used all 32 bits) must be re-numbered —
it is a toy RM. Every other id survives as `rm_id & 0xFFFF`.

**greybox stays exactly `0x00000000`** (design 0, version 0.0). This preserves
both the decoupler's `DECOUPLED_VALUE 0x0` semantics and the firmware's "0 =
greybox" convention, and it is honest: an inert tie-off is not a versioned
design. (Hardware would tolerate a non-zero greybox — `rm_id_valid` is a gate,
not a zero-check — but there is no reason to spend the compatibility.)

**Source of truth: `fpga/dfx/rm_list.tcl`**, with `rm_id` *derived*, not
hand-written, so the wrapper constant and the manifest cannot drift:

```tcl
set RM_LIB(rm_nanosoc_multicore,design_id) "0x0003"
set RM_LIB(rm_nanosoc_multicore,version)   "1.0.0"   ;# major.minor.patch
# rm_id = (major<<24) | (minor<<16) | design_id   -- computed by `proc rm_id_of`
```

Keep `RM_LIB(<rm>,rm_id)` as a computed value so `build_dfx.tcl` and
`gen_manifest.py` need no restructuring.

**Patch and git-SHA do not fit in 32 bits and live host-side only** (manifest,
§4.3). This is a deliberate, stated limitation: `major.minor` is what a cold
board needs to answer "is this newer?"; the full record is one manifest lookup
away.

### 3.3 Why not the alternatives

- **(ii) separate `rm_version` port + DFXCTL register.** Costed in §5. It widens
  the boundary 122 → 154 bits, forces a **static re-key** (new `static_id`,
  every fielded partial invalidated, the QSPI overlay store and the baked
  greybox blob re-minted, the flashable base re-`updatemem`'d) and forces all 8
  RMs to be re-implemented anyway — and it buys **nothing** that (i) does not
  already provide, because `rm_id` has the bits. **Strictly dominated. Reject.**
- **(iii) host-side only.** Not an alternative — it is the **other half**, and it
  is adopted (§4.3). Alone it fails the "a board found in an unknown state must
  self-report" requirement and would leave the LCD unable to show a design
  version at all.
- **(iv) USR_ACCESS per-partial.** **Impossible** — §2.4.

### 3.4 Deferred: fabric readback of USR_ACCESS (ride the next re-key)

Instantiating `USR_ACCESSE2` in the static shell and exposing it at
**`DFXCTL.SHELL_USR_ACCESS` @ `0x44A1_001C` (RO)** would let the MicroBlaze
compare the version *baked into the bitstream it is actually running in* against
the version *compiled into its own image*. That directly detects the
firmware/bitstream skew hazard this platform has already been bitten by (a
flashable base whose `updatemem` was not re-run).

It is a **static RTL change → re-key**, so it is not worth doing on its own.
Land it opportunistically the next time a static rebuild happens for another
reason. Until then, `set_property BITSTREAM.CONFIG.USR_ACCESS` alone already
gives the JTAG path for free.

> **BUILT 2026-09-14 (`8ff559a`), riding the 2026-09 mint.** The readback landed
> as its own block, not as a DFXCTL register: `usr_access_rd` (block name
> `USRACC`) at **`0x44B3_0000`**, interconnect port `M19` — see
> `fpga/shell/ip/usr_access_rd/README.md` and `tests/usr_access_rd/`. The
> `DFXCTL.SHELL_USR_ACCESS @ 0x44A1_001C` address above was the plan and was never
> built. The `version` verb now populates `usr_access` from it (a single CSR
> read in `firmware/coordinator/coordinator.c`, `null` where the fabric does not
> answer), so §8.4's third state is no longer the permanent one.

---

## 4. How it surfaces

### 4.1 LCD — `firmware/clcd/clcd.c` `reformat()`, 40 cols × 15 rows

Space is genuinely tight. Measured free space in the current layout:

| Row | Current content | Free columns |
|---|---|---|
| 0 | board name + "nanoSoC harness" | 35–39 (5) |
| 2 | `DUT : <rm_name>` + `rm_id 0x…` @21 | 37–39 (3) |
| **4** | `SID : 0xXXXXXXXX` | **16–39 (24)** ← best home |
| 6 | `UP  : ddd:hh:mm:ss` | 18–39 (22) |
| 10–12 | *(error banner — reserved, do not use)* | — |
| 14 | MAC + heartbeat | 21–33 (13) |

**Recommended edits (2 lines, no new rows — rows 10–12 must stay free for the
banner):**

```
Row 2:  "DUT : nanosoc_multicore  v1.0"      <- name, then v<major>.<minor>
Row 4:  "SID : 0xXXXXXXXX  HV 1.4.2*"        <- static_id, then harness version
```

- `HV` = harness version; trailing `*` = built from a dirty tree
  (`mps3_harness_dirty()`). `HV 255.255.255*` is 15 chars — fits the 24 free
  columns on row 4 with room to spare.
- The design version comes straight from the `rm_id` the firmware already has
  (`RM_VER_MAJOR/MINOR(g_shell_state.current_rm_id)`) — **no new firmware state,
  no new register read.**
- **Constraint for the LCD agent:** row 2 writes `rm_name` at col 6 and a second
  field at col 21, so `clcd_rm_name()` strings must be **≤ 14 chars** or they
  collide (`put_at` clips at 40 but does not prevent overlap — `nanosoc_multicore`
  is 17 chars and *already* overlaps today). Either shorten the name table or
  drop the raw `rm_id` hex from row 2 (it is redundant once name + version render,
  and the raw value remains in the 6900 JSON).
- Add helpers next to the existing `clcd_fmt_*` family:
  `clcd_fmt_semver(char *dst, uint8_t maj, uint8_t min, uint8_t patch, bool dirty)`.

### 4.2 6900 JSON — `firmware/common/net_proto.c`

**Additive extension to `ping`** (existing keys unchanged; host `PingResponse`
gains optional fields with defaults):

```json
{"ok":true,"shell_id":"0xe4b1c44a","rm_id":"0x01000003",
 "harness_version":"1.4.2","rm_version":"1.0"}
```

**New `version` verb** for the full record (new `MPS3_OP_VERSION` enumerator, an
`s_op_table` entry, a handler in `coordinator.c`, a `case` in
`mps3_ctrl_encode_response()`):

```json
{"ok":true,
 "harness_version":"1.4.2","harness_ver32":"0x01040200",
 "harness_sha":"a1b2c3d4","harness_dirty":false,"harness_built":"2026-07-14",
 "shell_id":"0xe4b1c44a",
 "rm_id":"0x01000003","rm_design":"0x0003","rm_name":"nanosoc_multicore",
 "rm_version":"1.0"}
```

> **Budget check:** `MPS3_CTRL_RESP_MAX` is **768** bytes since net-protocol
> v0.9 (`firmware/common/net_proto.h`; it was 384 when this was written).
> The response above is ~250 bytes — it fits, but it is the largest response in
> the protocol. Do not add free-text fields to it without re-checking the bound.

Keep the existing lowercase `"0x%08" PRIx32` rendering (`format_id_hex()`); note
the LCD renders uppercase — that asymmetry already exists and is harmless.

### 4.3 Manifests — schema 1 → 2 (purely additive)

`fpga/dfx/gen_manifest.py`, `docs/contracts/overlay-manifest.md`:

```json
{
  "schema": 2,
  "static_id": "0xE4B1C44A",
  "harness_version": "1.4.2",
  "rm_id": "0x01000003",
  "rm_name": "nanosoc_multicore",
  "rm_design": "0x0003",
  "rm_version": "1.0.0",
  "rm_git_sha": "a1b2c3d4",
  "clearing": { "file": "...", "len": 84084,   "crc32": "0x7a708b84" },
  "partial":  { "file": "...", "len": 1534084, "crc32": "0xfb9f560f" },
  "built": "2026-07-11",
  "vivado": "2024.1"
}
```

Every new key is **additive and optional**, which is why nothing breaks:
`gen_manifest.py verify`, `check_overlay_static_id.py` and
`check_clearing_fits.py` all ignore unknown keys, and `pyverify`'s required-key
tuple (`overlay.py:140`) is untouched. Readers must accept `schema` 1 **or** 2.

### 4.4 Host + fpgahub REST

`host/pyverify/pyverify/edge.py` already has the right shape — **extend it, do
not invent a parallel one**:

```python
@dataclass
class ShellLevelStatus:
    loaded: bool
    derived_by: str
    static_id: str | None
    harness_version: str | None   # NEW  "1.4.2"
    harness_sha: str | None       # NEW  "a1b2c3d4"
    harness_dirty: bool | None    # NEW

@dataclass
class RpLevelStatus:
    loaded: bool
    derived_by: str
    rm_id: str | None
    rm_name: str | None
    rm_version: str | None        # NEW  "1.0"  (from rm_id high half)
```

`fpga_status_to_dict()` then emits them under the existing `shell` / `rp` keys.
fpgahub's `BoardRuntimeInfo` (which carries **no** identity fields today) gains
one optional block, sourced from the 6900 `version` verb:

```json
"design": {
  "harness_version": "1.4.2", "harness_sha": "a1b2c3d4", "harness_dirty": false,
  "static_id": "0xe4b1c44a",
  "rm_id": "0x01000003", "rm_name": "nanosoc_multicore", "rm_version": "1.0",
  "source": "shell-control",
  "as_of": "2026-07-14T10:00:00Z"
}
```

`source` ∈ `shell-control` (read live over 6900) | `manifest` (inferred from what
the host last pushed) | `unknown` (board not reachable / no firmware).

---

## 5. Cost table (honest)

| Option | Boundary cost | Static re-key? | RM rebuilds | Other cost | Verdict |
|---|---|---|---|---|---|
| **USR_ACCESS on the static** (bitstream property) | **0 bits** | **No** | 0 | 2 lines of Tcl | ✅ **do now — free** |
| **(i) re-encode `rm_id`** | **0 bits** (32-bit bus exists) | **No** | 8, one-off; **0 marginal** | 8 wrapper constants; mask macros; `uart_echo` re-numbered | ✅ **RECOMMENDED** |
| (iii) host/manifest only | 0 bits | No | 0 | manifest + dataclasses | ✅ adopt as the **complement** (patch, SHA); ❌ insufficient alone |
| (ii) separate `rm_version` port | **+32 bits (122→154)** | **YES** | **8, forced** | new BD port + decoupler group + DFXCTL reg + contract v0.2 + `pin_check` update; **every fielded partial invalidated**; QSPI overlay store + baked greybox blob re-minted; flashable base re-`updatemem`'d; 8× `pr_verify` + timing | ❌ **strictly dominated — reject** |
| (iv) USR_ACCESS per-partial | — | — | — | — | ❌ **impossible** (§2.4) |
| USR_ACCESSE2 + `DFXCTL.SHELL_USR_ACCESS` (fabric readback) | 0 bits | **YES** (static RTL) | 8, forced | buys firmware↔bitstream skew detection | ⏸ **defer — ride the next re-key** |

The decisive asymmetry: **(i) costs 8 RM rebuilds and leaves `static_id`
untouched**, so fielded shells keep working and every existing partial's
replacement drops straight in. **(ii) costs 8 RM rebuilds *and* a shell re-key**,
which invalidates everything already in the field — for no additional capability.
And after the one-off, versioning an RM under (i) costs **nothing**: you were
rebuilding that RM anyway, because you changed it.

---

## 6. Migration — ordered, each step independently shippable

1. **`VERSION` + `gen_version.py` + generated header.** Zero hardware. The
   generated file is gitignored (like `greybox_blob.c`); add weak fallbacks in
   `coordinator.c` so `firmware/test/` host-gcc binaries keep linking.
   `make check` unaffected.
2. **`USR_ACCESS` / `USERID` at `write_bitstream`.** Two lines of Tcl, placed
   **after** the locked-DCP write so `static_id` is not perturbed (§3.1). The
   JTAG read path exists from this moment on, with no RTL change.
3. **Manifest schema 1 → 2.** Additive keys only; teach `gen_manifest.py` to emit
   them and `pyverify` to read them optionally. `make check` stages 4 and 8 pass
   unchanged because all validators tolerate unknown keys.
4. **`rm_id` v2 encoding.** Add `design_id` + `version` to `rm_list.tcl` and
   derive `rm_id`; update the 8 wrapper `localparam`s; rebuild the RMs.
   **`static_id` stays `0xE4B1C44A`, so fielded shells keep working and the new
   partials load on them.**
   *Order matters:* update `clcd_rm_name()` / `clcd_rm_caps()` / `known_rm_names`
   to key off `RM_DESIGN(rm_id)` **before** shipping new partials. If they are
   not, the only symptom is cosmetic (the LCD shows `rm?0x01000003`); the swap
   still verifies, because `step_verify()` compares against the value in the
   pushed partial's own header, not a table.
5. **Surfaces.** LCD rows 2 + 4; the `version` verb; host dataclasses; fpgahub
   `design` block.
6. **Deferred.** `USR_ACCESSE2` + `DFXCTL.SHELL_USR_ACCESS` @ `0x1C`, whenever the
   next static rebuild happens for an unrelated reason.

**New CI gate (recommended, alongside `pin_check` in `make check` stage 2):**
`scripts/harness_gates/check_rm_id_encoding.py` — assert that, for every RM,
`wrapper localparam == rm_list-derived rm_id == manifest rm_id`, and that all
`design_id`s are unique. This repo has repeatedly been bitten by exactly this
class of three-way drift; the encoding makes it cheap to catch mechanically.

---

## 7. What this does **not** change

- `static_id` — generation (`build_dfx.tcl` `file_crc32` over
  `static_routed_locked.dcp`), the firmware `CFG_AGENT_ERR_STATIC_ID` pre-ICAP
  check, the host pusher check, and the `check_overlay_static_id.py` gate all
  stand untouched. It remains the compatibility key.
- The DFX partition boundary — **not widened**. `partition-pins.md` stays at
  v0.1, 30 signals / 122 bits, and `pin_check.py` needs no contract change.
- `DFXCTL.RM_ID` / `RM_STATUS` — same offsets, same CDC, same `rm_id_valid`
  semantics. Only the *interpretation* of the 32-bit value changes, and only in
  lookup tables.
- The swap/verify FSM — `step_verify()` is value-agnostic and needs no edit.

---

## 8. Wave B (2026-09) — a fourth identity, and the skew check goes live on the wire

§3.0 named two orthogonal axes and stopped there. Two more exist now, and this
section is additive to everything above — nothing in §1–§7 is revised, and
`static_id` still is not touched.

### 8.1 `static_canon` — the CONTENT identity, answering what `static_id` cannot

`static_id` is a CRC-32 of `static_routed_locked.dcp`, a zip carrying embedded
timestamps, so **a no-op rebuild from byte-identical sources mints a different
id**. Nothing anywhere could answer "did the static actually change, or did we
just rebuild it?" — until now.

`fpga/dfx/tools/static_canon.py` computes a SHA-256 over a canonical manifest of
the tracked sources that decide the static (part, Vivado version, the build
flags that reach the netlist, and the sha256 of every declared source file — no
timestamps, no build artefacts). Same inputs, any number of rebuilds, at any
time of day → the **identical** digest. One bit different in one declared
source → a different digest. That is the whole claim, and it is the one
`static_id` structurally cannot make.

**What it deliberately does NOT cover:** firmware C — firmware is baked into the
`.bit` by `updatemem` *after* place-and-route, so it changes the artefact
without changing the netlist static_canon is over; that axis is §3.1's
`HARNESS_VER32`, tracked separately. It also does not cover
`linked_static_dcp` (the shell checkpoint a run linked against) — recorded
*beside* the digest for provenance, never hashed *into* it, because a `.dcp` is
exactly the timestamped artefact this file exists to stop depending on.

**What decides "reaches the static" is itself gated**, not trusted:
`fpga/dfx/tools/static_inputs.txt` declares roots, includes and excludes, and
`static_canon.py check` (wired as `make -C fpga/dfx canon-check`) fails if any
`include` glob matches nothing (a declaration that silently stopped covering
anything), any design-source file under a declared root is undeclared (the
failure that would have caught `fpga/ethernet/` being missed — it is a
*sibling* of `fpga/shell/`, not a child, and a hand-audited list walking only
`fpga/shell/` would never see it), or any `exclude` carries no reason.

`static_id` is unchanged by any of this — `static_canon` is recorded *beside*
`static_id` in the mint record (§8.3), never in place of it, and it is baked
into **nothing**: no overlay, no firmware, no fielded artefact. It is a
board-free, Vivado-free cross-check a human or a CI job runs against the
sources, full stop.

### 8.2 `static_usercode` — the IMPLEMENTATION identity

`static_id` is compared against a value **compiled into the shell's own
firmware**, so it identifies the *design*, not the *implementation of it* — it
reads "correct" no matter which place-and-route result is actually flying. Two
independent implementation runs of the identical design share one `static_id`
and have **incompatible routing**; loading a partial built against one onto the
other destroys the FPGA configuration outright. That happened, twice, on
2026-07-24.

`BITSTREAM.CONFIG.USERID` (the second 32-bit config-register slot, distinct
from `USR_ACCESS`/AXSS — §2.1–§2.3) is stamped with the 8-hex build commit at
`write_bitstream` and the device reports it back as `REGISTER.USERCODE` over
JTAG. `fpga/dfx/tools/stamp_usercode.py` (`make -C fpga/dfx overlay-usercode`,
run automatically inside mint stage 5) reads it out of the full static `.bit`'s
own ASCII header and writes it into every `overlay/<rm>/manifest.json` as
`static_usercode` — a field `gen_manifest.py` has carried since c0784ed and
`check_image_overlay_match.py` already gates, but that stage never populated
until now, which made the guard data-free.

**Two independent derivations, cross-checked, never merged in favour of one:**
the `.bit` header is the authority; `build_dfx.tcl`'s own `static_stamp.json`
(written by `fpga/dfx/tools/static_stamp.tcl` right after `write_bitstream`,
§8.3) is what the build *intended* to stamp. A disagreement between the two is
fatal — it means the `.bit` beside the record is not the one the record
describes — never silently resolved. An **unstamped** bitstream
(`0xFFFFFFFF`, `DFX_NO_VERSION_STAMP`) is refused outright: every unstamped
design in the world reads that value, so recording it would make the overlay
guard pass on exactly the builds it exists to catch (`--allow-unstamped` if
that is genuinely intended).

`static_usercode` answers "which implementation run"; `HARNESS_VER32`/
`USR_ACCESS` (§3.1, §8.4) answers "which firmware release". Neither
substitutes for the other, and USR_ACCESS is deliberately not (ab)used for the
implementation question: it is identical across different P&R runs of the
same harness version, so it cannot discriminate them.

### 8.3 `static_stamp.json` and the mint record — schema 1.0 → 1.1

`fpga/dfx/tools/static_stamp.tcl` (sourced by `build_dfx.tcl`, kept
Vivado-free so it is unit-testable under plain `tclsh`) writes
`$BUILD/prod/static_stamp.json` right after the reference RM's
`write_bitstream`: the bare bitstream name, `static_id` for cross-reference,
and whatever the design reports back for `BITSTREAM.CONFIG.USERID` and
`BITSTREAM.CONFIG.USR_ACCESS` — `null`, never a plausible-looking zero, for
whichever one `DFX_NO_VERSION_STAMP` suppressed.

`fpga/dfx/tools/mint_record.py` schema went `1.0` → `1.1`: two new top-level
slots, `static_canon` (the whole `static_canon.json`, inputs list included, so
a differing digest also says *which* file moved) and `static_usercode`
(`usercode`, plus the sibling `usr_access`/`harness_version` stamps, plus which
of the two derivations in §8.2 supplied the value). Both are `known`/`unknown`
value-with-reason slots like every other mint-record field — a run that passed
neither `--static-canon` nor `--static-bit`/`--static-stamp` records *why* the
slot is empty, never a bare `null`. `cmd_reconstruct` deliberately stays at
schema `1.0`: neither field is reconstructible from three tracked text files on
a fresh checkout (`static_canon` needs the exact repo revision a mint read,
which was never written down for a reconstruction; `static_usercode` needs the
gitignored 11 MB `.bit` itself), and `cmd_verify` holds a `1.0` record to the
`1.0` key set so a mint that predates this Wave does not start failing because
the tool moved on. See `docs/BUILD_AND_MINT.md` "Two more identities" for the
operator-facing commands.

### 8.4 The ver32-vs-USR_ACCESS skew check goes onto the wire — three states

§3.4 deferred the *fabric readback* — instantiating `USR_ACCESSE2` in the
static shell behind `DFXCTL.SHELL_USR_ACCESS` so the MicroBlaze can read back
its own bitstream's stamp — because that is a static RTL change and forces a
re-key. **That deferral is UNCHANGED by this Wave**: `coordinator.c` does not
touch a register here, and nothing in this Wave adds one. *(Superseded
2026-09-14 by `8ff559a` — the register was built, as `USRACC` at `0x44B3_0000`;
see the dated note at the end of §3.4. The paragraphs below describe the
firmware as it was in Wave B.)*

What *is* now live is everything on the *consumer* side of that eventual
register, wired and tested ahead of it:

- `net_proto.h`/`net_proto.c` (`firmware/common/`) add `usr_access` and `skew`
  to the `version` verb's response (`mps3_ctrl_response_t.usr_access`,
  `MPS3_OP_VERSION`'s encode arm). `skew` is **derived in the codec**, never
  carried in from elsewhere: it is `strcmp(ver32, usr_access) != 0`, compared
  as the two identically-rendered (`format_id_hex`) strings, so there is one
  formatter and one comparison and no second numeric copy of `ver32` to drift
  out of step with the first. A handler cannot report "agree" without setting
  both fields, and a caller cannot receive "agree" without both.
- **Three states, and the third is not a pass.** `usr_access` unset (always,
  until `8ff559a` built the register — see §3.4) emits `"usr_access":null,"skew":null`: the check was
  **not performed**, which must never be read as "performed, fine". Set,
  `"skew":false` means the image and the bitstream agree; `"skew":true` means
  they do not — the exact "flashable base whose `updatemem` was never re-run"
  hazard this platform has already been bitten by, now something a client can
  ask about directly instead of inferring from a wedged board.
- `host/pyverify/pyverify/client.py`'s `VersionResponse` carries `usr_access`/
  `skew` through with the `None`-vs-`False` distinction intact (a bare
  `bool(resp.get("skew", False))` would turn "not checked" into "checked,
  fine", which is the one reading this field exists to prevent), and exposes
  `skew_verdict` (`"ok"` / `"SKEW"` / `"unchecked"`) as the one property a
  caller should gate on — kept out of `ok`/`__bool__` deliberately, because
  `version` still answers successfully when it reports a skew.
- `host/pyverify/pyverify/testing/fakeshell.py`'s `FakeShell` grew the matching
  `harness_usr_access` scenario knob (`None` by default — the same "not
  provisioned" default `ctrl_echo` reports) so the double stays byte-for-byte
  conformant with the real firmware's `version` response
  (`tests/firmware_logic/test_fakeshell_conformance.py`).

**Why `usr_access` is `null` on every board today, and will be until §3.4
lands:** `coordinator_handle_version()` (`firmware/coordinator/coordinator.c`)
sets every OTHER field from a pure build-time constant and sets no
`usr_access` at all — there is no `DFXCTL.SHELL_USR_ACCESS` register yet for it
to read. The wire contract, the three-state codec logic, and every consumer of
it are proven today by construction (`firmware/test/test_net_proto_json.c`
exercises all three states through the real encoder with a hand-set
`resp->usr_access`) — proven as *code*, not yet exercised as a live comparison
on any silicon, because nothing has ever given the firmware a fabric value to
compare against. That wiring — `coordinator.c` reading a real register — is
the remaining piece of §3.4, unchanged in scope or cost (a re-key) by anything
in this section.
