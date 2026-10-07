#!/usr/bin/env python3
"""On-silicon bring-up for the M0 QSPI flash loader (firmware/qspi_loader).

The loader firmware is PROVEN on silicon (2026-07-19..21; commits b63da06,
fce26bb) — full 160 KB image programmed + CRC-verified in ~2m41s. But first
bring-up flushed out three DUT-ONLY silent-failure bugs the mirrored host model
could not see (SPI_CMD read-backs, BUSY-assert wait, XIP_ACTIVE clear at entry).
That is exactly why this runs in STAGES, cheapest and least destructive first —
and it remains the safe way to re-run the loader on a fresh board:

  --stage=enter   (default)  Load the loader into IMEM, enter it, and confirm it
                             reports its magic word. Touches NO flash at all.
                             This exercises the riskiest, most novel part: the
                             SP/PC entry and the SysTick/NVIC quiesce that stops
                             the previous image's armed interrupts wedging us in
                             Default_Handler (ARMv6-M cannot clear an ACTIVE
                             exception, and reset-halt is unreliable on this DUT).

  --stage=crc                ...then CRC a region of flash. READ-ONLY: proves the
                             loader can drive the controller, without writing.

  --stage=full               ...then erase + program + DUT-side-CRC verify a small
                             payload at a scratch offset.

SAFETY RAILS (same shape as scripts/qspi_write_smoke.py)
  * Writes only at a scratch offset; default 0x100000 sits between the nanoSoC
    boot map (0x0-0x60000) and the clearing regions (0x780000/0x7C0000).
  * REFUSES any overlap with those reserved regions unless --i-know.
  * Payload capped at the loader's 48 KB window, default 4 KB.
  * Never touches the XiP aperture (an unconfigured read there previously hung
    the shared AXI bus and took the whole shell off the network).

NOTE: this CLOBBERS DUT IMEM (the loader and payload live there), so whatever
the RM was running is gone. Re-deploy the RM afterwards if you need it.

Usage (from the repo root):
    python3 -u scripts/qspi_loader_bringup.py --dry-run
    python3 -u scripts/qspi_loader_bringup.py                 # enter-only
    python3 -u scripts/qspi_loader_bringup.py --stage=crc
    python3 -u scripts/qspi_loader_bringup.py --stage=full
"""
from __future__ import annotations

import argparse
import os
import binascii
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from pyverify.qspi_loader import (  # noqa: E402
    BUFFER_SIZE,
    M0FlashLoader,
    LoaderError,
)
from pyverify.swd import SwdDebugger, SwdError  # noqa: E402

LOADER_BIN = REPO / "firmware" / "qspi_loader" / "build" / "qspi_loader.bin"
RESERVED = (
    (0x000000, 0x060000, "nanoSoC boot map / overlay store"),
    (0x780000, 0x790000, "clearing STAGE"),
    (0x7C0000, 0x7D0000, "clearing CACHE"),
)
DEFAULT_OFFSET = 0x100000


def check_offset(base: int, length: int, forced: bool) -> None:
    end = base + length
    for r0, r1, name in RESERVED:
        if base < r1 and end > r0:
            msg = (f"REFUSING: [{base:#08x},{end:#08x}) overlaps {name} "
                   f"[{r0:#08x},{r1:#08x}).")
            if not forced:
                raise SystemExit(msg + " Use --i-know only if you truly mean it.")
            print("!! " + msg + " --i-know given; proceeding.", flush=True)


