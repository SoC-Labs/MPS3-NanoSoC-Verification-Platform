"""``webharness.pages`` — the single-page UI.

One self-contained HTML document: inline CSS, inline JS, no CDN, no fonts, no
build step. It is served by :func:`webharness.api.handle` for ``GET /`` and
fetches everything else from the JSON API, so the page and a ``curl`` user see
exactly the same data.

**Rendering the page must never contact the board.** ``:6900`` is a
single-client channel, and a page load that opened a control connection before
any JavaScript ran would make simply *looking* at the dashboard contend with
``pyverify``. :func:`render_page` therefore touches only
``backend.info()`` — pure — and the browser then polls the API on a timer that
the backend's TTL cache collapses.
"""
from __future__ import annotations

from typing import Any

from . import control

__all__ = ["render_page"]


_CSS = """
:root{
  --bg:#f7f7f8; --panel:#fff; --ink:#16181d; --muted:#5f6672; --line:#e2e5ea;
  --ok:#1a7f43; --bad:#c02626; --warn:#9a6400; --accent:#2b5fd9; --code:#f0f2f5;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#101216; --panel:#181b21; --ink:#e8eaed; --muted:#98a0ad; --line:#2a2f38;
    --ok:#4fca7f; --bad:#ff6b6b; --warn:#e0b050; --accent:#7aa2f7; --code:#11141a;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
code,kbd,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;
  border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:20px}
h1{font-size:18px;margin:0;letter-spacing:.2px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:0 0 10px}
.sub{color:var(--muted);font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;
  font-weight:600;letter-spacing:.04em;border:1px solid var(--line)}
.badge.live{color:var(--ok);border-color:var(--ok)}
.badge.fake{color:var(--warn);border-color:var(--warn)}
.badge.down{color:var(--bad);border-color:var(--bad)}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card.full{grid-column:1/-1}
dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:6px 14px}
dt{color:var(--muted);white-space:nowrap}
dd{margin:0;text-align:right;word-break:break-all}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .muted{color:var(--muted)}
button{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);
  border-radius:7px;padding:6px 12px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
button.on{border-color:var(--accent);color:var(--accent);font-weight:600}
button.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.note{font-size:12px;color:var(--muted);margin-top:10px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:620px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
tr.absent td{opacity:.45}
td.cmd{width:46%}
.cmdline{display:flex;gap:6px;align-items:flex-start}
.cmdline code{background:var(--code);border:1px solid var(--line);border-radius:5px;
  padding:3px 7px;font-size:12px;flex:1;word-break:break-all}
.copy{padding:3px 8px;font-size:11px;flex:0 0 auto}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px}
.dot.up{background:var(--ok)} .dot.no{background:var(--muted)}
.banner{border:1px solid var(--bad);color:var(--bad);border-radius:8px;padding:9px 12px;
  margin-bottom:16px;font-size:13px}
.banner.hide{display:none}
details{margin-top:8px} summary{cursor:pointer;color:var(--muted);font-size:12px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px 14px;
  margin-top:10px;font-size:13px}
.kv div{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);
  padding-bottom:3px}
.kv span:first-child{color:var(--muted)}
footer{margin-top:26px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
"""

