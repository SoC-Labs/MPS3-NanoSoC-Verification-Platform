#!/usr/bin/env bash
# mps3_lease_acquire.sh — a SHIM over `pyverify lease acquire`.
#
# WHY THIS FILE STILL EXISTS
#   It was written (2026-07-21) because scripts/mps3_board.sh's acquire did not
#   survive a CONTENDED board: it passed `--json`, and this fpgahub's QUEUED
#   response carries only a `position` and no token, so a queued acquire errored
#   out AND stranded a queue entry — and `lease wait` could not rescue it,
#   because it needs the token the queued response never gives.
#
#   That poller is now pyverify.lease.LeaseClient.acquire (fake-backed tests
#   pin the queued path, the give-up-and-cancel path and the bare-token stdout),
#   and mps3_board.sh execs the SAME code, so the two are no longer different
#   implementations. This file remains only because scripts/mps3_silicon_sweep.sh
#   and the runbooks call it by name.
#
# Usage (unchanged: prints ONLY the token on stdout):
#   TOKEN=$(scripts/mps3_lease_acquire.sh claude-myjob) || exit 1
#   trap 'scripts/mps3_board.sh release "$TOKEN" claude-myjob' EXIT
#
# Env: MPS3_HUB (hub host; no default), MPS3_LEASE_TARGET, MPS3_LEASE_TTL,
#      MPS3_LEASE_TIER, MPS3_ACQUIRE_POLL_S (20), MPS3_ACQUIRE_TIMEOUT_S (3600).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOLDER="${1:-${MPS3_LEASE_HOLDER:-claude-$(hostname -s)-$$}}"

exec env PYTHONPATH="$REPO/host/pyverify${PYTHONPATH:+:$PYTHONPATH}" \
  "${PYTHON:-python3}" -m pyverify.cli lease acquire \
    --holder "$HOLDER" \
    --ttl "${MPS3_LEASE_TTL:-3600}" \
    --tier "${MPS3_LEASE_TIER:-interactive}" \
    --poll "${MPS3_ACQUIRE_POLL_S:-20}" \
    --acquire-timeout "${MPS3_ACQUIRE_TIMEOUT_S:-3600}"
