# nanosoc_upy — flash-boot (XiP) is the SHIPPED DEFAULT

> **Resolved D17 (2026-07-22):** flash-boot XiP is now the shipped `nanosoc_upy`
> overlay. The baked-BRAM scaffold is retained as a NO-FLASH bring-up variant of
> the same fabric (same rm_id 0x01000005). They cannot be two overlays — the
> rm_id gate binds one fabric to one id — so this is a build-flavour choice, not
> two RMs.

`rm_nanosoc_upy` has two builds from **one** wrapper (`rp_nanosoc_upy_wrapper.sv`),
differing only by two synth-time environment overrides. No RTL forks.

| build | `FPGA_BOOTROM_DIR` | `UPY_IMEM_IMG` | what it proves |
|---|---|---|---|
| **XiP / flash-boot** (SHIPPED DEFAULT) | locally-built **QSPI-enabled** bootrom | **empty** (`gen_empty_imem.py`) | boots MicroPython from **flash** — the product path |
| **scaffold** (no-flash variant) | shared QSPI-disabled bootrom | `micropython_word.hex` (baked) | boots MicroPython from **BRAM** — bring-up/CI convenience, not shipped |

The empty IMEM is the load-bearing part of the XiP build: with nothing baked into
BRAM, a running MicroPython can only have come from flash.

## Build it

```bash
# OOC-synth the XiP variant (builds the QSPI bootrom + empty IMEM, then synths).
make -C fpga/dfx rm-nanosoc-upy-dcp        # XiP = the default

# Fold it into the LOCKED static of the shipped shell, preserving static_id
# (0xD84A2E7A) so all 9 existing overlays stay valid:
make -C fpga/dfx add-rm-nanosoc-upy BUILD=fpga/dfx/build_qspi_kvm

# Package the overlay into the shipped tree (XiP IS the shipped nanosoc_upy now):
make -C fpga/dfx overlays BUILD=fpga/dfx/build_qspi_kvm
```

## Boot it on the board

1. Deploy the XiP overlay (empty IMEM).
2. Program `firmware/micropython/build/xip/flash_image.bin` at flash **`0x0`**
   with the M0 loader:
   `scripts/qspi_loader_bringup.py --stage=full --image ... --offset 0x0 --i-know`
   (`--i-know` is correct: D16 gives the DUT `0x0`-`0x50000`.)
3. Reset the DUT → MicroPython banner on UART2 (port 6930).

## Proven on silicon

2026-07-21, with a rigorous negative control (erase the whole `0x0`-`0x28000`
image region → boot goes silent). Full write-up, plus the known traps (the loader
won't enter on a *failed*-boot empty RM; UART2 RX drops characters at speed), is
in `docs/QSPI_RP_BOARD_BRINGUP.md` under "PRODUCT GATE PASSED".

⚠️ **HOT vs COLD:** the banner + `print(1+1)` proves the **HOT** set (copied into
IMEM). The **COLD** segment (`.text.xip`, execute-in-place from flash via the
CG092 cache) is a separate claim and is **not yet proven on silicon** — it needs
a call into a `.text.xip` function, ideally with its own negative control.
