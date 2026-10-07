#!/bin/sh
# mps3_provision.sh — Buildroot post-build provisioning bake.
#
# Wired via BR2_ROOTFS_POST_BUILD_SCRIPT (mbv_harness_defconfig). Buildroot
# runs it AFTER the rootfs overlay is applied but BEFORE the filesystem images
# (rootfs.ext2 / rootfs.cpio.gz) are generated, with $1 = $TARGET_DIR. It bakes
# deploy provisioning from the ENVIRONMENT (build.sh exports these; a bare
# `make` leaves them unset -> the image ships unprovisioned, never a fake id or
# a default key). Same "documented seam, never a committed secret" model as the
# root-password bake in build.sh.
#
#   MPS3_STATIC_ID             hex shell static_id, e.g. 0xD84A2E7A. Unset ->
#                              /etc/mps3/static_id keeps its committed
#                              0x00000000 ("not provisioned") default.
#   MPS3_GREYBOX_CLEAR         the static's GREYBOX CLEARING .bin (a mint's
#                              prod/config_rm_greybox_<pblock>_partial_clear.bin,
#                              the file the bare-metal ELF bakes) -> /etc/mps3/
#                              greybox_clear.bin (0644): harnessd's swap_fsm seed.
#                              Without it the first swap away from the greybox
#                              fails closed. Its static is MPS3_GREYBOX_CLEAR_
#                              STATIC_ID, else the mint's static_id.txt beside the
#                              .bin; recorded with its sha256 in /etc/mps3/version
#                              (greybox_clear_static_id / greybox_clear_sha256).
#                              The IMAGE TAIL's greybox gate: an image provisioned
#                              for a static MUST carry that static's clearing.
#   MPS3_AUTHORIZED_KEYS_FILE  path to an authorized_keys file to install to
#                              /root/.ssh/authorized_keys (0600, dir 0700).
#   MPS3_AUTHORIZED_KEYS       inline authorized_keys content (used only when
#                              _FILE is unset).
#
#   MPS3_WG_PRIVKEY            wg0 interface private key. REQUIRED to activate
#                              the tunnel — unset => the image ships with NO
#                              wireguard files (unprovisioned). NEVER echoed to
#                              the build log (unlike static_id, key material is
#                              never printed).
#   MPS3_WG_PEER_PUBKEY        hub peer public key.
#   MPS3_WG_ENDPOINT           hub endpoint host:port.
#   MPS3_WG_ALLOWED_IPS        peer AllowedIPs (mesh cidr(s)).
#   MPS3_WG_ADDRESS            this board's wg0 mesh address in CIDR, e.g.
#                              10.99.0.5/32 -> /etc/mps3/wg0.addr.
#   MPS3_WG_PERSISTENT_KEEPALIVE  optional; defaults to 25 if unset.
#   When MPS3_WG_PRIVKEY is set, PEER_PUBKEY / ENDPOINT / ALLOWED_IPS / ADDRESS
#   are all mandatory — a half-provisioned tunnel is worse than none (exit 1).
#
#   MPS3_HOST_KEY_FILE         (ported from L0, 2026-09-23) OPT-IN, LAB images
#                              only. A dropbear ed25519 HOST key (unencrypted,
#                              OpenSSH or dropbear format), or "generate" = the
#                              gitignored artifacts/hostkey/ssh_host_ed25519_key
#                              that `MPS3_HOST_KEY_FILE=generate ./build.sh`
#                              creates. Installed as /etc/dropbear/dropbear_
#                              ed25519_host_key; S12mps3persist seeds /persist
#                              with it once. Unset or "none" (the DEFAULT) = no
#                              baked key: the board generates its own on first
#                              boot (DL5 as amended). Refused in a RELEASE image.
#   MPS3_SSH_PASSWORD_AUTH     RELEASE: always key-only (dropbear -s); 1 refused.
#                              LAB: 0 = key-only, 1 = password SSH allowed; unset
#                              => 0 when authorized_keys is baked, else 1 (L0's
#                              B0 rule: never lock a lab image out). The root
#                              password always works on the serial console.
#
# Every seam is IDEMPOTENT against a REUSED target dir: when a seam is unset the
# hook removes what an earlier provisioned build left there (Buildroot never
# resets output/target between builds, so "unset" must actively mean "absent").
#
#   MPS3_IMAGE_KIND            release (default) | lab. A RELEASE image may not
#                              contain an SSH host key (first-boot generation,
#                              IMAGE_CONTRACT §4.1) — the hostkey gate at the
#                              end fails the build otherwise.
#   MPS3_VARIANT               default | legacy (inittab merge, manifest)
#
# The IMAGE TAIL (after every seam, including L0's host-key / password seams,
# ported 2026-09-23): the -R strip, the release checks, the inittab.d merge
# and /etc/mps3/version. It must stay LAST — see the marker below.
#
set -e
TARGET="$1"
[ -n "$TARGET" ] || { echo "mps3_provision: no TARGET_DIR argument" >&2; exit 1; }
IMG_PY="${MPS3_PYTHON:-python3} $(dirname "$0")/mps3_image.py"
HELPER="$(dirname "$0")/mps3_hostkey.py"
KIND="${MPS3_IMAGE_KIND:-release}"

