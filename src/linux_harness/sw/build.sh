#!/bin/bash
# Build the MPS3 Linux harness image — IMAGE_CONTRACT.md §1 is the spec.
#
# ONE Buildroot configuration, ONE kernel: slim, nowfi, with the rootfs
# EMBEDDED as an initramfs. That single Image boots QEMU (-kernel), the 3-region
# xsdb recipe, and — wrapped with the DTB into an OpenSBI FW_PAYLOAD — the
# 1-region blob stage0 loads from the user microSD or a rescue push. Nothing is
# rebuilt with a toggled config between artefacts, which is what bit July twice:
# mk_1region.sh wiped target/usr/lib/modules and never reinstalled them (the
# July blob's initramfs has NO /lib/modules at all), and its re-applied
# fragments dropped NOWFI (the blob carried the wfi kernel).
#
#   ./build.sh                          release image  -> artifacts/
#   MPS3_BUILD_STEP=config ./build.sh   configure + checks only (seconds)
#
# Knobs (IMAGE_CONTRACT §1/§2): MPS3_IMAGE_KIND=release|lab, MPS3_VARIANT=
# default|legacy, MPS3_IDLE=nowfi|wfi, MPS3_ARTIFACTS, MPS3_BR_SRC, BR2_DL_DIR,
# MPS3_DL_SEED, JOBS, MPS3_STAGE0_ELF|MPS3_STAGE0_SHA256, MPS3_BUILD_FORCE,
# plus the provisioning seams (MPS3_STATIC_ID, MPS3_GREYBOX_CLEAR[_STATIC_ID],
# MPS3_ROOT_PASSWD, MPS3_AUTHORIZED_KEYS[_FILE], MPS3_WG_*).
#
# Everything this writes is under sw/build/ (Buildroot tree, dl cache, toolchain
# wrapper) and $MPS3_ARTIFACTS. It never writes another worktree: a seed cache
# is hard-linked (tarballs) or copied (git mirrors), never shared in place.
set -euo pipefail
cd "$(dirname "$0")"
SW=$PWD
REPO=$(cd ../../.. && pwd)
LINUX_SOC=$SW/../../linux_soc/linux
FW_STAGE0=$SW/../../linux_soc/hw/fw_stage0
BUILD=$SW/build
BR=$BUILD/buildroot
BR_TAG=2026.02.3
BRE="BR2_EXTERNAL=$SW/br2_external"
ART=${MPS3_ARTIFACTS:-$SW/artifacts}
JOBS=${JOBS:-4}
KIND=${MPS3_IMAGE_KIND:-release}
VARIANT=${MPS3_VARIANT:-default}
IDLE=${MPS3_IDLE:-nowfi}
STEP=${MPS3_BUILD_STEP:-all}
NICE="nice -n ${MPS3_NICE:-19}"
DTB_SLOT=$((0x82200000))
KERNEL_BASE=$((0x80400000))
BLOB_MAX=$((64 << 20))              # STAGE0_CONTRACT §5: S0_IMAGE_MAX = slot size

die() { echo "FATAL: $*" >&2; exit 1; }
case "$KIND" in release|lab) ;; *) die "MPS3_IMAGE_KIND=$KIND (release|lab)" ;; esac
case "$VARIANT" in default|legacy) ;; *) die "MPS3_VARIANT=$VARIANT (default|legacy)" ;; esac
case "$IDLE" in nowfi|wfi) ;; *) die "MPS3_IDLE=$IDLE (nowfi|wfi)" ;; esac
case "$STEP" in all|config) ;; *) die "MPS3_BUILD_STEP=$STEP (all|config)" ;; esac

# Buildroot hard-refuses '.'/empty entries in LD_LIBRARY_PATH (the lab env ships one).
export LD_LIBRARY_PATH="$(printf '%s' "${LD_LIBRARY_PATH:-}" | tr ':' '\n' | grep -vE '^\.?$' | paste -sd: - || true)"

echo "== 0/9  preflight ($KIND image, $VARIANT variant, $IDLE idle, step=$STEP)"
# One Buildroot build at a time on this host (LINUX_HARNESS_PLAN §4). A config
# step is seconds of kconfig and is allowed alongside.
if [ "$STEP" = all ] && [ "${MPS3_BUILD_FORCE:-0}" != 1 ]; then
    # processes NAMED make (-x), not any shell whose command line mentions it
    other=$(pgrep -ax make | grep -i 'buildroot' | grep -v "$BR" || true)
    [ -z "$other" ] || die "another Buildroot build is running (MPS3_BUILD_FORCE=1 to override):
