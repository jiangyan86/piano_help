// Follows you through the score and judges each measure once you have moved on (port of live_check.Session).
import { Chord, buildChords, loadScore, noteName } from "./score.js";
import { median, clip } from "./dsp.js";

const ARROW = String.fromCharCode(0x2192);

export function chordScore(ev, chord) {
  let ok = 0;
  for (const m of chord.notes) ok += ev.presence(m) * (ev.fresh(m) ? 1.0 : 0.4); // ringing is weak evidence
  return ok / (chord.notes.length + ev.extras(chord.notes).length);
}

const OVERTONES = [12, 19, 24, 28, 31, 34, 36];

/** Compare what was heard at one onset with chord i of the score. */
export function judge(chords, i, ev, t = null) {
  const c = chords[i];
  const r = { i, measure: c.measure, beat: c.beat + 1, expected: [...c.notes], missing: [], extra: [], subs: [], status: "ok", t };
  let missing = c.notes.filter((m) => !ev.present(m));
  const neighbours = new Set();
  for (const k of [i - 1, i + 1]) if (k >= 0 && k < chords.length) chords[k].notes.forEach((m) => neighbours.add(m));
  // ignore likely overtone/octave ghosts and bleed from adjacent chords (hands not exactly together)
  let extra = ev.extras(c.notes).filter((m) => !neighbours.has(m) && !c.notes.some((e) => OVERTONES.includes(Math.abs(m - e))));
  for (const e of [...extra]) {
    const near = missing.filter((m) => Math.abs(m - e) <= 2);
    if (near.length) {
      const m = near.reduce((a, b) => (Math.abs(b - e) < Math.abs(a - e) ? b : a));
      r.subs.push([e, m]);
      missing = missing.filter((x) => x !== m);
      extra = extra.filter((x) => x !== e);
    }
  }
  for (const e of [...extra]) { // a wrong key struck in place of an expected one that is silent
    if (missing.length) {
      const m = missing.reduce((a, b) => (Math.abs(b - e) < Math.abs(a - e) ? b : a));
      r.subs.push([e, m]);
      missing = missing.filter((x) => x !== m);
      extra = extra.filter((x) => x !== e);
    }
  }
  r.missing = missing;
  r.extra = extra;
  if (missing.length || extra.length || r.subs.length) r.status = "bad";
  return r;
}

export function describe(r) {
  if (r.status === "skipped") return "not heard";
  const nm = (ms) => ms.map(noteName).join(" ");
  const msg = r.subs.map(([e, m]) => "played " + noteName(e) + " instead of " + noteName(m));
  if (r.missing.length) msg.push("missing " + nm(r.missing));
  if (r.extra.length) msg.push("extra " + nm(r.extra));
  return msg.join("; ");
}

export class Session {
  constructor(jumps = true) {
    this.jumps = jumps;
    this.score = null;
    this.scoreName = null;
    this.nums = [];
    this.qlen = 4;
    this.audioNow = 0;
    this.running = false;
    this.level = 0;
    this.gain = 1;
    this.layoutId = 0;
    this.onChange = null;
    this.newPractice("");
    this.status = "Choose a music file";
  }

  setScore(name, xmlText) {
    const s = loadScore(xmlText);
    this.running = false;
    this.score = s;
    this.scoreName = name;
    this.nums = s.nums;
    this.qlen = s.qlen;
    this.newPractice("");
    this.status = "Loaded " + name + ". Press New practice, then play.";
  }

  newPractice(measures) {
    let mr = null;
    if (measures && measures.trim()) {
      const [lo, hi] = measures.trim().split("-");
      mr = [parseInt(lo, 10), parseInt(hi || lo, 10)];
      if (Number.isNaN(mr[0]) || Number.isNaN(mr[1])) throw new Error("measures should look like 5-12");
    }
    this.measures = measures || "";
    this.base = this.score ? buildChords(this.score.measures, this.jumps, mr) : [];
    this.chords = this.base;
    this.shift = 0;
    this.recs = new Map();
    this.pos = 0;
    this.evs = [];
    this.times = [];
    this.committed = null;
    this.track = new Map();
    this.cur = null;
    this.layoutId += 1;
    this.shiftLocked = false;
    this._bounds();
    this.nOnsets = 0;
    this.heard = [];
    this.status = this.base.length ? "Listening... play from the start (or any point)" : "Choose a music file";
  }