# --- static_id bake --------------------------------------------------------
if [ -n "${MPS3_STATIC_ID:-}" ]; then
    case "$MPS3_STATIC_ID" in
        0x*|0X*) : ;;
        *) echo "mps3_provision: MPS3_STATIC_ID must be 0x<hex> (got '$MPS3_STATIC_ID')" >&2
           exit 1 ;;
    esac
    mkdir -p "$TARGET/etc/mps3"
    printf '%s\n' "$MPS3_STATIC_ID" > "$TARGET/etc/mps3/static_id"
    echo "mps3_provision: /etc/mps3/static_id baked = $MPS3_STATIC_ID"
else
    echo "mps3_provision: static_id unprovisioned (ships 0x00000000)"
fi

# --- greybox clearing (harnessd's swap_fsm boot seed; the bare-metal ELF bakes it)
GBC="$TARGET/etc/mps3/greybox_clear.bin"
if [ -n "${MPS3_GREYBOX_CLEAR:-}" ]; then
    [ -f "$MPS3_GREYBOX_CLEAR" ] || { echo "mps3_provision: MPS3_GREYBOX_CLEAR '$MPS3_GREYBOX_CLEAR' not found" >&2; exit 1; }
    if [ -z "${MPS3_GREYBOX_CLEAR_STATIC_ID:-}" ]; then
        # the mint's own record of which static the clearing was built against
        MPS3_GREYBOX_CLEAR_STATIC_ID=$(head -n 1 "$(dirname "$MPS3_GREYBOX_CLEAR")/static_id.txt" 2>/dev/null | tr -d ' \r')
    fi
    case "${MPS3_GREYBOX_CLEAR_STATIC_ID:-}" in
        0x*|0X*) : ;;
        *) echo "mps3_provision: which static is $MPS3_GREYBOX_CLEAR for? Set MPS3_GREYBOX_CLEAR_STATIC_ID=0x<hex>, or keep the mint's static_id.txt beside it" >&2
           exit 1 ;;
    esac
    export MPS3_GREYBOX_CLEAR_STATIC_ID
    mkdir -p "$TARGET/etc/mps3"
    cp "$MPS3_GREYBOX_CLEAR" "$GBC" && chmod 0644 "$GBC"
    echo "mps3_provision: /etc/mps3/greybox_clear.bin baked ($(wc -c < "$GBC") B, static $MPS3_GREYBOX_CLEAR_STATIC_ID)"
else
    rm -f "$GBC"          # a reused target must not keep an earlier build's clearing
    unset MPS3_GREYBOX_CLEAR_STATIC_ID
    echo "mps3_provision: no greybox clearing (MPS3_GREYBOX_CLEAR unset)"
fi

# --- root authorized_keys bake --------------------------------------------
KEYSRC=""
if [ -n "${MPS3_AUTHORIZED_KEYS_FILE:-}" ]; then
    KEYSRC="$MPS3_AUTHORIZED_KEYS_FILE"
    [ -f "$KEYSRC" ] || { echo "mps3_provision: MPS3_AUTHORIZED_KEYS_FILE '$KEYSRC' not found" >&2; exit 1; }
fi
if [ -n "$KEYSRC" ] || [ -n "${MPS3_AUTHORIZED_KEYS:-}" ]; then
    mkdir -p "$TARGET/root/.ssh"
    chmod 0700 "$TARGET/root/.ssh"
    if [ -n "$KEYSRC" ]; then
        cp "$KEYSRC" "$TARGET/root/.ssh/authorized_keys"
    else
        printf '%s\n' "$MPS3_AUTHORIZED_KEYS" > "$TARGET/root/.ssh/authorized_keys"
    fi
    chmod 0600 "$TARGET/root/.ssh/authorized_keys"
    # /root is uid 0 in the final image (Buildroot's fakeroot pass); perms set
    # here are preserved. dropbear requires .ssh 0700 + authorized_keys 0600.
    echo "mps3_provision: /root/.ssh/authorized_keys installed ($(grep -c . "$TARGET/root/.ssh/authorized_keys") key line(s), .ssh 0700 / keys 0600)"
    KEYS_BAKED=1
