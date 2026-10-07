#!/usr/bin/env python3
"""stage0_status.py -- decode the stage0 STATUS BLOCK (256 bytes at LMB 0x1FE00).

The reference decoder for docs/planning/linux_lanes/STAGE0_CONTRACT.md §3.
Sources of the 256 bytes: a JTAG read of 0x1FE00 (64 words), harnessd's LMB
UIO page at offset 0xE00, or a TFTP read of "stage0.status" from stage0's
rescue server (stage0_push.py --status does that).

    stage0_status.py BLOCK.bin          human-readable
    stage0_status.py --json BLOCK.bin   JSON
    stage0_status.py --layout           "name offset" per field (the layout
                                        gate diffs this against the C header)

Importable: decode(data) -> dict, FIELDS, render(dict). Stdlib only.
"""
import argparse
import json
import struct
import sys

STATUS_ADDR = 0x1FE00
STATUS_BYTES = 0x100
MAGIC = 0x54533053          # "S0ST"
VERSION = 1
CONFIRM_MAGIC = 0x4B4F3053  # "S0OK", written by Linux to att_confirm

# (name, offset) -- MUST match S0_STATUS_FIELDS in stage0_status.h; the test
# Makefile's layout gate fails if it does not.
FIELDS = (
    ("magic", 0x00), ("version", 0x04), ("size", 0x08), ("build_id", 0x0C),
    ("fabric_static_id", 0x10), ("fabric_ver32", 0x14), ("boot_count", 0x18),
    ("phase", 0x1C), ("booted_from", 0x20), ("last_error", 0x24),
    ("ddr_calib", 0x28), ("sd_result", 0x2C), ("sd_detail", 0x30),
    ("slot_a_rc", 0x34), ("slot_b_rc", 0x38), ("default_slot", 0x3C),
    ("cfg_seq", 0x40), ("att_from", 0x44), ("att_confirm", 0x48),
    ("fails_a", 0x4C), ("fails_b", 0x50), ("boot_limit", 0x54),
    ("last_verdict", 0x58), ("rescue_reason", 0x5C), ("rescue_state", 0x60),
    ("rescue_bytes", 0x64), ("rescue_sessions", 0x68), ("rescue_rejects", 0x6C),
    ("rescue_last_rc", 0x70), ("n_boot_a", 0x74), ("n_boot_b", 0x78),
    ("n_boot_rescue", 0x7C), ("n_fallback", 0x80), ("entry_pc", 0x84),
    ("entry_a0", 0x88), ("entry_a1", 0x8C), ("image_hdr_crc", 0x90),
    ("handoff_ms", 0x94), ("heartbeat", 0x98), ("uptime_ms", 0x9C),
    ("reset_cause", 0xA0), ("trap_mcause", 0xA4), ("trap_mepc", 0xA8),
    ("trap_mtval", 0xAC), ("ip_addr", 0xB0), ("mac_lo", 0xB4), ("mac_hi", 0xB8),
    ("net_rc", 0xBC), ("tftp_errors", 0xC0), ("rx_frames", 0xC4),
    ("tx_frames", 0xC8), ("pings", 0xCC), ("identifies", 0xD0),
    ("verdict_from", 0xD4), ("sd_rd_fails", 0xD8), ("sd_rd_last", 0xDC),
    ("subphase", 0xE0), ("entry", 0xE4), ("prev_phase", 0xE8),
    ("label_lo", 0xEC), ("label_hi", 0xF0), ("prev_uptime_ms", 0xF4),
    ("ddr_ok_ms", 0xF8),
    ("magic_end", 0xFC),
)

PHASE = {0: "reset", 1: "ddr", 2: "sd", 3: "slot A", 4: "slot B", 5: "rescue",
         6: "handoff", 7: "trap"}
FROM = {0: "none", 1: "A", 2: "B", 3: "rescue"}
DDR = {0: "unknown", 1: "ok", 2: "FAIL", 3: "implied", 4: "LOST (dropped mid-load)"}
SD = {0: "not tried", 1: "ready", 2: "no card", 3: "no usd_spi block", 4: "unsupported",
      5: "init error", 6: "init timeout", 7: "no MBR", 8: "skipped"}
RR = {0: "none", 1: "ddr calib fail", 2: "no card", 3: "no usd_spi block",
      4: "card unsupported", 5: "card error", 6: "card has no stage0 slots",
      7: "no valid slot", 8: "slots exhausted (unconfirmed boots)", 9: "stage0 watchdog loop"}
RS = {0: "off", 1: "listen", 2: "receiving", 3: "verifying", 4: "rejected", 5: "accepted",
      6: "no net", 7: "ddr not calibrated"}
VD = {0: "none", 1: "confirmed", 2: "UNCONFIRMED"}
S0RC = {0: "ok", 1: "no boot table (magic)", 2: "bad version", 3: "header CRC",
        4: "bad num_entries", 5: "read/truncated", 6: "payload CRC", 7: "no slot",
        8: "not tried", 9: "boot limit", 10: "image too large", 11: "TFTP protocol error",
        12: "push timed out", 13: "DDR calib lost", 14: "slot load time limit"}
