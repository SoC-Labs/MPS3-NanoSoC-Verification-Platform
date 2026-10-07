# socket_harness — one endpoint registry + Session over pyverify

The MPS3 nanoSoC harness exposes a spread of host-facing endpoints — the
JSON control channel, the TFTP / raw-TCP bitstream push, the OpenOCD
remote-bitbang SWD and Vivado XVC debug servers, three raw-TCP UART/SWO
consoles, the MCC serial console, and the xsdb/hw_server JTAG path to the
CSR register file and the diag mailbox. Today the port numbers and CSR
bases for those are **re-declared in half a dozen places** (across
`pyverify.{client,console,debug,pusher,edge}`, `scripts/mps3_console.py`,
`scripts/mps3_diag.tcl`, and the hand-maintained ser2net/socat configs), and
the only register / diag access is a `.tcl` script.

`socket_harness` is a **board-free** package that consolidates all of that
behind **one frozen registry**, **one reconnecting `Session`**, a **Python
CSR/xsdb register endpoint**, and a **pure-Python ser2net-compatible console
bridge** — plus a CLI. It is stdlib-only beyond the already-vendored
`mps3-pyverify`.

Source of truth for every number in here: `docs/contracts/net-protocol.md`
(the TCP/UDP ports) and `docs/contracts/shell-regmap.md` (the CSR bases and
bitfields). This package does not invent addresses; it transcribes those
contracts once and lets everything else import them.

## 1. What this adds over pyverify

| Gap in the current host stack | What socket_harness adds |
|---|---|
| Port constants duplicated across `pyverify.client` (6900), `pyverify.pusher` (69/6910), `pyverify.debug` (6920/2542), `pyverify.console` (6930/6931/6932), plus `scripts/mps3_console.py` and `scripts/mps3_diag.tcl` | **`endpoints.REGISTRY`** — one frozen tuple of `EndpointSpec` rows with `by_name` / `by_port` / `by_family` lookups. Drift-guarded in tests against the pyverify constants (ports assert-equal; the hub *spelling* deliberately does not — see §5). |
| Ad-hoc `socket.create_connection` + hand-rolled recv loops, no reconnect | **`session.Session`** — a base lifecycle (`connect`/`close`/`reconnect`/context-manager) with an injectable clock + `RetryPolicy` backoff, and concrete `LineSession` / `RawStreamSession` / `SerialSession`. |
| CSR + diag access existed only as `scripts/mps3_diag.tcl` | **`xsdb.XsdbRegisterEndpoint`** + **`registers.RegisterAccess`** — a Python `RegisterBackend` over xsdb/hw_server with pure `parse_mrd_value`/`parse_mrd_block` cores, plus the DFXCTL/VPHY/GENCHK/… bitfield models transcribed from `shell-regmap.md`. |
| The ser2net / socat console configs were hand-maintained example files (`host/console/`, now **retired**) that drifted from the registry | **`console_bridge`** — a pure-Python pty↔tcp bridge (`ConsoleBridge`) *and* the `ser2net_yaml()` / `socat_argv()` emitters that generate those configs on demand from the one registry. |

## 2. The interop keystone

`socket_harness.session.LineChannel` is **byte-identical** to
`pyverify.client.Transport` — same three methods (`send_line(bytes)`,
`recv_line() -> bytes`, `close()`), same framing. That is deliberate: it
means a `LineSession` is a **drop-in `ShellClient` transport** with no
adapter:

```python
from socket_harness import SocketHarness

h = SocketHarness("192.168.10.101")
shell = h.shell()            # == ShellClient(host, transport=LineSession(...))
print(shell.ping())          # ping/swap/set_clk/macgen/display/diag ...
```

Because the transport underneath is a reconnecting `LineSession`, every
pyverify control verb gets connect-retry / reconnect *for free*, and the
structural equivalence is proven by a test that `isinstance`-checks a
`LineSession` against `pyverify.client.Transport`.

## 3. Endpoint registry

Reproduced from `endpoints.py` (the authoritative copy). `gated_by` uses the
same string values as `pyverify.edge.GatedBy` (`shell` / `rp` / `none`);
`survives_swap` is whether the endpoint stays up across a partial-reconfig
RM swap.

