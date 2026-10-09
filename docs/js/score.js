// MusicXML -> list of chords in the order they are played (follows D.S./D.C. al coda).
import { parseXML, child, kids, iter, intText } from "./xml.js";

export const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const STEP = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
export const noteName = (m) => NAMES[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1);

export class Chord {
  constructor(measure, beat, notes) {
    this.measure = measure;
    this.beat = beat;
    this.notes = [...notes].sort((a, b) => a - b);
  }
}

export function parseMeasures(root) {
  const merged = new Map();
  for (const part of kids(root, "part")) {
    let divisions = 1;
    kids(part, "measure").forEach((m, idx) => {
      if (!merged.has(idx)) merged.set(idx, { num: m.attrs.number, ons: new Map(), flags: {} });
      const d = merged.get(idx);
      let cur = 0;
      let lastOn = 0;
      for (const el of m.children) {
        if (el.tag === "attributes") {
          const dv = child(el, "divisions");
          if (dv) divisions = intText(dv);
        } else if (el.tag === "direction") {
          for (const s of iter(el, "sound")) {
            for (const [k, v] of Object.entries(s.attrs)) if (k !== "dynamics") d.flags[k] = v;
          }
        } else if (el.tag === "backup") {
          cur -= intText(child(el, "duration"));
        } else if (el.tag === "forward") {
          cur += intText(child(el, "duration"));
        } else if (el.tag === "note") {
          if (child(el, "grace")) continue;
          const durEl = child(el, "duration");
          const dur = durEl ? intText(durEl) : 0;
          const isChord = !!child(el, "chord");
          const start = isChord ? lastOn : cur;
          const p = child(el, "pitch");
          const tieStop = kids(el, "tie").some((t) => t.attrs.type === "stop");
          if (p && !tieStop) {
            let midi = 12 * (intText(child(p, "octave")) + 1) + STEP[child(p, "step").text.trim()];
            const a = child(p, "alter");
            if (a) midi += Math.trunc(parseFloat(a.text));
            const key = start / divisions;
            if (!d.ons.has(key)) d.ons.set(key, new Set());
            d.ons.get(key).add(midi);
          }
          if (!isChord) {
            lastOn = cur;
            cur += dur;
          }
        }
      }
    });
  }
  return [...merged.keys()].sort((a, b) => a - b).map((k) => merged.get(k));
}

export function expandJumps(measures) {
  let segno = null;
  let coda = null;
  measures.forEach((m, i) => {
    if ("segno" in m.flags) segno = i;
    if ("coda" in m.flags) coda = i;
  });
  const order = [];
  let i = 0;
  let jumped = false;
  let guard = 0;
  while (i < measures.length && guard < 10000) {
    guard++;
    const f = measures[i].flags;
    order.push(i);
    if (!jumped && "dalsegno" in f && segno !== null) { jumped = true; i = segno; continue; }
    if (!jumped && "dacapo" in f) { jumped = true; i = 0; continue; }
    if (jumped && "tocoda" in f && coda !== null) { i = coda; continue; }
    if (jumped && "fine" in f) break;
    i++;
  }
  return order;
}

export function buildChords(measures, useJumps = true, mrange = null) {
  const order = useJumps ? expandJumps(measures) : measures.map((_, i) => i);
  const chords = [];
  for (const i of order) {
    const m = measures[i];
    if (mrange) {
      const n = parseInt(m.num, 10);
      const v = Number.isNaN(n) ? -1 : n;
      if (!(v >= mrange[0] && v <= mrange[1])) continue;
    }
    for (const beat of [...m.ons.keys()].sort((a, b) => a - b)) chords.push(new Chord(m.num, beat, m.ons.get(beat)));
  }
  return chords;
}

/** Everything the tracker needs to know about a score file. */
export function loadScore(xmlText) {
  const root = parseXML(xmlText);
  if (!root || root.tag !== "score-partwise") throw new Error("not a partwise MusicXML file");
  const measures = parseMeasures(root);
  const firstPart = kids(root, "part")[0];
  const nums = kids(firstPart, "measure").map((m) => m.attrs.number);
  const ts = iter(root, "time").next().value;
  const qlen = ts ? (intText(child(ts, "beats")) * 4) / intText(child(ts, "beat-type")) : 4;
  return { measures, nums, qlen };
}