ES = {0: "none", 1: "ddr", 2: "sd", 3: "slot A", 4: "slot B", 5: "rescue", 6: "net", 7: "trap",
      8: "watchdog loop"}
# subphase [31:24] (S0_SP_*) and entry [7:0] (S0_EK_*), lane S0-COLDFIX 2026-09-28
SP = {0: "none", 1: "settle", 2: "ddr-wait", 3: "card-init", 4: "card-meta", 5: "slot-load",
      6: "crc", 7: "handoff", 8: "rescue"}
EK = {0: "none", 1: "cold", 2: "warm", 3: "WATCHDOG before a hand-off",
      4: "watchdog after a hand-off", 5: "warm inside the cold settle (re-settle)"}
USD_STATE = {0: "absent", 1: "settle", 2: "init", 3: "READY", 4: "unsupported", 5: "ERROR"}
# errno values as stage0 sees them: newlib (the rv32 build) has ETIMEDOUT = 116; 110 is
# glibc's, i.e. the host test build. Both decode, so a silicon status reads as a name.
RD_RC = {5: "EIO (bad R1 / data-error token)", 110: "ETIMEDOUT (no data token)",
         116: "ETIMEDOUT (no data token)",
         19: "ENODEV (card gone)", 250: "start refused (not READY)", 251: "op never ended"}


def last_error_text(le):
    """last_error's code is an S0_SD_* for the sd source, an S0_DDR_* for ddr,
    an S0_* load result for the slot/rescue sources."""
    src, code = le >> 16, le & 0xFFFF
    table = SD if src == 2 else DDR if src == 1 else S0RC
    return "%s %d (%s)" % (ES.get(src, src), code, table.get(code, ""))


def sd_text(d):
    r, st = d["sd_result"], d["sd_detail"] >> 16
    if r == 5 and st == 3:
        return "READY, but reading sector 0 (the MBR) failed"
    return SD.get(r, r)


def subphase_text(w):
    """S0_SUBPHASE word -> 'slot-load 1234' (the detail is a block, region or ms)."""
    sp, det = w >> 24, w & 0xFFFFFF
    return "%s %d" % (SP.get(sp, "?%d" % sp), det) if det else SP.get(sp, "?%d" % sp)


def prev_fields(w):
    """prev_phase: [7:0] phase, [15:8] subphase code, [31:16] its detail (sat 0xFFFF)."""
    return {"phase": w & 0xFF, "subphase": subphase_text(((w >> 8) & 0xFF) << 24 | (w >> 16))}


def entry_fields(e):
    """The packed `entry` word -> its parts."""
    return {"kind": EK.get(e & 0xFF, e & 0xFF), "wdog_run": (e >> 8) & 0xFF,
            "calib_drops": (e >> 16) & 0xFF, "ddr_recoveries": (e >> 24) & 0xF,
            "cal_at_settle": bool(e >> 28 & 1), "cal_rose_in_settle": bool(e >> 29 & 1),
            "settle_pending": bool(e >> 31)}


def valid(d):
    """The contract's acceptance rule: magic, version, size and magic_end."""
    return (d.get("magic") == MAGIC and d.get("version") == VERSION and
            d.get("size") == STATUS_BYTES and d.get("magic_end") == MAGIC)


def decode(data):
    """256 bytes (or 64 little-endian words as bytes) -> dict of raw fields plus
    'valid' and a few decoded strings."""
    if len(data) < STATUS_BYTES:
        raise ValueError("need %d bytes, got %d" % (STATUS_BYTES, len(data)))
    d = {name: struct.unpack_from("<I", data, off)[0] for name, off in FIELDS}
    d["valid"] = valid(d)
    mac = struct.pack("<IH", d["mac_lo"], d["mac_hi"] & 0xFFFF)
    d["mac"] = mac.hex()
    ip = d["ip_addr"]
    d["ip"] = "%d.%d.%d.%d" % (ip >> 24, (ip >> 16) & 255, (ip >> 8) & 255, ip & 255)
    d["confirmed"] = d["att_confirm"] == CONFIRM_MAGIC
    # the board's baked identity (lane IDENT; stage0_status.h "THE BOARD IDENTITY"):
    # written at every entry; 0 = absent (a stage0 older than 2026-09-28)
    lab = struct.pack("<II", d["label_lo"], d["label_hi"]).split(b"\0", 1)[0]
    d["label"] = lab.decode("ascii", "replace") if lab else None
    d["entry_decoded"] = entry_fields(d["entry"])
    d["prev_decoded"] = prev_fields(d["prev_phase"])
    return d


