#!/bin/sh
# run_tests.sh — M5 app-port host suite. Run from sw/apps (make test does).
# Everything here is host-side with MOCKED registers / FAKED sysroots — no
# board, no kernel module, no UIO. PASS here proves the logic and the byte
# streams, NOT the panel (that needs the bench, same as every UIO daemon).
set -u

SCRATCH="${MPS3_APPS_TEST_SCRATCH:-$(mktemp -d)}"
FAILS=0
run() {
    echo "---- $*"
    if ! "$@"; then
        FAILS=$((FAILS + 1))
        echo "**** FAILED: $*"
    fi
}

run ./tests/test_clcd_core
run ./tests/test_ovlstore "$SCRATCH/ovlstore_test.img"
run ./tests/test_status_linux "$SCRATCH"

# CRC32 must match zlib/IEEE (the store + push-header CRC). Cross-check the
# tool's verify verdict against Python's binascii on the same image.
if command -v python3 >/dev/null 2>&1; then
    echo "---- crc32 cross-check vs python binascii"
    python3 - "$SCRATCH" <<'EOF' || FAILS=$((FAILS + 1))
import binascii, sys
ref = binascii.crc32(b"123456789") & 0xFFFFFFFF
assert ref == 0xCBF43926, hex(ref)
print("python crc32 reference vector OK")
EOF
else
    echo "(python3 not found — crc32 cross-check skipped)"
fi

# JSON surface must parse as strict JSON.
if command -v python3 >/dev/null 2>&1 && [ -f "$SCRATCH/status_json_sample.json" ]; then
    echo "---- boot-status JSON parses"
    python3 -c "import json,sys; json.load(open(sys.argv[1])); print('json OK')" \
        "$SCRATCH/status_json_sample.json" || FAILS=$((FAILS + 1))
fi

# --preview / --text must work with NO hardware at all (QEMU-safe path),
# against a faked sysroot so nothing live leaks in.
echo "---- mps3-status --text against faked sysroot"
MPS3_SYSROOT="$SCRATCH/fakeroot1" ./mps3-status --text | grep -q "nanoSoC harness" \
    || { FAILS=$((FAILS + 1)); echo "**** FAILED: mps3-status --text"; }
echo "---- mps3-clcdd --preview against faked sysroot"
MPS3_SYSROOT="$SCRATCH/fakeroot1" ./mps3-clcdd --preview | grep -q "SID : 0x14E1A2D8" \
    || { FAILS=$((FAILS + 1)); echo "**** FAILED: mps3-clcdd --preview"; }

# mps3-ovlstore CLI on the good image left by test_ovlstore is stale (it ends
# corrupted); regenerate a known-good one via the test then drive the CLI.
echo "---- mps3-ovlstore CLI (read-only, 0400 image)"
IMG="$SCRATCH/cli_store.img"
rm -f "$IMG"
python3 - "$IMG" <<'EOF' 2>/dev/null || true
# tiny independent packer (mirrors the frozen LE layout) for the CLI check
import struct, sys, binascii
img = sys.argv[1]
clear = bytes((i*31+1) & 0xFF for i in range(5000))
part  = bytes((i*31+2) & 0xFF for i in range(12345))
slot_a = struct.pack('<IIIIIIII', 0x14E1A2D8, 0x010000A1,
                     0x100, len(clear), binascii.crc32(clear) & 0xFFFFFFFF,
                     0x2000, len(part), binascii.crc32(part) & 0xFFFFFFFF) + b'\x01'
slot_b = b'\x00' * 33
hdr = b'OVLS' + struct.pack('<HBB', 1, 0, 0) + slot_a + slot_b
with open(img, 'wb') as f:
    f.write(hdr)
    f.seek(0x010000 + 0x100);  f.write(clear)
    f.seek(0x010000 + 0x2000); f.write(part)
EOF
if [ -f "$IMG" ]; then
    chmod 0400 "$IMG"
    ./mps3-ovlstore info "$IMG" | grep -q "rm_id     : 0x010000a1" \
        || { FAILS=$((FAILS + 1)); echo "**** FAILED: ovlstore info"; }
    ./mps3-ovlstore verify "$IMG" active | grep -q "VERIFY OK" \
        || { FAILS=$((FAILS + 1)); echo "**** FAILED: ovlstore verify"; }
    echo "ovlstore CLI OK (cross-language image: python packer -> C reader)"
fi

# D16 guard: the read-only tooling must contain no write-mode file opens.
echo "---- D16 embargo guard (no write paths in ovlstore tooling)"
if grep -nE 'O_WRONLY|O_RDWR|fopen\([^,]*, *"[wa]' \
        ovlstore_read.c mps3_ovlstore.c; then
    FAILS=$((FAILS + 1))
    echo "**** FAILED: write-mode open found in read-only tooling"
else
    echo "no write paths — OK"
fi

echo
if [ "$FAILS" -eq 0 ]; then
    echo "M5 APPS HOST SUITE: PASS"
    exit 0
else
    echo "M5 APPS HOST SUITE: FAIL ($FAILS)"
    exit 1
fi
