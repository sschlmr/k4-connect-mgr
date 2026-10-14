"""The calendar page served by `k4cm calendar` (see calendar.py).

One self-contained HTML document: no framework, no CDN, nothing to bundle.
`__TOKEN__` is replaced with the per-run token the page must send with every
API call. Times are shown and entered in the browser's local zone, and each
booking is sent with that zone's IANA name.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K4 Calendar</title>
<style>
:root {
  --bg: #f6f7f9; --surface: #ffffff; --text: #1d2330; --muted: #5d6677; --line: #e1e5ec;
  --line-strong: #c9cfd9; --accent: #2563eb; --accent-text: #ffffff; --other: #e8ecf3;
  --other-text: #394254; --other-line: #c3cad6; --maint: #8a93a3; --danger: #c2410c;
  --danger-soft: #fdecdf; --ok-soft: #e6f4ea; --now: #dc2626; --shadow: 0 8px 28px rgba(15, 23, 42, .18);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --surface: #1b1f27; --text: #e6e9ef; --muted: #9aa3b2; --line: #2a303b;
    --line-strong: #3a4250; --accent: #4f8cff; --accent-text: #0b1020; --other: #262c37;
    --other-text: #c9d1de; --other-line: #3b4352; --maint: #6b7383; --danger: #fb923c;
    --danger-soft: #3a2416; --ok-soft: #173024; --now: #f87171; --shadow: 0 8px 28px rgba(0, 0, 0, .5);
  }
}
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
button, select, input { font: inherit; color: inherit; }
button { cursor: pointer; border: 1px solid var(--line-strong); background: var(--surface); border-radius: 8px; padding: 6px 12px; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; }
button.danger { color: var(--danger); }
button:disabled { opacity: .5; cursor: default; }
select, input[type=date], input[type=time], input[type=number], input[type=text] {
  border: 1px solid var(--line-strong); border-radius: 8px; padding: 5px 8px; background: var(--surface);
}
header { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 16px; background: var(--surface); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
header h1 { font-size: 16px; margin: 0 12px 0 0; }
.seg { display: inline-flex; border: 1px solid var(--line-strong); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; border-radius: 0; padding: 6px 12px; }
.seg button.on { background: var(--accent); color: var(--accent-text); }
.range { font-weight: 600; min-width: 200px; }
.spacer { flex: 1; }
.who { color: var(--muted); }
#notices { padding: 0 16px; }
.notice { margin: 8px 0 0; padding: 8px 12px; border-radius: 8px; background: var(--danger-soft); display: flex; gap: 12px; align-items: start; }
.notice span { flex: 1; }
.grid { display: grid; margin: 12px 16px 24px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.heads { display: contents; }
.head { padding: 8px 6px; text-align: center; border-bottom: 1px solid var(--line); border-left: 1px solid var(--line); font-weight: 600; cursor: pointer; }
.head small { display: block; font-weight: 400; color: var(--muted); }
.head.today { color: var(--accent); }
.corner { border-bottom: 1px solid var(--line); }
.scroller { grid-column: 1 / -1; display: grid; max-height: calc(100vh - 150px); overflow-y: auto; }
.gutter { position: relative; }
.gutter div { height: var(--hour); font-size: 11px; color: var(--muted); text-align: right; padding-right: 6px; transform: translateY(-7px); }
.col { position: relative; border-left: 1px solid var(--line); height: calc(var(--hour) * 24); cursor: crosshair;
  background-image: repeating-linear-gradient(to bottom, transparent 0, transparent calc(var(--hour) - 1px), var(--line) calc(var(--hour) - 1px), var(--line) var(--hour)); }
.blk { position: absolute; left: 3px; right: 3px; border-radius: 6px; padding: 3px 6px; font-size: 12px; overflow: hidden; cursor: pointer; border: 1px solid transparent; user-select: none; }
.blk b { display: block; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.blk.mine { background: var(--accent); color: var(--accent-text); }
.blk.other { background: var(--other); color: var(--other-text); border-color: var(--other-line); }
.blk.maint { color: var(--text); border-color: var(--maint); background: repeating-linear-gradient(45deg, var(--other), var(--other) 6px, transparent 6px, transparent 12px); }
.blk.ghost { background: color-mix(in srgb, var(--accent) 25%, transparent); border: 2px dashed var(--accent); color: var(--text); pointer-events: none; }
.blk.ghost.bad { background: var(--danger-soft); border-color: var(--danger); }
.blk.dragging { opacity: .35; }
.now { position: absolute; left: 0; right: 0; height: 2px; background: var(--now); pointer-events: none; z-index: 2; }
.now::before { content: ""; position: absolute; left: -4px; top: -3px; width: 8px; height: 8px; border-radius: 50%; background: var(--now); }
dialog { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); color: var(--text); box-shadow: var(--shadow); padding: 0; width: min(560px, calc(100vw - 32px)); }
dialog::backdrop { background: rgba(15, 23, 42, .35); }
.dlg { padding: 18px 20px; display: grid; gap: 12px; }
.dlg h2 { margin: 0; font-size: 17px; }
.row { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; }
.row > label:first-child, .lbl { width: 84px; color: var(--muted); }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { min-width: 34px; padding: 3px 6px; border-radius: 6px; font-size: 12px; }
.chip.on { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
.posrow { display: flex; gap: 6px; margin-bottom: 4px; }
.preview { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font-size: 13px; max-height: 180px; overflow-y: auto; background: var(--bg); }
.preview .skip { color: var(--danger); }
.preview .sum { font-weight: 600; margin-bottom: 4px; }
.err { color: var(--danger); }
.actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
.kv { display: grid; grid-template-columns: 96px 1fr; gap: 4px 12px; }
.kv div:nth-child(odd) { color: var(--muted); }
#toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); padding: 10px 16px; border-radius: 8px; box-shadow: var(--shadow); display: none; z-index: 20; max-width: calc(100vw - 32px); }
.hidden { display: none !important; }
</style>
</head>
<body>
<header>
  <h1>K4 Calendar</h1>
  <div class="seg" id="viewSeg"><button data-v="week">Week</button><button data-v="day">Day</button></div>
  <select id="radioPick" title="Radio"></select>
  <button id="prev" aria-label="Previous">&#8249;</button>
  <button id="today">Today</button>
  <button id="next" aria-label="Next">&#8250;</button>
  <span class="range" id="range"></span>
  <span class="spacer"></span>
  <div class="seg" id="filterSeg"><button data-f="all">Everyone</button><button data-f="mine">Mine</button></div>
  <span class="who" id="who"></span>
  <button class="primary" id="newBtn">New booking</button>
</header>
<div id="notices"></div>
<div id="cal"></div>
<div id="toast"></div>

<dialog id="bookDlg"><form class="dlg" method="dialog" id="bookForm">
  <h2 id="bookTitle">New booking</h2>
  <div class="row" id="scopeRow"><span class="lbl">Change</span>
    <select id="fScope"><option value="this">This date only</option><option value="following">This and later dates</option><option value="all">The whole series</option></select></div>
  <div class="row"><label for="fRadio">Radio</label><select id="fRadio"></select></div>
  <div class="row"><label for="fDate">Start</label><input type="date" id="fDate" required><input type="time" id="fTime" step="900" required></div>
  <div class="row"><label for="fH">Duration</label><input type="number" id="fH" min="0" max="999" style="width:70px"> h <input type="number" id="fM" min="0" max="59" step="15" style="width:70px"> min</div>
  <div class="row" id="modeRow"><span class="lbl">Mode</span>
    <label><input type="radio" name="mode" value="reserve" checked> Reserve</label>
    <label><input type="radio" name="mode" value="auto"> Auto-session</label></div>
  <div class="row hidden" id="adminRow"><span class="lbl">Admin</span>
    <label><input type="checkbox" id="fMaint"> Being serviced</label>
    <label><input type="checkbox" id="fOverride"> Book over members</label></div>
  <div id="repeatBox">
    <div class="row"><label for="fRep">Repeat</label>
      <select id="fRep"><option value="none">Does not repeat</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="mdate">Monthly, on dates</option><option value="mpos">Monthly, on a weekday</option><option value="yearly">Yearly</option></select>
      <span id="everyWrap">every <input type="number" id="fEvery" min="1" max="99" value="1" style="width:60px"> <span id="everyUnit">weeks</span></span></div>
    <div class="row" id="wdRow"><span class="lbl">On</span><div class="chips" id="wdChips"></div></div>
    <div class="row" id="mdRow"><span class="lbl">On the</span><div class="chips" id="mdChips"></div></div>
    <div class="row" id="mpRow"><span class="lbl">On the</span><div><div id="posRows"></div><button type="button" id="addPos">+ another</button></div></div>
    <div class="row" id="endRow"><span class="lbl">Ends</span>
      <label><input type="radio" name="ends" value="count" checked> After <input type="number" id="fCount" min="1" max="999" value="10" style="width:70px"> dates</label>
      <label><input type="radio" name="ends" value="until"> On <input type="date" id="fUntil"></label></div>
  </div>
  <div class="preview" id="preview">Fill in the details to see the dates.</div>
  <div class="actions"><button type="button" id="bookCancel">Cancel</button><button type="submit" class="primary" id="bookOk">Book</button></div>
</form></dialog>

<dialog id="infoDlg"><div class="dlg">
  <h2 id="infoTitle"></h2>
  <div class="kv" id="infoKv"></div>
  <div class="actions" id="infoActions"></div>
</div></dialog>

<dialog id="scopeDlg"><div class="dlg">
  <h2 id="scopeTitle">Which dates?</h2>
  <p id="scopeNote" class="who"></p>
  <div class="actions"><button type="button" data-s="cancel">Cancel</button><button type="button" data-s="this">This date</button><button type="button" data-s="following">This and later</button><button type="button" data-s="all">Whole series</button></div>
</div></dialog>

<script>
"use strict";
const TOKEN = "__TOKEN__";
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const SNAP = 15;
const WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WD_NAME = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const ORD = [["1st", "1st"], ["2nd", "2nd"], ["3rd", "3rd"], ["4th", "4th"], ["last", "Last"]];
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

const state = { view: "week", radio: null, anchor: startOfDay(new Date()), filter: "all", me: null, radios: [], occs: [], busy: false };
const hourPx = () => parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--hour")) || 44;
document.documentElement.style.setProperty("--hour", "44px");

// -- dates ---------------------------------------------------------------
function pad(n) { return String(n).padStart(2, "0"); }
function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function mondayOf(d) { const x = startOfDay(d); return addDays(x, -((x.getDay() + 6) % 7)); }
function ymd(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function hm(d) { return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function wall(d) { return `${ymd(d)} ${hm(d)}`; }
function fmtDay(d) { return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }); }
function fmtFull(d) { return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }); }
function fmtSpan(a, b) { return `${fmtFull(a)} ${hm(a)}–${a.toDateString() === b.toDateString() ? hm(b) : fmtDay(b) + " " + hm(b)}`; }
function parseWall(date, time) { const [y, m, d] = date.split("-").map(Number); const [h, mi] = time.split(":").map(Number); return new Date(y, m - 1, d, h, mi); }

// -- api -----------------------------------------------------------------
async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method, headers: { "X-K4CM-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) { const err = new Error((data && data.detail) || `HTTP ${res.status}`); err.data = data; throw err; }
  return data;
}
let toastTimer = null;
function toast(msg) { const t = $("toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.style.display = "none"), 6000); }

// -- loading -------------------------------------------------------------
function rangeDays() { return state.view === "week" ? [mondayOf(state.anchor), 7] : [startOfDay(state.anchor), 1]; }
async function load() {
  const [first, n] = rangeDays();
  try {
    const data = await api("GET", `/bookings?start=${encodeURIComponent(first.toISOString())}&end=${encodeURIComponent(addDays(first, n).toISOString())}`);
    state.occs = data.occurrences;
  } catch (e) { toast(e.message); }
  render();
}
async function loadNotices() {
  try {
    const data = await api("GET", "/notices");
    for (const n of data.notices) {
      const box = el("div", "notice"); box.append(el("span", null, n.message));
      const x = el("button", null, "Dismiss"); x.onclick = () => box.remove(); box.append(x);
      $("notices").append(box);
    }
  } catch (e) { /* the next poll retries */ }
}

// -- rendering -----------------------------------------------------------
function columns() {
  const [first, n] = rangeDays();
  if (state.view === "week") return Array.from({ length: n }, (_, i) => ({ day: addDays(first, i), radio: state.radio }));
  return state.radios.filter((r) => r.enabled).map((r) => ({ day: first, radio: r.id }));
}
function radioName(id) { const r = state.radios.find((x) => x.id === id); return r ? r.name : id; }
function canEdit(o) { return state.me.is_admin || (o.owner === state.me.callsign && o.kind === "booking"); }
function visible(o) { return state.filter === "all" || (o.owner === state.me.callsign && o.kind === "booking"); }

function render() {
  const cols = columns();
  const [first, n] = rangeDays();
  $("range").textContent = state.view === "week" ? `${fmtDay(first)} – ${fmtFull(addDays(first, n - 1))}` : fmtFull(first);
  $("radioPick").classList.toggle("hidden", state.view !== "week");
  for (const b of $("viewSeg").children) b.classList.toggle("on", b.dataset.v === state.view);
  for (const b of $("filterSeg").children) b.classList.toggle("on", b.dataset.f === state.filter);

  const cal = $("cal"); cal.textContent = "";
  const grid = el("div", "grid"); grid.style.gridTemplateColumns = `52px repeat(${cols.length}, minmax(90px, 1fr))`;
  grid.append(el("div", "corner"));
  const todayKey = ymd(new Date());
  cols.forEach((c) => {
    const h = el("div", "head");
    if (state.view === "week") {
      h.textContent = fmtDay(c.day); if (ymd(c.day) === todayKey) h.classList.add("today");
      h.title = "Show all radios on this day"; h.onclick = () => { state.view = "day"; state.anchor = c.day; load(); };
    } else {
      h.textContent = radioName(c.radio); h.append(el("small", null, c.radio));
      h.title = "Show this radio's week"; h.onclick = () => { state.view = "week"; state.radio = c.radio; $("radioPick").value = c.radio; load(); };
    }
    grid.append(h);
  });
  const scroller = el("div", "scroller"); scroller.style.gridTemplateColumns = grid.style.gridTemplateColumns;
  const gutter = el("div", "gutter");
  for (let i = 0; i < 24; i++) gutter.append(el("div", null, i ? `${pad(i)}:00` : ""));
  scroller.append(gutter);
  const px = hourPx() / 60;
  const now = new Date();
  cols.forEach((c, index) => {
    const col = el("div", "col"); col.dataset.index = index;
    const dayEnd = addDays(c.day, 1);
    for (const o of state.occs) {
      if (o.radio.id !== c.radio || !visible(o)) continue;
      const s = new Date(o.start), e = new Date(o.end);
      if (!(s < dayEnd && c.day < e)) continue;
      const top = Math.max(0, (s - c.day) / 60000), bottom = Math.min(1440, (e - c.day) / 60000);
      const mine = o.owner === state.me.callsign && o.kind === "booking";
      const b = el("div", "blk " + (o.kind === "maintenance" ? "maint" : mine ? "mine" : "other"));
      b.style.top = `${top * px}px`; b.style.height = `${Math.max(18, (bottom - top) * px - 2)}px`;
      b.append(el("b", null, o.label + (o.mode === "auto" && o.kind === "booking" ? " · auto" : "")));
      b.append(el("span", null, `${hm(s)}–${hm(e)}${o.series ? " ↻" : ""}`));
      b.title = `${o.label} · ${radioName(o.radio.id)} · ${fmtSpan(s, e)}`;
      b.addEventListener("pointerdown", (ev) => startMove(ev, o, index, b));
      col.append(b);
    }
    if (now >= c.day && now < dayEnd) { const line = el("div", "now"); line.style.top = `${((now - c.day) / 60000) * px}px`; col.append(line); }
    col.addEventListener("pointerdown", (ev) => { if (ev.target === col) startCreate(ev, index); });
    scroller.append(col);
  });
  grid.append(scroller); cal.append(grid);
  if (state.scrollTop == null) state.scrollTop = 7 * hourPx();
  scroller.scrollTop = state.scrollTop;
  scroller.addEventListener("scroll", () => (state.scrollTop = scroller.scrollTop));
}

// -- dragging --------------------------------------------------------------
let drag = null;
function colAt(x, y) { const hit = document.elementFromPoint(x, y); const c = hit && hit.closest(".col"); return c ? Number(c.dataset.index) : null; }
function timeAt(index, clientY) {
  const colEl = document.querySelector(`.col[data-index="${index}"]`);
  const rect = colEl.getBoundingClientRect();
  let mins = ((clientY - rect.top) / hourPx()) * 60;
  mins = Math.max(0, Math.min(1440, Math.round(mins / SNAP) * SNAP));
  return new Date(columns()[index].day.getTime() + mins * 60000);
}
function clashes(radio, s, e, ignore) {
  return state.occs.find((o) => o.radio.id === radio && !(ignore && o.booking_id === ignore.booking_id && o.date === ignore.date)
    && new Date(o.start) < e && s < new Date(o.end));
}
function drawGhost(index, s, e, bad) {
  document.querySelectorAll(".blk.ghost").forEach((g) => g.remove());
  const c = columns()[index]; const colEl = document.querySelector(`.col[data-index="${index}"]`);
  const px = hourPx() / 60, top = Math.max(0, (s - c.day) / 60000), bottom = Math.min(1440, (e - c.day) / 60000);
  if (bottom <= top) return;
  const g = el("div", "blk ghost" + (bad ? " bad" : ""));
  g.style.top = `${top * px}px`; g.style.height = `${(bottom - top) * px}px`;
  g.append(el("b", null, bad ? "Already booked" : `${hm(s)}–${hm(e)}`));
  colEl.append(g);
}
function startCreate(ev, index) {
  if (ev.button !== 0) return;
  ev.preventDefault();
  const t0 = timeAt(index, ev.clientY);
  drag = { mode: "create", index, t0, s: t0, e: new Date(t0.getTime() + 60 * 60000), moved: false };
  state.busy = true;
}
function startMove(ev, occ, index, blk) {
  if (ev.button !== 0) return;
  ev.preventDefault(); ev.stopPropagation();
  const s = new Date(occ.start), e = new Date(occ.end);
  drag = { mode: "move", occ, blk, index, offset: timeAt(index, ev.clientY) - s, len: e - s, s, e, x: ev.clientX, y: ev.clientY, moved: false };
  state.busy = true;
}
document.addEventListener("pointermove", (ev) => {
  if (!drag) return;
  if (drag.mode === "create") {
    const t = timeAt(drag.index, ev.clientY);
    if (Math.abs(t - drag.t0) >= SNAP * 60000) drag.moved = true;
    if (!drag.moved) return;
    drag.s = t < drag.t0 ? t : drag.t0; drag.e = t < drag.t0 ? drag.t0 : t;
    const radio = columns()[drag.index].radio;
    drawGhost(drag.index, drag.s, drag.e, !!clashes(radio, drag.s, drag.e));
  } else {
    if (!canEdit(drag.occ)) return;
    if (!drag.moved && Math.hypot(ev.clientX - drag.x, ev.clientY - drag.y) < 6) return;
    drag.moved = true; drag.blk.classList.add("dragging");
    const index = colAt(ev.clientX, ev.clientY); if (index == null) return;
    drag.index = index;
    const s = new Date(Math.round((timeAt(index, ev.clientY) - drag.offset) / (SNAP * 60000)) * SNAP * 60000);
    drag.s = s; drag.e = new Date(s.getTime() + drag.len);
    const radio = columns()[index].radio;
    drag.bad = !!clashes(radio, drag.s, drag.e, drag.occ);
    drawGhost(index, drag.s, drag.e, drag.bad);
  }
});
document.addEventListener("pointerup", async () => {
  if (!drag) return;
  const d = drag; drag = null;
  document.querySelectorAll(".blk.ghost").forEach((g) => g.remove());
  if (d.blk) d.blk.classList.remove("dragging");
  state.busy = false;
  const radio = columns()[d.index].radio;
  if (d.mode === "create") {
    const s = d.s, e = d.moved ? d.e : new Date(d.t0.getTime() + 60 * 60000);
    if (d.moved && clashes(radio, s, e)) { toast("That time is already booked."); return; }
    openBooking({ radio, start: s, minutes: Math.max(SNAP, (e - s) / 60000) });
  } else if (!d.moved) {
    openInfo(d.occ);
  } else if (d.bad) {
    toast("That time is already booked.");
  } else {
    await moveOccurrence(d.occ, d.s, radio);
  }
});

async function moveOccurrence(o, s, radio) {
  const sameDay = ymd(s) === ymd(new Date(o.start));
  try {
    if (!o.series) {
      await api("PATCH", `/bookings/${o.booking_id}`, { scope: "all", start: wall(s), ...(radio !== o.radio.id ? { radio } : {}) });
    } else {
      if (radio !== o.radio.id) { toast("To move a series to another radio, edit it and choose the whole series."); return; }
      const scope = await askScope("Move which dates?", sameDay ? "" : "Moving to another day applies to this date only; for the series, only the time of day changes.");
      if (scope === "cancel") return;
      let body;
      if (scope === "this") body = { scope, date: o.date, start: wall(s) };
      else {
        const b = await api("GET", `/bookings/${o.booking_id}`);
        const day = scope === "all" ? b.start_local.slice(0, 10) : o.date;
        body = { scope, date: o.date, start: `${day} ${hm(s)}` };
      }
      const r = await api("PATCH", `/bookings/${o.booking_id}`, body);
      if (r.skipped && r.skipped.length) toast(r.summary);
    }
  } catch (e) { toast(e.message); }
  load();
}

function askScope(title, note) {
  return new Promise((resolve) => {
    $("scopeTitle").textContent = title; $("scopeNote").textContent = note || "";
    const dlg = $("scopeDlg");
    for (const b of dlg.querySelectorAll("button[data-s]")) b.onclick = () => { dlg.close(); resolve(b.dataset.s); };
    dlg.oncancel = () => resolve("cancel");
    dlg.showModal();
  });
}

// -- booking details --------------------------------------------------------
async function openInfo(o) {
  const s = new Date(o.start), e = new Date(o.end);
  $("infoTitle").textContent = o.kind === "maintenance" ? `${radioName(o.radio.id)}: Being serviced` : `${o.label} · ${radioName(o.radio.id)}`;
  const kv = $("infoKv"); kv.textContent = "";
  const add = (k, v) => { kv.append(el("div", null, k), el("div", null, v)); };
  add("When", fmtSpan(s, e));
  if (o.kind === "booking") add("Mode", o.mode === "auto" ? "Auto-session (opens at the start time)" : "Reserve (held for the owner)");
  add("Booking", `#${o.booking_id}`);
  const acts = $("infoActions"); acts.textContent = "";
  const close = el("button", null, "Close"); close.type = "button"; close.onclick = () => $("infoDlg").close();
  if (o.series) {
    try { const b = await api("GET", `/bookings/${o.booking_id}`); add("Repeats", b.repeat); o._booking = b; } catch (err) { /* shown without */ }
  }
  if (canEdit(o)) {
    const edit = el("button", null, "Edit…"); edit.type = "button";
    edit.onclick = async () => { $("infoDlg").close(); const b = o._booking || await api("GET", `/bookings/${o.booking_id}`); openBooking({ edit: o, booking: b }); };
    const del = el("button", "danger", o.series ? "Delete…" : "Delete"); del.type = "button";
    del.onclick = async () => {
      $("infoDlg").close();
      const scope = o.series ? await askScope("Delete which dates?", "") : (confirm("Delete this booking?") ? "all" : "cancel");
      if (scope === "cancel") return;
      try { const r = await api("DELETE", `/bookings/${o.booking_id}?scope=${scope}&date=${o.date}`); toast(r.detail); } catch (err) { toast(err.message); }
      load();
    };
    acts.append(del, edit);
  }
  acts.append(close);
  $("infoDlg").showModal();
}

// -- booking dialog ---------------------------------------------------------
const form = { edit: null, booking: null, repeatDirty: false };
function buildChips() {
  const wd = $("wdChips"); wd.textContent = "";
  for (const d of WD) { const b = el("button", "chip", WD_NAME[d]); b.type = "button"; b.dataset.v = d; b.onclick = () => { b.classList.toggle("on"); preview(); }; wd.append(b); }
  const md = $("mdChips"); md.textContent = "";
  for (let i = 1; i <= 28; i++) { const b = el("button", "chip", String(i)); b.type = "button"; b.dataset.v = String(i); b.onclick = () => { b.classList.toggle("on"); preview(); }; md.append(b); }
  const last = el("button", "chip", "Last day"); last.type = "button"; last.dataset.v = "last"; last.onclick = () => { last.classList.toggle("on"); preview(); }; md.append(last);
}
function addPosRow(ord, wd) {
  const row = el("div", "posrow");
  const o = el("select"); for (const [v, t] of ORD) { const op = el("option", null, t); op.value = v; o.append(op); }
  const w = el("select"); for (const d of WD) { const op = el("option", null, WD_NAME[d]); op.value = d; w.append(op); }
  o.value = ord || "1st"; w.value = wd || "tue";
  const x = el("button", null, "Remove"); x.type = "button"; x.onclick = () => { row.remove(); preview(); };
  o.onchange = w.onchange = preview;
  row.append(o, w, x); $("posRows").append(row);
}
function defaultOn() {
  // A newly chosen pattern starts from the start date: its weekday, its date
  // (up to the 28th), or its position in the month ("3rd Fri").
  if (!$("fDate").value) return;
  const d = parseWall($("fDate").value, "00:00");
  const wd = WD[(d.getDay() + 6) % 7];
  const rep = $("fRep").value;
  if (rep === "weekly" && !$("wdChips").querySelector(".on")) $("wdChips").querySelector(`[data-v="${wd}"]`).classList.add("on");
  if (rep === "mdate" && !$("mdChips").querySelector(".on")) {
    const last = addDays(d, 1).getDate() === 1;
    const chip = $("mdChips").querySelector(`[data-v="${last ? "last" : d.getDate() <= 28 ? d.getDate() : "last"}"]`);
    chip.classList.add("on");
  }
  if (rep === "mpos" && !form.edit) {
    const lastWeek = addDays(d, 7).getMonth() !== d.getMonth();
    const nth = Math.ceil(d.getDate() / 7);
    $("posRows").textContent = "";
    addPosRow(lastWeek && nth > 4 ? "last" : ORD[Math.min(nth, 4) - 1][0], wd);
  }
}
function syncRepeatUi() {
  const rep = $("fRep").value;
  $("everyWrap").classList.toggle("hidden", rep === "none");
  $("everyUnit").textContent = { daily: "days", weekly: "weeks", mdate: "months", mpos: "months", yearly: "years" }[rep] || "";
  $("wdRow").classList.toggle("hidden", rep !== "weekly");
  $("mdRow").classList.toggle("hidden", rep !== "mdate");
  $("mpRow").classList.toggle("hidden", rep !== "mpos");
  $("endRow").classList.toggle("hidden", rep === "none");
}
function repeatSpec() {
  const rep = $("fRep").value;
  if (rep === "none") return null;
  const spec = { freq: rep === "mdate" || rep === "mpos" ? "monthly" : rep, every: Number($("fEvery").value) || 1, on: [] };
  if (rep === "weekly") spec.on = [...$("wdChips").querySelectorAll(".on")].map((b) => b.dataset.v);
  if (rep === "mdate") spec.on = [...$("mdChips").querySelectorAll(".on")].map((b) => b.dataset.v);
  if (rep === "mpos") spec.on = [...$("posRows").children].map((r) => `${r.children[0].value}-${r.children[1].value}`);
  if (document.querySelector('input[name=ends]:checked').value === "count") spec.count = Number($("fCount").value) || 1;
  else spec.until = $("fUntil").value;
  return spec;
}
function fillRepeat(rule) {
  buildChips(); $("posRows").textContent = "";
  $("fRep").value = "none"; $("fEvery").value = 1; $("fCount").value = 10; $("fUntil").value = "";
  document.querySelector('input[name=ends][value=count]').checked = true;
  if (!rule) { addPosRow(); syncRepeatUi(); return; }
  const f = Object.fromEntries(rule.split(";").map((p) => p.split("=")));
  const codes = { MO: "mon", TU: "tue", WE: "wed", TH: "thu", FR: "fri", SA: "sat", SU: "sun" };
  $("fEvery").value = f.INTERVAL || 1;
  if (f.FREQ === "WEEKLY") {
    $("fRep").value = "weekly";
    for (const c of (f.BYDAY || "").split(",")) { const b = $("wdChips").querySelector(`[data-v="${codes[c]}"]`); if (b) b.classList.add("on"); }
  } else if (f.FREQ === "MONTHLY" && f.BYMONTHDAY) {
    $("fRep").value = "mdate";
    for (const d of f.BYMONTHDAY.split(",")) { const b = $("mdChips").querySelector(`[data-v="${d === "-1" ? "last" : d}"]`); if (b) b.classList.add("on"); }
  } else if (f.FREQ === "MONTHLY") {
    $("fRep").value = "mpos";
    for (const c of (f.BYDAY || "").split(",")) {
      const pos = c.slice(0, -2), wd = codes[c.slice(-2)];
      addPosRow({ "1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "-1": "last" }[pos] || "1st", wd);
    }
  } else { $("fRep").value = f.FREQ.toLowerCase(); }
  if (!$("posRows").children.length) addPosRow();
  if (f.COUNT) $("fCount").value = f.COUNT;
  if (f.UNTIL) { $("fUntil").value = `${f.UNTIL.slice(0, 4)}-${f.UNTIL.slice(4, 6)}-${f.UNTIL.slice(6, 8)}`; document.querySelector('input[name=ends][value=until]').checked = true; }
  syncRepeatUi();
}

function openBooking(opts) {
  form.edit = opts.edit || null; form.booking = opts.booking || null; form.repeatDirty = false;
  const radioSel = $("fRadio"); radioSel.textContent = "";
  for (const r of state.radios) { const op = el("option", null, r.name); op.value = r.id; op.disabled = !r.enabled; radioSel.append(op); }
  $("adminRow").classList.toggle("hidden", !state.me.is_admin || !!form.edit);
  $("fMaint").checked = false; $("fOverride").checked = false;
  const editing = !!form.edit, series = editing && !!form.booking.rrule;
  $("scopeRow").classList.toggle("hidden", !series);
  $("bookTitle").textContent = editing ? `Edit booking #${form.edit.booking_id}` : "New booking";
  $("bookOk").textContent = editing ? "Save" : "Book";
  let start, minutes, radio, mode, rule;
  if (editing) {
    start = new Date(form.edit.start); minutes = (new Date(form.edit.end) - start) / 60000;
    radio = form.edit.radio.id; mode = form.booking.mode; rule = form.booking.rrule;
    $("fScope").value = series ? "this" : "all";
  } else {
    start = opts.start || new Date(Math.ceil(Date.now() / 3600000) * 3600000);
    minutes = opts.minutes || 60; radio = opts.radio || state.radio; mode = "reserve"; rule = null;
  }
  radioSel.value = radio;
  $("fDate").value = ymd(start); $("fTime").value = hm(start);
  $("fH").value = Math.floor(minutes / 60); $("fM").value = Math.round(minutes % 60);
  document.querySelector(`input[name=mode][value=${mode}]`).checked = true;
  $("modeRow").classList.toggle("hidden", editing && form.booking.kind === "maintenance");
  fillRepeat(rule);
  syncScopeUi();
  $("preview").textContent = "Fill in the details to see the dates.";
  $("bookDlg").showModal();
  preview();
}
function syncScopeUi() {
  const scope = form.edit && form.booking.rrule ? $("fScope").value : "all";
  const single = form.edit && scope === "this";
  $("repeatBox").classList.toggle("hidden", !!single);
  $("fRadio").disabled = !!single;
  for (const r of document.querySelectorAll("input[name=mode]")) r.disabled = !!single;
  if (form.edit && form.booking.rrule) {
    const s = new Date(form.edit.start);
    $("fDate").value = scope === "all" ? form.booking.start_local.slice(0, 10) : ymd(s);
  }
}
function formBody() {
  const minutes = (Number($("fH").value) || 0) * 60 + (Number($("fM").value) || 0);
  if (!$("fDate").value || !$("fTime").value || minutes <= 0) throw new Error("Give a start and a duration.");
  const body = { radio: $("fRadio").value, start: `${$("fDate").value} ${$("fTime").value}`, duration: `${minutes}m`, tz: TZ,
    mode: document.querySelector("input[name=mode]:checked").value };
  if (!form.edit) {
    body.repeat = repeatSpec();
    if (state.me.is_admin && $("fMaint").checked) body.kind = "maintenance";
    if (state.me.is_admin && $("fOverride").checked) body.override = true;
    return body;
  }
  const scope = form.booking.rrule ? $("fScope").value : "all";
  const edit = { scope, date: form.edit.date, start: body.start, duration: body.duration };
  if (scope !== "this") {
    if (body.radio !== form.booking.radio.id) edit.radio = body.radio;
    if (form.booking.kind === "booking" && body.mode !== form.booking.mode) edit.mode = body.mode;
    const spec = repeatSpec();
    if (!spec && form.booking.rrule) throw new Error("A series can't be turned into a single booking; delete the later dates instead.");
    // Only a changed pattern is sent: resending the stored count would give
    // the later half of a split series the whole series' length.
    if (spec && (form.repeatDirty || !form.booking.rrule)) edit.repeat = spec;
  }
  return edit;
}
let previewTimer = null, previewSeq = 0;
function preview() { clearTimeout(previewTimer); previewTimer = setTimeout(runPreview, 350); }
async function runPreview() {
  const box = $("preview"); const seq = ++previewSeq;
  let body;
  try { body = formBody(); } catch (e) { box.textContent = e.message; return; }
  try {
    const r = form.edit ? await api("PATCH", `/bookings/${form.edit.booking_id}?dry_run=true`, body) : await api("POST", "/bookings?dry_run=true", body);
    if (seq !== previewSeq) return;
    box.textContent = "";
    const live = r.dates.filter((d) => !d.skipped);
    const sum = el("div", "sum", r.total > 1 ? `${r.booked} of ${r.total} dates` + (r.last ? `, last on ${fmtFull(new Date(r.last))}` : "") : (r.booked ? "This time is free." : ""));
    box.append(sum);
    for (const d of r.dates.slice(0, 12)) {
      const line = el("div", d.skipped ? "skip" : null, fmtSpan(new Date(d.start), new Date(d.end)) + (d.skipped ? ` — skipped, held by ${d.blocked_by}` : ""));
      box.append(line);
    }
    if (r.dates.length > 12) box.append(el("div", "who", `…and ${r.dates.length - 12} more`));
    for (const a of r.affected || []) box.append(el("div", "skip", `Removes ${a.owner}'s booking ${fmtSpan(new Date(a.start), new Date(a.end))}`));
    $("bookOk").disabled = live.length === 0;
  } catch (e) {
    if (seq !== previewSeq) return;
    box.textContent = ""; box.append(el("div", "err", e.message)); $("bookOk").disabled = true;
  }
}
$("bookForm").addEventListener("input", preview);
$("bookForm").addEventListener("change", (ev) => { if (ev.target.id === "fRep") { defaultOn(); syncRepeatUi(); } if (ev.target.id === "fScope") syncScopeUi(); preview(); });
$("addPos").onclick = () => { addPosRow(); preview(); };
for (const type of ["input", "change", "click"]) $("repeatBox").addEventListener(type, () => (form.repeatDirty = true));
$("bookCancel").onclick = () => $("bookDlg").close();
$("bookForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const body = formBody();
    const r = form.edit ? await api("PATCH", `/bookings/${form.edit.booking_id}`, body) : await api("POST", "/bookings", body);
    $("bookDlg").close(); toast(r.summary.replace(/^Booked/, form.edit ? "Saved" : "Booked"));
    load();
  } catch (e) { $("preview").textContent = ""; $("preview").append(el("div", "err", e.message)); }
});

// -- toolbar ---------------------------------------------------------------
$("viewSeg").onclick = (ev) => { const v = ev.target.dataset.v; if (v) { state.view = v; load(); } };
$("filterSeg").onclick = (ev) => { const f = ev.target.dataset.f; if (f) { state.filter = f; render(); } };
$("radioPick").onchange = () => { state.radio = $("radioPick").value; render(); };
$("prev").onclick = () => { state.anchor = addDays(state.anchor, state.view === "week" ? -7 : -1); load(); };
$("next").onclick = () => { state.anchor = addDays(state.anchor, state.view === "week" ? 7 : 1); load(); };
$("today").onclick = () => { state.anchor = startOfDay(new Date()); load(); };
$("newBtn").onclick = () => openBooking({ radio: state.radio });

async function init() {
  try {
    const ctx = await api("GET", "/context");
    state.me = ctx.me; state.radios = ctx.radios;
    state.radio = (ctx.radios.find((r) => r.enabled) || ctx.radios[0] || {}).id;
    $("who").textContent = `${ctx.me.callsign}${ctx.me.is_admin ? " (admin)" : ""} · ${TZ}`;
    const pick = $("radioPick");
    for (const r of ctx.radios) { const op = el("option", null, r.name); op.value = r.id; pick.append(op); }
    pick.value = state.radio;
  } catch (e) { toast(e.message); return; }
  await load(); loadNotices();
  setInterval(() => { if (!state.busy && !document.querySelector("dialog[open]")) load(); }, 30000);
  setInterval(loadNotices, 60000);
}
init();
</script>
</body>
</html>
"""
