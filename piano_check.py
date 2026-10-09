"""Listen to piano playing (microphone or wav file) and report wrong keys vs a MusicXML score.

Usage:
  python piano_check.py SCORE.musicxml --record            # play, press Enter to stop
  python piano_check.py SCORE.musicxml --wav take.wav      # analyse an existing recording
  options: --measures 5-12  (practise a section)   --no-jumps  (ignore D.S./coda, play as written)
"""
import argparse
import sys
import wave
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

import numpy as np
np.seterr(all="ignore")
from scipy.signal import resample_poly

SR = 22050
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
STEP = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_name(m):
    return f"{NAMES[m % 12]}{m // 12 - 1}"


# ----------------------------------------------------------------------------- score
class Chord:
    def __init__(self, measure, beat, notes):
        self.measure, self.beat, self.notes = measure, beat, sorted(notes)


def load_xml(path):
    if path.lower().endswith(".mxl"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".xml") and not n.startswith("META-INF")]
            return ET.fromstring(z.read(names[0]))
    return ET.parse(path).getroot()


def parse_measures(root):
    """-> list of (number, {onset_quarters: set(midi)}, jump_flags) for all parts merged."""
    parts = root.findall("part")
    merged = {}
    for part in parts:
        divisions = 1
        for idx, m in enumerate(part.findall("measure")):
            d = merged.setdefault(idx, {"num": m.get("number"), "ons": defaultdict(set), "flags": {}})
            cur = 0
            last_on = 0
            for el in m:
                if el.tag == "attributes":
                    dv = el.find("divisions")
                    if dv is not None:
                        divisions = int(dv.text)
                elif el.tag == "direction":
                    for s in el.iter("sound"):
                        d["flags"].update({k: v for k, v in s.attrib.items() if k != "dynamics"})
                elif el.tag == "backup":
                    cur -= int(el.find("duration").text)
                elif el.tag == "forward":
                    cur += int(el.find("duration").text)
                elif el.tag == "note":
                    if el.find("grace") is not None:
                        continue
                    dur = int(el.find("duration").text) if el.find("duration") is not None else 0
                    is_chord = el.find("chord") is not None
                    start = last_on if is_chord else cur
                    p = el.find("pitch")
                    tie_stop = any(t.get("type") == "stop" for t in el.findall("tie"))
                    if p is not None and not tie_stop:
                        midi = 12 * (int(p.find("octave").text) + 1) + STEP[p.find("step").text]
                        a = p.find("alter")
                        if a is not None:
                            midi += int(float(a.text))
                        d["ons"][start / divisions].add(midi)
                    if not is_chord:
                        last_on = cur
                        cur += dur
    return [merged[i] for i in sorted(merged)]


def expand_jumps(measures):
    """Follow D.S./D.C. al coda so the expected sequence is the order actually played."""
    segno = coda = None
    for i, m in enumerate(measures):
        if "segno" in m["flags"]:
            segno = i
        if "coda" in m["flags"]:
            coda = i
    order, i, jumped, guard = [], 0, False, 0
    while i < len(measures) and guard < 10000:
        guard += 1
        f = measures[i]["flags"]
        order.append(i)
        if not jumped and "dalsegno" in f and segno is not None:
            jumped, i = True, segno
            continue
        if not jumped and "dacapo" in f:
            jumped, i = True, 0
            continue
        if jumped and "tocoda" in f and coda is not None:
            i = coda
            continue
        if jumped and "fine" in f:
            break
        i += 1
    return order


def build_chords(path, use_jumps=True, mrange=None):
    measures = parse_measures(load_xml(path))
    order = expand_jumps(measures) if use_jumps else list(range(len(measures)))
    chords = []
    for i in order:
        m = measures[i]
        if mrange:
            try:
                n = int(m["num"])
            except ValueError:
                n = -1
            if not (mrange[0] <= n <= mrange[1]):
                continue
        for beat in sorted(m["ons"]):
            chords.append(Chord(m["num"], beat, m["ons"][beat]))
    return chords


# ----------------------------------------------------------------------------- audio
def record(seconds_limit=600):
    import sounddevice as sd
    fs = 44100
    frames = []

    def cb(indata, n, t, status):
        frames.append(indata.copy())

    print("Recording... play the piece, then press Enter to stop.")
    with sd.InputStream(samplerate=fs, channels=1, callback=cb):
        input()
    x = np.concatenate(frames)[:, 0]
    return x.astype(np.float32), fs