$other"
fi
if [ "$KIND" = release ] && [ -n "${MPS3_HOST_KEY_FILE:-}" ] && [ "$MPS3_HOST_KEY_FILE" != none ]; then
    die "MPS3_HOST_KEY_FILE is for LAB images only (MPS3_IMAGE_KIND=lab): a release image
       must not carry a host key — every board would share it (IMAGE_CONTRACT §4.1)"
fi
mkdir -p "$BUILD" "$ART"

echo "== 1/9  device tree (generated) + gates"
python3 "$REPO/tools/gen_dts.py" --check >/dev/null \
    || die "src/linux_harness/shell_linux.dts is stale: python3 tools/gen_dts.py"
python3 "$REPO/tools/dts_gates.py" > "$BUILD/dts_gates.log" 2>&1 \
    || { cat "$BUILD/dts_gates.log"; die "tools/dts_gates.py failed"; }
tail -1 "$BUILD/dts_gates.log" | sed 's/^/   /'
dtc -I dts -O dtb -p 4096 -o "$ART/shell_linux.dtb" "$SW/../shell_linux.dts"

echo "== 2/9  toolchain wrapper ($BUILD/toolchain_wrapper)"
# mk_toolchain_wrapper.sh writes <its own dir>/toolchain_wrapper: run a copy
# that lives here, so the tracked linux_soc tree is never written.
if [ ! -x "$BUILD/toolchain_wrapper/bin/riscv32-amd-linux-gnu-gcc" ]; then
    cp "$LINUX_SOC/mk_toolchain_wrapper.sh" "$BUILD/mk_toolchain_wrapper.sh"
    bash "$BUILD/mk_toolchain_wrapper.sh"
fi

echo "== 3/9  Buildroot $BR_TAG ($BR)"
if [ ! -d "$BR/.git" ]; then
    src=${MPS3_BR_SRC:-}
    if [ -z "$src" ]; then
        if [ -d "$LINUX_SOC/buildroot/.git" ]; then src=$LINUX_SOC/buildroot
        else src=https://gitlab.com/buildroot.org/buildroot.git; fi
    fi
    git clone --quiet --no-hardlinks "$src" "$BR"
fi
git -C "$BR" -c advice.detachedHead=false checkout --quiet "$BR_TAG"

export BR2_DL_DIR=${BR2_DL_DIR:-$BUILD/dl}
mkdir -p "$BR2_DL_DIR"
if [ -n "${MPS3_DL_SEED:-}" ] && [ -z "$(ls -A "$BR2_DL_DIR")" ]; then
    echo "   seeding $BR2_DL_DIR from $MPS3_DL_SEED (hard links; git mirrors copied)"
    ( cd "$MPS3_DL_SEED" && find . -type d -name git -prune -o -type f -print ) | while read -r f; do
        mkdir -p "$BR2_DL_DIR/$(dirname "$f")"
        ln "$MPS3_DL_SEED/$f" "$BR2_DL_DIR/$f" 2>/dev/null || cp -p "$MPS3_DL_SEED/$f" "$BR2_DL_DIR/$f"
    done
    ( cd "$MPS3_DL_SEED" && find . -type d -name git -prune -print ) | while read -r d; do
        mkdir -p "$BR2_DL_DIR/$(dirname "$d")"
        cp -a "$MPS3_DL_SEED/$d" "$BR2_DL_DIR/$d"
    done
fi

echo "== 4/9  configure"
cp "$SW/configs/mbv_harness_defconfig" "$BR/configs/mbv_harness_defconfig"
make -s -C "$BR" $BRE mbv_harness_defconfig >/dev/null

# Root password: serial console only (dropbear -s). Never a committed secret.
if [ -z "${MPS3_ROOT_PASSWD:-}" ]; then
    MPS3_ROOT_PASSWD=$(python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(16)))')
    ( umask 077; printf '%s\n' "$MPS3_ROOT_PASSWD" > "$ART/ROOT_PASSWD" )
    echo "   root password: GENERATED -> $ART/ROOT_PASSWD (serial console only)"
else
    rm -f "$ART/ROOT_PASSWD"
    echo "   root password: from \$MPS3_ROOT_PASSWD (serial console only)"
