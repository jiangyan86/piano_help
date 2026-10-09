"""Real-time piano mistake checker. Listens to the microphone, follows you through the score,
and shows mistakes on the sheet music in your browser. Mistakes stay until you press "New practice".

  python live_check.py SCORE.musicxml            # then use the page that opens
  python live_check.py SCORE.musicxml --wav take.wav   # simulate live playback of a recording (for testing)
  python live_check.py --list-devices
"""
import argparse
import http.server
import json
import os
import queue
import threading
import time
import webbrowser

import numpy as np

import piano_check as pc
from piano_check import SR, Evidence, build_chords, load_xml, note_name
from report_html import describe, judge

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_RUN = os.path.join(HERE, "last_run.json")
LAST_SCORE = os.path.join(HERE, "last_score.txt")
HISTORY = os.path.join(HERE, "score_history.json")
SCORES = os.path.join(HERE, "scores")
HOP, NFR = 512, 2048
MAX_SECONDS = 20 * 60


class LiveOnsets:
    """Incremental spectral-flux onset detector (same idea as piano_check.detect_onsets)."""

    def __init__(self, gate):
        self.gate = gate
        self.rel, self.k = 0.04, 0.25
        self.reset()

    def reset(self):
        self.tail = np.zeros(0, np.float32)
        self.win = np.hanning(NFR)
        self.f = np.fft.rfftfreq(NFR, 1 / SR)
        self.band = (self.f > 60) & (self.f < 4500)
        self.prev = None
        self.env, self.rms, self.raw = [], [], []
        self.frame = -1

    def feed(self, x):
        """-> list of (onset_time, strength) peaks"""
        out = []
        self.tail = np.concatenate([self.tail, x])
        while len(self.tail) >= NFR:
            fr = self.tail[:NFR]
            self.tail = self.tail[HOP:]
            self.frame += 1
            lm = np.log1p(200 * np.abs(np.fft.rfft(fr * self.win)))
            e = 0.0 if self.prev is None else float(np.maximum(lm - self.prev, 0)[self.band].sum())
            self.prev = lm
            self.raw.append(e)
            self.env.append(float(np.mean(self.raw[-3:])))  # light smoothing, as in the offline detector
            self.rms.append(float(np.sqrt(np.mean(fr ** 2))))
            k = len(self.env)
            if k < 4:
                continue
            hist = np.array(self.env[-400:])
            med = np.median(hist[-80:])
            thr = med + self.k * (np.percentile(hist, 95) - np.median(hist)) + 1e-6
            c = k - 2  # candidate (needs one frame of look-ahead)
            if (self.env[c] > thr and self.env[c] >= self.env[c - 1] and self.env[c] >= self.env[c + 1]
                    and max(self.rms[-4:]) > max(self.gate, self.rel * np.percentile(self.rms[-1500:], 98))):
                out.append(((c * HOP + NFR // 2) / SR, self.env[c]))
        return out


def chord_score(ev, chord):
    ok = sum(ev.presence(m) * (1.0 if ev.fresh(m) else 0.4) for m in chord.notes)  # ringing is weak evidence
    return ok / (len(chord.notes) + len(ev.extras(chord.notes)))


class Session:
    def __init__(self, jumps):
        self.score_path, self.score_name, self.nums, self.jumps = None, None, [], jumps
        self.xml_text = ""
        self.lock = threading.Lock()
        self.qlen, self.audio_now = 4.0, 0.0
        self.running = False
        self.status = "Choose a music file (top left)"
        self.level = 0.0
        self.heard = []
        self.layout_id = 0
        self.new_practice("")
        self.running = False

    def set_score(self, path):
        """Switch to another MusicXML file (stops any running practice)."""
        root = load_xml(path)
        self.running = False
        self.score_path, self.score_name = path, os.path.basename(path)
        self.nums = [m.get("number") for m in root.find("part").findall("measure")]
        self.xml_text = '<?xml version="1.0" encoding="UTF-8"?>' + pc.ET.tostring(root, encoding="unicode")
        ts = root.find(".//time")
        self.qlen = int(ts.find("beats").text) * 4.0 / int(ts.find("beat-type").text) if ts is not None else 4.0
        if hasattr(self, "saved"):
            del self.saved
        self.new_practice("")
        self.status = f"Loaded {self.score_name}. Press New practice, then play."
        self.load_last()
        try:
            with open(LAST_SCORE, "w", encoding="utf8") as f:
                f.write(os.path.abspath(path))
        except OSError:
            pass
        remember_score(os.path.abspath(path))

    # -- state -----------------------------------------------------------------------------
    def new_practice(self, measures):
        mr = None
        if measures.strip():
            lo, _, hi = measures.strip().partition("-")
            mr = (int(lo), int(hi or lo))
        self.base = build_chords(self.score_path, self.jumps, mr) if self.score_path else []
        self.chords, self.shift = self.base, 0
        self.recs, self.pos, self.first, self.lost = {}, 0, True, 0
        self.evs, self.times, self.committed = [], [], None
        self.track, self.cur = {}, None
        self.layout_id = getattr(self, 'layout_id', 0) + 1
        self.shift_locked = False
        self._bounds()
        self.n_onsets = 0
        self.heard = []
        self.status = ("Listening... play from the start (or any point)" if self.base
                       else "Choose a music file (top left)")

    def to_json(self):
        recs = []
        for i in sorted(self.recs):
            r = self.recs[i]
            if r["status"] == "ok":
                continue
            c = self.chords[i] if i < len(self.chords) else None
            mi = self.nums.index(r["measure"]) if r["measure"] in self.nums else 0
            sh = self.shift  # recs are in the pitch space you played; the sheet shows the written notes
            r2 = dict(r, expected=[m - sh for m in r["expected"]], missing=[m - sh for m in r["missing"]],
                      subs=[(e, m - sh) for e, m in r["subs"]])
            recs.append(dict(i=i, measure=r["measure"], mi=mi, onset=round(r["beat"] - 1, 4), beat=r["beat"],
                             expected=[note_name(m) for m in r2["expected"]], expected_midi=r2["expected"],
                             missing=r2["missing"], extra=[note_name(m) for m in r["extra"]],
                             subs=[[note_name(e), m] for e, m in r2["subs"]], status=r["status"],
                             retried=r.get("retried", False), text=describe(r2)))
        nxt = None
        if self.pos < len(self.chords):
            c = self.chords[self.pos]
            nxt = dict(mi=self.nums.index(c.measure) if c.measure in self.nums else 0, onset=c.beat, measure=c.measure,
                       label=f"m.{c.measure} beat {c.beat + 1:g}")
        cursor = self._cursor() if self.running else None
        return dict(score=self.score_name, layout_id=self.layout_id, cursor=cursor, running=self.running, status=self.status, level=self.level, heard=self.heard,
                    recs=recs, next=nxt, total=len(self.chords), pos=self.pos, onsets=self.n_onsets)

    def _cursor(self):
        """Tempo-following position: a robust line through the last ~2 measures of heard chords (time vs
        score position), so the cursor follows your tempo instead of every single note."""
        if self.cur is None:
            return None
        k, t_k = self.cur
        pts = [(self.track[i], self.qpos[i]) for i in sorted(self.track)[-14:] if i <= k]
        r = 1 / 0.6
        a = self.qpos[k] - r * t_k
        if len(pts) >= 3:
            ts = np.array([p[0] for p in pts]); qs = np.array([p[1] for p in pts])
            sl = [(qs[j] - qs[i]) / (ts[j] - ts[i]) for i in range(len(ts)) for j in range(i + 1, len(ts))
                  if ts[j] - ts[i] > 0.05 and qs[j] > qs[i]]
            if sl:
                r = float(np.clip(np.median(sl), 0.5, 5.0))  # quarters per second
                a = float(np.median(qs - r * ts))
        q_last = self.qpos[k]
        q_now = float(np.clip(a + r * self.audio_now, q_last - 0.5, q_last + self.qlen))
        return dict(q=q_now, r=r, qlast=q_last, qlen=self.qlen)

    def save(self):
        try:
            with open(LAST_RUN, "w", encoding="utf8") as f:
                json.dump(self.to_json(), f)
        except OSError:
            pass

    def load_last(self):
        try:
            with open(LAST_RUN, encoding="utf8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if d.get("score") != self.score_name:
            return
        self.saved = d
        self.status = "Showing your last practice. Press New practice to start again."

    # -- tracking: onsets are collected and a whole measure is judged once you have moved on -----------
    def _bounds(self):
        ch = self.chords
        ms, me, start = [0] * len(ch), [0] * len(ch), 0
        for i in range(len(ch) + 1):
            if i == len(ch) or (i > 0 and (ch[i].measure != ch[i - 1].measure or ch[i].beat < ch[i - 1].beat)):
                for k in range(start, i):
                    ms[k], me[k] = start, i
                start = i
        self.mstart, self.mend = ms, me
        q, base = [], 0.0
        for i, c in enumerate(ch):
            if i > 0 and ms[i] != ms[i - 1]:
                base += self.qlen
            q.append(base + c.beat)
        self.qpos = q

    def _score(self, ev, i):
        cs = ev.__dict__.setdefault("_cs", {})
        key = (self.shift, i)
        if key not in cs:
            cs[key] = chord_score(ev, self.chords[i])
        return cs[key]

    def _set_shift(self, k):
        self.shift = k
        self.chords = self.base if k == 0 else [pc.Chord(c.measure, c.beat, [m + k for m in c.notes])
                                                for c in self.base]

    def _align(self, lo, hi, anchored):
        """Order-preserving alignment of the collected onsets with chords lo..hi -> {chord: onset index}."""
        k, n = len(self.evs), hi - lo
        if k == 0 or n <= 0:
            return {}, -1e9
        S = np.array([[2 * self._score(ev, lo + i) - 0.8 for i in range(n)] for ev in self.evs])
        skip = -0.5
        H = np.zeros((n + 1, k + 1))
        P = np.zeros((n + 1, k + 1), dtype=np.int8)
        for i in range(1, n + 1):
            H[i, 0] = i * skip if anchored else -i * 0.1  # un-anchored: mild preference for starting at the top
            P[i, 0] = 1
        for j in range(1, k + 1):
            P[0, j] = 2
        for i in range(1, n + 1):
            for j in range(1, k + 1):
                c = (H[i - 1, j - 1] + S[j - 1, i - 1], H[i - 1, j] + skip, H[i, j - 1] + skip)
                a = int(np.argmax(c))
                H[i, j], P[i, j] = c[a], a
        bi = max(range(n + 1), key=lambda i: (H[i, k], -i))
        i, j, pairs = bi, k, {}
        while i > 0 and j > 0:
            a = P[i, j]
            if a == 0:
                pairs[lo + i - 1] = j - 1
                i, j = i - 1, j - 1
            elif a == 1:
                i -= 1
            else:
                j -= 1
        return pairs, float(H[bi, k])

    def _commit(self, a, b, pairs, cut):
        """Judge chords a..b-1 using the alignment, then drop the onsets that belong to them."""
        for k in range(a, b):
            c = self.chords[k]
            if k in pairs:
                self.recs[k] = judge(self.chords, k, self.evs[pairs[k]], self.times[pairs[k]])
            else:
                self.recs[k] = dict(i=k, measure=c.measure, beat=c.beat + 1, expected=list(c.notes), missing=[],
                                    extra=[], subs=[], status="skipped", t=None)
        self.evs, self.times = self.evs[cut:], self.times[cut:]
        self.committed = b

    def handle(self, ev, t):
        n = len(self.chords)
        if n == 0:
            return
        self.n_onsets += 1
        self.heard = [note_name(m) for m in ev.detected()]
        self.evs.append(ev)
        self.times.append(t)
        self._step(final=False)

    def _step(self, final):
        n = len(self.chords)
        while self.evs:
            anchored = self.committed is not None
            lo = self.committed if anchored else 0
            if anchored:
                hi = min(n, self.mend[min(lo, n - 1)] + 70)
            else:
                if len(self.evs) < 8 and not final:
                    return  # too little history to tell repeated passages apart
                hi = n
            if lo >= n:
                self.evs, self.times = [], []
                break
            if not anchored and self.shift == 0 and not self.shift_locked:
                # you may be playing the piece an octave or two away from what is written
                res = {}
                for k in (0, 12, -12, 24, -24):
                    self._set_shift(k)
                    res[k] = self._align(lo, hi, False)
                best_k = max(res, key=lambda k: res[k][1] - (0 if k == 0 else 2.5))
                self._set_shift(best_k)
                pairs = res[best_k][0]
                if best_k:
                    self.status = f"Hearing you {abs(best_k) // 12} octave(s) {'higher' if best_k > 0 else 'lower'} than written"
            else:
                pairs, _ = self._align(lo, hi, anchored)
            if not pairs:
                if final:
                    self.evs, self.times = [], []
                return
            last = max(pairs)
            for k, j in pairs.items():
                self.track[k] = self.times[j]
            if self.cur is None or last >= self.cur[0]:
                self.cur = (last, self.times[pairs[last]])
            self.pos = max(self.pos, last + 1)
            first = min(pairs)
            a = lo if anchored else self.mstart[first]
            b = self.mend[a]
            beyond = [k for k in pairs if k >= b]
            if len(beyond) >= 2 and (anchored or len(pairs) >= 10 or final):
                self.shift_locked = True
                cut = min(pairs[k] for k in beyond)
                self._commit(a, b, pairs, cut)
                continue
            if final:  # judge everything up to the last chord that was heard
                self._commit(a, last + 1, pairs, len(self.evs))
                self.pos = last + 1
            return
        if self.committed is not None and self.committed >= n:
            self.status = "Finished the piece."
        self.save()

    def flush(self):
        """Judge whatever has been collected (called on silence or Stop)."""
        if self.evs:
            self._step(final=True)
        self.save()

    def stop(self):
        self.running = False
        self.flush()
        self.status = "Stopped. Press Start for a new practice."
        self.save()


# ----------------------------------------------------------------------------- audio worker
class Worker(threading.Thread):
    def __init__(self, sess, args):
        super().__init__(daemon=True)
        self.sess, self.args = sess, args
        self.q = queue.Queue()
        self.audio = np.zeros(SR * MAX_SECONDS, np.float32)
        self.n = 0
        self.det = LiveOnsets(args.gate)
        self.pending = []  # onset times awaiting enough audio
        self.last_peak = None
        self.stream = None
        self.sim = None

    # microphone ---------------------------------------------------------------------------
    def open_mic(self):
        import sounddevice as sd
        dev = self.args.device
        try:
            self.fs = SR
            self.stream = sd.InputStream(samplerate=SR, channels=1, device=dev, dtype="float32",
                                         callback=self._cb, blocksize=1024)
        except Exception:
            self.fs = 44100
            self.zi = None
            self.stream = sd.InputStream(samplerate=44100, channels=1, device=dev, dtype="float32",
                                         callback=self._cb, blocksize=2048)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        x = indata[:, 0].copy()
        if self.fs != SR:
            from scipy.signal import resample_poly
            x = resample_poly(x, 1, 2).astype(np.float32)  # block-wise; fine for pitch work
        self.q.put(x)

    def close_mic(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    # simulated playback of a wav (testing) ---------------------------------------------------
    def start_sim(self):
        x, fs = pc.load_wav(self.args.wav)
        x = pc.to_sr(x, fs) * 0.2
        self.sim = x
        self.sim_pos = 0
        self.sim_t0 = time.time()

    def _sim_feed(self):
        want = int((time.time() - self.sim_t0) * SR * self.args.speed)
        while self.sim_pos < min(want, len(self.sim)):
            e = min(self.sim_pos + 1024, len(self.sim))
            self.q.put(self.sim[self.sim_pos:e])
            self.sim_pos = e

    # main loop --------------------------------------------------------------------------------
    def begin(self):
        self.n = 0
        self.audio[:] = 0
        self.det.reset()
        self.pending = []
        self.last_peak = None
        while not self.q.empty():
            self.q.get_nowait()
        if self.args.wav:
            self.start_sim()
        else:
            self.open_mic()

    def end(self):
        try:  # keep the take so odd results can be re-analysed afterwards
            import soundfile as sf
            sf.write(os.path.join(HERE, "last_take.wav"), self.audio[:self.n], SR)
        except Exception:
            pass
        if self.args.wav:
            self.sim = None
        else:
            self.close_mic()

    def run(self):
        with np.errstate(all="ignore"):  # numpy error settings are per thread
            self._run()

    def _run(self):
        s = self.sess
        was = False
        while True:
            if s.running and not was:
                with s.lock:
                    self.begin()
                was = True
            if not s.running and was:
                self.end()
                was = False
            if not s.running:
                time.sleep(0.05)
                continue
            if self.sim is not None:
                self._sim_feed()
            try:
                x = self.q.get(timeout=0.05)
            except queue.Empty:
                x = None
            if x is not None and len(x):
                e = min(self.n + len(x), len(self.audio))
                self.audio[self.n:e] = x[:e - self.n]
                self.n = e
                s.level = float(min(1.0, np.sqrt(np.mean(x ** 2)) * 10))
                s.audio_now = self.n / SR
                for t, strength in self.det.feed(x):
                    last = self.last_peak
                    if last and t - last[0] < 0.2 and strength < last[1]:
                        continue  # echo of the previous strike
                    if self.pending and t - self.pending[-1][0] < 0.08:
                        self.pending[-1] = (t, strength)  # same strike seen twice: keep the later, stronger peak
                    else:
                        self.pending.append((t, strength))
                    self.last_peak = (t, strength)
            now = self.n / SR
            while self.pending and now >= self.pending[0][0] + 0.5:
                t = self.pending.pop(0)[0]
                nxt = self.pending[0][0] if self.pending else None
                ev = Evidence(self.audio, t, nxt)
                with s.lock:
                    if s.running:
                        s.handle(ev, t)
                self.last_onset = t
            if s.evs and now - getattr(self, "last_onset", 0) > 3.0:
                with s.lock:
                    if s.running:
                        s.flush()  # you paused: judge what has been played so far


# ----------------------------------------------------------------------------- http
def read_history():
    try:
        with open(HISTORY, encoding="utf8") as f:
            return [p for p in json.load(f) if os.path.isfile(p)]
    except (OSError, ValueError):
        return []


def remember_score(path):
    """Keep the files you opened, most recent first, so they can be switched back to."""
    hist = [path] + [p for p in read_history() if os.path.normcase(p) != os.path.normcase(path)]
    try:
        with open(HISTORY, "w", encoding="utf8") as f:
            json.dump(hist[:30], f)
    except OSError:
        pass


def library(sess):
    """Scores you can pick from: ones you opened before (newest first), then others in this folder."""
    paths = read_history()
    for d in (HERE, SCORES):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if f.lower().endswith((".musicxml", ".mxl", ".xml")) and os.path.isfile(p) and p not in paths:
                    paths.append(p)
    if sess.score_path and os.path.abspath(sess.score_path) not in paths:
        paths.insert(0, os.path.abspath(sess.score_path))
    lib = {}
    for p in paths:
        name = os.path.basename(p)
        if name in lib and os.path.normcase(lib[name]) != os.path.normcase(p):
            name = f"{name}  ({os.path.basename(os.path.dirname(p))})"
        lib.setdefault(name, p)
    return lib


def make_handler(sess, worker):
    page = open(os.path.join(HERE, "live_page.html"), encoding="utf8").read()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json"):
            b = body.encode("utf8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/state":
                with sess.lock:
                    if hasattr(sess, "saved") and not sess.running:
                        d = dict(sess.saved, running=False, status=sess.status, level=0, next=None, heard=[])
                    else:
                        d = sess.to_json()
                self._send(json.dumps(d))
            elif self.path == "/layout":
                with sess.lock:
                    lay = [dict(q=sess.qpos[i], mi=sess.nums.index(c.measure) if c.measure in sess.nums else 0,
                                onset=c.beat, measure=c.measure) for i, c in enumerate(sess.chords)]
                self._send(json.dumps(lay))
            elif self.path == "/scores":
                with sess.lock:
                    lib = library(sess)
                    cur = next((k for k, v in lib.items() if sess.score_path and
                                os.path.normcase(os.path.abspath(v)) == os.path.normcase(os.path.abspath(sess.score_path))), None)
                    self._send(json.dumps(dict(current=cur, files=list(lib))))
            elif self.path == "/score.xml":
                with sess.lock:
                    xml = sess.xml_text
                if not xml:
                    self.send_error(404)
                    return
                self._send(xml, "application/xml")
            else:
                self._send(page, "text/html")

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            if self.path == "/start":
                with sess.lock:
                    try:
                        if not sess.chords:
                            raise ValueError("choose a music file first")
                        sess.running = False
                        if hasattr(sess, "saved"):
                            del sess.saved
                        sess.new_practice(body.get("measures", ""))
                        sess.running = True
                    except Exception as e:  # bad measure range etc.
                        sess.status = f"Could not start: {e}"
            elif self.path == "/stop":
                with sess.lock:
                    sess.stop()
            elif self.path in ("/load", "/upload"):
                try:
                    with sess.lock:
                        if self.path == "/upload":
                            import base64
                            name = os.path.basename(body["name"])
                            if not name.lower().endswith((".musicxml", ".mxl", ".xml")):
                                raise ValueError("choose a .musicxml, .xml or .mxl file")
                            os.makedirs(SCORES, exist_ok=True)
                            path = os.path.join(SCORES, name)
                            with open(path, "wb") as f:
                                f.write(base64.b64decode(body["data"]))
                        else:
                            path = library(sess)[body["name"]]
                        sess.set_score(path)
                except Exception as e:
                    sess.status = f"Could not open that file: {e}"
            self._send("{}")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("score", nargs="?")
    ap.add_argument("--device", help="input device id or name")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--wav", help="simulate live input from a recording instead of the microphone")
    ap.add_argument("--speed", type=float, default=1.0, help="playback speed for --wav")
    ap.add_argument("--gate", type=float, default=0.001, help="minimum loudness for a note (raise in noisy rooms)")
    ap.add_argument("--no-jumps", action="store_true")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if a.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if a.device and a.device.isdigit():
        a.device = int(a.device)
    sess = Session(not a.no_jumps)
    start = a.score
    if not start and os.path.exists(LAST_SCORE):
        try:
            start = open(LAST_SCORE, encoding="utf8").read().strip()
        except OSError:
            start = None
    if start and os.path.exists(start):
        sess.set_score(start)
    w = Worker(sess, a)
    w.start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(sess, w))
    url = f"http://127.0.0.1:{a.port}/"
    print("Open", url, "(Ctrl+C to quit)")
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
