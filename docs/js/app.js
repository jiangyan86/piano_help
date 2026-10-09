// Page logic: score picker, microphone, sheet music with marks, beat cursor.
import { Session } from "./tracker.js";
import { Engine } from "./engine.js";
import { startMic, replayFile } from "./audio.js";
import * as store from "./store.js";

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const COL = { missing: "#f5a524", bad: "#e5484d", skipped: "#8e4ec6" };
const ARROW = String.fromCharCode(0x2192);

const session = new Session(true);
let engine = null;
let input = null;
let wakeLock = null;
let xmlText = "";
let osmd = null;
let GN = new Map();
let lastRecs = [];
let lastSig = "";
let LAY = [];
let layId = -1;
let boxCache = new Map();
let CUR = null;
let curLocal = 0;
let qd = null;
let lastScroll = 0;
let lastFrame = 0;
let saveTimer = null;

// ---------------------------------------------------------------------------------------- sheet music
function buildIndex() {
  GN = new Map();
  osmd.GraphicSheet.MeasureList.forEach((staves, mi) => staves.forEach((gm) => {
    if (!gm) return;
    gm.staffEntries.forEach((se) => {
      const onset = se.sourceStaffEntry.VerticalContainerParent.Timestamp.RealValue * 4;
      se.graphicalVoiceEntries.forEach((gve) => gve.notes.forEach((gn) => {
        const n = gn.sourceNote;
        if (!n.Pitch) return;
        const g = gn.getSVGGElement();
        if (!g) return;
        const key = mi + ":" + Math.round(onset * 1000) / 1000;
        if (!GN.has(key)) GN.set(key, []);
        GN.get(key).push({ g, midi: n.Pitch.getHalfTone() + 12 });
      }));
    });
  }));
}

function render() {
  osmd.render();
  buildIndex();
  boxCache = new Map();
  apply(lastRecs);
}

async function showScore() {
  GN = new Map();
  osmd = null;
  if (!xmlText) { $("sheet").textContent = "Choose a music file to begin."; return; }
  $("sheet").innerHTML = "";
  osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay("sheet", { autoResize: false, drawTitle: false, drawPartNames: false });
  await osmd.load(xmlText);
  await new Promise((r) => setTimeout(r, 150));
  lastRecs = [];
  render();
}

function clearMarks() {
  document.querySelectorAll("#sheet [data-mark]").forEach((e) => { e.removeAttribute("data-mark"); e.style.fill = ""; e.style.stroke = ""; });
  document.querySelectorAll("#sheet .mlabel").forEach((e) => e.remove());
}
function paint(g, color) {
  const heads = g.querySelectorAll(".vf-notehead path, .vf-notehead");
  (heads.length ? heads : g.querySelectorAll("path")).forEach((p) => { p.style.fill = color; p.style.stroke = color; p.dataset.mark = 1; });
}
function label(g, text, color) {
  const b = g.getBBox();
  const t = document.createElementNS(NS, "text");
  t.setAttribute("class", "mlabel");
  t.setAttribute("x", b.x - 2);
  t.setAttribute("y", b.y - 14);
  t.setAttribute("fill", color);
  t.setAttribute("font-size", "14");
  t.setAttribute("font-weight", "bold");
  t.textContent = text;
  g.appendChild(t);
}
function apply(recs) {
  lastRecs = recs;
  clearMarks();
  for (const r of recs) {
    if (r.status === "skipped" && !$("showskip").checked) continue;
    const notes = GN.get(r.mi + ":" + Math.round(r.onset * 1000) / 1000);
    if (!notes || !notes.length) continue;
    const labs = [];
    if (r.status === "skipped") { notes.forEach((n) => paint(n.g, COL.skipped)); continue; }
    for (const n of notes) {
      if (r.missing.includes(n.midi)) paint(n.g, COL.missing);
      for (const [played, exp] of r.subs) if (exp === n.midi) { paint(n.g, COL.bad); labs.push(ARROW + played); }
    }
    r.extra.forEach((e) => labs.push("+" + e));
    if (labs.length) {
      if (!r.subs.length) paint(notes[0].g, COL.bad);
      label(notes[0].g, labs.join(" "), COL.bad);
    }
  }
}
function list(recs) {
  if (!$("showskip").checked) recs = recs.filter((r) => r.status !== "skipped");
  $("count").textContent = recs.length;
  $("list").innerHTML = recs.map((r) => {
    const cls = r.status === "skipped" ? "skipped" : (r.missing.length && !r.extra.length && !r.subs.length ? "missing" : "") + (r.retried ? " retried" : "");
    return '<div class="item ' + cls + '" data-k="' + r.mi + ":" + Math.round(r.onset * 1000) / 1000 + '"><b>m.' + r.measure + " beat " + r.beat +
      (r.retried ? " (fixed on retry)" : "") + "</b>expected " + r.expected.join(" ") + "<br><small>" + r.text + "</small></div>";
  }).join("") || "<small>No mistakes so far.</small>";
}
$("list").addEventListener("click", (e) => {
  const it = e.target.closest(".item");
  if (!it) return;
  const n = (GN.get(it.dataset.k) || [])[0];
  if (n) n.g.scrollIntoView({ block: "center", behavior: "smooth" });
});

