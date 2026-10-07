# `rm_nanosoc_upy` — MicroPython from QSPI flash

Build and design notes: [`README_XIP.md`](README_XIP.md). This file covers two
things: the **image/bootrom pair** and the **W1 re-flash procedure**.

The pair's record is [`flash_image.json`](flash_image.json), a sidecar next to
the overlay files. It is not a `manifest.json`.

---

## Why the flash image and the RM's bootrom are a matched pair

This RM boots in two halves, and they come from two different builds:

| half | built by | ends up in |
|---|---|---|
| stage-0 bootrom | `tests/micropython_flash_boot/tools/build_bootrom.sh`, run by the `rm_nanosoc_upy` synth PREP (`FPGA_BOOTROM_DIR=$(BUILD)/rm_nanosoc_upy_synth/bootrom`) | the RM's ROM, baked into the **partial bitstream** |
| boot table + HOT + COLD | `make -C firmware/micropython xip` (`flash_pack.py`) | the board's **QSPI flash**, written by hand with the M0 loader |

Stage-0 reads the boot table at flash `0x0` using the struct layout and CRC32
from `nanosoc_m0_soc/firmware/bootloader/boot_table.h`. It copies HOT (flash
`0x1000`) into IMEM and checks its CRC. Then it sets up the XiP aperture:
opcode `0x0B`, **8 dummy cycles as a plain count**, CG092 cache on. Then it
jumps. COLD (flash `0x20000`, linked at `0x70020000`) runs in place, through
the aperture the image's own `xip_bringup.c` programs again.

Change either half and the other can go wrong without any error:

* the boot-table layout or CRC changes → `Q:BADTBL` / `Q:HOTCRC`, then
  fallback to an empty IMEM, then silence;
* the aperture setup or dummy cycles change → HOT runs, but every COLD fetch
  is shifted or faults. The symptom is a REPL that echoes and then dies inside
  string or builtin code.

Nothing on the board checks that the two halves match. The mint re-synthesises
the RM, and nobody re-flashes the board. So an RM rebuilt without a matching
flash image is the footgun, and `flash_image.json` records both md5s side by
side.

### What 2026-09-23 found — the bootrom did NOT change

The 09-23 re-proof on `0x3F1A560F` failed (`docs/evidence/2026-09-w2/rf_flash_boot_20260923.txt`).
The console showed `~BQI=18010000,…`, so the HOT copy and CRC passed. Then
`print(1+1)` raised and the traceback hung part-way through. The leading
hypothesis was an image/RM **pair mismatch**. For the bootrom half of the pair,
it is now **refuted**:

* The stage-0 `stage0.bin` md5 is `7d7ee052…` in all four of these:
  * the July co-sim build (`tests/micropython_flash_boot/build`, 07-15);
  * the `0xA8C1C535` mint;
  * the `0x3F1A560F` mint;
  * a fresh build today (two builds, byte-identical).
* Today's image differs from the July-15 image in **exactly 3 bytes**: the
  banner date string. HOT is byte-identical.

So if the flash still holds the July image, re-flashing writes the **same
code**. It is expected to change nothing. The regression then lies in:

* the flash contents (corruption since July), or
* the fabric: SoC or `ahb_qspi` RTL, re-synthesised from the repointed
  `nanosoc_m0_soc` tree.

The first W1 step below is a **read-only CRC** of the flash. It tells these two
cases apart before anything is erased.

### The clock trap, stated correctly

`dut_clk` is **50 MHz**. Stage-0 computes `BAUDDIV = NANOSOC_SYS_CLK_FREQ_HZ / 38400`.
The value comes from the `imp/fpga/fw_config` header, which says **25 MHz**, so
`BAUDDIV = 651` (the literal `0x28B` in `stage0.elf`). At 50 MHz that gives
**76800 baud**. That is the shell's `uart_axis_shim` rate, and it equals
MicroPython's own `BAUDDIV = 651`. It is right at the pad by coincidence.
**Do not "fix" the macro to 50 MHz.** That would make `BAUDDIV = 1302`
(38400 baud) and garble the stage-0 line on 6930. Every silicon run so far,
July and 09-23 included, used the 651 bootrom and read its line cleanly.

---

## W1 re-flash (2026-09-24) — over JTAG on 6921

**Not yet run.** 6920/SWD is dead on the fielded shell. The live debug path is
OpenOCD `remote_bitbang` → fw `jtag_server` on **6921** → SWJ-DP (TAP
`0x6ba00477`), using `host/openocd/nanosoc_mps3_jtag.cfg`.

**Time: about 25 min.** Loader entry ~1 min, CRC ~1 min, program 160 KB, then
probes 5 min. The program step took 2 min 41 s in July over SWD; over JTAG
rbb it has never been measured, so budget 15 min for it.

### Before you start

* Hold a lease, and have `nanosoc_upy` (rm_id `0x01000005`) resident. The
  loader needs its **128 KiB IMEM**: mailbox `0x10010000`, stack top
  `0x10020000`.
* **Do not swap away from `nanosoc_upy` afterwards** unless you are ready to
  recover. It is a DAP RM, and the DAP-RM teardown bug applies.
