// Small DSP helpers: FFT, windows, statistics.
const fftCache = new Map();

function plan(n) {
  let p = fftCache.get(n);
  if (p) return p;
  const rev = new Uint32Array(n);
  let bits = 0;
  while ((1 << bits) < n) bits++;
  for (let i = 0; i < n; i++) {
    let r = 0;
    for (let b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b);
    rev[i] = r;
  }
  const cos = new Float64Array(n / 2), sin = new Float64Array(n / 2);
  for (let i = 0; i < n / 2; i++) { cos[i] = Math.cos((2 * Math.PI * i) / n); sin[i] = -Math.sin((2 * Math.PI * i) / n); }
  p = { rev, cos, sin, re: new Float64Array(n), im: new Float64Array(n) };
  fftCache.set(n, p);
  return p;
}

/** Magnitude spectrum (bins 0..n/2) of x*w zero-padded to n. */
export function rfftMag(x, w, n) {
  const p = plan(n);
  const { rev, cos, sin, re, im } = p;
  const len = Math.min(x.length, n);
  for (let i = 0; i < n; i++) { re[rev[i]] = i < len ? x[i] * (w ? w[i] : 1) : 0; im[rev[i]] = 0; }
  for (let size = 2; size <= n; size <<= 1) {
    const half = size >> 1, step = n / size;
    for (let start = 0; start < n; start += size) {
      for (let k = 0, t = 0; k < half; k++, t += step) {
        const a = start + k, b = a + half;
        const tr = re[b] * cos[t] - im[b] * sin[t];
        const ti = re[b] * sin[t] + im[b] * cos[t];
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
      }
    }
  }
  const out = new Float64Array(n / 2 + 1);
  for (let i = 0; i <= n / 2; i++) out[i] = Math.hypot(re[i], im[i]);
  return out;
}

const hannCache = new Map();
export function hann(n) {
  let w = hannCache.get(n);
  if (!w) {
    w = new Float64Array(n);
    for (let i = 0; i < n; i++) w[i] = n === 1 ? 1 : 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
    hannCache.set(n, w);
  }
  return w;
}

/** numpy-style linear-interpolated percentile of an array-like (copied and sorted). */
export function percentile(a, p) {
  const n = a.length;
  if (!n) return NaN;
  const s = Float64Array.from(a).sort();
  const pos = ((n - 1) * p) / 100, lo = Math.floor(pos), hi = Math.ceil(pos);
  return s[lo] + (s[hi] - s[lo]) * (pos - lo);
}
export const median = (a) => percentile(a, 50);
export const clip = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const sum = (a) => a.reduce((x, y) => x + y, 0);

/** Streaming integer-factor decimator with a windowed-sinc low-pass. */
export class Decimator {
  constructor(factor) {
    this.m = factor;
    const taps = factor * 16 + 1, fc = 0.45 / factor;
    this.h = new Float64Array(taps);
    let s = 0;
    for (let i = 0; i < taps; i++) {
      const k = i - (taps - 1) / 2;
      const sinc = k === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * k) / (Math.PI * k);
      this.h[i] = sinc * (0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (taps - 1)));
      s += this.h[i];
    }
    for (let i = 0; i < taps; i++) this.h[i] /= s;
    this.acc = new Float32Array(0);
    this.next = 0;
  }
  push(x) {
    if (this.m === 1) return x;
    const t = this.h.length, m = this.m;
    const buf = new Float32Array(this.acc.length + x.length);
    buf.set(this.acc); buf.set(x, this.acc.length);
    const out = [];
    let next = this.next;
    while (next + t <= buf.length) {
      let v = 0;
      for (let k = 0; k < t; k++) v += buf[next + k] * this.h[k];
      out.push(v);
      next += m;
    }
    this.acc = buf.slice(next);
    this.next = 0;
    return Float32Array.from(out);
  }
}