// ---------------------------------------------------------------------------------------- beat cursor
function box(i) {
  if (i < 0 || i >= LAY.length) return null;
  if (boxCache.has(i)) return boxCache.get(i);
  const c = LAY[i];
  const notes = GN.get(c.mi + ":" + Math.round(c.onset * 1000) / 1000);
  let out = null;
  if (notes && notes.length) {
    const w = $("wrap").getBoundingClientRect();
    const wr = $("wrap");
    const rs = notes.map((n) => n.g.getBoundingClientRect());
    const st = [...document.querySelectorAll("#sheet svg .vf-measure")].filter((e) => e.id === String(c.measure)).map((e) => e.getBoundingClientRect());
    const ref = st.length ? st : rs;
    const ox = -w.left + wr.scrollLeft;
    const oy = -w.top + wr.scrollTop;
    out = {
      q: c.q,
      x: (Math.min(...rs.map((r) => r.left)) + Math.max(...rs.map((r) => r.right))) / 2 + ox,
      top: Math.min(...ref.map((r) => r.top)) - 6 + oy,
      bot: Math.max(...ref.map((r) => r.bottom)) + 6 + oy,
    };
  }
  boxCache.set(i, out);
  return out;
}
function qToPos(q) {
  let lo = 0;
  let hi = LAY.length - 1;
  if (hi < 0) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (LAY[mid].q <= q) lo = mid; else hi = mid - 1;
  }
  const A = box(lo);
  if (!A) return null;
  const B = box(lo + 1);
  if (B && Math.abs(B.top - A.top) < 40 && B.q > A.q) {
    const f = Math.min(1, Math.max(0, (q - A.q) / (B.q - A.q)));
    return { x: A.x + f * (B.x - A.x), top: A.top, bot: A.bot };
  }
  return { x: A.x, top: A.top, bot: A.bot };
}
// The cursor does not chase each heard note: it runs at your recent tempo and is pulled towards the
// tempo-fitted position with a time constant of about one measure, so it converges instead of jumping.
function frame() {
  const now = performance.now();
  const dt = Math.min(0.1, (now - lastFrame) / 1000 || 0.016);
  lastFrame = now;
  const m = $("marker");
  if (!CUR || !GN.size || !LAY.length) { m.style.display = "none"; return; }
  const target = Math.min(CUR.q + (CUR.r * (now - curLocal)) / 1000, CUR.qlast + CUR.qlen);
  const tau = Math.min(6, Math.max(1.5, CUR.qlen / CUR.r));
  if (qd === null || Math.abs(target - qd) > 2 * CUR.qlen) qd = target;
  else { const v = Math.max(0, CUR.r + (target - qd) / tau); qd += v * dt; }
  const p = qToPos(qd);
  if (!p) { m.style.display = "none"; return; }
  m.style.display = "block";
  m.style.left = p.x - 2 + "px";
  m.style.width = "4px";
  m.style.top = p.top + "px";
  m.style.height = p.bot - p.top + "px";
  const r = m.getBoundingClientRect();
  if ((r.top < 70 || r.bottom > innerHeight - 10) && now - lastScroll > 1500) {
    lastScroll = now;
    m.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}
function setCursor(c) {
  if (!c) { CUR = null; qd = null; return; }
  CUR = c;
  curLocal = performance.now();
}

// ---------------------------------------------------------------------------------------- state -> page
function tick() {
  const s = session.toState();
  $("status").textContent = s.status + (s.next && s.running ? "  •  next: " + s.next.label : "") + (s.running ? "  •  " + s.pos + "/" + s.total : "");
  $("lvl").style.width = s.level * 100 + "%";
  $("heard").textContent = s.heard && s.heard.length ? "heard: " + s.heard.join(" ") : "";
  $("stop").disabled = !s.running;
  const sig = JSON.stringify(s.recs);
  if (sig !== lastSig && GN.size) { lastSig = sig; apply(s.recs); list(s.recs); }
  if (s.layoutId !== layId) { LAY = session.layout(); layId = s.layoutId; boxCache = new Map(); }
  setCursor(s.running ? s.cursor : null);
}

session.onChange = () => {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { if (session.scoreName) store.putRun(session.scoreName, session.snapshot()); }, 400);
};