fi
FRAGS=""
[ "$IDLE" = nowfi ] && FRAGS="$FRAGS \$(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../configs/kernel_fragment_harness_nowfi.config"
[ "$VARIANT" = legacy ] && FRAGS="$FRAGS \$(BR2_EXTERNAL_MPS3_HARNESS_PATH)/../configs/kernel_fragment_harness_legacy.config"
MPS3_ROOT_PASSWD="$MPS3_ROOT_PASSWD" JOBS="$JOBS" FRAGS="$FRAGS" VARIANT="$VARIANT" \
python3 - "$BR/.config" <<'PY'
import os, sys
cfg = sys.argv[1]
pw = os.environ["MPS3_ROOT_PASSWD"].replace("\\", "\\\\").replace('"', '\\"')
want = {
    "BR2_TARGET_GENERIC_ROOT_PASSWD": '"%s"' % pw,
    "BR2_JLEVEL": str(int(os.environ["JOBS"])),
}
legacy = os.environ["VARIANT"] == "legacy"
for k, on in (("BR2_PACKAGE_MPS3_HARNESSD", not legacy),
              ("BR2_PACKAGE_MPS3_LEGACY_DAEMONS", legacy),
              ("BR2_PACKAGE_MPS3_DFX", legacy),
              ("BR2_PACKAGE_MPS3_APPS", legacy)):
    want[k] = "y" if on else None
lines, seen = [], set()
for l in open(cfg).read().splitlines():
    key = l.split("=", 1)[0] if not l.startswith("# ") else l[2:].split(" ", 1)[0]
    if key == "BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES" and "=" in l:
        l = l[:-1] + os.environ["FRAGS"] + '"'
    if key in want:
        seen.add(key)
        v = want[key]
        l = ("%s=%s" % (key, v)) if v is not None else ("# %s is not set" % key)
    lines.append(l)
lines += [("%s=%s" % (k, v)) if v is not None else ("# %s is not set" % k)
          for k, v in want.items() if k not in seen]
open(cfg, "w").write("\n".join(lines) + "\n")
PY
make -s -C "$BR" $BRE olddefconfig >/dev/null
for k in BR2_TARGET_ROOTFS_INITRAMFS BR2_PACKAGE_MPS3_SPI_USD BR2_PACKAGE_MPS3_SLOT BR2_PACKAGE_E2FSPROGS; do
    grep -q "^$k=y" "$BR/.config" || die "$k did not stick in .config"
done
if [ "$VARIANT" = default ]; then
    grep -q '^BR2_PACKAGE_MPS3_HARNESSD=y' "$BR/.config" || die "mps3-harnessd not selected"
    # the on-board GDB server (MVP 6 Oct): the launcher and the OpenOCD it runs
    grep -q '^BR2_PACKAGE_MPS3_DEBUG=y' "$BR/.config" || die "mps3-debug not selected"
    grep -q '^BR2_PACKAGE_OPENOCD=y' "$BR/.config" || die "openocd not selected"
    make -s -C "$BR" $BRE printvars VARS=OPENOCD_CONF_OPTS | grep -q -- '--enable-remote-bitbang' \
        || die "OPENOCD_CONF_OPTS lacks --enable-remote-bitbang (package/mps3-debug/mps3-debug.mk)"
else
    grep -q '^BR2_PACKAGE_MPS3_LEGACY_DAEMONS=y' "$BR/.config" || die "legacy daemons not selected"
fi
TCDIR=$(make -s -C "$BR" $BRE printvars VARS=TOOLCHAIN_EXTERNAL_INSTALL_DIR | sed 's/^[^=]*=//')
[ -x "$TCDIR/bin/riscv32-amd-linux-gnu-gcc" ] || die "toolchain path resolves to '$TCDIR' (no gcc there)"
echo "   toolchain: $TCDIR"
echo "   fragments: $(sed -n 's/^BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="\(.*\)"/\1/p' "$BR/.config" | tr ' ' '\n' | sed 's|.*/||' | paste -sd' ' -)"

# Assert the ISA before burning an hour: F/D here builds a userland that boots
# in QEMU (which has an FPU) and traps on the FPU-less core — the F/D landmine.
CFLAGS_OUT=$(make -s -C "$BR" $BRE printvars VARS=TOOLCHAIN_EXTERNAL_CFLAGS)
case "$CFLAGS_OUT" in *rv32imac_zicsr_zifencei*ilp32*) : ;; *) die "ISA/ABI is not rv32imac/ilp32: $CFLAGS_OUT" ;; esac
case "$CFLAGS_OUT" in *f*d*c_zicsr*|*imafd*) die "F/D in -march: $CFLAGS_OUT" ;; esac
echo "   ISA: rv32imac_zicsr_zifencei / ilp32 (no F/D)"