def make_stager(hub: str, hub_dir: str):
    """load_image hands its path straight to OpenOCD, which runs on the hub, so
    payloads must be staged THERE — a local path would not resolve."""
    subprocess.run(["ssh", "-oBatchMode=yes", hub, f"mkdir -p {hub_dir}"], check=True)

    def stage(data: bytes, name: str) -> str:
        local = Path("/tmp") / name
        local.write_bytes(data)
        remote = f"{hub_dir}/{name}"
        subprocess.run(["scp", "-q", str(local), f"{hub}:{remote}"], check=True)
        return remote

    return stage


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("enter", "crc", "full"), default="enter")
    ap.add_argument("--offset", type=lambda x: int(x, 0), default=DEFAULT_OFFSET)
    ap.add_argument("--bytes", type=int, default=4096, dest="nbytes")
    ap.add_argument("--image", default=None,
                    help="program a REAL file instead of a synthetic payload. "
                         "May exceed the 48 KB window — program_image() chunks "
                         "it. Use this to validate the loader at full scale.")
    ap.add_argument("--hub", default=os.environ.get("MPS3_HUB"))
    ap.add_argument("--repo-dir", default=os.environ.get("MPS3_REPO_DIR") or str(REPO))
    ap.add_argument("--hub-stage-dir", default="/tmp/qspi_loader_stage")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--i-know", action="store_true")
    a = ap.parse_args()

    image_data = None
    if a.image:
        image_data = Path(a.image).read_bytes()
        if not image_data:
            raise SystemExit(f"--image {a.image} is empty")
        a.nbytes = len(image_data)          # chunked by program_image()
    elif not 0 < a.nbytes <= BUFFER_SIZE:
        raise SystemExit(f"--bytes must be 1..{BUFFER_SIZE} (the loader's window)")
    if a.stage == "full":
        check_offset(a.offset, a.nbytes, a.i_know)

    if not LOADER_BIN.exists():
        raise SystemExit(f"missing {LOADER_BIN} — run: make -C firmware/qspi_loader")
    blob = LOADER_BIN.read_bytes()

    print(f"PLAN: stage={a.stage}  loader={len(blob)} B"
          + (f"  image={a.image}" if a.image else ""), flush=True)
    if a.stage != "enter":
        print(f"      region {a.offset:#08x} + {a.nbytes} B", flush=True)
    print("      NOTE: this clobbers DUT IMEM; re-deploy the RM afterwards.",
          flush=True)
    if a.dry_run:
        print("--dry-run: nothing touched.")
        return 0

    swd = SwdDebugger(repo_dir=a.repo_dir)
    try:
        print(f"cpuid = 0x{swd.cpuid():08x}", flush=True)
    except SwdError as e:
        print(f"SWD unreachable: {e}", file=sys.stderr)
        return 2

    ldr = M0FlashLoader(swd, make_stager(a.hub, a.hub_stage_dir), loader_bin=blob)

    t0 = time.time()
    print("entering loader (load -> quiesce IRQs -> set SP/PC -> resume) ...",
          flush=True)
    ldr.start()
    print(f"  LOADER LIVE (magic seen)  [{time.time() - t0:.0f}s]", flush=True)
    if a.stage == "enter":
        print("stage=enter complete; NO flash was touched.")
        return 0

    print(f"CRC of {a.offset:#08x}+{a.nbytes} (read-only) ...", flush=True)
    before = ldr.crc32(a.offset, a.nbytes)
    print(f"  flash CRC32 = {before:#010x}  [{time.time() - t0:.0f}s]", flush=True)
    if a.stage == "crc":
        print("stage=crc complete; NO flash was written.")
        return 0

    payload = image_data if image_data is not None else bytes(
        (i * 7 + 0x5A) & 0xFF for i in range(a.nbytes))
    print(f"erase+program+verify {a.nbytes} B at {a.offset:#08x} ...", flush=True)
    crc = ldr.program_image(payload, base=a.offset)
    want = binascii.crc32(payload) & 0xFFFFFFFF
    print(f"  flash CRC32 = {crc:#010x}  image CRC32 = {want:#010x}", flush=True)
    print(f"  LOADER WRITE PATH PROVEN  [{time.time() - t0:.0f}s]", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LoaderError as e:
        print(f"LOADER ERROR: {e}", file=sys.stderr)
        sys.exit(1)
