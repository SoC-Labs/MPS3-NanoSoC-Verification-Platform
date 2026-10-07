# ICAP-SPIKE — honest coverage notes

Date: 2026-07-16. Two-tier harness: host unit tests (gcc, seconds) + the
QEMU suite (the REAL `mps3_dfx.ko` inside the proven Buildroot rv32
kernel 6.18.7 under `qemu-system-riscv32 -M virt`, driven over the serial
console with the RECORDED regdemo_a/regdemo_b clearing+partial `.bin`
pairs from `fpga/dfx/build_v2enc/prod`).

## What IS proven (and where)

| Requirement (SERVICE_DISPOSITION §4) | Host test | QEMU scenario |
|---|---|---|
| MSB-first packing, AA995566 reaches ICAP | test_pack (incl. REAL .bin sync scan @0x50), engine capture-CRC checks | `happy` x3: capture CRC == CRC(file bytes), saw_sync, saw_desync |
| Word straddling split writes (≤3-byte residue) | sd cut sizes 1/2/3/509 | sd pushes in 64 KiB writes over 4 KiB kernel bounces |
| FIFO CR protocol (WFV pacing, batch StartConfig, CR self-clear) | all FIFO scenarios; mock counts overflow (0 required) | `happy sd`/`happy staged` (depth-1024 mock) |
| LITE CR protocol (per-word StartConfig) | t_lite_mode (staged+sd) | `happy` under `fifo_mode=0` reload |
| Chunking w/ scheduling points | relax() call count asserted >0 | cond_resched() exercised on 1 MB streams |
| Decouple confirm-poll, fail-closed | t_decouple_never | `decouple` (ETIMEDOUT, engine idle) |
| Release confirm-poll, fail-closed + repark | t_release_never | `release` (ECONFIRM, parked re-checked) |
| Clearing-before-partial ordering | t_ordering (incl. nothing-reaches-ICAP) | `order` (pre-arm push, early partial, early finish, abort) |
| CRC strictly before ICAP (staged) | (engine API contract: caller CRCs) | `badcrc`: reject leaves 0 words in ICAP AND the session armed; retry streams |
| Stream-direct containment (CRC-at-end → parked) | t_sd_crc_mismatch | `sdcrc` |
| sd arm gate (FSM-armed AND DFXCTL-parked — the 2026-07-10 fix) | t_ordering (sd_begin at wrong state) | `order` early-partial refusal |
| RM_ID verify: valid+wrong → immediate RE-ISOLATE | t_wrong_rm_id | `wrongid` (parked, rm_id not updated) |
| RM_ID verify: never-valid → bounded timeout → RE-ISOLATE | t_rm_id_never_valid | `novalid` |
| Decoupler RM_ID clamp / release-then-verify (R1) | mock clamps RM_ID to 0 while decoupled — verify passes only post-release | same model in-kernel |
| Commit point ordering (shutdown clear + dut/dbg release only after verify) | t_happy asserts shutdown_reg/clkrst_reg | mock state via GET_STATUS |
| Stuck ICAP fail-closed (CR stuck / WFV stuck) | t_cr_stuck, t_wfv_stuck | `crstuck`, `wfvstuck` (short-poll reload) |
| 30 s RX-idle reap (re-armed on progress, parks) | t_abort_park (engine half) | `idle` at idle_ms=1500: reap fires mid-sd, parked, recoverable |
| Parked-decoupled failure invariant | every fault scenario checks is_parked() | every fault scenario re-reads DFXCTL |
| EOS capture-only default vs strict gate | t_eos_strict_and_never (both modes) | `happy` asserts eos_status=SEEN; strict mode host-only |
| hostio4 hook placement (post-partial, pre-release, parked) | hook counter == 1 per swap | GET_STATUS hostio4_calls >= 1 |
| Pure table port fidelity | test_transitions (all arcs + 13x4096 totality + frozen enum values) | table linked into the .ko |
| Single-owner + gating surface | — | `second` (EBUSY mid-swap), sysfs attrs read |
| Module lifecycle | — | 5 insmod/rmmod cycles + `reload` |
| Progress observable mid-swap | — | sysfs `state`/`icap_bytes`/`rm_id`/`eos_status` (racy-by-design reads) |

Also proven: engine `icap_bytes` counts only CONFIRMED words and is
free-running across swaps (ported diag semantics) — the first QEMU run
caught a *test* assuming per-swap reset (fixed to a delta check; driver
behaviour was correct).

## What is NOT proven (be explicit before anyone leans on this)

1. **Real MMIO / real ICAPE3.** The mock binds at the engine's ops seam;
   `readl/writel` paths, ioremap, bus ordering, and the actual axi_hwicap
   IP have NOT been executed. The EOS-asserts-on-this-build question
   (I18(3)) remains open — the driver captures raw SR precisely so the
   first board run answers it. Board bring-up items: byte order vs a real
   ICAP (`MPS3_HWICAP_MSB_FIRST`-equivalent is compile-time here), real
   poll-bound tuning, throughput vs the proven ~570 KB/s.
2. **fpga-manager / fpga-bridge veneer is compile-checked only**
   (`make check-fpga`; CONFIG_FPGA is unset in the proven kernel config).
   Registration, region flow, and the bridge-enable→finish tail have never
   executed. Treat the chardev as the only tested interface.
3. **No DT binding executed.** The spike self-instantiates (mock=1 or
   ioremap of the frozen map). The platform-driver + phandle binding and
   the remove-generic-uio step are documented, not implemented.
4. **Wire-header validation beyond the pair rule** (magic/version/static_id
   /slot ordering) is deliberately daemon-side (SERVICE_DISPOSITION §3.3);
   the driver checks only what the hardware invariants need (kind, length,
   CRC, ordering). The daemon does not exist yet.
5. **Idle reap tested at 1.5 s, not 30 s** (same mechanism, scaled module
   param — the 30 s default itself is untested wall-clock).
6. **Concurrency**: single-open + one mutex is the tested model. No
   multi-threaded writer torture; sysfs reads are racy by design.
7. **QEMU is `-M virt`**, not the MBV SoC: no uartlite/axi_intc/Sstc
   interaction, no S-mode DECERR behaviour (the mock cannot DECERR).
8. **Clearing-cache promote (STAGE→CACHE) and A/B slot logic** are
   daemon/MTD work, out of scope here (and still embargoed on the shared
   board — D16).
9. **The nanosoc_multicore 2.9 MB partial** was not run in QEMU (regdemo
   pairs only, ~1.1 MB); the 8 MiB staging cap covers it but is untested
   at that size in-kernel.

## Recorded-sequence provenance

`clr_a/part_a` = `config_rm_regdemo_a_pblock_rp_dut_partial{_clear,}.bin`,
`clr_b/part_b` = regdemo_b equivalents (read-only main repo,
`fpga/dfx/build_v2enc/prod`, the 1 MiB-shell 0x14E1A2D8-era set built
2026-07-14). rm_ids 0x010000A1 / 0x010000B2 per `fpga/dfx/rm_list.tcl`
(v2 encoding). The mock presents the expected id only after the SECOND
sync word since arm (clearing then partial) — so a swap that never
streamed its partial cannot verify.