# Seams + provenance for the post-build hook (br2_external/board/mps3_provision.sh)
export MPS3_IMAGE_KIND=$KIND MPS3_VARIANT=$VARIANT MPS3_IDLE=$IDLE
export MPS3_STATIC_ID="${MPS3_STATIC_ID:-}"
# the static's greybox clearing (harnessd's swap_fsm seed; bare metal bakes it into
# the ELF). The post-build hook installs it, records its static + sha256 in the
# manifest, and refuses a provisioned image without it (mps3_image.py greybox-gate).
if [ -n "${MPS3_GREYBOX_CLEAR:-}" ]; then
    [ -f "$MPS3_GREYBOX_CLEAR" ] || die "MPS3_GREYBOX_CLEAR=$MPS3_GREYBOX_CLEAR not found"
    MPS3_GREYBOX_CLEAR=$(cd "$(dirname "$MPS3_GREYBOX_CLEAR")" && pwd)/$(basename "$MPS3_GREYBOX_CLEAR")
elif [ -n "$MPS3_STATIC_ID" ] && [ "$STEP" = all ]; then
    die "MPS3_STATIC_ID=$MPS3_STATIC_ID but no MPS3_GREYBOX_CLEAR: harnessd would fail closed on the
       first swap away from the greybox. Pass that mint's prod/config_rm_greybox_*_partial_clear.bin"
fi
export MPS3_GREYBOX_CLEAR="${MPS3_GREYBOX_CLEAR:-}" MPS3_GREYBOX_CLEAR_STATIC_ID="${MPS3_GREYBOX_CLEAR_STATIC_ID:-}"
export MPS3_AUTHORIZED_KEYS="${MPS3_AUTHORIZED_KEYS:-}"
export MPS3_AUTHORIZED_KEYS_FILE="${MPS3_AUTHORIZED_KEYS_FILE:-}"
if [ -z "$MPS3_AUTHORIZED_KEYS_FILE" ] && [ -z "$MPS3_AUTHORIZED_KEYS" ] && [ -f "$SW/authorized_keys" ]; then
    export MPS3_AUTHORIZED_KEYS_FILE="$SW/authorized_keys"
fi
for v in MPS3_WG_PRIVKEY MPS3_WG_PEER_PUBKEY MPS3_WG_ENDPOINT MPS3_WG_ALLOWED_IPS \
         MPS3_WG_ADDRESS MPS3_WG_PERSISTENT_KEEPALIVE; do
    export "$v=${!v:-}"
done
[ -z "$MPS3_WG_PRIVKEY" ] && [ -f "$SW/wg0.privkey" ] && export MPS3_WG_PRIVKEY="$(cat "$SW/wg0.privkey")"
export MPS3_KERNEL_PATCH_DIR=$SW/patches/linux MPS3_BUILDROOT_VERSION=$BR_TAG MPS3_KERNEL_VERSION=6.18.7
# harness identity from scripts/gen_version.py — unless FLOW's linux-image-env
# already exported it (then MPS3_HARNESS_VER32 is the static's USR_ACCESS word,
# which is what the manifest must carry).
for f in version ver32 sha dirty date; do
    case $f in version) v=MPS3_HARNESS_VERSION ;; ver32) v=MPS3_HARNESS_VER32 ;;
               sha) v=MPS3_HARNESS_SHA ;; dirty) v=MPS3_HARNESS_DIRTY ;; date) v=MPS3_HARNESS_DATE ;; esac
    [ -n "${!v:-}" ] || export "$v=$(python3 "$REPO/scripts/gen_version.py" --print "$f" 2>/dev/null || echo unknown)"
    export "$v"
done
# stage0_sha256 = sha256 of the per-static stage0.ELF FLOW bakes (FLOW_CONTRACT
# §1.2 linux-image-env exports it; its bundle packer checks it strictly).
if [ -n "${MPS3_STAGE0_ELF:-}" ]; then
    [ -f "$MPS3_STAGE0_ELF" ] || die "MPS3_STAGE0_ELF=$MPS3_STAGE0_ELF not found"
    s0=$(sha256sum "$MPS3_STAGE0_ELF" | cut -d' ' -f1)
    [ -z "${MPS3_STAGE0_SHA256:-}" ] || [ "$MPS3_STAGE0_SHA256" = "$s0" ] \
        || die "MPS3_STAGE0_SHA256 disagrees with sha256($MPS3_STAGE0_ELF)"
    export MPS3_STAGE0_SHA256=$s0
