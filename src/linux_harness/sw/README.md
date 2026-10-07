# sw/ — the MPS3 Linux harness image

Buildroot image for the MicroBlaze V Linux harness: kernel, rootfs, OpenSBI and the
one-blob boot image stage0 loads. The spec is
[`docs/planning/linux_lanes/IMAGE_CONTRACT.md`](../../../docs/planning/linux_lanes/IMAGE_CONTRACT.md);
this file is the quick guide.

```
./build.sh                              # release image -> artifacts/   (≈ 1–1.5 h, niced)
MPS3_BUILD_STEP=config ./build.sh       # clone + configure + checks only (≈ 30 s)
MPS3_QEMU=<qemu-system-riscv32> ./boot_qemu_harness.sh   # the QEMU proof (4 boots)
./mk_debug.sh && MPS3_QEMU=<…> ./boot_qemu_debug.sh      # debug overlay + rescue DTB
MPS3_QEMU=<…> ./rehearse_first_install_qemu.sh           # docs/LINUX_HARNESS.md §6.4, run verbatim (3 boots)
```

`rehearse_first_install_qemu.sh` takes §6.3's claim line and §6.4's blocks straight
out of `docs/LINUX_HARNESS.md` and runs them against a blank virtio card. It runs
the real pyverify, ssh, and the image's own tools. The steps QEMU cannot do are
emulated, and each one is logged as an `EMUL` line. For example, stage0 is replaced
by STAGE0's card reader, and the fabric verbs are served by the rv32 mock
`mps3-harnessd` built from `harnessd/`. Its log is `rehearse_first_install.log`.

`build.sh` refuses to start while another Buildroot build runs on the host
(`MPS3_BUILD_FORCE=1` overrides). For a board release, provision it for the minted static
first: `eval "$(make -s -C fpga/dfx linux-image-env SHELL_CPU=mbv BUILD=<b>)"`
(FLOW_CONTRACT §1.4).

## 1. What `build.sh` makes

One Buildroot configuration, one kernel: 6.18.7, **slim** (no `STRICT_KERNEL_RWX` — no
kernel W^X, the price of fitting below the DTB slot), **nowfi** idle (every July silicon
success ran it), with the **rootfs embedded as an initramfs**. Nothing is rebuilt with a
toggled config between artefacts.

| `artifacts/` | What |
|---|---|
| `fw_payload_1region.bin` | **the board image**: OpenSBI `FW_PAYLOAD` = OpenSBI + `Image` + DTB, one region at `0x8000_0000`, `a1=0`. FLOW wraps it into the stage0 slot image (`stage0_pack.py`) |
| `linux_slot.img` | the same blob as the S0LB boot-table image stage0 boots: `stage0_push.py <board> linux_slot.img` (TFTP rescue) or `mps3-slot write A\|B` |
| `Image` | the kernel with the rootfs inside (QEMU `-kernel`, xsdb) |
| `fw_jump.bin` / `.elf` | OpenSBI for QEMU and the 3-region xsdb boot |
| `shell_linux.dtb` | the generated device tree (§4), `dtc -p 4096` |
| `rootfs.cpio.gz` | the same rootfs, standalone (inspection, `mk_debug.sh`) |
| `version` | the image's `/etc/mps3/version` manifest |
| `legal-info/` | Buildroot `make legal-info` (sources + licences) — **ships with every image** (GPL) |
| `SHA256SUMS`, `MANIFEST.txt`, `ROOT_PASSWD` | checksums, provenance, the console password |

Gone since 2026-09-23: `Image_nowfi` (the one `Image` is nowfi), `rootfs.ext2`, the
separate rootfs region and its `linux,initrd-end` stamping, and `mk_1region.sh` as the way
to make the blob (it lost every kernel module and carried the wfi kernel).

xsdb (lab): `fw_jump.bin@0x80000000`, `Image@0x80400000`, `shell_linux.dtb@0x82200000`,
`pc=0x80000000 a0=0 a1=0x82200000`.

## 2. Image kinds and variants

| Knob | Values |
|---|---|
| `MPS3_IMAGE_KIND` | **`release`** (default): no SSH host key baked (a gate fails the build), SSH key-only. **`lab`**: may bake a host key (`MPS3_HOST_KEY_FILE=generate\|<path>`, the B0 seam) and allow password SSH (`MPS3_SSH_PASSWORD_AUTH=1`). FLOW never bundles a lab image |
| `MPS3_VARIANT` | **`default`**: `mps3-harnessd` (the firmware services on Linux) under inittab `respawn`. **`legacy`**: the July v0.7 daemons (`daemons/`), `mps3_dfx.ko` (`../drivers/icap/`) and `mps3-clcdd` (`apps/`), each package installing its own S-script — kept buildable until the deletion PR |
| `MPS3_IDLE` | `nowfi` (default) \| `wfi` (experiment only) |

Other seams, all build-time env and never committed: `MPS3_STATIC_ID` (with it,
`MPS3_GREYBOX_CLEAR` — the static's greybox clearing; the build refuses one without the
other), `MPS3_ROOT_PASSWD`
(serial console only), `MPS3_AUTHORIZED_KEYS[_FILE]` (baked keys), `MPS3_WG_*`,
`MPS3_STAGE0_ELF`/`MPS3_STAGE0_SHA256`.