else
    # a reused target must not keep an earlier build's key
    rm -f "$TARGET/root/.ssh/authorized_keys"
    rmdir "$TARGET/root/.ssh" 2>/dev/null || true
    echo "mps3_provision: root authorized_keys not provisioned (no baked key; the first TFTP claim owns the board)"
    KEYS_BAKED=0
fi
# The baked set, image-owned, for /usr/sbin/mps3-keys-sync (which merges it
# with the TOFU claim into /root/.ssh/authorized_keys at every boot). The
# image's own /root/.ssh/authorized_keys keeps the baked keys too, so a boot
# where the sync never ran still admits them (fail-safe, never fail-open).
mkdir -p "$TARGET/usr/share/mps3/ssh"
if [ -s "$TARGET/root/.ssh/authorized_keys" ]; then
    cp "$TARGET/root/.ssh/authorized_keys" "$TARGET/usr/share/mps3/ssh/authorized_keys.baked"
else
    rm -f "$TARGET/usr/share/mps3/ssh/authorized_keys.baked"
fi

# --- dropbear host key bake (L0 seam; LAB images only) ------------------------
# Two things in Buildroot's stock dropbear layout defeat a baked key, and both
# are handled here (verified against package/dropbear/{dropbear.mk,S50dropbear}):
#  1. dropbear.mk installs /etc/dropbear as a SYMLINK to /var/run/dropbear
#     (tmpfs). A key written "into /etc/dropbear" would land in the build host's
#     /var/run. -> replace the symlink with a real 0700 directory. (S12mps3persist
#     does the same conversion at boot for an image with no baked key.)
#  2. S50dropbear unconditionally appends -R ("generate host keys as required").
#     With a baked ed25519 key it would STILL advertise rsa/ecdsa and lazily
#     generate ephemeral ones per boot -> strip -R. (The IMAGE TAIL strips it in
#     every image anyway; this marker stays so an un-provision restores stock.)
DBDIR="$TARGET/etc/dropbear"
HKEY="$DBDIR/dropbear_ed25519_host_key"
S50="$TARGET/etc/init.d/S50dropbear"
S50_STOCK='DROPBEAR_ARGS="$DROPBEAR_ARGS -R"'
S50_BAKED='DROPBEAR_ARGS="$DROPBEAR_ARGS" # mps3_provision: -R stripped, baked host key only'
if [ "${MPS3_HOST_KEY_FILE:-}" = "generate" ]; then
    MPS3_HOST_KEY_FILE="$(dirname "$0")/../../artifacts/hostkey/ssh_host_ed25519_key"
    [ -f "$MPS3_HOST_KEY_FILE" ] || { echo "mps3_provision: MPS3_HOST_KEY_FILE=generate but no key yet — run MPS3_HOST_KEY_FILE=generate ./build.sh first (it creates it)" >&2; exit 1; }
fi
if [ -n "${MPS3_HOST_KEY_FILE:-}" ] && [ "$MPS3_HOST_KEY_FILE" != "none" ]; then
    [ "$KIND" = lab ] || { echo "mps3_provision: MPS3_HOST_KEY_FILE bakes a host key — LAB images only (MPS3_IMAGE_KIND=lab); a release image's boards generate their own (IMAGE_CONTRACT §4.1)" >&2; exit 1; }
    [ -f "$MPS3_HOST_KEY_FILE" ] || { echo "mps3_provision: MPS3_HOST_KEY_FILE '$MPS3_HOST_KEY_FILE' not found" >&2; exit 1; }
    [ -f "$S50" ] || { echo "mps3_provision: $S50 missing (dropbear not in the image?)" >&2; exit 1; }
    [ -L "$DBDIR" ] && rm -f "$DBDIR"
    mkdir -p "$DBDIR"
    chmod 0700 "$DBDIR"
    # a dropbear reinstall over a real dir runs `ln -snf /var/run/dropbear
    # /etc/dropbear`, which drops a stray link INSIDE it — remove it
    [ -L "$DBDIR/dropbear" ] && rm -f "$DBDIR/dropbear"
    ${MPS3_PYTHON:-python3} "$HELPER" to-dropbear "$MPS3_HOST_KEY_FILE" "$HKEY"
    chmod 0600 "$HKEY"
    if grep -qF "$S50_STOCK" "$S50"; then
        ${MPS3_PYTHON:-python3} - "$S50" "$S50_STOCK" "$S50_BAKED" <<'PY'
