# patches/openocd — the image's OpenOCD (the on-board GDB server)

Buildroot's own `openocd` package (0.12.0, the release tarball — the same revision
as the SoC Labs recipe's pin, the v0.12.0 tag `9ea7f3d`) plus these, applied after
Buildroot's `0001`–`0003` through `BR2_GLOBAL_PATCH_DIR`:

| Patch | What | From |
|---|---|---|
| `0101-register-ahb_qspi-v0.12.0.patch` | registers the ahb_qspi NOR flash driver | soclabs-openocd `39eba58`, verbatim |
| `0102-register-hostio4-v0.12.0.patch` | registers the hostio4 adapter (configure.ac, Makefile.am, interfaces.c) | soclabs-openocd `39eba58`, verbatim |
| `0103-add-ahb_qspi-driver.patch` | `src/flash/nor/ahb_qspi.c` as a new file | ahb_qspi `c97deb22` `sw/openocd/ahb_qspi.c` |
| `0104-add-hostio4-driver.patch` | `src/jtag/drivers/hostio4.c` as a new file | NanoSoC-Ethernet-Chiplet `057d317c` |

The recipe symlinks the two driver sources in from their owning repos; a Buildroot
build cannot reach sibling repos, so they are carried as new-file patches and
**sha-pinned** in `PINS` (`br2_external/tests/run.sh` checks that each patch still
creates a file with the pinned sha256, and fails otherwise). To bump a driver:
regenerate its patch from the owning repo's commit, update `PINS`, run the check.

`remote_bitbang` is OFF by default in 0.12.0's configure and Buildroot never passes it;
the `mps3-debug` package appends `--enable-remote-bitbang` to `OPENOCD_CONF_OPTS`
(package/mps3-debug/mps3-debug.mk). Every USB adapter stays off (Buildroot's
per-adapter options, all unset; no libusb in the image).

Verified 2026-09-30: all four apply with no fuzz on the 0.12.0 tarball after
Buildroot 2026.02.3's three, and the two driver files come out at the pinned sha256.
