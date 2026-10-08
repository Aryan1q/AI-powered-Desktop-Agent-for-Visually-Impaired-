"""A self-contained control panel, served as one HTML string.

No build step, no CDN, no framework: one file, works offline, and is
keyboard-navigable and screen-reader friendly — which matters, because the
person most likely to need an automation agent is the person least able to use
a dense grid of buttons.

It talks to the same JSON API as everything else, so there is exactly one
behaviour to reason about.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..core.memory import get_memory
from ..core.queue import TaskQueue
from ..core.scheduler import Scheduler
from ..core.task import load_history

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UFO control panel</title>
<style>
  :root {
    --bg: #12141a; --panel: #1b1e26; --line: #2b303c; --text: #e6e9ef;
    --muted: #9aa3b2; --accent: #4c8dff; --ok: #3fbf7f; --warn: #e0a33e;
    --bad: #e05561;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f5f6f8; --panel:#fff; --line:#dde1e8; --text:#14171d;
            --muted:#5b6472; --accent:#2563eb; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         background:var(--bg); color:var(--text); }
  header { padding:14px 20px; border-bottom:1px solid var(--line);
           display:flex; gap:16px; align-items:center; flex-wrap:wrap;
           position:sticky; top:0; background:var(--panel); z-index:5; }
  h1 { font-size:17px; margin:0; letter-spacing:.3px; }
  main { display:grid; gap:16px; padding:16px 20px 40px;
         grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
  section { background:var(--panel); border:1px solid var(--line); border-radius:10px;
            padding:14px 16px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.8px;
       color:var(--muted); margin:0 0 10px; }
  button { font:inherit; padding:7px 12px; border-radius:7px; border:1px solid var(--line);
           background:var(--bg); color:var(--text); cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button:focus-visible, input:focus-visible, textarea:focus-visible {
      outline:2px solid var(--accent); outline-offset:2px; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.danger  { border-color:var(--bad); color:var(--bad); }
  input, textarea { font:inherit; width:100%; padding:8px 10px; border-radius:7px;
                    border:1px solid var(--line); background:var(--bg); color:var(--text); }
  textarea { min-height:64px; resize:vertical; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .row + .row { margin-top:8px; }
  .pill { font-size:12px; padding:2px 9px; border-radius:999px; border:1px solid var(--line);
          color:var(--muted); }
  .pill.ok { color:var(--ok); border-color:var(--ok); }
  .pill.bad { color:var(--bad); border-color:var(--bad); }
  .pill.warn { color:var(--warn); border-color:var(--warn); }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td, th { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line);
           vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  .shot { width:100%; border:1px solid var(--line); border-radius:8px; display:block; }
  .log { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size:12px;
         max-height:260px; overflow:auto; white-space:pre-wrap; }
  .muted { color:var(--muted); }
  .banner { padding:10px 20px; background:color-mix(in srgb, var(--bad) 18%, transparent);
            color:var(--text); display:none; }
  .banner.on { display:block; }
  ul { margin:6px 0 0; padding-left:18px; }
  .visually-hidden { position:absolute; width:1px; height:1px; overflow:hidden;
                     clip:rect(0 0 0 0); white-space:nowrap; }
</style>
</head>
<body>
<div class="banner" id="banner" role="alert"></div>
<header>
  <h1>UFO control panel</h1>
  <span class="pill" id="daemon">daemon: ?</span>
  <span class="pill" id="llm">model: ?</span>
  <span class="pill" id="queue">queue: ?</span>
  <span class="pill" id="watch">screens: ?</span>
  <span style="flex:1"></span>
  <button id="refresh">Refresh</button>
  <button class="primary" id="watchToggle">Watch screen</button>
  <button class="danger" id="panic">Emergency stop</button>
</header>

<main>
  <section>
    <h2>Run a task</h2>
    <label class="visually-hidden" for="goal">Task</label>
    <textarea id="goal" placeholder="e.g. summarise today's emails in Notepad"></textarea>
    <div class="row">
      <button class="primary" id="submit">Run now</button>
      <button id="queueBtn">Add to queue</button>
      <button id="planBtn">Preview plan</button>
      <label class="row"><input type="checkbox" id="dry" style="width:auto"> dry run</label>
    </div>
    <div class="row">
      <label class="visually-hidden" for="token">API token</label>
      <input id="token" type="password" placeholder="API token (X-UFO-Token)">
      <button id="saveToken">Save</button>
    </div>
    <p class="muted" style="font-size:12px">
      The token is in <code>~/.ufo/secrets.json</code> as <code>api_token</code>.
      It is required for anything that changes the computer.
    </p>
  </section>

  <section>
    <h2>Screen</h2>
    <img class="shot" id="shot" alt="Screenshot of the virtual desktop">
    <p class="muted" id="shotInfo" style="font-size:12px"></p>
  </section>

  <section>
    <h2>Status</h2>
    <table id="status"><tbody></tbody></table>
  </section>

  <section>
    <h2>Queue and recent runs</h2>
    <div class="row"><button id="cancelAll">Cancel everything</button></div>
    <table><thead><tr><th>id</th><th>status</th><th>task</th><th></th></tr></thead>
      <tbody id="tasks"></tbody></table>
  </section>

  <section>
    <h2>Schedules</h2>
    <div class="row">
      <input id="schWhen" placeholder="in 30 minutes / 0 9 * * 1-5">
      <input id="schTask" placeholder="task to run">
      <button id="schAdd">Add</button>
    </div>
    <ul id="schList"></ul>
  </section>

  <section>
    <h2>Memory</h2>
    <pre class="log" id="mem"></pre>
  </section>

  <section style="grid-column: 1 / -1;">
    <h2>Activity</h2>
    <pre class="log" id="log" role="log" aria-live="polite"></pre>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const tokenKey = "ufo.token";
$("token").value = localStorage.getItem(tokenKey) || "";
$("saveToken").onclick = () => localStorage.setItem(tokenKey, $("token").value.trim());

const logEl = $("log");
function log(msg) {
  const t = new Date().toLocaleTimeString();
  logEl.textContent = `[${t}] ${msg}\n` + logEl.textContent;
}
function banner(text) {
  const b = $("banner");
  b.textContent = text || "";
  b.classList.toggle("on", !!text);
}
function headers() {
  const t = localStorage.getItem(tokenKey) || "";
  return { "Content-Type": "application/json", "X-UFO-Token": t };
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: headers(), ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
async function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body || {}) });
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("daemon").textContent = "daemon: " + (s.daemon?.alive ? "running" : "stopped");
    $("daemon").className = "pill " + (s.daemon?.alive ? "ok" : "bad");
    $("llm").textContent = `model: ${s.llm?.provider}/${s.llm?.model || "?"}`;
    $("queue").textContent = "queue: " + (s.queue?.length ?? 0);
    $("queue").className = "pill " + ((s.queue?.length ?? 0) > 0 ? "warn" : "");
    banner(s.emergency ? "EMERGENCY STOP IS ACTIVE (" + s.emergency_hotkey + ")" : "");

    const rows = [
      ["Version", s.version],
      ["Open windows", `${s.windows?.allowed ?? 0} allowed of ${s.windows?.total ?? 0}`],
      ["Allowlist", (s.allowlist || []).join(", ") || "(none — nothing is permitted)"],
      ["Dry run", s.dry_run ? "ON" : "off"],
      ["Daemon uptime", s.daemon?.uptime_seconds ? s.daemon.uptime_seconds + "s" : "-"],
      ["Done / failed", `${s.daemon?.tasks_done ?? 0} / ${s.daemon?.tasks_failed ?? 0}`],
      ["Schedules", s.schedules ?? 0],
    ];
    $("status").querySelector("tbody").innerHTML = rows
      .map(([k, v]) => `<tr><th>${k}</th><td>${escapeHtml(String(v))}</td></tr>`).join("");

    const tasks = await api("/api/tasks");
    $("tasks").innerHTML = [...tasks.queued, ...tasks.recent].slice(0, 30).map(t => `
      <tr>
        <td><code>${t.id}</code></td>
        <td><span class="pill ${t.status === "succeeded" ? "ok" :
              t.status === "failed" ? "bad" : ""}">${t.status}</span></td>
        <td>${escapeHtml(t.goal)}</td>
        <td>${["pending", "running"].includes(t.status)
              ? `<button data-cancel="${t.id}">cancel</button>` : ""}</td>
      </tr>`).join("");
    $("tasks").querySelectorAll("[data-cancel]").forEach(b => b.onclick = async () => {
      try { await post("/api/tasks/cancel", { id: b.dataset.cancel }); log("cancelled " + b.dataset.cancel); refresh(); }
      catch (e) { log("cancel failed: " + e.message); }
    });

    const sch = await api("/api/schedule");
    $("schList").innerHTML = sch.schedules.length
      ? sch.schedules.map(s => `<li><code>${s.task_id}</code> — ${s.cron || s.repeat ||
          "one-shot"} — ${escapeHtml(s.goal)}
          <button data-rmsch="${s.task_id}">remove</button></li>`).join("")
      : "<li class='muted'>no schedules</li>";
    $("schList").querySelectorAll("[data-rmsch]").forEach(b => b.onclick = async () => {
      log("remove schedule " + b.dataset.rmsch + ": use `ufo schedule remove " + b.dataset.rmsch + "`");
    });

    const mem = await api("/api/memory");
    $("mem").textContent = (mem.brief || "(nothing remembered yet)") +
                           "\n\n" + JSON.stringify(mem.stats, null, 2);

    const screens = await api("/api/screens");
    $("shotInfo").textContent =
      `virtual desktop ${screens.virtual[2] - screens.virtual[0]}x` +
      `${screens.virtual[3] - screens.virtual[1]} at (${screens.virtual[0]},${screens.virtual[1]})` +
      ` — ${screens.monitors.length} monitor(s), DPI scale ${screens.dpi_scale}`;
  } catch (e) {
    log("status failed: " + e.message);
  }
}

let watching = true;
async function tickShot() {
  if (!watching) return;
  try {
    const s = await api("/api/screenshots?format=jpeg&quality=55");
    $("shot").src = "data:image/jpeg;base64," + s.data;
  } catch (e) { /* the panel keeps working without the live view */ }
  if (watching) setTimeout(tickShot, 2000);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("refresh").onclick = refresh;
$("watchToggle").onclick = () => {
  watching = !watching;
  $("watchToggle").textContent = watching ? "Watch screen" : "Watch (off)";
  if (watching) tickShot();
};

$("submit").onclick = async () => {
  const goal = $("goal").value.trim();
  if (!goal) { log("type a task first"); return; }
  try {
    const r = await post("/api/tasks", { task: goal, dry_run: $("dry").checked });
    log(`queued ${r.id}. Start the worker with:  ufo daemon start`);
    refresh();
  } catch (e) { log("submit failed: " + e.message); }
};
$("queueBtn").onclick = () => log("'Run now' and 'Add to queue' both queue the task; " +
    "use `ufo daemon start` to process the queue.");
$("planBtn").onclick = () => log("use `ufo plan \"" + $("goal").value + "\"` to preview a plan.");

$("cancelAll").onclick = async () => {
  try { const r = await post("/api/tasks/cancel", {}); log("cancelled " + r.cancelled); refresh(); }
  catch (e) { log("cancel failed: " + e.message); }
};

$("schAdd").onclick = async () => {
  const when = $("schWhen").value.trim(), task = $("schTask").value.trim();
  if (!when || !task) { log("both a time and a task are required"); return; }
  try {
    const cron = when.split(/\s+/).length === 5 ? when : "";
    const r = await post("/api/schedule", { when: cron ? "" : when, cron, task });
    log("scheduled " + r.task_id);
    refresh();
  } catch (e) { log("schedule failed: " + e.message); }
};

$("panic").onclick = async () => {
  if (!confirm("Trip the emergency stop? Any running agent will halt immediately.")) return;
  try { await post("/api/emergency", {}); log("emergency stop tripped"); refresh(); }
  catch (e) { log("emergency failed: " + e.message); }
};

refreshStatus();
tickShot();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""


def render(cfg: Config | None = None) -> dict:
    """The same data the dashboard polls, as plain Python — for scripts."""
    from ..core.daemon import status
    from ..perception import uia
    from ..utils import win32

    cfg = cfg or Config.load()
    windows = uia.list_all_windows()
    queue: TaskQueue = TaskQueue(cfg.storage.tasks_path).load()
    scheduler: Scheduler = Scheduler(cfg.storage.schedule_path).load()
    return {
        "version": _version(),
        "daemon": status(cfg),
        "queue": queue.stats(),
        "schedules": [s.to_dict() for s in scheduler.list()],
        "memory": get_memory(cfg.storage.memory_path).stats(),
        "history": [t.to_dict() for t in
                    load_history(cfg.storage.history_path, limit=20)],
        "windows": {
            "total": len(windows),
            "allowed": [w.title for w in windows
                        if uia.is_allowed(w, cfg.safety.allowlist)],
            "blocked": [w.title for w in windows
                        if not uia.is_allowed(w, cfg.safety.allowlist)],
        },
        "screens": {
            "virtual": list(win32.virtual_screen()),
            "monitors": [m.__dict__ for m in win32.monitors()],
        },
        "config_path": str(cfg.config_path) if cfg.config_path else None,
    }


def _version() -> str:
    from .. import __version__

    return __version__


def to_json(data: Any) -> str:
    from .server import _json_default

    return json.dumps(data, indent=2, default=_json_default)
