// Replays a 22.05 kHz mono WAV through the web engine and prints what it flags (for comparing with the Python tool).
//   node docs/test/run.mjs score.musicxml take22.wav [measures]
import fs from "node:fs";
import { Session } from "../js/tracker.js";
import { Engine } from "../js/engine.js";

export function readWav(path) {
  const b = fs.readFileSync(path);
  let p = 12, fmt = null, data = null;
  while (p < b.length - 8) {
    const id = b.toString("ascii", p, p + 4), len = b.readUInt32LE(p + 4);
    if (id === "fmt ") fmt = { tag: b.readUInt16LE(p + 8), ch: b.readUInt16LE(p + 10), sr: b.readUInt32LE(p + 12), bits: b.readUInt16LE(p + 22) };
    if (id === "data") { data = b.subarray(p + 8, p + 8 + len); break; }
    p += 8 + len + (len & 1);
  }
  const n = data.length / (fmt.bits / 8) / fmt.ch, x = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let acc = 0;
    for (let c = 0; c < fmt.ch; c++) {
      const o = (i * fmt.ch + c) * (fmt.bits / 8);
      acc += fmt.bits === 16 ? data.readInt16LE(o) / 32768 : fmt.tag === 3 ? data.readFloatLE(o) : data.readInt32LE(o) / 2147483648;
    }
    x[i] = acc / fmt.ch;
  }
  return { x, sr: fmt.sr };
}

const [, , scorePath, wavPath, measures = ""] = process.argv;
const { x, sr } = readWav(wavPath);
const s = new Session(true);
s.setScore(scorePath.split(/[\\/]/).pop(), fs.readFileSync(scorePath, "utf8"));
s.newPractice(measures);
s.running = true;
const eng = new Engine(s, sr, { gate: 0.001, agc: process.env.AGC === "1" });
const t0 = Date.now();
for (let i = 0; i < x.length; i += 1024) eng.push(x.subarray(i, Math.min(i + 1024, x.length)));
s.flush();
const st = s.toState();
console.log(JSON.stringify({ shift: s.shift, status: s.status, pos: s.pos, onsets: s.nOnsets, secs: (Date.now() - t0) / 1000,
  rhythm: st.rhythm,
  recs: st.recs.map((r) => [r.measure, r.beat, r.status, r.text]) }));