fi
export MPS3_STAGE0_SHA256="${MPS3_STAGE0_SHA256:-unknown}"
echo "   greybox clearing: ${MPS3_GREYBOX_CLEAR:-none}"
echo "   static_id=${MPS3_STATIC_ID:-0x00000000 (unprovisioned)}  keys=$([ -n "$MPS3_AUTHORIZED_KEYS_FILE$MPS3_AUTHORIZED_KEYS" ] && echo baked || echo 'none (TOFU claim)')  harness=$MPS3_HARNESS_VERSION/$MPS3_HARNESS_SHA"

# dropbear HOST key (L0 seam, ported 2026-09-23) — OPT-IN, LAB images only.
# A RELEASE image never carries one (every board flashed from it would share one
# identity, and the private key would ship inside a public artefact); its boards
# generate their own on first boot (IMAGE_CONTRACT §4.1). For a lab image:
#   unset / none                 no baked key (first-boot generation, as release)
#   MPS3_HOST_KEY_FILE=generate  $SW/artifacts/hostkey/ssh_host_ed25519_key
#                                (gitignored), GENERATED on first use, REUSED after
#   MPS3_HOST_KEY_FILE=<path>    an unencrypted ed25519 key (OpenSSH or dropbear)
# The post-build hook installs it as /etc/dropbear/dropbear_ed25519_host_key; the
# matching known_hosts line (public) goes to $ART/known_hosts_mps3-linux. Only
# the fingerprint is ever printed.
HOSTKEY_DEFAULT=$SW/artifacts/hostkey/ssh_host_ed25519_key
MPS3_SSH_HOSTS=${MPS3_SSH_HOSTS:-mps3-linux,192.168.10.101}
case "${MPS3_HOST_KEY_FILE:-}" in
    ""|none)
        MPS3_HOST_KEY_FILE=
        rm -f "$ART/known_hosts_mps3-linux"
        echo "   ssh host key: none baked (each board generates its own on first boot)"
        ;;
    *)
        if [ "$MPS3_HOST_KEY_FILE" = generate ]; then
            MPS3_HOST_KEY_FILE=$HOSTKEY_DEFAULT
            if [ ! -f "$MPS3_HOST_KEY_FILE" ]; then
                mkdir -p "$(dirname "$MPS3_HOST_KEY_FILE")"
                chmod 700 "$(dirname "$MPS3_HOST_KEY_FILE")"
                ssh-keygen -q -t ed25519 -N "" -C "mps3-linux dropbear host key (LAB)" -f "$MPS3_HOST_KEY_FILE"
                echo "   ssh host key: GENERATED $MPS3_HOST_KEY_FILE"
            else
                echo "   ssh host key: REUSED $MPS3_HOST_KEY_FILE"
            fi
        fi
        [ -f "$MPS3_HOST_KEY_FILE" ] || die "MPS3_HOST_KEY_FILE=$MPS3_HOST_KEY_FILE not found"
        python3 "$SW/br2_external/board/mps3_hostkey.py" known-hosts "$MPS3_HOST_KEY_FILE" "$MPS3_SSH_HOSTS" \
            > "$ART/known_hosts_mps3-linux"
        echo "   ssh host key: BAKED (LAB image) $(python3 "$SW/br2_external/board/mps3_hostkey.py" fingerprint "$MPS3_HOST_KEY_FILE") -> $ART/known_hosts_mps3-linux ($MPS3_SSH_HOSTS)"
        ;;
esac
export MPS3_HOST_KEY_FILE="${MPS3_HOST_KEY_FILE:-}"
# release: key-only, always (the hook refuses =1); lab: L0's rule (see the hook)
export MPS3_SSH_PASSWORD_AUTH="${MPS3_SSH_PASSWORD_AUTH:-}"

if [ "$STEP" = config ]; then
    cat <<EOF

CONFIGURED. Ready to run (after MINT COMPLETE, with no other Buildroot build running):
    cd $SW && MPS3_IMAGE_KIND=$KIND MPS3_VARIANT=$VARIANT JOBS=$JOBS ./build.sh
EOF
    exit 0
fi

