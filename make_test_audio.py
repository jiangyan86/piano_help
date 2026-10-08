"""Synthesize a piano-ish take of a score with deliberate mistakes, to test piano_check.py.
python make_test_audio.py SCORE out.wav [measures lo-hi]"""
import sys
import numpy as np
import soundfile as sf
from piano_check import build_chords, note_name

SR = 22050


def tone(m, dur, vel=0.5):
    f = 440 * 2 ** ((m - 69) / 12)
    t = np.arange(int(SR * dur)) / SR
    y = np.zeros_like(t)
    for h in range(1, 9):
        y += (0.6 ** (h - 1)) * np.sin(2 * np.pi * f * h * (1 + 0.0003 * h * h) * t) * np.exp(-t * (1.2 + 0.5 * h))
    y *= np.minimum(t / 0.004, 1)
    return vel * y


def main():
    score, out = sys.argv[1], sys.argv[2]
    mr = None
    if len(sys.argv) > 3:
        lo, _, hi = sys.argv[3].partition("-")
        mr = (int(lo), int(hi))
    chords = build_chords(score, True, mr)
    rng = np.random.default_rng(1)
    buf = np.zeros(int(SR * (len(chords) * 0.6 + 5)))
    t = 1.0
    truth = []
    for i, c in enumerate(chords):
        notes = list(c.notes)
        if i % 17 == 5:  # wrong key
            k = rng.integers(len(notes))
            old = notes[k]
            notes[k] += int(rng.choice([-2, -1, 1, 2]))
            truth.append(f"m.{c.measure}+{c.beat:g}: {note_name(old)} -> {note_name(notes[k])}")
        elif i % 23 == 9 and len(notes) > 1:  # missing key
            old = notes.pop(0)
            truth.append(f"m.{c.measure}+{c.beat:g}: missing {note_name(old)}")
        for m in notes:
            y = tone(m, 1.5)
            a = int(t * SR)
            buf[a:a + len(y)] += y
        t += 0.5 + 0.1 * rng.random()
    buf += 0.003 * rng.standard_normal(len(buf))
    buf /= np.abs(buf).max()
    sf.write(out, buf[: int((t + 2) * SR)], SR)
    print("Injected mistakes:")
    print("\n".join(truth))


if __name__ == "__main__":
    main()
