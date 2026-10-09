// Harmonic evidence for every piano note at one onset (port of piano_check.Evidence).
import { rfftMag, hann, median } from "./dsp.js";

export const NFFT = 1 << 15;
export const NH = 5;
let SR = 22050;
const FREQS = new Float64Array(NFFT / 2 + 1);

export function setSampleRate(sr) {
  SR = sr;
  for (let i = 0; i < FREQS.length; i++) FREQS[i] = (i * SR) / NFFT;
}
setSampleRate(22050);
export const getSampleRate = () => SR;

/** Ring buffer of the most recent audio, addressed by absolute sample index. */
export class AudioBuf {
  constructor(seconds = 40) {
    this.size = Math.floor(seconds * SR);
    this.data = new Float32Array(this.size);
    this.n = 0;
  }
  push(x) {
    for (let i = 0; i < x.length; i++) this.data[(this.n + i) % this.size] = x[i];
    this.n += x.length;
  }
  get(a, len) {
    const out = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      const idx = a + i;
      if (idx >= 0 && idx < this.n && idx >= this.n - this.size) out[i] = this.data[idx % this.size];
    }
    return out;
  }
}

function searchsorted(x) {
  // first index with FREQS[idx] >= x
  let lo = 0;
  let hi = FREQS.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (FREQS[mid] < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function spectrum(buf, t0, dur) {
  const a = Math.max(0, Math.floor(t0 * SR));
  const len = Math.floor(dur * SR);
  if (len < 512) return null;
  const seg = buf.get(a, len);
  const w = hann(len);
  let ws = 0;
  for (let i = 0; i < len; i++) ws += w[i];
  const m = rfftMag(seg, w, NFFT);
  for (let i = 0; i < m.length; i++) m[i] /= ws; // window-gain normalised so pre/post compare
  return m;
}

function peak(spec, f, semi = 0.4) {
  if (!spec || f > SR / 2 - 200) return 0;
  const lo = searchsorted(f * 2 ** (-semi / 12));
  const hi = searchsorted(f * 2 ** (semi / 12)) + 1;
  let m = -Infinity;
  for (let i = lo; i < Math.min(hi, spec.length); i++) if (spec[i] > m) m = spec[i];
  return m === -Infinity ? NaN : m;
}

function floorOf(spec, f) {
  if (!spec) return 1e-9;
  const lo = searchsorted(f * 0.75);
  const hi = Math.min(searchsorted(f * 1.35), spec.length);
  if (hi <= lo) return NaN;
  return median(spec.subarray(lo, hi)) + 1e-9;
}

const f0Of = (m) => 440 * 2 ** ((m - 69) / 12);

export class Evidence {
  constructor(buf, t, tNext = null) {
    let nxt = 0.4;
    if (tNext !== null) nxt = Math.min(nxt, Math.max(0.2, tNext - t - 0.06)); // stop before the next strike's attack
    this.post = spectrum(buf, t + 0.03, nxt);
    const half = Math.max(0.1, nxt / 2);
    this.early = spectrum(buf, t + 0.03, half);
    this.late = nxt > 0.25 ? spectrum(buf, t + 0.03 + half, half) : null;
    this.pre = spectrum(buf, Math.max(0, t - 0.15), 0.12);
    this.cache = new Map();
    this._det = null;
    this.cs = new Map(); // chord-score cache used by the tracker
  }

  note(m) {
    let r = this.cache.get(m);
    if (r) return r;
    const f0 = f0Of(m);
    const prom = [];
    const pk = [];
    const rise = [];
    for (let h = 1; h <= NH; h++) {
      const f = f0 * h;
      const p = peak(this.post, f);
      const q = peak(this.pre, f);
      prom.push(20 * Math.log10(p / floorOf(this.post, f)));
      pk.push(p);
      rise.push(20 * Math.log10((p + 1e-9) / (q + 1e-9)));
    }
    r = { prom, pk, rise };
    this.cache.set(m, r);
    return r;
  }

  presence(m) {
    const { prom } = this.note(m);
    const hit = prom.map((v) => v > 12);
    const cnt = hit.slice(0, 4).filter(Boolean).length;
    const base = Math.min(cnt / 3, 1);
    return hit[0] || hit[2] ? base : base * 0.5;
  }

  fresh(m) {
    const { prom, rise } = this.note(m);
    return (prom[0] > 12 ? rise[0] : Math.min(rise[1], rise[2])) >= 6;
  }

  present(m) {
    const { prom } = this.note(m);
    const hit = prom.map((v) => v > 12);
    return (hit[0] || hit[2]) && hit.slice(0, 4).filter(Boolean).length >= 2;
  }

  /** Notes freshly struck at this onset (independent of the score). */
  detected() {
    if (this._det) return this._det;
    let det = [];
    for (let m = 33; m < 97; m++) {
      const { prom, pk, rise } = this.note(m);
      const hit = prom.map((v) => v > 12);
      if (hit.slice(0, 4).filter(Boolean).length < 3 || !(prom[0] > 15 || (prom[1] > 15 && prom[2] > 15))) continue;
      const fresh = hit[0] ? rise[0] : Math.min(rise[1], rise[2]);
      if (fresh < 6) continue; // was already ringing before this onset
      let explained = false;
      for (const d of [12, 19, 24, 28, 31, 34, 36]) {
        // m is a harmonic of an accepted lower note
        const e = m - d;
        if (det.includes(e) && pk[0] < this.note(e).pk[0]) explained = true;
      }
      for (const d of [-1, 1, -2, 2]) {
        // leakage from a near neighbour
        const e = m + d;
        if (det.includes(e) && pk[0] < this.note(e).pk[0] * 0.5) explained = true;
      }
      // a note struck at this onset dies away; one that grows afterwards belongs to a later strike
      if (this.late) {
        const f0 = f0Of(m);
        const hh = hit[0] ? 0 : 1;
        const ePk = peak(this.early, f0 * (hh + 1));
        const lPk = peak(this.late, f0 * (hh + 1));
        if (lPk > 1.2 * ePk) explained = true;
      }
      if (!explained) det.push(m);
    }
    // drop spectral-skirt ghosts: a note a semitone or tone from a much stronger one
    const all = det;
    det = all.filter((m) => !all.some((e) => [1, 2].includes(Math.abs(m - e)) && this.note(e).pk[0] > 2 * this.note(m).pk[0]));
    // sympathetic ringing and hammer noise are far quieter than a key you really pressed
    const level = (m) => {
      const { prom, pk } = this.note(m);
      return prom[0] > 12 ? pk[0] : Math.max(pk[1], pk[2]);
    };
    if (det.length) {
      const top = Math.max(...det.map(level));
      det = det.filter((m) => level(m) >= 0.25 * top);
    }
    this._det = det;
    return det;
  }

  extras(expected) {
    return this.detected().filter((m) => !expected.includes(m));
  }
}