def load_wav(path):
    import soundfile as sf
    x, fs = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x, fs


def to_sr(x, fs):
    if fs != SR:
        from math import gcd
        g = gcd(int(fs), SR)
        x = resample_poly(x, SR // g, int(fs) // g)
    x = x - np.mean(x)
    peak = np.max(np.abs(x)) + 1e-9
    return (x / peak).astype(np.float32)


def detect_onsets(x, hop=512, n=2048):
    win = np.hanning(n)
    nfr = 1 + (len(x) - n) // hop
    if nfr < 3:
        return []
    idx = np.arange(n)[None, :] + hop * np.arange(nfr)[:, None]
    mag = np.abs(np.fft.rfft(x[idx] * win, axis=1))
    lm = np.log1p(200 * mag)
    flux = np.maximum(lm[1:] - lm[:-1], 0)
    # ignore very low band (rumble) and very high band
    f = np.fft.rfftfreq(n, 1 / SR)
    band = (f > 60) & (f < 4500)
    env = flux[:, band].sum(axis=1)
    env = np.convolve(env, np.ones(3) / 3, mode="same")
    k = 40
    med = np.array([np.median(env[max(0, i - k):i + k + 1]) for i in range(len(env))])
    thr = med + 0.35 * (np.percentile(env, 95) - np.median(env)) + 1e-6
    # loudness gate
    rms = np.sqrt((x[idx] ** 2).mean(axis=1))
    gate = 0.03 * np.percentile(rms, 98)
    peaks, last = [], -999
    min_gap = int(0.07 * SR / hop)
    for i in range(1, len(env) - 1):
        if env[i] > thr[i] and env[i] >= env[i - 1] and env[i] >= env[i + 1] and rms[min(i + 3, len(rms) - 1)] > gate:
            if i - last >= min_gap:
                peaks.append(i + 1)
                last = i
            elif peaks and env[i] > env[peaks[-1] - 1]:
                peaks[-1] = i + 1
                last = i
    return [(p * hop + n // 2) / SR for p in peaks]


NFFT = 1 << 15
FREQS = np.fft.rfftfreq(NFFT, 1 / SR)
MIDIS = np.arange(33, 101)


def spectrum(x, t0, dur):
    a = max(0, int(t0 * SR))
    seg = x[a:a + int(dur * SR)]
    if len(seg) < 512:
        return np.zeros(len(FREQS))
    w = np.hanning(len(seg))
    return np.abs(np.fft.rfft(seg * w, NFFT)) / w.sum()  # window-gain normalised so pre/post compare


def _peak(spec, f, semi=0.4):
    if f > SR / 2 - 200:
        return 0.0
    lo = np.searchsorted(FREQS, f * 2 ** (-semi / 12))
    hi = np.searchsorted(FREQS, f * 2 ** (semi / 12)) + 1
    return float(spec[lo:hi].max())


def _floor(spec, f):
    lo = np.searchsorted(FREQS, f * 0.75)
    hi = np.searchsorted(FREQS, f * 1.35)
    return float(np.median(spec[lo:hi])) + 1e-9


NH = 5


class Evidence:
    """Per-onset harmonic evidence for every midi note."""

    def __init__(self, x, t, t_next=None):
        nxt = 0.4
        if t_next is not None:
            nxt = min(nxt, max(0.2, t_next - t - 0.06))  # stop before the next strike's attack
        self.post = spectrum(x, t + 0.03, nxt)
        half = max(0.1, nxt / 2)
        self.early = spectrum(x, t + 0.03, half)
        self.late = spectrum(x, t + 0.03 + half, half) if nxt > 0.25 else None
        self.pre = spectrum(x, max(0.0, t - 0.15), 0.12)
        self.cache = {}

    def note(self, m):
        if m in self.cache:
            return self.cache[m]
        f0 = 440 * 2 ** ((m - 69) / 12)
        prom, pk, rise = [], [], []
        for h in range(1, NH + 1):
            f = f0 * h
            p = _peak(self.post, f)
            q = _peak(self.pre, f)
            prom.append(20 * np.log10(p / _floor(self.post, f)))
            pk.append(p)
            rise.append(20 * np.log10((p + 1e-9) / (q + 1e-9)))
        r = (np.array(prom), np.array(pk), np.array(rise))
        self.cache[m] = r
        return r

    def presence(self, m):
        """0..1 how convincingly note m is sounding."""
        prom, _, _ = self.note(m)
        hit = prom > 12
        cnt = int(hit[:4].sum())
        base = min(cnt / 3, 1.0)
        return base if (hit[0] or hit[2]) else base * 0.5

    def fresh(self, m):
        """True if note m was struck at this onset (its energy jumped), not just still ringing."""
        prom, _, rise = self.note(m)
        hit = prom > 12
        return (rise[0] if hit[0] else min(rise[1], rise[2])) >= 6

    def present(self, m):
        prom, _, _ = self.note(m)
        hit = prom > 12
        return (hit[0] or hit[2]) and hit[:4].sum() >= 2

    def detected(self):
        """Notes freshly struck at this onset (independent of the score)."""
        if hasattr(self, "_det"):
            return self._det
        det = []
        for m in range(33, 97):
            prom, pk, rise = self.note(m)
            hit = prom > 12
            if hit[:4].sum() < 3 or not (prom[0] > 15 or (prom[1] > 15 and prom[2] > 15)):
                continue
            # a fresh strike lifts the fundamental; if that is too weak, the next two partials must both lift
            fresh = rise[0] if hit[0] else min(rise[1], rise[2])
            if fresh < 6:
                continue  # was already ringing before this onset
            explained = False
            for d in (12, 19, 24, 28, 31, 34, 36):  # m is a harmonic of an accepted lower note
                e = m - d
                if e in det and pk[0] < self.note(e)[1][0]:
                    explained = True
            for d in (-1, 1, -2, 2):  # leakage from a near neighbour
                e = m + d
                if e in det and pk[0] < self.note(e)[1][0] * 0.5:
                    explained = True
            # a note struck at this onset dies away; one that grows afterwards belongs to a later strike
            if self.late is not None:
                f0 = 440 * 2 ** ((m - 69) / 12)
                hh = 0 if hit[0] else 1
                e_pk, l_pk = _peak(self.early, f0 * (hh + 1)), _peak(self.late, f0 * (hh + 1))
                if l_pk > 1.2 * e_pk:
                    explained = True
            if not explained:
                det.append(m)
        # drop spectral-skirt ghosts: a note a semitone or tone from a much stronger one
        det = [m for m in det
               if not any(abs(m - e) in (1, 2) and self.note(e)[1][0] > 2 * self.note(m)[1][0] for e in det)]
        # sympathetic ringing and hammer noise are far quieter than a key you really pressed
        def level(m):
            prom, pk, _ = self.note(m)
            return pk[0] if prom[0] > 12 else max(pk[1], pk[2])
        if det:
            top = max(level(m) for m in det)
            det = [m for m in det if level(m) >= 0.25 * top]
        self._det = det
        return det

    def extras(self, expected):
        return [m for m in self.detected() if m not in expected]


# ----------------------------------------------------------------------------- alignment
def align(chords, evs):
    n, k = len(chords), len(evs)
    pres = np.zeros((k, n))
    for j, ev in enumerate(evs):
        for i, c in enumerate(chords):
            ok = sum(ev.presence(m) for m in c.notes)
            ex = len(ev.extras(c.notes))
            pres[j, i] = ok / (len(c.notes) + ex)
    S = 2 * pres - 0.8  # match score in [-0.8, 1.2]
    skip_chord, skip_onset = -0.5, -0.5
    NEG = -1e9
    H = np.zeros((n + 1, k + 1))
    P = np.zeros((n + 1, k + 1), dtype=np.int8)
    for i in range(1, n + 1):
        for j in range(1, k + 1):
            cand = (H[i - 1, j - 1] + S[j - 1, i - 1], H[i - 1, j] + skip_chord, H[i, j - 1] + skip_onset)
            a = int(np.argmax(cand))
            H[i, j], P[i, j] = cand[a], a
    # free leading/trailing gaps: first row/col are 0; end anywhere on last row/col
    best, bi, bj = NEG, n, k
    for j in range(k + 1):
        if H[n, j] > best:
            best, bi, bj = H[n, j], n, j
    for i in range(n + 1):
        if H[i, k] > best:
            best, bi, bj = H[i, k], i, k
    i, j, pairs = bi, bj, {}
    while i > 0 and j > 0:
        a = P[i, j]
        if a == 0:
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif a == 1:
            i -= 1
        else:
            j -= 1
    return pairs, bi


# ----------------------------------------------------------------------------- report
def report(chords, evs, times, pairs, last_idx, first_idx):
    mistakes = 0
    lines = []
    for i in range(first_idx, last_idx):
        c = chords[i]
        where = f"m.{c.measure} beat {c.beat + 1:g}"
        if i not in pairs:
            lines.append((i, f"{where}: expected {' '.join(map(note_name, c.notes))} - nothing heard (skipped?)"))
            mistakes += 1
            continue
        ev = evs[pairs[i]]
        missing = [m for m in c.notes if not ev.present(m)]
        neighbours = set()
        for k in (i - 1, i + 1):
            if 0 <= k < len(chords):
                neighbours.update(chords[k].notes)
        # ignore likely overtone/octave ghosts and bleed from the adjacent chords (hands not exactly together)
        extra = [m for m in ev.extras(c.notes)
                 if m not in neighbours and not any(abs(m - e) in (12, 19, 24) for e in c.notes)]
        if not missing and not extra:
            continue
        mistakes += 1
        msg = []
        used = set()
        for e in list(extra):
            near = [m for m in missing if abs(m - e) <= 2 and m not in used]
            if near:
                m = min(near, key=lambda z: abs(z - e))
                msg.append(f"played {note_name(e)} instead of {note_name(m)}")
                used.add(m)
                extra.remove(e)
        rest_missing = [m for m in missing if m not in used]
        if rest_missing:
            msg.append("missing " + " ".join(map(note_name, rest_missing)))
        if extra:
            msg.append("extra " + " ".join(map(note_name, extra)))
        lines.append((i, f"{where}: expected {' '.join(map(note_name, c.notes))} -> " + "; ".join(msg)
                      + f"   [t={times[pairs[i]]:.1f}s]"))
    return lines, mistakes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("score")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--wav")
    ap.add_argument("--save", help="save the recording to this wav")
    ap.add_argument("--measures", help="e.g. 5-12")
    ap.add_argument("--html", default="report.html", help="graphical report file")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-jumps", action="store_true")
    a = ap.parse_args()

    mr = None
    if a.measures:
        lo, _, hi = a.measures.partition("-")
        mr = (int(lo), int(hi or lo))
    chords = build_chords(a.score, not a.no_jumps, mr)
    print(f"Score: {len(chords)} chords/notes to play.")

    if a.record:
        x, fs = record()
        if a.save:
            import soundfile as sf
            sf.write(a.save, x, fs)
    elif a.wav:
        x, fs = load_wav(a.wav)
    else:
        sys.exit("give --record or --wav")
    x = to_sr(x, fs)
    times = detect_onsets(x)
    print(f"Heard {len(times)} note onsets in {len(x) / SR:.1f}s.")
    if not times:
        sys.exit("No notes detected (too quiet?).")
    evs = [Evidence(x, t, times[j + 1] if j + 1 < len(times) else None) for j, t in enumerate(times)]
    pairs, last = align(chords, evs)
    first = min(pairs) if pairs else 0
    from report_html import analyse, describe, write_html
    recs = analyse(chords, evs, times, pairs, last, first)
    bad = [r for r in recs if r["status"] != "ok"]
    for r in bad:
        print(f"m.{r['measure']} beat {r['beat']:g}: expected {' '.join(map(note_name, r['expected']))} -> {describe(r)}")
    print(f"\n{len(bad)} chord(s) with problems out of {len(recs)}.")
    import os
    import webbrowser
    out = os.path.abspath(a.html)
    root = load_xml(a.score)
    nums = [m.get("number") for m in root.find("part").findall("measure")]
    write_html(out, recs, os.path.basename(a.score), "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" + ET.tostring(root, encoding="unicode"), nums)
    print("Report:", out)
    if not a.no_open:
        webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()