echo "== 5/9  build (nice, BR2_JLEVEL=$JOBS)"
# Buildroot never notices a source change in a SITE_METHOD=local package (the
# rsync/build stamps satisfy it), so an incremental build would ship the OLD
# harnessd/spi-usd/mps3-slot. Always rebuild this tree's own packages (≈ 1 min).
LOCAL_PKGS="mps3-harnessd mps3-spi-usd mps3-slot mps3-debug"
[ "$VARIANT" = legacy ] && LOCAL_PKGS="mps3-legacy-daemons mps3-spi-usd mps3-slot mps3-dfx mps3-apps"
for p in $LOCAL_PKGS; do
    $NICE make -s -C "$BR" $BRE "$p-dirclean" >/dev/null
done
$NICE make -C "$BR" $BRE

echo "== 6/9  verify what was built"
LNX=$(ls -d "$BR"/output/build/linux-* | grep -v -- -headers | head -1)
KC=$LNX/.config
need() { grep -q "^$1\$" "$KC" || die "kernel .config lacks '$1'"; }
need_not() { grep -q "^# $1 is not set\$" "$KC" || ! grep -q "^$1=" "$KC" || die "kernel .config has $1 set"; }
[ "$IDLE" = nowfi ] && need CONFIG_RISCV_SOCLABS_NOWFI_IDLE=y
need_not CONFIG_STRICT_KERNEL_RWX
need CONFIG_SOCLABS_MBV_TIMER=y; need CONFIG_SMSC911X=y; need CONFIG_UIO_PDRV_GENIRQ=y
need CONFIG_MMC_SPI=y; need CONFIG_EXT4_FS=y; need CONFIG_OVERLAY_FS=y; need CONFIG_WIREGUARD=y
need_not CONFIG_WATCHDOG; need_not CONFIG_XILINX_HWICAP; need_not CONFIG_COMMON_CLK_XLNX_CLKWZRD
# ILA #24: no MTD / SPI-NOR stack, no AXI Quad SPI master -- nothing here may be able
# to program or erase the DUT's SST26 (mps3_image.py KCONFIG_FORBIDDEN). B1 2026-09-24:
# no riscv-pmu-sbi (its COUNTER_STOP freezes mcycle == `time` on the MBV) and the
# embedded initramfs uncompressed (the stage0 WDOG budget) -- KCONFIG_FORBIDDEN_CLOCK.
python3 "$SW/br2_external/board/mps3_image.py" kconfig-gate "$KC" \
    || die "kernel .config fails the kconfig gate (above)"
grep -q "^CONFIG_INITRAMFS_SOURCE=\".*rootfs.cpio\"" "$KC" || die "the kernel does not embed rootfs.cpio"
CPIO=$BR/output/images/rootfs.cpio
LIST=$(cpio -it --quiet < "$CPIO")
has() { echo "$LIST" | grep -qE "$1" || die "rootfs lacks $2"; }
has '^(\./)?usr/lib/modules/6\.18\.7/.*/spi-usd\.ko$' "spi-usd.ko (the July 1-region blob had NO modules)"
has '^(\./)?etc/mps3/version$' "/etc/mps3/version"
has '^(\./)?etc/init\.d/S12mps3persist$' "S12mps3persist"
has '^(\./)?usr/sbin/mps3-slot$' "mps3-slot"
has '^(\./)?usr/sbin/mkfs\.ext4$' "mkfs.ext4 (mps3-persist format)"
# the bounded watchdog bridge (HARNESSD_CONTRACT §5.3): the helper AND its start line
if [ "$VARIANT" = default ]; then
    has '^(\./)?usr/sbin/mps3-wdkick$' "mps3-wdkick (the watchdog bridge until harnessd runs)"
    has '^(\./)?etc/init\.d/S00mps3wdkick$' "S00mps3wdkick (starts the bridge first in rcS)"
    # THE BOARD IDENTITY (lane IDENT): the resolver and its rcS line, before S41
    has '^(\./)?usr/sbin/mps3-identity$' "mps3-identity (this board's label/hostname/IP/MAC)"
    has '^(\./)?etc/init\.d/S13mps3identity$' "S13mps3identity (resolves it before S41mps3net)"
    # THE ON-BOARD GDB SERVER (MVP 6 Oct): OpenOCD with remote_bitbang + ahb_qspi +
    # hostio4 inside its size budget, no F/D in it or the launcher, the configs, and
    # the unprivileged `openocd` user (board/mps3_openocd_gate.py)
    has '^(\./)?usr/bin/openocd$' "openocd (the on-board GDB server)"
    has '^(\./)?usr/bin/mps3-debug$' "mps3-debug (its launcher)"
    has '^(\./)?usr/share/mps3/openocd/designs\.conf$' "the mps3-debug design table"
    # the `openocd` user exists only in the rootfs Buildroot generated (mkusers runs in the
    # filesystem step, under fakeroot), never in output/target: check the shipped passwd
    cpio -i --quiet --to-stdout etc/passwd < "$CPIO" > "$BUILD/rootfs_passwd"
    python3 "$SW/br2_external/board/mps3_openocd_gate.py" "$BR/output/target" \
        "$BR/output/host/bin/riscv32-amd-linux-gnu-objdump" --passwd="$BUILD/rootfs_passwd" \
        > "$BUILD/openocd_gate.log" 2>&1 \
        || { sed 's/^/   /' "$BUILD/openocd_gate.log"; die "the on-board OpenOCD fails its gate (above)"; }
    sed 's/^/   /' "$BUILD/openocd_gate.log"