* Stage on the hub, where OpenOCD runs:

  ```bash
  # build host
  make -C firmware/qspi_loader                      # build/qspi_loader.bin, md5 19e9db99ed2b03c41e0f3c16f6bb8d4d (== July's)
  make -C firmware/micropython xip SOURCE_DATE_EPOCH=1786110110
  md5sum firmware/micropython/build/xip/flash_image.bin   # 45017bb6c1c0c3d09ba31433dd190839
  ST=<hub-staging-dir>; ssh <hub> mkdir -p $ST
  scp firmware/qspi_loader/build/qspi_loader.bin firmware/micropython/build/xip/flash_image.bin \
      firmware/qspi_loader/qspi_loader_jtag.tcl host/openocd/nanosoc_mps3_jtag.cfg <hub>:$ST/
  # hub
  cd <hub-staging-dir> && split -b 49152 -d -a 2 flash_image.bin chunk_   # chunk_00..chunk_03
  ```

  ```bash
  # the OpenOCD prefix used below (hub). ORDER IS LOAD-BEARING: the sets precede -f.
  ST=<hub-staging-dir>
  OOCD="openocd -c 'set TRANSPORT_MODE rbb' -c 'set RBB_HOST 192.168.10.101' \
        -c 'set RBB_PORT 6921' -f $ST/nanosoc_mps3_jtag.cfg -f $ST/qspi_loader_jtag.tcl -c init"
  ```

### Steps

1. **TAP check** (read-only):
   `eval $OOCD -c shutdown`. Pass if the output shows `tap/device found: 0x6ba00477`.
2. **Enter the loader and CRC the flash as it is now** (read-only, no erase):
   `eval $OOCD -c "'ql_enter $ST/qspi_loader.bin'" -c "'ql_crc 0x0 163840'" -c shutdown`
   Compare the printed CRC32:

   | flash CRC32 | meaning | next |
   |---|---|---|
   | `0x6133A3D8` | the July-15 image, **intact** | the regression is in the **fabric**, not the image. Still do step 3 to record the pair; do not expect it to fix RF |
   | `0x0DBE004A` | this image is already there | skip step 3 |
   | anything else | flash **changed or corrupt** since July | step 3 is the fix candidate |

3. **Program** (erase `0x0`–`0x28000`, 4 chunks, then CRC):
   `eval $OOCD -c "'ql_enter $ST/qspi_loader.bin'" -c "'ql_program_split $ST/chunk_ 4 0x0 163840'" -c "'ql_crc 0x0 163840'" -c shutdown`
   Pass only if the CRC is **`0x0DBE004A`**. Judge by the CRC, not by status
   codes (see trap 1 below).
4. **Reset the DUT:**
   `python3 -c 'import socket;s=socket.create_connection(("192.168.10.101",6900));s.sendall(b"{\"op\":\"reset\",\"target\":\"dut\"}\n");print(s.recv(256))'`
   (expect `{"ok":true}`; `coordinator_handle_reset` pulses `DUT_RESETN`).
   If the console then shows `!REMAP!`, the pulse did not clear REMAP. Re-push
   the `nanosoc_upy` overlay instead.
5. **Three probes on 6930.** Pace your input at 20 ms per character. At line
   rate the shallow RX FIFO mangles it.
   1. Stage-0 line: expect `~BQ` … `W=18010000,000083B5` … `R`. The vector is
      HOT word0/word1, read from the image. `W=00000000` means empty flash.
   2. The MicroPython banner `MicroPython 532428cc6d on 2026-08-07` and `>>>`.
      The new date string is itself proof that the new image is the one
      running.
   3. `print(1+1)\r\n` → `2`.

   ```bash
   python3 - <<'PY'
   import socket, time
   s = socket.create_connection(("192.168.10.101", 6930)); s.settimeout(0.5)
   def rd(t):
       end, b = time.time() + t, b""
       while time.time() < end:
           try: b += s.recv(4096)
           except socket.timeout: pass
       return b
   print(rd(3.0))                                      # banner if you reset after connecting
   for ch in b"print(1+1)\r\n": s.send(bytes([ch])); time.sleep(0.020)
   print(rd(3.0))                                      # expect b'...\r\n2\r\n>>> '
   PY
   ```

### The traps, and the halt-first rule

* **Halt first.** `ql_enter` begins with `nanosoc_halt_examine`: a raw
  AP write to DHCSR, then `arp_examine`. The JTAG cfg's target is
  `-defer-examine`, so nothing works before that. Then it disables SysTick and
  NVIC before it loads the loader. ARMv6-M cannot clear an ACTIVE exception,
  and reset-halt is not reliable on this DUT.
* **Trap 1 — SPI_CMD read-backs** (DUT side, already fixed in
  `qspi_loader.c`): every controller register write needs a read-back to land.
  That includes **both** `SPI_CMD` writes. Without them, every op reports
  success and does nothing. So judge only by the CRC against known data.
* **Trap 2 — wait for BUSY to ASSERT, then for it to clear** (also fixed in
  the loader). If you poll only for "clear", the poll lands before BUSY rises
  and you read `RDATA0` early. That failure is silent and depends on the
  transfer size.
* **The loader clears `XIP_ACTIVE` at entry.** After a flash boot the
  controller owns the bus for XiP and rejects APB commands.
* **Known risk (July):** the loader would not enter on a *failed-boot, empty*
  RM. The July workaround was the scaffold RM, which is not on the SD. If
  `ql_enter` times out on the magic, stop and record it. Do not erase.

### The Python client

`scripts/qspi_loader_bringup.py` → `pyverify.qspi_loader` → `pyverify.swd.SwdDebugger`
has **no working 6921 mode today**. `SwdConfig(port=6921,
bare_cfg="host/openocd/nanosoc_mps3_jtag.cfg", cortex_cfg=…)` builds, but:

* every op is a fresh OpenOCD session on a `-defer-examine` target, so a bare
  `halt` or `mdw` fails "not examined";
* adding `nanosoc_halt_examine` to each session would **halt the loader on
  every mailbox poll**.

It needs lane HOST's retarget: one session, or a `mem_ap` target for polling.
If that lands, the calls are the same as above:
`--stage=crc --offset 0x0 --bytes 163840`, then
`--stage=full --image flash_image.bin --offset 0x0 --i-know`, with the JTAG
cfg. Until then, `qspi_loader_jtag.tcl` above is the path.