| name | family | framing | proto | port | csr_base | gated_by | survives_swap |
|---|---|---|---|---|---|---|---|
| `control` | CONTROL | JSON_LINE | tcp | 6900 | — | shell | yes |
| `push_tftp` | PUSH | TFTP | udp | 69 | — | shell | yes |
| `push_raw` | PUSH | RAW | tcp | 6910 | — | shell | yes |
| `xvc` | DEBUG_TOOL | XVC | tcp | 2542 | — | shell | yes |
| `swd` | DEBUG_TOOL | OPENOCD_RBB | tcp | 6920 | — | rp | no |
| `uart0` | CONSOLE | RAW | tcp | 6930 | — | shell | yes |
| `uart1` | CONSOLE | RAW | tcp | 6931 | — | rp | no |
| `swo` | CONSOLE | RAW | tcp | 6932 | — | rp | no |
| `hw_server` | JTAG | XSDB_REG | jtag | 3121 | — | none | yes |
| `mcc_console` | SERIAL | SERIAL | serial | — | — | none | no |
| `diag_mbox` | JTAG_REG | XSDB_REG | jtag | — | — | shell | yes |
| `clkrst` | CSR | XSDB_REG | jtag | — | `0x44A00000` | shell | yes |
| `dfxctl` | CSR | XSDB_REG | jtag | — | `0x44A10000` | shell | yes |
| `hwicap` | CSR | XSDB_REG | jtag | — | `0x44A20000` | shell | yes |
| `vphy` | CSR | XSDB_REG | jtag | — | `0x44A30000` | shell | yes |
| `usd` | CSR | XSDB_REG | jtag | — | `0x44A40000` | shell | yes |
| `telem` | CSR | XSDB_REG | jtag | — | `0x44A50000` | shell | yes |
| `genchk` | CSR | XSDB_REG | jtag | — | `0x44A60000` | shell | yes |
| `swdbb` | CSR | XSDB_REG | jtag | — | `0x44A70000` | shell | yes |
| `dbgbr` | CSR | XSDB_REG | jtag | — | `0x44A80000` | shell | yes |
| `uartbr` | CSR | XSDB_REG | jtag | — | `0x44A90000` | shell | yes |
| `gpio` | CSR | XSDB_REG | jtag | — | `0x44AA0000` | shell | yes |
| `mmcm_drp` | CSR | XSDB_REG | jtag | — | `0x44AB0000` | shell | yes |
| `clcd` | CSR | XSDB_REG | jtag | — | `0x44AC0000` | shell | yes |
| `clcdkvm` | CSR | XSDB_REG | jtag | — | `0x44AD0000` | shell | yes |

`mcc_console`'s `/dev/ttyUSB6` default is the **harness Linux/MicroBlaze
console (uartlite)** reached over the FT4232H, *not* the board's MCC `Cmd>`
prompt — those are separate links into the USB-serial chip. Do not expect MCC
commands on it.

`vphy`, `genchk` and `clcdkvm` **are all instantiated** on the shipped static
(`fpga/shell/bd/shell_bd.tcl`), and `gen_checker`'s DUT-IP sniffer is fielded —
an older "RESERVED / not instantiated" note here was wrong. They still answer
only when the *shell* is live and the relevant DUT is loaded, so
`RegisterAccess`/`probe` must report an honest failure rather than a plausible
zero — which is what `probe` does.

## 4. CLI usage

`python -m socket_harness <subcommand>` (the board-free replacement for
`socat_consoles.sh` / `mps3_diag.tcl` / `mps3_console.py`):

```sh
# The registry, board-free:
python -m socket_harness endpoints [--json]

# Emit a ser2net / socat config from the one registry (board-free):
python -m socket_harness emit ser2net [--host H]
python -m socket_harness emit socat   [--host H]

# Bridge a shell console to a local pty (or a listen socket):
python -m socket_harness console uart0 --pty /tmp/mps3-console/uart0 --host H
python -m socket_harness console swo   --listen 7000 --host H

# CSR ops via xsdb; --dry-run previews the argv/tcl with NO subprocess:
python -m socket_harness reg dfxctl.RM_STATUS read  --dry-run
python -m socket_harness reg genchk.INJECT   write giant=1 --dry-run
python -m socket_harness reg dfxctl.RM_ID     read --hub-url tcp:...:3121

# One-line honest liveness (never fabricates a plausible zero):
python -m socket_harness probe control --host H

# All board-free proofs (pty loopback, retry schedule, decode/encode,
# xsdb parse, session reassembly, harness interop):
python -m socket_harness selftest
```

Exit-code discipline follows `pyverify.cli`: a clean one-line stderr, never
a traceback. `0` = ok, `1` = endpoint declined or selftest failed, `2` =
usage error, `3` = unreachable.

## 5. Board-free discipline

Every module is unit-tested with **in-process fakes** (`loopback.py`) or a
**pty loopback** — zero real sockets to a board, no Vivado, no lease, no
JTAG. Openers, clocks, and subprocess runners are all injected so the tests
drive fakes.

The design honours the traps documented in `scripts/mps3_diag.tcl`:

- **Never `stop`/`con` a running MicroBlaze mid-swap.** `xsdb.write_word`
  emits only `mwr`; halting the MB resets the TCP stream and corrupts a
  partial swap. Tests assert the emitted tcl contains neither `stop` nor
  `con`.
- **The hub URL must be an FQDN.** `DEFAULT_HUB_URL =
  "tcp:<hub-fqdn>:3121"` — xsdb requires the fully
  qualified name. This is intentionally **different** from
  `pyverify.swd.DEFAULT_HUB` (the bare `<hub-host>`); the drift tests
  assert **ports and CSR bases**, never the hub spelling.
- **Ascending LMB-alias scan.** `XsdbConfig.candidates` is
  `(0x0003FF80, 0x0007FF80, 0x000FFF80)` in **ascending** order: on a 256 KB
  shell `0x0007FF80` aliases back to `0x0003FF80`, so a newest-first scan
  would report the wrong base. Targets renumber between sessions, so the
  diag mailbox is identified by its `magic` word, not by index.

The `ser2net_yaml()` / `socat_argv()` emitters produce a **valid** config
from the registry — they were never claimed to be byte-identical to the
hand-written examples they replaced (`host/console/`, since retired). Tests
only substring-check that the outputs contain the ports (6930/6931/6932) and
the `/tmp/mps3-console/<name>` pty links. This also resolves the "`pty`
accepter syntax not yet confirmed" caveat those examples carried: the
pure-Python `ConsoleBridge` is a fallback that needs no ser2net install at all.