  _bounds() {
    const ch = this.chords;
    const ms = new Array(ch.length).fill(0);
    const me = new Array(ch.length).fill(0);
    let start = 0;
    for (let i = 0; i <= ch.length; i++) {
      if (i === ch.length || (i > 0 && (ch[i].measure !== ch[i - 1].measure || ch[i].beat < ch[i - 1].beat))) {
        for (let k = start; k < i; k++) { ms[k] = start; me[k] = i; }
        start = i;
      }
    }
    this.mstart = ms;
    this.mend = me;
    const q = [];
    let base = 0;
    ch.forEach((c, i) => {
      if (i > 0 && ms[i] !== ms[i - 1]) base += this.qlen;
      q.push(base + c.beat);
    });
    this.qpos = q;
  }

  _score(ev, i) {
    const key = this.shift + ":" + i;
    let v = ev.cs.get(key);
    if (v === undefined) { v = chordScore(ev, this.chords[i]); ev.cs.set(key, v); }
    return v;
  }

  _setShift(k) {
    this.shift = k;
    this.chords = k === 0 ? this.base : this.base.map((c) => new Chord(c.measure, c.beat, c.notes.map((m) => m + k)));
  }

  /** Order-preserving alignment of the collected onsets with chords lo..hi -> Map(chord -> onset index). */
  _align(lo, hi, anchored) {
    const k = this.evs.length;
    const n = hi - lo;
    if (k === 0 || n <= 0) return { pairs: new Map(), score: -1e9 };
    const S = this.evs.map((ev) => Array.from({ length: n }, (_, i) => 2 * this._score(ev, lo + i) - 0.8));
    const skip = -0.5;
    const H = Array.from({ length: n + 1 }, () => new Float64Array(k + 1));
    const P = Array.from({ length: n + 1 }, () => new Int8Array(k + 1));
    for (let i = 1; i <= n; i++) {
      H[i][0] = anchored ? i * skip : -i * 0.1; // un-anchored: mild preference for starting at the top
      P[i][0] = 1;
    }
    for (let j = 1; j <= k; j++) P[0][j] = 2;
    for (let i = 1; i <= n; i++) {
      for (let j = 1; j <= k; j++) {
        const c0 = H[i - 1][j - 1] + S[j - 1][i - 1];
        const c1 = H[i - 1][j] + skip;
        const c2 = H[i][j - 1] + skip;
        let a = 0;
        let v = c0;
        if (c1 > v) { a = 1; v = c1; }
        if (c2 > v) { a = 2; v = c2; }
        H[i][j] = v;
        P[i][j] = a;
      }
    }
    let bi = 0;
    for (let i = 1; i <= n; i++) if (H[i][k] > H[bi][k]) bi = i; // ties keep the smaller i
    let i = bi;
    let j = k;
    const pairs = new Map();
    while (i > 0 && j > 0) {
      const a = P[i][j];
      if (a === 0) { pairs.set(lo + i - 1, j - 1); i--; j--; }
      else if (a === 1) i--;
      else j--;
    }
    return { pairs, score: H[bi][k] };
  }

  _commit(a, b, pairs, cut) {
    for (let k = a; k < b; k++) {
      const c = this.chords[k];
      if (pairs.has(k)) this.recs.set(k, judge(this.chords, k, this.evs[pairs.get(k)], this.times[pairs.get(k)]));
      else this.recs.set(k, { i: k, measure: c.measure, beat: c.beat + 1, expected: [...c.notes], missing: [], extra: [], subs: [], status: "skipped", t: null });
    }
    this.evs = this.evs.slice(cut);
    this.times = this.times.slice(cut);
    this.committed = b;
  }

  handle(ev, t) {
    if (!this.chords.length) return;
    this.nOnsets += 1;
    this.heard = ev.detected().map(noteName);
    this.evs.push(ev);
    this.times.push(t);
    this._step(false);
  }

  _step(final) {
    const n = this.chords.length;
    while (this.evs.length) {
      const anchored = this.committed !== null;
      const lo = anchored ? this.committed : 0;
      let hi;
      if (anchored) hi = Math.min(n, this.mend[Math.min(lo, n - 1)] + 70);
      else {
        if (this.evs.length < 8 && !final) return; // too little history to tell repeated passages apart
        hi = n;
      }
      if (lo >= n) { this.evs = []; this.times = []; break; }
      let pairs;
      if (!anchored && this.shift === 0 && !this.shiftLocked) {
        // you may be playing the piece an octave or two away from what is written
        const res = new Map();
        for (const k of [0, 12, -12, 24, -24]) {
          this._setShift(k);
          res.set(k, this._align(lo, hi, false));
        }
        let bestK = 0;
        let bestV = -Infinity;
        for (const [k, r] of res) {
          const v = r.score - (k === 0 ? 0 : 2.5);
          if (v > bestV) { bestV = v; bestK = k; }
        }
        this._setShift(bestK);
        pairs = res.get(bestK).pairs;
        if (bestK) this.status = "Hearing you " + Math.abs(bestK) / 12 + " octave(s) " + (bestK > 0 ? "higher" : "lower") + " than written";
      } else {
        pairs = this._align(lo, hi, anchored).pairs;
      }
      if (!pairs.size) {
        if (final) { this.evs = []; this.times = []; }
        return;
      }
      const keys = [...pairs.keys()];
      const last = Math.max(...keys);
      for (const [k, j] of pairs) this.track.set(k, this.times[j]);
      if (this.cur === null || last >= this.cur[0]) this.cur = [last, this.times[pairs.get(last)]];
      this.pos = Math.max(this.pos, last + 1);
      const first = Math.min(...keys);
      const a = anchored ? lo : this.mstart[first];
      const b = this.mend[a];
      const beyond = keys.filter((k) => k >= b);
      if (beyond.length >= 2 && (anchored || pairs.size >= 10 || final)) {
        this.shiftLocked = true;
        const cut = Math.min(...beyond.map((k) => pairs.get(k)));
        this._commit(a, b, pairs, cut);
        continue;
      }
      if (final) { // judge everything up to the last chord that was heard
        this._commit(a, last + 1, pairs, this.evs.length);
        this.pos = last + 1;
      }
      return;
    }
    if (this.committed !== null && this.committed >= n) this.status = "Finished the piece.";
    if (this.onChange) this.onChange();
  }

