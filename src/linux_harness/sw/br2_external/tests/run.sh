#!/bin/sh
#
# Host tests of the image's own scripts (IMAGE_CONTRACT §11) — seconds, no
# Buildroot, no board. Each gate is also driven with a broken input that MUST
# fail (a negative control), so the gate is known to be able to fail.
#
#   sh src/linux_harness/sw/br2_external/tests/run.sh
#
# Covers: mps3-keys-sync (merge, validation, perms), the post-build hook's
# release host-key gate, the kernel flash gate (ILA #24), the kernel clock +
# initramfs gates (B1 2026-09-24), the greybox clearing
# seam + gate, the inittab.d merge (placeholder, dedupe, missing exe), the
# /etc/mps3/version manifest, and the dropbear hardening tail.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BR2X=$(dirname "$HERE")
OVL=$BR2X/rootfs_overlay
PROV=$BR2X/board/mps3_provision.sh
IMG="${MPS3_PYTHON:-python3} $BR2X/board/mps3_image.py"
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
pass=0
fail=0
ok()  { pass=$((pass + 1)); echo "  PASS  $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL  $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

K1='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBakedBakedBakedBakedBakedBakedBakedBake baked@lab'
K2='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIClaimClaimClaimClaimClaimClaimClaimClai owner@pc'

echo "== mps3-keys-sync"
printf '%s\n' "$K1" > "$W/baked"
printf '# comment\n%s\nnot a key\nrm -rf /\n' "$K2" > "$W/claim"
out=$(MPS3_KEYS_BAKED=$W/baked MPS3_KEYS_CLAIM=$W/claim MPS3_KEYS_OUT=$W/r/.ssh/authorized_keys \
      sh "$OVL/usr/sbin/mps3-keys-sync")
AK=$W/r/.ssh/authorized_keys
check "baked + claimed keys merged"         "grep -qF '$K1' $AK && grep -qF '$K2' $AK"
check "invalid claim lines dropped (2)"     "! grep -q 'rm -rf' $AK && echo '$out' | grep -q 'dropped=2'"
check "dir 0700 / file 0600"                "[ \$(ls -ld $W/r/.ssh | cut -c1-10) = drwx------ ] && [ \$(ls -l $AK | cut -c1-10) = -rw------- ]"
rm -f "$W/claim"
MPS3_KEYS_BAKED=$W/baked MPS3_KEYS_CLAIM=$W/claim MPS3_KEYS_OUT=$AK sh "$OVL/usr/sbin/mps3-keys-sync" >/dev/null
check "unclaim leaves only the baked key"   "grep -qF '$K1' $AK && ! grep -qF '$K2' $AK"
: > "$W/claim2"
MPS3_KEYS_BAKED=$W/none MPS3_KEYS_CLAIM=$W/claim2 MPS3_KEYS_OUT=$AK sh "$OVL/usr/sbin/mps3-keys-sync" >/dev/null
check "no baked + empty claim -> no key"    "! grep -q '^ssh-' $AK"

mk_target() {   # a Buildroot-shaped fake TARGET_DIR
    T=$W/t$1; rm -rf "$T"; mkdir -p "$T/etc/init.d" "$T/etc/mps3" "$T/usr/sbin" "$T/usr/libexec/mps3" \
        "$T/usr/share/mps3/placeholder" "$T/root"
    cat > "$T/etc/inittab" <<'IT'
::sysinit:/etc/init.d/rcS
console::respawn:/sbin/getty -L  console 0 vt100 # GENERIC_SERIAL
::shutdown:/etc/init.d/rcK
IT
    printf '#!/bin/sh\nstart() {\n\tDROPBEAR_ARGS="$DROPBEAR_ARGS -R"\n}\n' > "$T/etc/init.d/S50dropbear"
    cp "$OVL/usr/share/mps3/placeholder/05-placeholder.inittab" "$T/usr/share/mps3/placeholder/"
    cp "$OVL/usr/libexec/mps3/harnessd-placeholder" "$T/usr/libexec/mps3/"
    echo 0x00000000 > "$T/etc/mps3/static_id"
    echo "$T"
}

echo "== host-key gate (release images carry none)"
T=$(mk_target 1)
check "release, no key -> pass"             "$IMG hostkey-gate $T release >/dev/null 2>&1"
mkdir -p "$T/etc/dropbear"; echo k > "$T/etc/dropbear/dropbear_ed25519_host_key"
check "release + baked key -> FAIL [negative control]" "! $IMG hostkey-gate $T release >/dev/null 2>&1"
check "lab + baked key -> pass"             "$IMG hostkey-gate $T lab >/dev/null 2>&1"
mkdir -p "$T/etc/ssh"; rm -rf "$T/etc/dropbear"; echo k > "$T/etc/ssh/ssh_host_rsa_key"
check "release + OpenSSH key -> FAIL [negative control]" "! $IMG hostkey-gate $T release >/dev/null 2>&1"

echo "== kernel flash gate (ILA #24: nothing may program the DUT's SST26)"
KC=$W/kconfig
cat > "$KC" <<'KC'
CONFIG_SPI=y
CONFIG_SPI_MASTER=y
CONFIG_MMC_SPI=y
# CONFIG_MTD is not set
# CONFIG_SPI_XILINX is not set
KC
check "a .config with MTD / SPI_XILINX unset -> pass" "$IMG kconfig-gate $KC >/dev/null 2>&1"
for sym in CONFIG_MTD CONFIG_MTD_SPI_NOR CONFIG_SPI_XILINX CONFIG_SPI_XILINX_QSPI; do
    { grep -v "^# $sym is not set" "$KC"; echo "$sym=y"; } > "$KC.$sym"
    check "$sym=y -> FAIL [negative control]" "! $IMG kconfig-gate $KC.$sym >/dev/null 2>&1"
done
{ cat "$KC"; echo "CONFIG_SPI_XILINX=m"; } > "$KC.m"
check "CONFIG_SPI_XILINX=m (a module is still a driver) -> FAIL [negative control]" \
      "! $IMG kconfig-gate $KC.m >/dev/null 2>&1"
{ cat "$KC"; echo "CONFIG_MTD_SPI_NOR_SWP_DISABLE_ON_VOLATILE=y"; } > "$KC.pfx"
check "a longer symbol sharing a forbidden prefix is not mistaken for it" \
      "$IMG kconfig-gate $KC.pfx >/dev/null 2>&1"
: > "$KC.empty"
check "a file with no CONFIG_ lines -> FAIL (no vacuous pass) [negative control]" \
      "! $IMG kconfig-gate $KC.empty >/dev/null 2>&1"
# The fragment must SAY "is not set": rv32's defconfig turns MTD + MTD_SPI_NOR on,
# so leaving them out of a fragment leaves them on (the 2026-09-23 image did).
CFG=$BR2X/../configs
frag_ok() { grep -qx '# CONFIG_MTD is not set' "$1" && grep -qx '# CONFIG_SPI_XILINX is not set' "$1"; }
check "kernel_fragment_harness.config unsets MTD and SPI_XILINX" "frag_ok $CFG/kernel_fragment_harness.config"
grep -vx '# CONFIG_MTD is not set' "$CFG/kernel_fragment_harness.config" > "$W/frag_nomtd"
check "the same fragment without its MTD line -> FAIL [negative control]" "! frag_ok $W/frag_nomtd"
set_in=$(grep -lE '^CONFIG_(MTD|MTD_SPI_NOR|SPI_XILINX|SPI_XILINX_QSPI)=[ym]' \
             "$CFG"/kernel_fragment_harness*.config "$BR2X/../../../linux_soc/linux/configs/kernel_fragment.config" 2>/dev/null)
check "no fragment of any variant sets a forbidden symbol${set_in:+ ($set_in)}" "[ -z '$set_in' ]"

echo "== kernel clock gate (B1 2026-09-24: MBV time == mcycle; riscv-pmu-sbi freezes it)"
{ cat "$KC"; echo "# CONFIG_RISCV_PMU_SBI is not set"; echo "CONFIG_RISCV_PMU=y"; echo "CONFIG_RISCV_PMU_LEGACY=y"; } > "$KC.clk"
check "RISCV_PMU_SBI unset (PMU core + legacy left on) -> pass" "$IMG kconfig-gate $KC.clk >/dev/null 2>&1"
for v in y m; do
    { grep -v '^# CONFIG_RISCV_PMU_SBI is not set' "$KC.clk"; echo "CONFIG_RISCV_PMU_SBI=$v"; } > "$KC.pmu$v"
    check "CONFIG_RISCV_PMU_SBI=$v -> FAIL [negative control]" "! $IMG kconfig-gate $KC.pmu$v >/dev/null 2>&1"
done
{ cat "$KC.clk"; echo "CONFIG_RISCV_PMU_SBI_FOO=y"; } > "$KC.pmupfx"
check "a longer symbol sharing the RISCV_PMU_SBI prefix is not mistaken for it" \
      "$IMG kconfig-gate $KC.pmupfx >/dev/null 2>&1"
echo "== initramfs gate (embedded => uncompressed: the WDOG budget)"
{ cat "$KC.clk"; echo 'CONFIG_INITRAMFS_SOURCE="${BR_BINARIES_DIR}/rootfs.cpio"'; } > "$KC.irfs"
{ cat "$KC.irfs"; echo "CONFIG_INITRAMFS_COMPRESSION_NONE=y"; } > "$KC.irfs_none"
check "embedded + INITRAMFS_COMPRESSION_NONE=y -> pass" "$IMG kconfig-gate $KC.irfs_none >/dev/null 2>&1"
{ cat "$KC.irfs"; echo "CONFIG_INITRAMFS_COMPRESSION_GZIP=y"; echo "# CONFIG_INITRAMFS_COMPRESSION_NONE is not set"; } > "$KC.irfs_gz"
check "embedded + gzip (the b1_v2 image) -> FAIL [negative control]" "! $IMG kconfig-gate $KC.irfs_gz >/dev/null 2>&1"
check "embedded + no compression line at all -> FAIL [negative control]" "! $IMG kconfig-gate $KC.irfs >/dev/null 2>&1"
{ cat "$KC.clk"; echo 'CONFIG_INITRAMFS_SOURCE=""'; echo "CONFIG_INITRAMFS_COMPRESSION_GZIP=y"; } > "$KC.noirfs"
check "no embedded initramfs (SOURCE=\"\") + gzip -> pass (nothing to inflate)" "$IMG kconfig-gate $KC.noirfs >/dev/null 2>&1"
# NONE only survives the merge if INITRAMFS_SOURCE is set in the same fragment: the
# compression choice depends on it and Buildroot sets it only AFTER the merge (the
# first b1_v3 build lost NONE that way and the post-build kconfig gate caught it).
clk_ok() { grep -qx '# CONFIG_RISCV_PMU_SBI is not set' "$1" && grep -qx 'CONFIG_INITRAMFS_COMPRESSION_NONE=y' "$1" \
           && grep -qE '^CONFIG_INITRAMFS_SOURCE=".+"$' "$1"; }
check "kernel_fragment_harness.config unsets RISCV_PMU_SBI + sets INITRAMFS_SOURCE and COMPRESSION_NONE" \
      "clk_ok $CFG/kernel_fragment_harness.config"
grep -vx '# CONFIG_RISCV_PMU_SBI is not set' "$CFG/kernel_fragment_harness.config" > "$W/frag_nopmu"
check "the same fragment without its RISCV_PMU_SBI line -> FAIL [negative control]" "! clk_ok $W/frag_nopmu"
grep -v '^CONFIG_INITRAMFS_SOURCE=' "$CFG/kernel_fragment_harness.config" > "$W/frag_nosrc"
check "the same fragment without its INITRAMFS_SOURCE line (NONE would be dropped) -> FAIL [negative control]" \
      "! clk_ok $W/frag_nosrc"
set_in=$(grep -lE '^CONFIG_RISCV_PMU_SBI=[ym]' \
             "$CFG"/kernel_fragment_harness*.config "$BR2X/../../../linux_soc/linux/configs/kernel_fragment.config" 2>/dev/null)
check "no fragment of any variant sets RISCV_PMU_SBI${set_in:+ ($set_in)}" "[ -z '$set_in' ]"

echo "== inittab.d merge"
T=$(mk_target 2)
$IMG inittab "$T" default >/dev/null
check "default, no harnessd drop-in -> placeholder respawn" "grep -q '^::respawn:/usr/libexec/mps3/harnessd-placeholder' $T/etc/inittab"
check "managed block sits after the getty line" "sed -n '2,3p' $T/etc/inittab | grep -q '^# BEGIN mps3 inittab.d'"
mkdir -p "$T/usr/share/mps3/inittab.d"
echo '::respawn:/usr/sbin/mps3-harnessd' > "$T/usr/share/mps3/inittab.d/50-mps3-harnessd.inittab"
touch "$T/usr/sbin/mps3-harnessd"
echo '::respawn:/usr/sbin/mps3-harnessd' >> "$T/etc/inittab"      # a package hook appended it too
$IMG inittab "$T" default >/dev/null
check "harnessd drop-in replaces the placeholder" "grep -q '^::respawn:/usr/sbin/mps3-harnessd$' $T/etc/inittab && ! grep -q harnessd-placeholder $T/etc/inittab"
check "exactly one harnessd line (hook duplicate removed)" "[ \$(grep -c '^::respawn:/usr/sbin/mps3-harnessd' $T/etc/inittab) = 1 ]"
$IMG inittab "$T" default >/dev/null
check "idempotent on a reused target"       "[ \$(grep -c 'BEGIN mps3 inittab.d' $T/etc/inittab) = 1 ]"
echo '::respawn:/usr/sbin/not-there' > "$T/usr/share/mps3/inittab.d/60-x.inittab"
check "drop-in naming a missing binary -> FAIL [negative control]" "! $IMG inittab $T default >/dev/null 2>&1"
rm -f "$T/usr/share/mps3/inittab.d/60-x.inittab"
T=$(mk_target 3)
$IMG inittab "$T" legacy >/dev/null
check "legacy variant -> no placeholder"    "! grep -q harnessd-placeholder $T/etc/inittab"

echo "== /etc/mps3/version"
T=$(mk_target 4)
$IMG inittab "$T" default >/dev/null
MPS3_HARNESS_VERSION=1.0.0 MPS3_HARNESS_SHA=abcd1234 SOURCE_DATE_EPOCH=1790000000 $IMG manifest "$T" >/dev/null
V=$T/etc/mps3/version
check "format/impl/harness/sha keys"        "grep -q '^format=1$' $V && grep -q '^impl=linux$' $V && grep -q '^harness=1.0.0$' $V && grep -q '^sha=abcd1234$' $V"
check "harnessd_sha256=placeholder"         "grep -q '^harnessd_sha256=placeholder$' $V"
check "every line key=value or comment"     "! grep -v '^#' $V | grep -qv '^[a-z0-9_]*=[!-~]*$'"
h1=$(sed -n 's/^rootfs_tree_sha256=//p' "$V")
MPS3_HARNESS_SHA=ffff $IMG manifest "$T" >/dev/null
h2=$(sed -n 's/^rootfs_tree_sha256=//p' "$V")
check "tree hash ignores the manifest itself" "[ '$h1' = '$h2' ]"
echo x >> "$T/etc/mps3/static_id"
$IMG manifest "$T" >/dev/null
h3=$(sed -n 's/^rootfs_tree_sha256=//p' "$V")
check "tree hash moves when a file moves [negative control]" "[ '$h1' != '$h3' ]"
echo bin > "$T/usr/sbin/mps3-harnessd"
$IMG manifest "$T" >/dev/null
check "harnessd_sha256 = the binary's sha"  "grep -q \"^harnessd_sha256=\$(sha256sum $T/usr/sbin/mps3-harnessd | cut -d' ' -f1)\$\" $V"

echo "== the whole post-build hook on a fake target"
T=$(mk_target 5)
( unset MPS3_STATIC_ID MPS3_AUTHORIZED_KEYS MPS3_AUTHORIZED_KEYS_FILE MPS3_WG_PRIVKEY
  MPS3_IMAGE_KIND=release sh "$PROV" "$T" ) > "$W/p5.log" 2>&1
rc=$?
check "release hook passes"                 "[ $rc = 0 ]"
check "-R stripped from S50dropbear"        "! grep -q 'DROPBEAR_ARGS -R' $T/etc/init.d/S50dropbear"
check "dropbear -s (key-only)"              "grep -q '^DROPBEAR_ARGS=.*-s' $T/etc/default/dropbear"
check "version written"                     "[ -s $T/etc/mps3/version ]"
T=$(mk_target 6)
printf '%s\n' "$K1" > "$W/keys"
( MPS3_IMAGE_KIND=release MPS3_AUTHORIZED_KEYS_FILE=$W/keys sh "$PROV" "$T" ) > "$W/p6.log" 2>&1
check "baked keys -> /usr/share/mps3/ssh/authorized_keys.baked" "grep -qF '$K1' $T/usr/share/mps3/ssh/authorized_keys.baked"
T=$(mk_target 7)
# a stray key the seams do not own (the host-key seam re-links a leftover
# /etc/dropbear dir, so plant it where only the gate can see it)
mkdir -p "$T/etc/ssh"; echo k > "$T/etc/ssh/ssh_host_ed25519_key"
( MPS3_IMAGE_KIND=release sh "$PROV" "$T" ) > "$W/p7.log" 2>&1
rc=$?
check "release hook with a baked host key -> FAIL [negative control]" "[ $rc != 0 ] && grep -q 'RELEASE image contains SSH host key' $W/p7.log"

echo "== greybox clearing seam + gate (a provisioned image carries ITS static's clearing)"
MINT=$W/mint/prod; mkdir -p "$MINT" "$W/mint2/prod" "$W/nosid"
head -c 66460 /dev/urandom > "$MINT/config_rm_greybox_pblock_rp_dut_partial_clear.bin"
GB=$MINT/config_rm_greybox_pblock_rp_dut_partial_clear.bin
echo 0x61BC6789 > "$MINT/static_id.txt"
cp "$GB" "$W/mint2/prod/clear.bin"; echo 0x0BADCAFE > "$W/mint2/prod/static_id.txt"
cp "$GB" "$W/nosid/clear.bin"
head -c 66461 /dev/urandom > "$W/mint2/prod/odd.bin"
gbhook() {   # gbhook TAG STATIC CLEAR [extra env] -> rc; log in $W/gb_TAG.log
    T=$(mk_target "gb$1")
    ( unset MPS3_AUTHORIZED_KEYS MPS3_AUTHORIZED_KEYS_FILE MPS3_WG_PRIVKEY MPS3_GREYBOX_CLEAR_STATIC_ID
      [ -n "$2" ] && export MPS3_STATIC_ID="$2" || unset MPS3_STATIC_ID
      [ -n "$3" ] && export MPS3_GREYBOX_CLEAR="$3" || unset MPS3_GREYBOX_CLEAR
      [ -n "${4:-}" ] && export "$4"
      MPS3_IMAGE_KIND=release sh "$PROV" "$T" ) > "$W/gb_$1.log" 2>&1
}
gbhook ok 0x61BC6789 "$GB"; rc=$?
check "provisioned + the mint's clearing (static_id.txt beside it) -> pass" "[ $rc = 0 ]"
check "…installed byte for byte, mode 0644" \
      "cmp -s $GB $T/etc/mps3/greybox_clear.bin && [ \$(stat -c %a $T/etc/mps3/greybox_clear.bin) = 644 ]"
check "…version records its sha256 and static" \
      "grep -qx \"greybox_clear_sha256=\$(sha256sum $GB | cut -d' ' -f1)\" $T/etc/mps3/version && grep -qx 'greybox_clear_static_id=0x61BC6789' $T/etc/mps3/version"
echo 0x00000000 > "$T/etc/mps3/static_id"      # Buildroot re-copies the overlay's default
( unset MPS3_STATIC_ID MPS3_GREYBOX_CLEAR MPS3_GREYBOX_CLEAR_STATIC_ID MPS3_AUTHORIZED_KEYS_FILE MPS3_WG_PRIVKEY
  MPS3_IMAGE_KIND=release sh "$PROV" "$T" ) > "$W/gb_reuse.log" 2>&1; rc=$?
check "reused target, unprovisioned rebuild: the old clearing is removed, gate passes" \
      "[ $rc = 0 ] && [ ! -e $T/etc/mps3/greybox_clear.bin ] && grep -qx 'greybox_clear_sha256=none' $T/etc/mps3/version"
gbhook none 0x61BC6789 ""; rc=$?
check "provisioned, NO clearing -> FAIL [negative control]" \
      "[ $rc != 0 ] && grep -q 'carries no /etc/mps3/greybox_clear.bin' $W/gb_none.log"
gbhook other 0x61BC6789 "$W/mint2/prod/clear.bin"; rc=$?
check "provisioned for 0x61BC6789, clearing from a 0x0BADCAFE mint -> FAIL [negative control]" \
      "[ $rc != 0 ] && grep -q 'recorded for 0x0BADCAFE' $W/gb_other.log"
gbhook envsid 0x61BC6789 "$GB" MPS3_GREYBOX_CLEAR_STATIC_ID=0x12345678; rc=$?
check "an explicit MPS3_GREYBOX_CLEAR_STATIC_ID that disagrees -> FAIL [negative control]" \
      "[ $rc != 0 ] && grep -q 'recorded for 0x12345678' $W/gb_envsid.log"
gbhook nosid 0x61BC6789 "$W/nosid/clear.bin"; rc=$?
check "a clearing with no static_id.txt and no MPS3_GREYBOX_CLEAR_STATIC_ID -> FAIL [negative control]" \
      "[ $rc != 0 ] && grep -q 'which static' $W/gb_nosid.log"
gbhook unprov "" "$GB"; rc=$?
check "UNPROVISIONED image carrying a clearing -> FAIL [negative control]" \
      "[ $rc != 0 ] && grep -q 'UNPROVISIONED' $W/gb_unprov.log"
gbhook odd 0x0BADCAFE "$W/mint2/prod/odd.bin"; rc=$?
check "a clearing that is not whole words -> FAIL [negative control]" \
      "[ $rc != 0 ] && grep -q 'whole words' $W/gb_odd.log"
gbhook ok2 0x61BC6789 "$GB" >/dev/null; printf 'x' | dd of="$T/etc/mps3/greybox_clear.bin" bs=1 seek=100 conv=notrunc 2>/dev/null
check "clearing changed after the manifest -> the gate FAILS [negative control]" \
      "! $IMG greybox-gate $T >/dev/null 2>&1"

echo "== L0's host-key / password seams (lab opt-in; release refuses)"
ssh-keygen -q -t ed25519 -N "" -C lab -f "$W/labkey"
T=$(mk_target 8)
( MPS3_IMAGE_KIND=lab MPS3_HOST_KEY_FILE=$W/labkey sh "$PROV" "$T" ) > "$W/p8.log" 2>&1
rc=$?
check "lab image bakes the key (hook passes)" "[ $rc = 0 ] && [ -d $T/etc/dropbear ] && [ ! -L $T/etc/dropbear ]"
fp_img=$(${MPS3_PYTHON:-python3} "$BR2X/board/mps3_hostkey.py" fingerprint "$T/etc/dropbear/dropbear_ed25519_host_key" 2>/dev/null)
fp_src=$(ssh-keygen -l -E sha256 -f "$W/labkey.pub" | awk '{print $2}')
check "baked key is dropbear format and the same key ($fp_src)" "[ -n '$fp_img' ] && [ '$fp_img' = '$fp_src' ] && ! grep -q 'OPENSSH' $T/etc/dropbear/dropbear_ed25519_host_key"
check "lab, no baked authorized_keys -> password SSH allowed (L0's B0 rule)" "! grep -q '^DROPBEAR_ARGS=.*-s' $T/etc/default/dropbear 2>/dev/null"
( MPS3_IMAGE_KIND=release sh "$PROV" "$T" ) > "$W/p8b.log" 2>&1
rc=$?
check "same target rebuilt as release: key dir reverted, gate passes" "[ $rc = 0 ] && [ -L $T/etc/dropbear ] && grep -q '^DROPBEAR_ARGS=.*-s' $T/etc/default/dropbear"
T=$(mk_target 9)
( MPS3_IMAGE_KIND=release MPS3_HOST_KEY_FILE=$W/labkey sh "$PROV" "$T" ) > "$W/p9.log" 2>&1
check "release + MPS3_HOST_KEY_FILE -> FAIL [negative control]" "[ $? != 0 ] && grep -q 'LAB images only' $W/p9.log"
T=$(mk_target 10)
( MPS3_IMAGE_KIND=release MPS3_SSH_PASSWORD_AUTH=1 sh "$PROV" "$T" ) > "$W/p10.log" 2>&1
check "release + password SSH -> FAIL [negative control]" "[ $? != 0 ]"
T=$(mk_target 11)
( MPS3_IMAGE_KIND=lab MPS3_SSH_PASSWORD_AUTH=0 sh "$PROV" "$T" ) > "$W/p11.log" 2>&1
check "lab key-only with no keys (lock-out) -> FAIL [negative control]" "[ $? != 0 ] && grep -q 'lock a LAB' $W/p11.log"
T=$(mk_target 12)
( MPS3_IMAGE_KIND=release MPS3_HOST_KEY_FILE=none sh "$PROV" "$T" ) > "$W/p12.log" 2>&1
check "release + MPS3_HOST_KEY_FILE=none -> pass (L0's opt-out spelling)" "[ $? = 0 ]"

echo "== mps3-wdkick: the bounded watchdog bridge until harnessd (HARNESSD_CONTRACT §5.3)"
HD=$BR2X/../harnessd
WK=$W/wk/host-uio/mps3-wdkick
if make -s -C "$HD" host-wdkick BUILD="$W/wk" > "$W/wk.log" 2>&1 && [ -x "$WK" ]; then
    # a fake sysfs + a plain file as /dev/uio0, driven through hal_uio.c's own seams
    wkcase() {   # wkcase TAG CSR0_HEX ADDR -> $W/wk_TAG/{sys,dev/uio0}
        d=$W/wk_$1; mkdir -p "$d/sys/uio0/maps/map0" "$d/dev"
        echo "$3" > "$d/sys/uio0/maps/map0/addr"; echo 0x10000 > "$d/sys/uio0/maps/map0/size"
        python3 -c "import struct,sys; open(sys.argv[1],'wb').write(struct.pack('<II', int(sys.argv[2],16), 0xA5A5A5A5) + bytes(65536-8))" "$d/dev/uio0" "$2"
    }
    word() { od -An -tx4 -j "$2" -N4 "$1" | tr -d ' '; }
    up=$(cut -d. -f1 /proc/uptime)
    wkcase arm 00000002 0x44b40000
    timeout 30 "$WK" --uio-sysfs "$W/wk_arm/sys" --uio-devdir "$W/wk_arm/dev" --period-ms 100 \
        --max-s $((up + 2)) --name wdk-nobody > "$W/wk_arm.log" 2>&1
    check "EWDT1 set: kicks (TWCSR0 = EWDT1|WDS = 0x6) and stops at --max-s" \
          "[ \$(word $W/wk_arm/dev/uio0 0) = 00000006 ] && grep -q 'bridging until harnessd (max' $W/wk_arm.log && grep -q 'max reached, stopping -- the watchdog will reset' $W/wk_arm.log && ! grep -q '(0 kick' $W/wk_arm.log"
    check "TWCSR1 (EWDT2, write-only) never written" "[ \$(word $W/wk_arm/dev/uio0 4) = a5a5a5a5 ]"
    wkcase off 00000000 0x44b40000
    timeout 30 "$WK" --uio-sysfs "$W/wk_off/sys" --uio-devdir "$W/wk_off/dev" --period-ms 100 \
        --max-s $((up + 2)) --name wdk-nobody > "$W/wk_off.log" 2>&1
    check "EWDT1 clear (stage0 did not arm it): never kicks, never enables [negative control]" \
          "[ \$(word $W/wk_off/dev/uio0 0) = 00000000 ] && [ \$(word $W/wk_off/dev/uio0 4) = a5a5a5a5 ] && grep -q '(0 kick' $W/wk_off.log"
    wkcase own 00000002 0x44b40000
    cp "$(command -v sleep)" "$W/wdk-owner"; "$W/wdk-owner" 30 & opid=$!
    t0=$(date +%s)
    timeout 30 "$WK" --uio-sysfs "$W/wk_own/sys" --uio-devdir "$W/wk_own/dev" --period-ms 100 \
        --max-s $((up + 60)) --name wdk-owner > "$W/wk_own.log" 2>&1
    t1=$(date +%s); kill $opid 2>/dev/null; wait $opid 2>/dev/null
    check "stops as soon as the owner runs (long before --max-s)" \
          "grep -q 'harnessd up at .* stopping' $W/wk_own.log && [ \$((t1 - t0)) -lt 10 ]"
    wkcase nowd 00000002 0x44a00000
    timeout 30 "$WK" --uio-sysfs "$W/wk_nowd/sys" --uio-devdir "$W/wk_nowd/dev" --period-ms 100 \
        --max-s $((up + 60)) --name wdk-nobody > "$W/wk_nowd.log" 2>&1; rc=$?
    check "no watchdog window (another block at 0x44A0): nothing to bridge, nothing written" \
          "[ $rc = 0 ] && grep -q 'nothing to bridge' $W/wk_nowd.log && [ \$(word $W/wk_nowd/dev/uio0 0) = 00000002 ]"
    check "the source never names TWCSR1's register or stage0's confirm word" \
          "! grep -Eq 'WDOG_TWCSR1|4B4F3053|1FE48|S0_CONFIRM|att_confirm' $HD/mps3_wdkick.c"
    check "S00mps3wdkick backgrounds it (never delays rcS) and is the first rcS script" \
          "grep -q 'mps3-wdkick .*&\$' $BR2X/package/mps3-harnessd/S00mps3wdkick && ! ls $BR2X/rootfs_overlay/etc/init.d/S00* >/dev/null 2>&1"
    check "the package installs the helper and its start script" \
          "grep -q 'usr/sbin/mps3-wdkick' $BR2X/package/mps3-harnessd/mps3-harnessd.mk && grep -q 'etc/init.d/S00mps3wdkick' $BR2X/package/mps3-harnessd/mps3-harnessd.mk"
else
    bad "mps3-wdkick host build failed (see $W/wk.log)"; cat "$W/wk.log"
fi

echo "== mps3-slot vs STAGE0's own tools (stage0_pack.py / stage0_mkcard.py)"
F0=$(cd "$BR2X/../../../linux_soc/hw/fw_stage0" 2>/dev/null && pwd)
if [ -n "$F0" ] && [ -f "$F0/stage0_mkcard.py" ] && command -v "${CC:-gcc}" >/dev/null 2>&1; then
    SL=$W/slot; mkdir -p "$SL"
    SRC=$BR2X/package/mps3-slot/src/mps3-slot.c
    HD=$(cd "$BR2X/../harnessd" && pwd)      # the ONE card layer, shared with mps3-harnessd
    CCF="-O2 -Wall -Wextra -Werror -std=c99 -I$HD -I$F0"
    # the real tool (built by its own package Makefile, as Buildroot does); the
    # tool over a caching mock device; the same with the read-back uncache
    # compiled out (the pre-fix behaviour, as a control). The card I/O -- and so
    # the mock seam -- is slot_card.c's; mps3-slot.c is the CLI over it.
    cp -r "$BR2X/package/mps3-slot/src" "$SL/pkg"
    make -s -C "$SL/pkg" CC="${CC:-gcc}" S0="$F0" HD="$HD" all && cp "$SL/pkg/mps3-slot" "$SL/mps3-slot"
    MOCKD="-DSLOT_PREAD=mock_pread -DSLOT_PWRITE=mock_pwrite -DSLOT_FSYNC=mock_fsync -DSLOT_FADVISE=mock_posix_fadvise -include $HERE/mock_blockdev.h"
    ${CC:-gcc} $CCF -c -o "$SL/core.o" "$F0/stage0_core.c"
    ${CC:-gcc} $CCF -c -o "$SL/cli.o" "$SRC"
    ${CC:-gcc} -O2 -Wall -Wextra -Werror -std=c99 -I"$HERE" -c -o "$SL/mockdev.o" "$HERE/mock_blockdev.c"
    ${CC:-gcc} $CCF $MOCKD -c -o "$SL/card_mock.o" "$HD/slot_card.c"
    ${CC:-gcc} $CCF $MOCKD -DMPS3_SLOT_NO_UNCACHE -c -o "$SL/card_buggy.o" "$HD/slot_card.c"
    ${CC:-gcc} -o "$SL/mps3-slot-mock" "$SL/cli.o" "$SL/card_mock.o" "$SL/core.o" "$SL/mockdev.o"
    ${CC:-gcc} -o "$SL/mps3-slot-buggy" "$SL/cli.o" "$SL/card_buggy.o" "$SL/core.o" "$SL/mockdev.o"
    check "mps3-slot.c is a thin CLI: no card I/O, boot-select rule or loader of its own" \
          "! grep -Eq 'pread|pwrite|fsync|posix_fadvise|s0_bootcfg_pick|s0_mbr_parse|s0_load|BLKFLSBUF' $SRC"
    head -c 8192 /dev/urandom > "$SL/pa"; head -c 12288 /dev/urandom > "$SL/pb"
    python3 "$F0/stage0_pack.py" --out "$SL/A.img" --pc 0x80000000 --a1 0 "$SL/pa@0x80000000" >/dev/null
    python3 "$F0/stage0_pack.py" --out "$SL/B.img" --pc 0x80000000 --a1 0 "$SL/pb@0x80000000" >/dev/null
    mkcard() {   # mkcard NAME -> a fresh 256 MiB sparse card: slot A = A.img, B empty,
                 # boot-select both copies seq 1 default A (a TIE: stage0 uses LBA1)
        python3 "$F0/stage0_mkcard.py" card --slot-a "$SL/A.img" --slot-b none --out-dir "$SL/o_$1" \
            --card-img "$SL/$1.img" --card-mib 256 >/dev/null
    }
    mkcard card
    MS="$SL/mps3-slot --disk $SL/card.img"
    check "status reads mkcard's card (A = S0LB, B empty, default A, tie -> LBA1 in use)" \
          "$MS status | grep -q '^slot A: .*S0LB' && $MS status | grep -q '^slot B: .*empty' && $MS status | grep -q 'default A (seq 1, in use: LBA1)'"
    check "write B (the non-default slot), read back off the card" "$MS write B $SL/B.img >/dev/null"
    check "write A while A is the default -> refused [negative control]" "! $MS write A $SL/A.img >/dev/null 2>&1"
    cp "$SL/B.img" "$SL/bad.img"   # flip the LAST byte: inside the payload whatever the padding
    printf 'X' | dd of="$SL/bad.img" bs=1 seek=$(( $(wc -c < "$SL/bad.img") - 1 )) conv=notrunc 2>/dev/null
    check "an image with a bad region CRC -> refused by stage0's loader [negative control]" "! $MS write B $SL/bad.img >/dev/null 2>&1"
    check "a non-S0LB file -> refused [negative control]" "! $MS write B $SL/pa >/dev/null 2>&1"

    echo "-- the tie: never overwrite the copy stage0 is using"
    dd if="$SL/card.img" bs=512 skip=1 count=1 2>/dev/null > "$SL/lba1.before"
    $MS default B >/dev/null
    dd if="$SL/card.img" bs=512 skip=1 count=1 2>/dev/null > "$SL/lba1.after"
    check "tie (both seq 1): default B writes LBA2, stage0's pick (LBA1) byte-identical" \
          "cmp -s $SL/lba1.before $SL/lba1.after && $MS status | grep -q 'default B (seq 2, in use: LBA2)'"
    check "stage0_mkcard.py check agrees: default B, seq 2" \
          "python3 $F0/stage0_mkcard.py check $SL/card.img | grep -q 'default slot B (boot-select seq 2)'"
    $MS default A >/dev/null
    check "default A: the LOWER-seq copy (LBA1) rewritten with seq 3, stage0 agrees" \
          "python3 $F0/stage0_mkcard.py check $SL/card.img | grep -q 'default slot A (boot-select seq 3)' && $MS status | grep -q 'LBA1 seq 3, LBA2 seq 2'"
    dd if=/dev/zero of="$SL/card.img" bs=512 seek=1 count=1 conv=notrunc 2>/dev/null   # tear the newest copy
    check "a torn newest copy falls back to the previous pick (default B, seq 2)" \
          "$MS status | grep -q 'default B (seq 2'"

    echo "-- read-backs come off the card, not the page cache (caching mock device)"
    mkcard m1; mkcard m2; mkcard m3
    MM="$SL/mps3-slot-mock --disk"
    check "mock device, card keeps writes: write B + default B pass" \
          "$MM $SL/m1.img write B $SL/B.img >/dev/null && $MM $SL/m1.img default B >/dev/null"
    check "mock device that LOSES writes: write B -> FAIL (read-back off the card)" \
          "! MOCK_DROP_WRITES=1 $MM $SL/m2.img write B $SL/B.img >/dev/null 2>&1"
    check "mock device that LOSES writes: default B -> FAIL" \
          "! MOCK_DROP_WRITES=1 $MM $SL/m2.img default B >/dev/null 2>&1"
    check "the same with the uncache compiled out PASSES — the cached read-back lies [negative control]" \
          "MOCK_DROP_WRITES=1 $SL/mps3-slot-buggy --disk $SL/m3.img write B $SL/B.img >/dev/null 2>&1 && MOCK_DROP_WRITES=1 $SL/mps3-slot-buggy --disk $SL/m3.img default B >/dev/null 2>&1"
    check "…and the card it 'verified' really has no image in slot B" \
          "$SL/mps3-slot --disk $SL/m3.img status | grep -q '^slot B: .*empty'"
else
    bad "mps3-slot tests need src/linux_soc/hw/fw_stage0 and a host C compiler"
fi

echo "== the on-board GDB server: patches/openocd pins, the image gate, mps3-debug"
OCP=$BR2X/../patches/openocd
REPO_ROOT=$(cd "$BR2X/../../../.." && pwd)
pin() { sed -n "s/^$1=//p" "$OCP/PINS"; }
# the file a new-file patch creates, re-derived from its + lines
newfile_sha() { awk 'f && /^[+]/ {print substr($0, 2)} /^[+][+][+] b[/]/ {f=1}' "$1" | sha256sum | cut -d' ' -f1; }
check "0103 creates ahb_qspi.c at its PINS sha256" \
      "[ \"\$(newfile_sha $OCP/0103-add-ahb_qspi-driver.patch)\" = \"\$(pin ahb_qspi_sha256)\" ]"
check "0104 creates hostio4.c at its PINS sha256" \
      "[ \"\$(newfile_sha $OCP/0104-add-hostio4-driver.patch)\" = \"\$(pin hostio4_sha256)\" ]"
sed '0,/^+.*ahb_qspi/s//+ edited by hand/' "$OCP/0103-add-ahb_qspi-driver.patch" > "$W/0103.edited"
check "a hand-edited driver patch misses its pin [negative control]" \
      "[ \"\$(newfile_sha $W/0103.edited)\" != \"\$(pin ahb_qspi_sha256)\" ]"
check "PINS carries the image's VERSION line" "[ -n \"\$(pin version_line)\" ]"

GATE="${MPS3_PYTHON:-python3} $BR2X/board/mps3_openocd_gate.py"
mk_ocd_target() {   # a TARGET_DIR with the package's real designs.conf + host/openocd cfgs
    T=$W/ocd$1; rm -rf "$T"; mkdir -p "$T/usr/bin" "$T/usr/share/mps3/openocd" "$T/etc"
    printf '\177ELF fake openocd: remote_bitbang ahb_qspi hostio4\n' > "$T/usr/bin/openocd"
    printf '\177ELF fake mps3-debug\n' > "$T/usr/bin/mps3-debug"
    cp "$REPO_ROOT"/host/openocd/*.cfg "$REPO_ROOT"/host/openocd/*.tcl "$T/usr/share/mps3/openocd/"
    cp "$BR2X/package/mps3-debug/designs.conf" "$T/usr/share/mps3/openocd/"
    pin version_line > "$T/usr/share/mps3/openocd/VERSION"
    printf 'root:x:0:0:root:/root:/bin/sh\nopenocd:x:1001:1001:OpenOCD:/:/bin/false\n' > "$T/etc/passwd"
    echo "$T"
}
printf '#!/bin/sh\necho "   0:\t00000013 \tnop"\n' > "$W/objdump.ok"; chmod +x "$W/objdump.ok"
printf '#!/bin/sh\necho "  10:\t00b57553 \tfadd.s\tfa0,fa0,fa1"\n' > "$W/objdump.fd"; chmod +x "$W/objdump.fd"
T=$(mk_ocd_target 1)
check "openocd gate: the real designs.conf + host/openocd cfgs pass" "$GATE $T $W/objdump.ok >/dev/null"
T=$(mk_ocd_target 2); printf '\177ELF remote_bitbang ahb_qspi\n' > "$T/usr/bin/openocd"
check "openocd gate: no hostio4 driver -> FAIL [negative control]" "! $GATE $T $W/objdump.ok >/dev/null"
T=$(mk_ocd_target 3); printf '\177ELF ahb_qspi hostio4\n' > "$T/usr/bin/openocd"
check "openocd gate: no remote_bitbang -> FAIL [negative control]" "! $GATE $T $W/objdump.ok >/dev/null"
T=$(mk_ocd_target 4)
check "openocd gate: an F/D instruction -> FAIL [negative control]" "! $GATE $T $W/objdump.fd >/dev/null"
check "openocd gate: over the size budget -> FAIL [negative control]" \
      "! $GATE $T $W/objdump.ok --max-bytes=16 >/dev/null"
T=$(mk_ocd_target 5); sed -i 's/^openocd:x:1001:/openocd:x:0:/' "$T/etc/passwd"
check "openocd gate: an openocd user with uid 0 -> FAIL [negative control]" "! $GATE $T $W/objdump.ok >/dev/null"
T=$(mk_ocd_target 6); rm "$T/usr/share/mps3/openocd/nanosoc_iice_chain.cfg"
check "openocd gate: a recipe's cfg not installed -> FAIL [negative control]" "! $GATE $T $W/objdump.ok >/dev/null"
T=$(mk_ocd_target 7); sed -i '/^openocd:/d' "$T/etc/passwd"
check "openocd gate: no openocd user -> FAIL [negative control]" "! $GATE $T $W/objdump.ok >/dev/null"
# Buildroot's output/target never has a <PKG>_USERS user (mkusers runs in the fs step):
# the gate reads the rootfs's passwd through --passwd= (build.sh: the rootfs.cpio's)
mk_ocd_target 8 >/dev/null; cp "$W/ocd8/etc/passwd" "$W/rootfs_passwd"; sed -i '/^openocd:/d' "$W/ocd8/etc/passwd"
check "openocd gate: the user from --passwd= (the rootfs's), not output/target's" \
      "$GATE $W/ocd8 $W/objdump.ok --passwd=$W/rootfs_passwd >/dev/null && ! $GATE $W/ocd8 $W/objdump.ok >/dev/null"

if command -v "${CC:-gcc}" >/dev/null 2>&1 && ${MPS3_PYTHON:-python3} -c 'import pytest' 2>/dev/null; then
    check "mps3-debug: the launcher's host tests (package/mps3-debug/tests)" \
          "${MPS3_PYTHON:-python3} -m pytest -q -p no:cacheprovider $BR2X/package/mps3-debug/tests > $W/mps3_debug_pytest.log 2>&1 || { tail -30 $W/mps3_debug_pytest.log; false; }"
else
    bad "mps3-debug host tests need a host C compiler and pytest"
fi

echo "== $pass PASS, $fail FAIL"
[ $fail = 0 ]
