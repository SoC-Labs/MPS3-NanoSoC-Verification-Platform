#!/usr/bin/env bash
# Build the B2 stimulus image for the nanosoc RM.
#
# The linker MEMORY block is NOT the stock CMSDK one. cmsdk_cm0.ld puts RAM at
# 0x3000_0000; nanoSoC's DMEM_0 is at 0x1800_0000 (nanosoc_memmap.h). An image
# built with the stock script gets an initial stack pointer of 0x3000_3C00,
# pointing at unmapped space -- it would fault on the first push, on the board,
# after a power-cycle and a board session had been spent getting there. The
# generated nanosoc_cmsdk_cm0_memory.ld is authoritative; the shipped
# hello_image.hex's first word (0x1800FC00) agrees with it exactly, which is how
# this was confirmed rather than assumed.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../../../.." && pwd)
# Site paths: the environment first, else the same key in the repo-root
# tools.env (see tools.env.example). No personal defaults.
_tools_env() { sed -n "s/^[[:space:]]*$1[[:space:]]*[:?]\{0,1\}=[[:space:]]*//p" "$REPO/tools.env" 2>/dev/null | tail -n 1; }
TOOLS=${TOOLS:-${ARM_GCC_BIN:-$(_tools_env ARM_GCC_BIN)}}      # empty = arm-none-eabi-* on PATH
TECH=${SOCLABS_NANOSOC_TECH_DIR:-$(_tools_env SOCLABS_NANOSOC_TECH_DIR)}
SOC=${NANOSOC_M0_SOC:-${SOCLABS_NANOSOC_SOC_DIR:-$(_tools_env SOCLABS_NANOSOC_SOC_DIR)}}
[ -n "$TECH" ] || { echo "set SOCLABS_NANOSOC_TECH_DIR (the nanosoc_tech checkout) in tools.env or the environment" >&2; exit 1; }
[ -n "$SOC" ]  || { echo "set SOCLABS_NANOSOC_SOC_DIR (or NANOSOC_M0_SOC) in tools.env or the environment" >&2; exit 1; }
TC=${TOOLS:+$TOOLS/}
OUT=${OUT:-$HERE/build}

S=$TECH/software
MEMLD=$SOC/build_soc/firmware/nanosoc_cmsdk_cm0_memory.ld
SECLD=$S/common/scripts/sections.ld

for f in "$MEMLD" "$SECLD"; do
  [ -f "$f" ] || { echo "missing linker input: $f" >&2; exit 1; }
done

mkdir -p "$OUT"

# One script = the generated MEMORY block + the shared sections. Composed here
# rather than edited into either, so a regenerated memory map is picked up.
{ cat "$MEMLD"; echo; echo "INCLUDE \"$SECLD\""; } > "$OUT/nanosoc_link.ld"

"${TC}arm-none-eabi-gcc" -g -O2 -mthumb -mcpu=cortex-m0 \
  --specs=nano.specs -Wl,--gc-sections \
  "$S/cmsis/Device/ARM/CMSDK_CM0/Source/GCC/startup_CMSDK_CM0.s" \
  "$HERE/socscope_exp.c" \
  "$S/common/retarget/retarget.c" \
  "$S/common/retarget/uart_stdout.c" \
  "$S/cmsis/Device/ARM/CMSDK_CM0/Source/system_CMSDK_CM0.c" \
  -I "$S/cmsis/Device/ARM/CMSDK_CM0/Include" \
  -I "$S/cmsis/CMSIS/Include" \
  -I "$S/common/retarget" \
  -D__STACK_SIZE=0x200 -D__HEAP_SIZE=0x1000 -DCORTEX_M0 \
  -T "$OUT/nanosoc_link.ld" -o "$OUT/socscope_exp.elf"
# No -lnosys and no stdio: socscope_exp.c writes the UART through UartPutc
# directly. printf would pull newlib stdio -- and _fstat/_isatty/_lseek/_read with
# it -- into an image that must fit a 16 KB IMEM.

"${TC}arm-none-eabi-objcopy" -O binary "$OUT/socscope_exp.elf" "$OUT/socscope_exp.bin"
"${TC}arm-none-eabi-objdump" -d "$OUT/socscope_exp.elf" > "$OUT/socscope_exp.lst"

# $readmemh format: one 32-bit WORD per line, little-endian, after an @ origin.
# objcopy -O verilog emits BYTES, which the RM's loader would read as one word
# each. Converted explicitly so the format is stated rather than depended upon.
python3 - "$OUT/socscope_exp.bin" "$OUT/socscope_exp.hex" <<'PY'
import struct, sys
raw = open(sys.argv[1], "rb").read()
raw += b"\0" * ((-len(raw)) % 4)
with open(sys.argv[2], "w") as fh:
    fh.write("@00000000\n")
    for i in range(0, len(raw), 4):
        fh.write("%08X\n" % struct.unpack_from("<I", raw, i)[0])
PY

# The committed artefact sits BESIDE the source, not under build/: a repo-wide
# .gitignore excludes build/ directories, and hello_image.hex sets the precedent of
# a checked-in image next to the RM that bakes it. build/ keeps the intermediates.
cp -f "$OUT/socscope_exp.hex" "$HERE/socscope_exp.hex"

echo "built: $HERE/socscope_exp.hex"
head -1 "$HERE/socscope_exp.hex"
sed -n '2p' "$HERE/socscope_exp.hex" | sed 's/^/  initial SP: 0x/'