import sys
p, a, b = sys.argv[1:4]
s = open(p).read()
open(p, "w").write(s.replace(a, b, 1))
PY
    fi
    echo "mps3_provision: dropbear host key baked, LAB image (ed25519 $(${MPS3_PYTHON:-python3} "$HELPER" fingerprint "$MPS3_HOST_KEY_FILE")), /etc/dropbear real dir"
else
    # no baked key: a reused target must not keep an earlier lab build's key
    # dir. Back to Buildroot's stock symlink; S12mps3persist turns it into the
    # /persist/dropbear bind at boot and generates this board's key there.
    if [ -d "$DBDIR" ] && [ ! -L "$DBDIR" ]; then
        rm -rf "$DBDIR"
        ln -s /var/run/dropbear "$DBDIR"
    fi
    if [ -f "$S50" ] && grep -qF "$S50_BAKED" "$S50"; then
        ${MPS3_PYTHON:-python3} - "$S50" "$S50_BAKED" "$S50_STOCK" <<'PY'
import sys
p, a, b = sys.argv[1:4]
s = open(p).read()
open(p, "w").write(s.replace(a, b, 1))
PY
    fi
    echo "mps3_provision: dropbear host key not baked (each board generates its own on first boot)"
fi

# --- SSH password auth (L0 seam, with the release rule) ------------------------
# /etc/default/dropbear is sourced by S50dropbear ($DROPBEAR_ARGS). -s disables
# password logins over SSH; the root password still works on the serial-console
# getty (/bin/login, not dropbear). A RELEASE image is always key-only — with no
# baked key the first TFTP claim (harnessd, TOFU) or the console gets you in.
DEFAULTS="$TARGET/etc/default/dropbear"
PWAUTH="${MPS3_SSH_PASSWORD_AUTH:-}"
if [ "$KIND" = release ]; then
    [ "$PWAUTH" = 1 ] && { echo "mps3_provision: MPS3_SSH_PASSWORD_AUTH=1 is LAB-only; release images are key-only" >&2; exit 1; }
    PWAUTH=0
else
    [ -n "$PWAUTH" ] || { [ "$KEYS_BAKED" = 1 ] && PWAUTH=0 || PWAUTH=1; }
fi
case "$PWAUTH" in
    0)
        # a lab image may predate harnessd's TOFU provider (B0 runs the July
        # daemons): key-only there with no baked key would lock SSH out
        [ "$KEYS_BAKED" = 1 ] || [ "$KIND" = release ] || { echo "mps3_provision: MPS3_SSH_PASSWORD_AUTH=0 with no authorized_keys would lock a LAB image's SSH out" >&2; exit 1; }
        mkdir -p "$TARGET/etc/default"
        cat > "$DEFAULTS" <<'EOF'
# /etc/default/dropbear — written by mps3_provision.sh (key-only SSH)
# -s disables password logins. The root password still works on the serial
# console (getty -> /bin/login), never over the network.
DROPBEAR_ARGS="-s"
EOF
        echo "mps3_provision: SSH key-only (dropbear -s); root password = serial console only"
        ;;
    1)
        [ -f "$DEFAULTS" ] && grep -q "written by mps3_provision.sh" "$DEFAULTS" && rm -f "$DEFAULTS"
        echo "mps3_provision: SSH password auth allowed (LAB image)"
        ;;
    *)  echo "mps3_provision: MPS3_SSH_PASSWORD_AUTH must be 0 or 1 (got '$PWAUTH')" >&2; exit 1 ;;
esac

# --- wireguard wg0 bake ----------------------------------------------------
# MPS3_WG_PRIVKEY is the activation switch: unset => no wg files at all. When
# set, the peer/endpoint/allowed-ips/address are ALL mandatory (a half-baked
# tunnel is worse than none). The private key is written to the rootfs but
# NEVER echoed — status only, never key material (cf. static_id, which echoes
# its non-secret value).
if [ -n "${MPS3_WG_PRIVKEY:-}" ]; then
    for v in MPS3_WG_PEER_PUBKEY MPS3_WG_ENDPOINT MPS3_WG_ALLOWED_IPS MPS3_WG_ADDRESS; do
        eval "val=\${$v:-}"
        [ -n "$val" ] || { echo "mps3_provision: MPS3_WG_PRIVKEY set but $v is missing (a half-provisioned tunnel is worse than none)" >&2; exit 1; }
    done
    WG_KEEPALIVE="${MPS3_WG_PERSISTENT_KEEPALIVE:-25}"
    mkdir -p "$TARGET/etc/wireguard"
    # wg setconf format — NO Address= key (that is wg-quick-only; the mesh
    # address goes in /etc/mps3/wg0.addr for the runtime to apply to the link).
    cat > "$TARGET/etc/wireguard/wg0.conf" <<EOF