## 3. On the board

**`/persist`** (`S12mps3persist`). The user microSD's **partition 3** — found by the MBR
layout stage0 uses (entries 1/2 type `0x7F`, entry 3 `0x83`), never guessed. It must already
be ext4: nothing is formatted automatically; `mps3-persist format --erase` does it on
request. No card ⇒ no wait, no error, and `/persist` is a tmpfs, so every path still exists.
It holds the host key (bound on `/etc/dropbear`), the SSH claim, harnessd's state
(`/persist/mps3`) and an overlay for `/etc/mps3` edits (image-owned files such as
`static_id` and `version` always come from the image).

**SSH.** Key-only. Each board generates its own ed25519 host key on first boot (a fresh one
each boot with no card); the fingerprint is in `/run/mps3/ssh/host_key_sha256`. The
**first key claim** (TOFU): a TFTP WRQ named `authorized_keys` to port 69 while unclaimed —
harnessd writes `/persist/ssh/authorized_keys` and runs `mps3-keys-sync`, which merges the
claim with any baked keys into `/root/.ssh/authorized_keys`. **`mps3-unclaim`** on the
serial console (root password) forgets the claim and reboots.

**Network** (`S41mps3net`, `/etc/mps3/net.conf`). DHCP first (bounded, then background),
and **192.168.10.101/24 always as a secondary** after duplicate-address detection — so a
direct-cabled board is always findable.

**Supervision.** The harness process runs from `/etc/inittab` (`::respawn:`), merged from
`/usr/share/mps3/inittab.d/` drop-ins; init restarts it within about a second. Until the
harnessd package ships its drop-in, a placeholder stands in. `mps3-reboot` resets the board
through the watchdog (a plain `reboot` cannot reset the MicroBlaze V).

**Healthy boot.** `S99mps3health` writes `/run/mps3/boot-health`; harnessd confirms the boot
to stage0 only when it says `healthy=1`, so a slot image that comes up broken falls back to
the other slot.

**µSD slots.** `mps3-slot status | default A|B | write A|B IMAGE` — the Linux side of
stage0's card layout (power-safe boot-select, checked slot writes).

**On-board GDB server (MVP, 6 Oct).** `mps3-debug up [--rm auto|NAME] | down | status |
version`, run as root over the claim's SSH, one JSON object out (contract `mps3-debug/1`;
exit 0 / 4 busy / 6 failed / 12 no OpenOCD / 13 no DAP / 14 no recipe). It starts
`/usr/bin/openocd` (0.12.0 + remote_bitbang + the SoC Labs ahb_qspi/hostio4 drivers,
`patches/openocd`) as the unprivileged `openocd` user at nice 10, dialling the harness's
own `127.0.0.1:6921`, with GDB 3333 (+3334 for cpu1), telnet 4444 and Tcl 6666 bound to
127.0.0.1 — reach them with `ssh -L`. `--rm auto` reads the rm_id from identify
(127.0.0.1:6899, never 6900) and maps it through `/usr/share/mps3/openocd/designs.conf`.
harnessd is unchanged: 6921 stays single-client, so a host OpenOCD on it makes `up` busy.
The session ends on `down`, an OpenOCD exit, 2 h without a GDB client, or a swap
(identify's rm_id changes: `target_lost`). Sources and host tests:
`br2_external/package/mps3-debug`; QEMU check: `qemu_mps3_debug_check.sh`; the board
measurement: `scripts/linux_board/demo_gdb_on_board.sh`.

## 4. Device tree and drivers

`../shell_linux.dts` is **generated** — `python3 tools/gen_dts.py`, never edit it by hand —
from the shell BD and `fpga/shell/generated/regmap_mbv.json`. Every block is kernel-owned (its
driver) or harnessd-owned (a node whose only compatible is `generic-uio`), never both. The
user microSD is kernel-owned: `spi-usd.ko` (`br2_external/package/mps3-spi-usd`) + the stock
`mmc_spi` slot → `/dev/mmcblk0`.

Kernel patches (`patches/linux`): 0001 uartlite poll mode (DT-gated), 0002 the nowfi Kconfig
option, 0003 the AXI-timer clockevent (MBV errata E1/E2), 0004 uartlite In1 race, 0006
smsc911x self-test, 0007 `udelay` off the frozen `rdtime`. `patches/diag/` is not applied.

## 5. Gates (`make check-linux-dts`)

```
python3 tools/gen_dts.py --check          # the DTS is fresh
python3 tools/dts_gates.py                 # ownership, dtc, ETH_INT triple, D13 preview, INTC inputs
sh src/linux_harness/sw/br2_external/tests/run.sh   # image scripts, host-key gate, mps3-slot
```
Every check has a negative control that must fail.

QEMU (`-M virt`) proves the kernel, rootfs, `/persist` across reboots, the pinned host key,
the claim, respawn and key-only SSH. It cannot prove anything on the MPS3 buses (uartlite,
INTC, LAN9220, UIO, `usd_spi`): that is board window B1.
