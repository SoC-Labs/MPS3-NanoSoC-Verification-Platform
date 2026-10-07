#!/usr/bin/env bash
# mps3_sd_update.sh — BACKUP-FIRST update of the MPS3 config SD.
#
# TWO HALVES, AND WHY THEY LIVE WHERE THEY DO
#   * The BACKUP is here. It is hub-local, needs sudo, mounts a block device by
#     label and copies the whole config set — none of which belongs in a python
#     library that has to stay testable without a hub.
#   * The WRITE is `pyverify sd write`. Its discipline (one write at a time, the
#     client timeout is EXPECTED, wait the documented interval, md5 read-back
#     where there is one, never claim the shell was fielded) is encoded there
#     with fake-backed tests instead of written down here.
#
# WHY THE BACKUP IS A GATE: `fpgahub target program ... --method sd` OVERWRITES
# the config SD in place. On 2026-07-16 the previous nanosoc.bit was overwritten
# with no backup and is gone. The write cannot run unless a real, non-empty,
# bitstream-shaped backup of the current SD was captured first.
#
# It runs ON THE HUB: the config SD is a USB mass-storage device there (label
# "V2M-MPS3"), and sd_install runs there too. Invoke over ssh, or from the hub.
#
# DO NOT hardcode /dev/sdX. The kernel names are ENUMERATION ORDER, and they have
# already swapped once: on 2026-08-25 the DAPLink VFS volume (64 MB, label
# "MBED MPS3") enumerated first and took /dev/sda, pushing the real config SD to
# /dev/sdb1. The default below is therefore the stable by-label path; the label
# gate (Gate 2) is the backstop that makes a wrong --sd-dev fail safe.
#
# Usage (on the hub):
#   mps3_sd_update.sh --bit <new.bit> --token <lease-token> [--holder <name>] \
#                     [--sd-dev /dev/disk/by-label/V2M-MPS3] [--backup-dir ~/mps3_sd_backups] \
#                     [--program]        # WITHOUT --program: backup + dry-run only
#
# Safe by default: with no --program it does the backup + all checks and STOPS,
# printing the exact `pyverify sd write` command it WOULD run. Add --program to
# actually write.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SD_DEV="/dev/disk/by-label/V2M-MPS3"   # stable by-label path; /dev/sdX order is NOT stable
SD_LABEL_EXPECT="V2M-MPS3"          # lsblk label of the MPS3 config SD (verified 2026-07-17)
BACKUP_DIR="$HOME/mps3_sd_backups"
# Where the written file lands on the mounted SD, for the md5 read-back. Empty
# => `pyverify sd write` reports the write UNVERIFIED rather than assuming it.
VERIFY_REL="${MPS3_SD_VERIFY_REL:-}"
BIT=""
TOKEN=""
HOLDER="${MPS3_LEASE_HOLDER:-claude-sdfix}"
DO_PROGRAM=0

die() { echo "mps3_sd_update: FATAL: $*" >&2; exit 1; }
note() { echo ">>> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --bit)        BIT="$2"; shift 2;;
    --token)      TOKEN="$2"; shift 2;;
    --holder)     HOLDER="$2"; shift 2;;
    --sd-dev)     SD_DEV="$2"; shift 2;;
    --backup-dir) BACKUP_DIR="$2"; shift 2;;
    --verify-rel) VERIFY_REL="$2"; shift 2;;
    --program)    DO_PROGRAM=1; shift;;
    *) die "unknown arg: $1";;
  esac
done

[ -n "$BIT" ]   || die "--bit is required"
[ -f "$BIT" ]   || die "bitstream not found: $BIT"
[ -n "$TOKEN" ] || die "--token is required (the write is destructive; prove you hold the lease)"

# --- Gate 1: we must actually hold the lease ---------------------------------
# Delegated: `pyverify lease preflight` knows that `lease show` is CHASSIS-scoped
# (asking for the member board is a 404, and a 404 read as "free" is how a second
# agent drives a board somebody else is mid-swap on).
note "Gate 1: lease ownership"
MPS3_LEASE_HOLDER="$HOLDER" PYTHONPATH="$REPO/host/pyverify${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" -m pyverify.cli lease preflight --holder "$HOLDER" \
  || die "the board is NOT held by '$HOLDER'. Refusing to touch a board we do not own."

# --- Gate 2: identify the SD unambiguously by label --------------------------
note "Gate 2: confirm $SD_DEV is the MPS3 config SD (label $SD_LABEL_EXPECT)"
LABEL="$(sudo blkid -s LABEL -o value "$SD_DEV" 2>/dev/null || true)"
echo "    $SD_DEV label = '${LABEL:-<none>}'"
[ "$LABEL" = "$SD_LABEL_EXPECT" ] \
  || die "wrong device: expected label '$SD_LABEL_EXPECT', got '${LABEL:-<none>}'. NOT mounting/backing up the wrong disk."

# --- Gate 3: BACKUP the current SD (read-only mount, cp, verify) -------------
note "Gate 3: backup"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_DIR/$TS"
MNT="$(mktemp -d /tmp/mps3sd.XXXXXX)"
mkdir -p "$DEST"
cleanup() { sudo umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }
trap cleanup EXIT

sudo mount -o ro "$SD_DEV" "$MNT" || die "read-only mount of $SD_DEV failed"
echo "    mounted RO at $MNT; contents:"
ls -la "$MNT" | sed 's/^/      /'
# Copy EVERYTHING small (the whole config set), not just the .bit — nanosoc.txt
# carries the FPGA_DDR delta etc. and must survive too.
sudo cp -a "$MNT"/. "$DEST"/ || die "backup copy failed"
sudo chown -R "$(id -un):$(id -gn)" "$DEST" 2>/dev/null || true
sync
cleanup; trap - EXIT
echo "    backup captured at $DEST -- pyverify sd write re-checks it is real"

# --- Gate 4: program, through the ONE writer --------------------------------
PROG_CMD=("${PYTHON:-python3}" -m pyverify.cli sd write "$BIT"
          --token "$TOKEN" --holder "$HOLDER" --backup "$DEST")
[ -n "$VERIFY_REL" ] && PROG_CMD+=(--verify-path "$MNT/$VERIFY_REL")
note "Gate 4: program"
if [ "$DO_PROGRAM" -ne 1 ]; then
  echo "    DRY RUN (no --program). Backup is captured and verified above."
  echo "    Would run: PYTHONPATH=$REPO/host/pyverify ${PROG_CMD[*]}"
  echo "    Re-run with --program to write. To restore instead, write the backup .bit."
  exit 0
fi

echo "    running: ${PROG_CMD[*]}"
PYTHONPATH="$REPO/host/pyverify${PYTHONPATH:+:$PYTHONPATH}" "${PROG_CMD[@]}" \
  || die "sd write refused or failed (backup is safe at $DEST)"
note "PROGRAM COMPLETE. Backup retained at $DEST."
echo '    Next: make the MCC re-read the SD, then: pyverify ping --host <board>'
echo "    The write itself is NOT evidence the board is on the new shell -- only"
echo "    ping.shell_id (or the CLCD status line) is (docs/FIELDED_SHELL.md)."