fi
if [ "$KIND" = release ] && echo "$LIST" | grep -qE '(dropbear_[a-z0-9]+_host_key|ssh_host_[a-z0-9]+_key)$'; then
    die "release rootfs contains an SSH host key"
fi
if [ "$KIND" = release ]; then
    cpio -i --quiet --to-stdout etc/default/dropbear < "$CPIO" 2>/dev/null | grep -q '^DROPBEAR_ARGS=.*-s' \
        || die "release rootfs: dropbear is not key-only (-s)"
fi
cpio -i --quiet --to-stdout etc/mps3/version < "$CPIO" > "$ART/version"
if [ -n "$MPS3_GREYBOX_CLEAR" ]; then
    gb=$(cpio -i --quiet --to-stdout etc/mps3/greybox_clear.bin < "$CPIO" | sha256sum | cut -d' ' -f1)
    [ "$gb" = "$(sha256sum "$MPS3_GREYBOX_CLEAR" | cut -d' ' -f1)" ] \
        || die "the rootfs's /etc/mps3/greybox_clear.bin is not $MPS3_GREYBOX_CLEAR"
    grep -qx "greybox_clear_sha256=$gb" "$ART/version" || die "version does not record the clearing's sha256"
    echo "   greybox clearing in the rootfs: sha256 $gb ($(sed -n 's/^greybox_clear_static_id=//p' "$ART/version"))"
fi
cpio -i --quiet --to-stdout etc/inittab < "$CPIO" | grep -q 'BEGIN mps3 inittab.d' || die "inittab has no managed block"
echo "   kernel config, modules, manifest, inittab, host-key rule: OK"

echo "== 7/9  artefacts + the DTB-slot margin"
cp "$BR/output/images/Image"          "$ART/Image"
cp "$BR/output/images/rootfs.cpio.gz" "$ART/rootfs.cpio.gz"
cp "$BR/output/images/fw_jump.bin"    "$ART/fw_jump.bin"
cp "$BR/output/images/fw_jump.elf"    "$ART/fw_jump.elf"
SZ=$(od -A n -t u8 -j 16 -N 8 "$ART/Image" | tr -d ' ')
END=$((KERNEL_BASE + SZ))
MARGIN=$((DTB_SLOT - END))
printf '   Image image_size=0x%X end=0x%X margin to the DTB slot=%d KiB\n' "$SZ" "$END" $((MARGIN / 1024))
[ "$MARGIN" -gt 0 ] || die "the kernel (bss included) reaches the 0x82200000 DTB slot — move the slot (OpenSBI FW_JUMP_FDT_ADDR), do not trim drivers blindly"

echo "== 8/9  the 1-region blob (OpenSBI FW_PAYLOAD = OpenSBI + Image + DTB)"
TMP=$(mktemp -d)
OS_SRC=$(ls -d "$BR"/output/build/opensbi-* | head -1) \
XTOOL=$BR/output/host/bin/riscv32-amd-linux-gnu- \
KERNEL=$ART/Image DTB=$ART/shell_linux.dtb OUT=$TMP \
    bash "$FW_STAGE0/mk_fw_payload.sh" >/dev/null
