#!/usr/bin/env python3
"""check_core_stub_ports.py — refuse to build this bench against a stale boundary.

tests/jtag_chain instantiates the REAL fpga/rp/nanosoc_iice/rp_nanosoc_iice_shim.sv
over a bench-local stand-in for the instrumented EDIF cell
(tests/jtag_chain/iice_core_stub.sv, module `rp_nanosoc_iice_core`). That
stand-in has to present exactly the boundary the real instrumented cell does:

    the 47 ports of fpga/rp/nanosoc_iice/rp_nanosoc_iice_core.sv
  + the 4 ports `device jtagport soft` adds (identify_jtag_tck/tms/tdi/tdo)
  = 51 ports / 152 bits

If it does not, the bench still elaborates and still passes — against a topology
that is not the one being shipped. That is the failure mode this file exists to
prevent, and it is the same failure the IICE README records as trap 8 ("an
unfilled black box passes every other gate").

Nothing here is retyped. The expected list is DERIVED:
  * the 47 contract ports from the real core, via fpga/dfx/pin_check.py's own
    parse_ports() — the same parser the boundary gate uses;
  * the 4 soft-TAP ports from fpga/rp/nanosoc_iice/lint/gen_lint_stubs.py's
    IDENTIFY_SOFT_TAP list, imported, not copied.

It also checks the third thing that can silently rot: that the shim really does
instantiate a module of that name, and that BOTH shim and stub agree on NGPIO.

Usage:
    python3 tests/jtag_chain/check_core_stub_ports.py [<repo_root>]
Exit 0 on agreement, 1 with a diff on drift.
"""
import importlib.util
import sys
from pathlib import Path


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else \
        Path(__file__).resolve().parents[2]

    iice_dir = root / "fpga" / "rp" / "nanosoc_iice"
    real_core = iice_dir / "rp_nanosoc_iice_core.sv"
    real_shim = iice_dir / "rp_nanosoc_iice_shim.sv"
    stub = Path(__file__).resolve().parent / "iice_core_stub.sv"

    for f in (real_core, real_shim, stub):
        if not f.exists():
            print(f"check_core_stub_ports: missing {f}", file=sys.stderr)
            return 1

    sys.path.insert(0, str(root / "fpga" / "dfx"))
    import pin_check  # noqa: E402  (needs the path above)

    gen_stubs = _load("gen_lint_stubs", iice_dir / "lint" / "gen_lint_stubs.py")

    core_name, core_ports, core_ngpio = pin_check.parse_ports(real_core)
    stub_name, stub_ports, stub_ngpio = pin_check.parse_ports(stub)
    shim_name, _shim_ports, shim_ngpio = pin_check.parse_ports(real_shim)

    problems = []

    if core_name != "rp_nanosoc_iice_core":
        problems.append(f"{real_core}: module is {core_name}")
    if stub_name != "rp_nanosoc_iice_core":
        problems.append(
            f"{stub}: module is `{stub_name}`, but the shim instantiates "
            "`rp_nanosoc_iice_core` — the bench would elaborate a different cell")
    if shim_name != "rp_nanosoc_iice_shim":
        problems.append(f"{real_shim}: module is {shim_name}")

    if not (core_ngpio == stub_ngpio == shim_ngpio == 16):
        problems.append(
            f"NGPIO disagreement: core={core_ngpio} stub={stub_ngpio} "
            f"shim={shim_ngpio} (contract v0 requires 16 everywhere)")

    # The expected instrumented boundary: contract ports, then the four the
    # instrumentor adds. Order matters only for readability; the diff below is
    # on the mapping.
    want = dict(core_ports)
    for name, direction, width in gen_stubs.IDENTIFY_SOFT_TAP:
        if name in want:
            problems.append(
                f"{real_core}: pre-declares {name}; the instrumentor ADDS it "
                "and would collide")
        want[name] = (direction, width)

    if len(want) != 51:
        problems.append(f"derived boundary is {len(want)} ports, expected 51")

    missing = [k for k in want if k not in stub_ports]
    extra = [k for k in stub_ports if k not in want]
    wrong = [(k, want[k], stub_ports[k])
             for k in want if k in stub_ports and stub_ports[k] != want[k]]

    for k in missing:
        problems.append(f"stub MISSING port `{k}` {want[k]}")
    for k in extra:
        problems.append(f"stub EXTRA port `{k}` {stub_ports[k]} "
                        "— not in the instrumented boundary")
    for k, w, g in wrong:
        problems.append(f"stub port `{k}` is {g}, instrumented boundary says {w}")

    bits = 0
    for _n, (_d, w) in want.items():
        bits += core_ngpio if w == "NGPIO" else int(w)

    if problems:
        print("check_core_stub_ports: FAIL — tests/jtag_chain would simulate a "
              "topology the RM does not have\n")
        for p in problems:
            print(f"  - {p}")
        print("\n  Fix tests/jtag_chain/iice_core_stub.sv to match "
              f"{real_core.relative_to(root)}\n"
              "  plus identify_jtag_{tck,tms,tdi,tdo}.")
        return 1

    print(f"check_core_stub_ports: OK — iice_core_stub.sv presents the "
          f"instrumented boundary ({len(want)} ports / {bits} bits: "
          f"{len(core_ports)} contract + {len(gen_stubs.IDENTIFY_SOFT_TAP)} "
          f"soft-TAP), NGPIO={core_ngpio}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