_JS = r"""
const $ = (s) => document.querySelector(s);
const HOST = window.__HOST__;
let statusTimer = null;

function txt(el, s, cls) {
  el.textContent = s;
  el.className = cls || "";
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { body = {ok:false, err:"unparseable response"}; }
  return {status: r.status, body};
}

function offline(on, msg) {
  const b = $("#banner");
  b.classList.toggle("hide", !on);
  if (on) b.textContent = msg;
}

/* ---- status ---------------------------------------------------------- */
async function refreshStatus() {
  const {body} = await api("/api/status");
  const reachable = body.shell && body.shell.reachable;
  offline(!reachable, "Shell unreachable — " + ((body.shell && body.shell.err) || body.err || "no answer on :6900"));

  txt($("#shell-state"), reachable ? "reachable" : "unreachable", reachable ? "ok" : "bad");
  txt($("#shell-id"), (body.shell && body.shell.static_id) || "—", "mono");

  const rm = body.rm || {};
  txt($("#rm-name"), rm.name || (rm.loaded ? "unnamed" : "none loaded"), rm.loaded ? "" : "muted");
  txt($("#rm-id"), rm.rm_id || "—", "mono");
  txt($("#rm-design"), rm.design || "—", "mono");
  txt($("#rm-caps"), rm.caps || "—", rm.caps ? "" : "muted");

  const t = body.telemetry || {};
  txt($("#power"), "no sensor", "muted");
  if (t.lockup === null || t.lockup === undefined) {
    txt($("#lockup"), "—", "muted");
  } else {
    txt($("#lockup"), t.lockup ? "LOCKED UP" : "no",
        t.lockup ? "bad" : (t.lockup_meaningful ? "ok" : "muted"));
  }
  $("#lockup-note").textContent = t.lockup_note || "";

  const st = $("#backend-badge");
  if (body.backend && !body.backend.live) { st.textContent = "FAKE"; st.className = "badge fake"; }
  else if (reachable) { st.textContent = "LIVE"; st.className = "badge live"; }
  else { st.textContent = "DOWN"; st.className = "badge down"; }

  refreshServices();
}

/* ---- services -------------------------------------------------------- */
async function refreshServices() {
  const {body} = await api("/api/services");
  const tb = $("#svc-body");
  tb.innerHTML = "";
  (body.services || []).forEach((s) => {
    const tr = document.createElement("tr");
    if (!s.present) tr.className = "absent";
    const dot = '<span class="dot ' + (s.present ? "up" : "no") + '"></span>';
    const port = s.proto.toUpperCase() + " " + s.port;
    tr.innerHTML =
      "<td>" + dot + s.label + '<div class="sub">' + s.note + "</div></td>" +
      '<td class="mono">' + port + "</td>" +
      '<td>' + (s.scope === "shell" ? "shell" : "DUT") + "</td>" +
      '<td class="cmd"><div class="cmdline"><code>' + s.command +
        '</code><button class="copy">copy</button></div></td>';
    tr.querySelector(".copy").addEventListener("click", (e) => copy(s.command, e.target));
    tb.appendChild(tr);
  });
  $("#svc-note").textContent = body.note || "";
}

function copy(text, btn) {
  const done = () => { const o = btn.textContent; btn.textContent = "copied"; setTimeout(() => btn.textContent = o, 1200); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else { fallbackCopy(text, done); }
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) {}
  document.body.removeChild(ta);
}

/* ---- clocks ---------------------------------------------------------- */
async function loadClocks() {
  const {body} = await api("/api/clocks");
  const row = $("#clk-buttons");
  row.innerHTML = "";
  (body.presets || []).forEach((p) => {
    const b = document.createElement("button");
    b.textContent = p.name + "  (" + p.mhz + " MHz)";
    b.title = "M=" + p.mult + " D=" + p.divclk + " O=" + p.clkout0_div +
              "  DUT_CLK_SEL id=" + p.id;
    b.addEventListener("click", () => setClock(p.name, b));
    row.appendChild(b);
  });
  const c = body.contract || {};
  $("#clk-caveat").textContent = c.caveat || "";
  $("#clk-mech").textContent = c.mechanism || "";
}

async function setClock(preset, btn) {
  const all = $("#clk-buttons").querySelectorAll("button");
  all.forEach((b) => b.disabled = true);
  txt($("#clk-result"), "setting " + preset + "…", "muted");
  const {body} = await api("/api/clock", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({preset}),
  });
  all.forEach((b) => { b.disabled = false; b.classList.remove("on"); });
  if (body.ok) {
    btn.classList.add("on");
    if (body.locked) txt($("#clk-result"), preset + " set — MMCM locked", "ok");
    else txt($("#clk-result"), preset + " set — " + (body.warn || "MMCM not locked"), "warn");
  } else {
    txt($("#clk-result"), "failed: " + (body.err || "unknown"), "bad");
  }
  refreshStatus();
}

/* ---- resets ---------------------------------------------------------- */
async function loadResets() {
  const {body} = await api("/api/resets");
  const tb = $("#rst-body");
  tb.innerHTML = "";
  (body.kinds || []).forEach((k) => {
    const tr = document.createElement("tr");
    if (!k.actionable) tr.className = "absent";
    tr.innerHTML =
      '<td class="mono">' + k.kind + "</td>" +
      "<td>" + (k.actionable ? "<b>this server</b>" : k.driver) + "</td>" +
      "<td>" + (k.invalidated_channels.length ? k.invalidated_channels.join(", ") : "—") + "</td>";
    tb.appendChild(tr);
  });
  $("#rst-note").textContent = body.note || "";
}

async function doReset(btn) {
  if (!confirm("Pulse CLKRST.RESET_CTRL.dut_resetn — reset the DUT now?")) return;
  btn.disabled = true;
  txt($("#rst-result"), "resetting…", "muted");
  const {body} = await api("/api/reset", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({target: "dut"}),
  });
  btn.disabled = false;
  if (body.ok) txt($("#rst-result"), "DUT reset pulsed", "ok");
  else txt($("#rst-result"), "failed: " + (body.err || "unknown"), "bad");
  refreshStatus();
}

/* ---- diag ------------------------------------------------------------ */
async function loadDiag() {
  const {body} = await api("/api/diag");
  const box = $("#diag-kv");
  box.innerHTML = "";
  if (!body.ok) {
    box.innerHTML = '<div><span>diag</span><span class="bad">' +
                    (body.err || "unavailable") + "</span></div>";
    return;
  }
  Object.keys(body).forEach((k) => {
    if (k === "ok" || k === "err" || k === "cached") return;
    const d = document.createElement("div");
    d.innerHTML = "<span>" + k + '</span><span class="mono">' + body[k] + "</span>";
    box.appendChild(d);
  });
}

/* ---- boot ------------------------------------------------------------ */
function boot() {
  $("#rst-go").addEventListener("click", (e) => doReset(e.target));
  $("#diag-refresh").addEventListener("click", loadDiag);
  loadClocks(); loadResets(); loadDiag();
  refreshStatus();
  statusTimer = setInterval(refreshStatus, 3000);
}
document.addEventListener("DOMContentLoaded", boot);
"""


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_page(*, backend: Any, host_label: str) -> str:
    """The whole dashboard as one HTML string.

    Only ``backend.info()`` is consulted — pure, no board contact (see the
    module docstring). Everything else arrives over the JSON API.
    """
    info = backend.info()
    mode_badge = "live" if info.live else "fake"
    mode_text = "LIVE" if info.live else "FAKE"
    presets = ", ".join(control.preset_names())

    return """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MPS3 nanoSoC harness</title>
<style>%(css)s</style>
</head><body>
<div class="wrap">

<header>
  <h1>MPS3 nanoSoC harness</h1>
  <span id="backend-badge" class="badge %(badge)s">%(mode)s</span>
  <span class="sub mono">%(target)s</span>
  <span class="sub">&middot; backend %(backend_mode)s</span>
</header>

<div id="banner" class="banner hide"></div>

<div class="grid">

  <div class="card">
    <h2>Shell</h2>
    <dl>
      <dt>control :6900</dt><dd id="shell-state">…</dd>
      <dt>static_id</dt><dd id="shell-id">…</dd>
    </dl>
    <div class="note">The static shell survives an RM swap; its services stay up.</div>
  </div>

  <div class="card">
    <h2>Resident RM (DUT)</h2>
    <dl>
      <dt>name</dt><dd id="rm-name">…</dd>
      <dt>rm_id</dt><dd id="rm-id">…</dd>
      <dt>design</dt><dd id="rm-design">…</dd>
      <dt>makeup</dt><dd id="rm-caps">…</dd>
    </dl>
  </div>

  <div class="card">
    <h2>Health</h2>
    <dl>
      <dt>power</dt><dd id="power">…</dd>
      <dt>dut_lockup</dt><dd id="lockup">…</dd>
    </dl>
    <div class="note" id="lockup-note"></div>
    <div class="note">There is no power sensor on this platform, by
      construction — not a fault to chase.</div>
  </div>

  <div class="card">
    <h2>DUT clock</h2>
    <div class="row" id="clk-buttons"></div>
    <div class="note" id="clk-result">presets: %(presets)s</div>
    <details>
      <summary>what this actually does</summary>
      <div class="note" id="clk-mech"></div>
      <div class="note" id="clk-caveat"></div>
    </details>
  </div>

  <div class="card">
    <h2>DUT reset</h2>
    <div class="row">
      <button id="rst-go" class="danger">Reset DUT</button>
      <span class="note" id="rst-result"></span>
    </div>
    <div class="note">Pulses <code>CLKRST.RESET_CTRL.dut_resetn</code> with a
      1&nbsp;ms hold (calibrated for the 3-FF sync chain at the slowest preset).</div>
  </div>

  <div class="card full">
    <h2>Applications &amp; ports</h2>
    <div class="tablewrap">
      <table>
        <thead><tr><th>service</th><th>port</th><th>owner</th><th>attach from Linux</th></tr></thead>
        <tbody id="svc-body"></tbody>
      </table>
    </div>
    <div class="note" id="svc-note"></div>
  </div>

  <div class="card full">
    <h2>Reset taxonomy</h2>
    <div class="tablewrap">
      <table>
        <thead><tr><th>kind</th><th>driven by</th><th>invalidates</th></tr></thead>
        <tbody id="rst-body"></tbody>
      </table>
    </div>
    <div class="note" id="rst-note"></div>
  </div>

  <div class="card full">
    <h2>Diagnostic counters
      <button id="diag-refresh" class="copy" style="float:right">refresh</button>
    </h2>
    <div class="kv" id="diag-kv"></div>
    <div class="note">Read over :6900, which is PARKED during a swap — these go
      stale mid-swap by design.</div>
  </div>

</div>

<footer>
  Served by <code>webharness</code> on the hub, over the frozen :6900 control
  channel. Same data as the JSON API: <code>/api/status</code>,
  <code>/api/services</code>, <code>/api/clocks</code>, <code>/api/resets</code>,
  <code>/api/diag</code>.
</footer>

</div>
<script>window.__HOST__ = "%(host)s";</script>
<script>%(js)s</script>
</body></html>
""" % {
        "css": _CSS,
        "js": _JS,
        "badge": mode_badge,
        "mode": mode_text,
        "target": _esc(info.target),
        "backend_mode": _esc(info.mode),
        "host": _esc(host_label),
        "presets": _esc(presets),
    }