def render(d):
    if not d["valid"]:
        return ("no stage0 status block (magic=0x%08X magic_end=0x%08X): a bare-metal "
                "image, stage0 never ran, or a torn read" % (d["magic"], d["magic_end"]))
    le = d["last_error"]
    out = [
        "stage0 build 0x%08X  fabric static_id 0x%08X ver32 0x%08X"
        % (d["build_id"], d["fabric_static_id"], d["fabric_ver32"]),
        "boot #%d  phase %s  booted from %s  default slot %s  (reset_cause 0x%X%s)"
        % (d["boot_count"], PHASE.get(d["phase"], d["phase"]), FROM.get(d["booted_from"]),
           FROM.get(d["default_slot"]), d["reset_cause"],
           ", watchdog" if d["reset_cause"] & 8 else ""),
        "DDR %s   uSD %s (detail 0x%08X)"
        % (DDR.get(d["ddr_calib"], d["ddr_calib"]), sd_text(d), d["sd_detail"]),
        "slot A: %s   slot B: %s" % (S0RC.get(d["slot_a_rc"]), S0RC.get(d["slot_b_rc"])),
        "attempt: from %s, %s; fails A %d B %d (limit %d); previous attempt %s (%s)"
        % (FROM.get(d["att_from"]), "CONFIRMED" if d["confirmed"] else "pending/none",
           d["fails_a"], d["fails_b"], d["boot_limit"], VD.get(d["last_verdict"]),
           FROM.get(d["verdict_from"])),
        "boots: A %d  B %d  rescue %d  fallbacks %d"
        % (d["n_boot_a"], d["n_boot_b"], d["n_boot_rescue"], d["n_fallback"]),
        "last error: %s" % last_error_text(le),
        "identity: label %s  ip %s  mac %s" % (d["label"] or "-", d["ip"] if d["ip_addr"] else "-",
                                               d["mac"] if (d["mac_lo"] or d["mac_hi"]) else "-"),
    ]
    ed = entry_fields(d["entry"])
    out.append("entry %s; now %s; DDR gate passed at %s; calib drops %d; ddr recoveries %d; "
               "pre-hand-off watchdog run %d%s"
               % (ed["kind"], subphase_text(d["subphase"]),
                  "%d ms" % d["ddr_ok_ms"] if d["ddr_ok_ms"] else "-", ed["calib_drops"],
                  ed["ddr_recoveries"], ed["wdog_run"],
                  ", cold settle PENDING" if ed["settle_pending"] else ""))
    if ed["kind"] == EK[1] and ed["cal_at_settle"] and not ed["cal_rose_in_settle"] \
            and not ed["calib_drops"]:
        out.append("NOTE: calib was already 1 when the cold settle began and never moved: the "
                   "MIG calibrated before the MCC programmed OSC6 (stale calibration suspect)")
    if d["prev_phase"] or d["prev_uptime_ms"]:
        pp = prev_fields(d["prev_phase"])
        out.append("previous run: phase %s, %s, at %d ms"
                   % (PHASE.get(pp["phase"], pp["phase"]), pp["subphase"], d["prev_uptime_ms"]))
    if d["sd_rd_fails"]:
        rl = d["sd_rd_last"]
        out.append("uSD reads: %d failed op(s); last %s, then usd %s err %d"
                   % (d["sd_rd_fails"], RD_RC.get(rl >> 24, "rc -%d" % (rl >> 24)),
                      USD_STATE.get((rl >> 16) & 0xFF, (rl >> 16) & 0xFF), rl & 0xFFFF))
    if d["phase"] in (5,) or d["rescue_state"]:
        out.append("rescue: %s -- %s; %d B pushed, %d sessions, %d rejected (last: %s); "
                   "%s %s; pings %d identifies %d"
                   % (RS.get(d["rescue_state"]), RR.get(d["rescue_reason"]), d["rescue_bytes"],
                      d["rescue_sessions"], d["rescue_rejects"],
                      S0RC.get(d["rescue_last_rc"]), d["ip"], d["mac"], d["pings"],
                      d["identifies"]))
    if d["phase"] == 6:
        out.append("hand-off: pc 0x%08X a0 0x%X a1 0x%08X image hdr_crc 0x%08X after %d ms"
                   % (d["entry_pc"], d["entry_a0"], d["entry_a1"], d["image_hdr_crc"],
                      d["handoff_ms"]))
    if d["trap_mcause"]:
        out.append("TRAP: mcause 0x%08X mepc 0x%08X mtval 0x%08X"
                   % (d["trap_mcause"], d["trap_mepc"], d["trap_mtval"]))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("block", nargs="?", help="256-byte file, or - for stdin")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--layout", action="store_true", help="print 'name offset' per field")
    a = ap.parse_args()
    if a.layout:
        for name, off in FIELDS:
            print("%s 0x%02X" % (name, off))
        return 0
    if not a.block:
        ap.error("a block file (or --layout) is required")
    data = sys.stdin.buffer.read() if a.block == "-" else open(a.block, "rb").read()
    d = decode(data)
    print(json.dumps(d, indent=1, sort_keys=True) if a.json else render(d))
    return 0 if d["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