  flush() {
    if (this.evs.length) this._step(true);
    if (this.onChange) this.onChange();
  }

  stop() {
    this.running = false;
    this.flush();
    this.status = "Stopped. Press New practice to start again.";
    if (this.onChange) this.onChange();
  }

  /** Tempo-following position: a robust line through the last ~2 measures of heard chords. */
  cursor() {
    if (this.cur === null) return null;
    const [k, tK] = this.cur;
    const idx = [...this.track.keys()].sort((a, b) => a - b).slice(-14).filter((i) => i <= k);
    const pts = idx.map((i) => [this.track.get(i), this.qpos[i]]);
    let r = 1 / 0.6;
    let a = this.qpos[k] - r * tK;
    if (pts.length >= 3) {
      const sl = [];
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          if (pts[j][0] - pts[i][0] > 0.05 && pts[j][1] > pts[i][1]) sl.push((pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0]));
        }
      }
      if (sl.length) {
        r = clip(median(sl), 0.5, 5.0);
        a = median(pts.map((p) => p[1] - r * p[0]));
      }
    }
    const qLast = this.qpos[k];
    const q = clip(a + r * this.audioNow, qLast - 0.5, qLast + this.qlen);
    return { q, r, qlast: qLast, qlen: this.qlen };
  }

  layout() {
    return this.chords.map((c, i) => ({
      q: this.qpos[i], mi: Math.max(0, this.nums.indexOf(c.measure)), onset: c.beat, measure: c.measure,
    }));
  }

  toState() {
    const recs = [];
    const sh = this.shift;
    for (const i of [...this.recs.keys()].sort((a, b) => a - b)) {
      const r = this.recs.get(i);
      if (r.status === "ok") continue;
      const r2 = { ...r, expected: r.expected.map((m) => m - sh), missing: r.missing.map((m) => m - sh), subs: r.subs.map(([e, m]) => [e, m - sh]) };
      recs.push({
        i, measure: r.measure, mi: Math.max(0, this.nums.indexOf(r.measure)), onset: Math.round((r.beat - 1) * 10000) / 10000,
        beat: r.beat, expected: r2.expected.map(noteName), expectedMidi: r2.expected, missing: r2.missing,
        extra: r.extra.map(noteName), subs: r2.subs.map(([e, m]) => [noteName(e), m]), status: r.status,
        retried: !!r.retried, text: describe(r2),
      });
    }
    let next = null;
    if (this.pos < this.chords.length) {
      const c = this.chords[this.pos];
      next = { label: "m." + c.measure + " beat " + (c.beat + 1) };
    }
    return {
      score: this.scoreName, layoutId: this.layoutId, cursor: this.running ? this.cursor() : null, running: this.running,
      status: this.status, level: this.level, gain: this.gain || 1, heard: this.heard, recs, next, total: this.chords.length, pos: this.pos,
      onsets: this.nOnsets,
    };
  }

  /** What is worth saving between visits (the raw judgements, so they can be shown again later). */
  snapshot() {
    return { score: this.scoreName, measures: this.measures, shift: this.shift, total: this.chords.length,
      recs: [...this.recs.entries()] };
  }

  restore(snap) {
    if (!snap || snap.score !== this.scoreName) return false;
    try { this.newPractice(snap.measures || ""); } catch { return false; }
    if (snap.total !== this.chords.length) return false;
    this._setShift(snap.shift || 0);
    this.recs = new Map(snap.recs);
    this.status = "Showing your last practice. Press New practice to start again.";
    return true;
  }
}