// ---------------------------------------------------------------------------------------- scores
async function refreshList() {
  const names = await store.listScores();
  const sel = $("score");
  sel.innerHTML = (session.scoreName ? "" : '<option value="">choose a file...</option>') +
    names.map((n) => '<option value="' + n.replace(/"/g, "&quot;") + '"' + (n === session.scoreName ? " selected" : "") + ">" + n + "</option>").join("");
}

async function useScore(name, xml) {
  stopInput();
  try {
    session.setScore(name, xml);
  } catch (e) {
    $("status").textContent = "Could not read that file: " + e.message;
    return false;
  }
  xmlText = xml;
  await store.putScore(name, xml);
  localStorage.setItem("lastScore", name);
  lastSig = "";
  CUR = null;
  qd = null;
  layId = -1;
  const snap = await store.getRun(name);
  if (snap) session.restore(snap);
  await refreshList();
  try { await showScore(); } catch (e) { $("sheet").textContent = "Could not draw sheet music: " + e; }
  list([]);
  return true;
}

async function readScoreFile(file) {
  const name = file.name;
  if (name.toLowerCase().endsWith(".mxl")) {
    const files = fflate.unzipSync(new Uint8Array(await file.arrayBuffer()));
    const key = Object.keys(files).find((k) => k.endsWith(".xml") && !k.startsWith("META-INF"));
    if (!key) throw new Error("no MusicXML inside the .mxl file");
    return fflate.strFromU8(files[key]);
  }
  return await file.text();
}

// ---------------------------------------------------------------------------------------- audio
function stopInput() {
  if (input) { input.stop(); input = null; }
  engine = null;
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  if (session.running) session.stop();
}

async function startPractice(file) {
  if (!xmlText) { $("status").textContent = "Choose a music file first."; return; }
  stopInput();
  try {
    session.newPractice($("range").value);
  } catch (e) {
    $("status").textContent = e.message;
    return;
  }
  lastSig = "";
  list([]);
  const onChunk = (x) => { if (engine) engine.push(x); };
  try {
    input = file ? await replayFile(file, onChunk) : await startMic(onChunk);
  } catch (e) {
    $("status").textContent = "Could not use the microphone: " + (e.message || e) + ". Allow microphone access for this page and try again.";
    return;
  }
  engine = new Engine(session, input.sr, { gate: parseFloat(localStorage.getItem("gate") || "0.001") });
  session.running = true;
  session.status = file ? "Replaying " + file.name + "..." : "Listening... play from the start (or any point)";
  if (file) input.done.then(() => { setTimeout(() => { if (input) stopInput(); }, 3500); });
  try { wakeLock = await navigator.wakeLock.request("screen"); } catch { /* not available */ }
}

// ---------------------------------------------------------------------------------------- wiring
$("start").onclick = () => startPractice(null);
$("stop").onclick = () => stopInput();
$("showskip").onchange = () => { apply(lastRecs); list(lastRecs); };
$("score").onchange = async () => {
  const name = $("score").value;
  if (!name) return;
  const rec = await store.getScore(name);
  if (rec) await useScore(name, rec.xml);
};
$("open").onclick = () => $("file").click();
$("file").onchange = async () => {
  const f = $("file").files[0];
  if (!f) return;
  try { await useScore(f.name, await readScoreFile(f)); } catch (e) { $("status").textContent = "Could not read that file: " + e.message; }
  $("file").value = "";
};
$("replay").onclick = () => $("audiofile").click();
$("audiofile").onchange = async () => {
  const f = $("audiofile").files[0];
  if (f) await startPractice(f);
  $("audiofile").value = "";
};
let resizeTimer;
addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (osmd) render(); }, 400); });
setInterval(frame, 33); // a timer rather than requestAnimationFrame: keeps running when the tab is not focused
setInterval(tick, 200);

(async function init() {
  let names = await store.listScores();
  if (!names.length) {
    try {
      const r = await fetch("samples/c_major_scale.musicxml");
      if (r.ok) await store.putScore("c_major_scale.musicxml", await r.text());
    } catch { /* offline: no sample */ }
    names = await store.listScores();
  }
  const last = localStorage.getItem("lastScore");
  const first = names.includes(last) ? last : names[0];
  if (first) {
    const rec = await store.getScore(first);
    await useScore(first, rec.xml);
  } else {
    await refreshList();
    await showScore();
  }
})();
