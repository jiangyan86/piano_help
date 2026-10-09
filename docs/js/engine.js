// Audio in -> onsets -> evidence -> Session (port of the live_check.py worker loop).
import { rfftMag, hann, percentile, median } from "./dsp.js";
import { Evidence, AudioBuf, setSampleRate } from "./evidence.js";

const HOP = 512;
const NFR = 2048;

/** Incremental spectral-flux onset detector. */
export class LiveOnsets {
  constructor(sr, gate) {
    this.sr = sr;
    this.gate = gate;
    this.rel = 0.04;
    this.k = 0.25;
    this.win = hann(NFR);
    this.bandLo = Math.floor((60 * NFR) / sr) + 1;
    this.bandHi = Math.ceil((4500 * NFR) / sr) - 1;
    this.reset();
  }

  reset() {
    this.tail = new Float32Array(0);
    this.prev = null;
    this.env = [];
    this.rms = [];
    this.raw = [];
    this.frame = -1;
  }

  /** -> array of [onset_time, strength] */
  feed(x) {
    const out = [];
    const buf = new Float32Array(this.tail.length + x.length);
    buf.set(this.tail);
    buf.set(x, this.tail.length);
    let pos = 0;
    while (buf.length - pos >= NFR) {
      const fr = buf.subarray(pos, pos + NFR);
      pos += HOP;
      this.frame += 1;
      const mag = rfftMag(fr, this.win, NFR);
      const lm = new Float64Array(mag.length);
      for (let i = 0; i < mag.length; i++) lm[i] = Math.log1p(200 * mag[i]);
      let e = 0;
      if (this.prev) for (let i = this.bandLo; i <= this.bandHi; i++) e += Math.max(lm[i] - this.prev[i], 0);
      this.prev = lm;
      this.raw.push(e);
      const r3 = this.raw.slice(-3);
      this.env.push(r3.reduce((a, b) => a + b, 0) / r3.length);
      let ss = 0;
      for (let i = 0; i < NFR; i++) ss += fr[i] * fr[i];
      this.rms.push(Math.sqrt(ss / NFR));
      if (this.env.length > 4000) { this.env = this.env.slice(-2000); this.rms = this.rms.slice(-2000); this.raw = this.raw.slice(-2000); }
      const kk = this.env.length;
      if (kk < 4) continue;
      const hist = this.env.slice(-400);
      const med = median(hist.slice(-80));
      const thr = med + this.k * (percentile(hist, 95) - median(hist)) + 1e-6;
      const c = kk - 2; // candidate (needs one frame of look-ahead)
      if (this.env[c] > thr && this.env[c] >= this.env[c - 1] && this.env[c] >= this.env[c + 1]) {
        const gate = Math.max(this.gate, this.rel * percentile(this.rms.slice(-1500), 98));
        if (Math.max(...this.rms.slice(-4)) > gate) out.push([((this.frame - 1) * HOP + NFR / 2) / this.sr, this.env[c]]);
      }
    }
    this.tail = buf.slice(pos);
    return out;
  }
}

export class Engine {
  constructor(session, sr, { gate = 0.001 } = {}) {
    this.session = session;
    this.sr = sr;
    setSampleRate(sr);
    this.gate = gate;
    this.start();
  }

  start() {
    this.buf = new AudioBuf(40);
    this.det = new LiveOnsets(this.sr, this.gate);
    this.pending = [];
    this.lastPeak = null;
    this.lastOnset = 0;
  }

  /** Feed a block of mono samples at this.sr. */
  push(x) {
    const s = this.session;
    if (!x.length) return;
    this.buf.push(x);
    let ss = 0;
    for (let i = 0; i < x.length; i++) ss += x[i] * x[i];
    s.level = Math.min(1, Math.sqrt(ss / x.length) * 10);
    s.audioNow = this.buf.n / this.sr;
    for (const [t, strength] of this.det.feed(x)) {
      const last = this.lastPeak;
      if (last && t - last[0] < 0.2 && strength < last[1]) continue; // echo of the previous strike
      if (this.pending.length && t - this.pending[this.pending.length - 1][0] < 0.08) {
        this.pending[this.pending.length - 1] = [t, strength]; // same strike seen twice: keep the later, stronger peak
      } else this.pending.push([t, strength]);
      this.lastPeak = [t, strength];
    }
    const now = s.audioNow;
    while (this.pending.length && now >= this.pending[0][0] + 0.5) {
      const t = this.pending.shift()[0];
      const nxt = this.pending.length ? this.pending[0][0] : null;
      const ev = new Evidence(this.buf, t, nxt);
      if (s.running) s.handle(ev, t);
      this.lastOnset = t;
    }
    if (s.evs.length && now - this.lastOnset > 3.0 && s.running) s.flush(); // you paused: judge what has been played
  }
}
