#!/usr/bin/env bash
#
# MPS3 board lease helper — a SHIM over `pyverify lease`.
#
# The dialect (which fpgahub verb takes the CHASSIS name and which takes the
# LEASE TARGET, how a queued acquire is polled, why --json must not be passed,
# why release needs the holder as well as the token) now lives in ONE place,
# host/pyverify/pyverify/lease.py, with fake-backed tests. This file exists only
# so the callers that already say `scripts/mps3_board.sh <verb>` keep working:
# scripts/mps3_silicon_sweep.sh, scripts/mps3_shell_update.sh,
# scripts/harness_regression.sh and the runbooks.
#
# WHY THE LEASE AT ALL
#   The MPS3 is driven over two independent channels at once — Ethernet
#   (6900/6910) and JTAG (hw_server on the hub, 3121) — and they do not know
#   about each other. We have already destroyed a live 1.31 MB partial-bitstream
#   transfer by issuing an `xsdb stop` from one context while another was
#   streaming. The lease is ADVISORY for our workflow (see
#   docs/internal/MPS3_BOARD_LEASE.md for exactly which channels are and are not
#   enforced: today, none). Honour it by convention; `preflight` makes that cheap.
#
# USAGE (unchanged)
#   scripts/mps3_board.sh preflight            # exit 0 iff safe for US to touch
#   scripts/mps3_board.sh acquire [holder]     # blocks via FCFS queue; prints token
#   scripts/mps3_board.sh heartbeat <token> [holder]
#   scripts/mps3_board.sh release   <token> [holder]
#   scripts/mps3_board.sh status
#
#   Typical agent session:
#       export MPS3_LEASE_HOLDER=claude-myjob
#       TOKEN=$(scripts/mps3_board.sh acquire "$MPS3_LEASE_HOLDER") || exit 1
#       trap 'scripts/mps3_board.sh release "$TOKEN"' EXIT
#
# Env: MPS3_HUB (hub host to ssh; no default), MPS3_HUB_GROUP (socket group the
#      remote fpgahub runs under via `sg`; default fpga, empty = bare),
#      MPS3_CHASSIS, MPS3_LEASE_TARGET, MPS3_LEASE_HOLDER, MPS3_LEASE_TTL,
#      MPS3_LEASE_TIER.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
# `exec` inside, not at the call site: `exec pv ...` would try to exec a shell
# FUNCTION and die with "exec: pv: not found" (it did).
pv() { exec env PYTHONPATH="$REPO/host/pyverify${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m pyverify.cli "$@"; }

HOLDER_DEFAULT="${MPS3_LEASE_HOLDER:-claude-$(hostname -s)-$$}"
TTL="${MPS3_LEASE_TTL:-3600}"
# A swap must never be pre-empted, so take the interactive tier: the background
# tier is revocable and would let another holder yank the board out from under
# an in-flight reconfiguration.
TIER="${MPS3_LEASE_TIER:-interactive}"

case "${1:-}" in
  acquire)   pv lease acquire   --holder "${2:-$HOLDER_DEFAULT}" --ttl "$TTL" --tier "$TIER" ;;
  release)   pv lease release   --token "${2:?release needs the token from acquire}" \
                                     --holder "${3:-$HOLDER_DEFAULT}" ;;
  heartbeat) pv lease heartbeat --token "${2:?heartbeat needs the token from acquire}" \
                                     --holder "${3:-$HOLDER_DEFAULT}" ;;
  status)    pv lease status ;;
  preflight) pv lease preflight --holder "${MPS3_LEASE_HOLDER:-}" ;;
  *)         sed -n '2,33p' "$0" >&2; exit 2 ;;
esac