mv "$TMP/fw_payload.bin" "$ART/fw_payload_1region.bin"; rm -rf "$TMP"
BS=$(stat -c%s "$ART/fw_payload_1region.bin"); IS=$(stat -c%s "$ART/Image")
printf '   fw_payload_1region.bin %d B (Image %d B at +0x400000)\n' "$BS" "$IS"
[ "$BS" -ge $((0x400000 + IS)) ] || die "blob smaller than OpenSBI offset + Image — the payload is not in it"
[ "$BS" -le "$BLOB_MAX" ] || die "blob $BS B exceeds the $BLOB_MAX B stage0 slot (STAGE0_CONTRACT §5)"
# The same blob as the S0LB boot-table image stage0 boots — what a TFTP rescue
# push carries and what a uSD slot holds (STAGE0_CONTRACT §5; one region at
# 0x80000000, a1=0). STAGE0's own packer, then its own checker.
python3 "$FW_STAGE0/stage0_pack.py" --out "$ART/linux_slot.img" --pc 0x80000000 --a1 0 \
    "$ART/fw_payload_1region.bin@0x80000000" >/dev/null
python3 "$FW_STAGE0/stage0_pack.py" --check "$ART/linux_slot.img" >/dev/null \
    || die "stage0_pack.py --check refuses linux_slot.img"
printf '   linux_slot.img %d B (S0LB, stage0_pack.py --check OK)\n' "$(stat -c%s "$ART/linux_slot.img")"
# rv32 FW_PAYLOAD_OFFSET is 0x400000 (OpenSBI platform/generic/objects.mk)
cmp -s <(dd if="$ART/fw_payload_1region.bin" bs=4194304 skip=1 count=1 2>/dev/null | head -c 4096) \
       <(head -c 4096 "$ART/Image") || die "the Image is not at +0x400000 in the blob"

echo "== 9/9  legal-info (GPL: sources + licences, shipped with every image)"
$NICE make -s -C "$BR" $BRE legal-info >/dev/null
rm -rf "$ART/legal-info"
cp -al "$BR/output/legal-info" "$ART/legal-info" 2>/dev/null || cp -a "$BR/output/legal-info" "$ART/legal-info"
[ -s "$ART/legal-info/manifest.csv" ] || die "legal-info has no manifest.csv"
echo "   $(($(wc -l < "$ART/legal-info/manifest.csv") - 1)) target packages, $(du -sh "$ART/legal-info" | cut -f1)"

( cd "$ART" && sha256sum fw_payload_1region.bin linux_slot.img Image fw_jump.bin fw_jump.elf \
      shell_linux.dtb rootfs.cpio.gz version > SHA256SUMS )
cat > "$ART/MANIFEST.txt" <<EOF
MPS3 Linux harness image (IMAGE_CONTRACT §1) — built $(date -u +%Y-%m-%dT%H:%M:%SZ)
kind=$KIND variant=$VARIANT idle=$IDLE kernel=6.18.7 buildroot=$BR_TAG
harness=$MPS3_HARNESS_VERSION sha=$MPS3_HARNESS_SHA dirty=$MPS3_HARNESS_DIRTY static_id=${MPS3_STATIC_ID:-0x00000000}
Verify: sha256sum -c SHA256SUMS

BOARD (stage0)   fw_payload_1region.bin  one region @0x80000000, pc=0x80000000 a0=0 a1=0
                 linux_slot.img          the same, as the S0LB boot-table image stage0 boots
                                         (TFTP rescue: stage0_push.py <board> linux_slot.img)
QEMU / xsdb      fw_jump.bin@0x80000000 + Image@0x80400000 [+ shell_linux.dtb@0x82200000, a1=0x82200000]
                 (the rootfs is inside Image; there is no initrd region)
MANIFEST IN IMAGE version                (= /etc/mps3/version)
GPL              legal-info/             sources + licences for every target package — ship with the image
SSH              $( [ -n "$MPS3_HOST_KEY_FILE" ] && echo "LAB image: host key BAKED $(python3 "$SW/br2_external/board/mps3_hostkey.py" fingerprint "$MPS3_HOST_KEY_FILE") -> known_hosts_mps3-linux" || echo "host key generated per board on first boot (none baked)"); key-only unless a LAB image sets MPS3_SSH_PASSWORD_AUTH=1
                 root password = serial console only ($([ -f "$ART/ROOT_PASSWD" ] && echo "random -> ROOT_PASSWD" || echo "from \$MPS3_ROOT_PASSWD"))
EOF
ls -la "$ART"
echo
echo "Next: MPS3_QEMU=<qemu-system-riscv32> ./boot_qemu_harness.sh"