[Interface]
PrivateKey = $MPS3_WG_PRIVKEY
[Peer]
PublicKey = $MPS3_WG_PEER_PUBKEY
Endpoint = $MPS3_WG_ENDPOINT
AllowedIPs = $MPS3_WG_ALLOWED_IPS
PersistentKeepalive = $WG_KEEPALIVE
EOF
    chmod 0600 "$TARGET/etc/wireguard/wg0.conf"
    mkdir -p "$TARGET/etc/mps3"
    printf '%s\n' "$MPS3_WG_ADDRESS" > "$TARGET/etc/mps3/wg0.addr"
    chmod 0644 "$TARGET/etc/mps3/wg0.addr"
    # NEVER echo $MPS3_WG_PRIVKEY — endpoint/address are not secret.
    echo "mps3_provision: /etc/wireguard/wg0.conf baked (wg0 -> $MPS3_WG_ENDPOINT, addr $MPS3_WG_ADDRESS, keepalive $WG_KEEPALIVE), 0600"
else
    echo "mps3_provision: wireguard unprovisioned (no wg0 tunnel)"
fi
# =========================== IMAGE TAIL — KEEP LAST ===========================
# Everything above may bake things; everything below checks and finishes the
# image. Every seam (incl. L0's host key / password-auth, ported 2026-09-23)
# goes ABOVE this line.

# --- dropbear: only the ed25519 key S12mps3persist generates; key-only SSH --
# S50dropbear appends -R ("generate host keys as required"), which would have
# dropbear mint RSA/ECDSA keys on demand beside the per-board ed25519 key.
# Strip it in every image (L0's lab seam strips it with its own marker; both
# forms count as done).
S50="$TARGET/etc/init.d/S50dropbear"
S50_STOCK='DROPBEAR_ARGS="$DROPBEAR_ARGS -R"'
S50_IMAGE='DROPBEAR_ARGS="$DROPBEAR_ARGS" # mps3_provision: -R stripped (ed25519 host key from S12mps3persist only)'
if [ -f "$S50" ]; then
    if grep -qF "$S50_STOCK" "$S50"; then
        ${MPS3_PYTHON:-python3} - "$S50" "$S50_STOCK" "$S50_IMAGE" <<'PY'
import sys
p, a, b = sys.argv[1:4]
s = open(p).read()
open(p, "w").write(s.replace(a, b, 1))
PY
    fi
    grep -q 'DROPBEAR_ARGS -R' "$S50" && { echo "mps3_provision: S50dropbear still passes -R" >&2; exit 1; }
    echo "mps3_provision: dropbear -R stripped (no on-demand RSA/ECDSA host keys)"
else
    echo "mps3_provision: WARNING no S50dropbear (dropbear not in the image?)"
fi
# Key-only SSH is decided by the password-auth seam above; here it is CHECKED:
# a release image must reach this point with dropbear -s.
if [ "$KIND" = release ] && ! grep -q '^DROPBEAR_ARGS=.*-s' "$TARGET/etc/default/dropbear" 2>/dev/null; then
    echo "mps3_provision: RELEASE image without dropbear -s (password SSH would be open)" >&2
    exit 1
fi

# --- release gate: no SSH host key in a release image --------------------------
$IMG_PY hostkey-gate "$TARGET" "$KIND"
# ILA #24: the kernel carries no flash stack (a no-op until Buildroot has
# configured the kernel; build.sh step 6 gates the built .config regardless)
$IMG_PY kconfig-gate auto

# --- supervision: merge /usr/share/mps3/inittab.d into /etc/inittab --------------
$IMG_PY inittab "$TARGET" "${MPS3_VARIANT:-default}"

# --- /etc/mps3/version (last: it hashes the finished tree) ----------------------
$IMG_PY manifest "$TARGET"
# --- release gate: a provisioned image carries ITS static's greybox clearing --
$IMG_PY greybox-gate "$TARGET"
exit 0
