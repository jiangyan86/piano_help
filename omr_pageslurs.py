"""Ties and slurs that run across a page break.

Audiveris finds a tie or slur only when both of its ends are in the same page (or the arc is passed on to the next
*system* of the same page).  An arc that leaves the last bar of a page, and the stub that comes in at the start of the
next page, are not recognised at all, so the tie / slur between the last chord of one page and the first chord of the
next one is missing from the MusicXML.

Here the arc is looked for in the page picture: in the last bar of the last system of a page, to the right of the last
chord of a staff, a thin curved stroke that runs on to the final barline can only be a tie or slur that continues on the
next page.  It is then written into the score between that chord and the first chord of the next page:
  * notes of the same pitch in both chords -> ties (one per arc seen, the upper arcs on the upper notes, the lower arcs
    on the lower notes);
  * otherwise -> a slur from the outer note on that side to the matching note of the next chord.
Nothing is added where Audiveris already wrote a tie or slur on those notes.

    apply_to_score(root, stacks, page_images, offset) -> list of what was added
"""
import xml.etree.ElementTree as ET
from fractions import Fraction

import cv2
import numpy as np

from omr_ottava import _notes_of_staff, _slot_x


def arcs_at_end(gray, x_from, x_to, staff):
    """Thin curved strokes in a staff's band between x_from and x_to that run to x_to.  [(side, left y, width)]"""
    top, bottom = staff[0], staff[1]
    u = (bottom - top) / 4.0
    x0, x1 = int(x_from + 1.2 * u), int(x_to - 0.35 * u)
    if x1 - x0 < 4 * u:
        return []
    y0 = max(0, int(top - 3.4 * u))
    y1 = min(gray.shape[0], int(bottom + 3.4 * u))
    reg = (gray[y0:y1, x0:x1] < 128).astype(np.uint8)
    t = max(1, int(round(u * 0.14)))
    for k in range(5):                                        # take the staff lines out, the arcs cross them in gaps
        yk = int(round(top + k * u)) - y0
        reg[max(0, yk - t - 1): yk + t + 2, :] = 0
    close = cv2.morphologyEx(reg, cv2.MORPH_CLOSE, np.ones((int(0.45 * u) | 1, 1), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(close, connectivity=8)
    width = x1 - x0
    found = []
    for i in range(1, n):
        x, y, w, h, _a = st[i]
        if w < 0.45 * width or w < 4 * u or h > 2.6 * u or w / float(h) < 5:
            continue
        if x > 4 * u or x + w < width - 4 * u:                # starts at the chord, runs (nearly) to the barline; nested
            continue                                          # ties of a chord are a little shorter than the outer one
        cols = np.where((lab[:, x:x + w] == i).any(axis=0))[0]

        def ymean(c):
            return float(np.mean(np.where(lab[:, x + c] == i)[0]))
        ends = (ymean(cols[0]) + ymean(cols[-1])) / 2.0
        mid = ymean(cols[len(cols) // 2])
        side = "above" if mid < ends - 0.2 * u else "below" if mid > ends + 0.2 * u else "flat"
        # stacked arcs (the ties of a chord) can touch at their ends and come out as one stroke: count them in the middle
        runs = 1
        for c in range(int(len(cols) * 0.35), int(len(cols) * 0.65) + 1):
            col = lab[:, x + cols[min(c, len(cols) - 1)]] == i
            runs = max(runs, int(np.sum(col[1:] & ~col[:-1])) + int(col[0]))
        for r in range(runs):
            found.append((side, y0 + y, int(w)))
    return sorted(found, key=lambda f: f[1])


def _pitch(n):
    p = n.find("pitch")
    return None if p is None else (p.findtext("step"), p.findtext("alter", "0"), p.findtext("octave"))


def _has_arc(n):
    return n.find("tie") is not None or n.find(".//slur") is not None


def _notations(n):
    nt = n.find("notations")
    if nt is None:
        nt = ET.SubElement(n, "notations")
    return nt


def _add_tie(n, kind):
    # <tie> goes before <voice>/<type>...; keep the order the schema wants: after <duration>
    t = ET.Element("tie", {"type": kind})
    dur = n.find("duration")
    n.insert(list(n).index(dur) + 1 if dur is not None else 0, t)
    # a second tie element (stop then start) must stay in document order
    tied = ET.Element("tied", {"type": kind})
    nt = _notations(n)
    nt.insert(0 if kind == "stop" else len(nt), tied)


def _add_slur(n, kind, placement):
    ET.SubElement(_notations(n), "slur", {"type": kind, "number": "1", "placement": placement})


def apply_to_score(root, stacks, page_images, offset=0):
    parts = root.findall("part")
    bars, divs = {}, {}
    for pi, part in enumerate(parts):
        div = 1
        for m in part.findall("measure"):
            at = m.find("attributes")
            if at is not None and at.find("divisions") is not None:
                div = int(at.find("divisions").text)
            g = offset + int(m.get("number"))
            bars[(g, pi)] = m
            divs[(g, pi)] = div
    added = []
    for g, s in enumerate(stacks, 1):
        if g >= len(stacks) or stacks[g]["sheet"] == s["sheet"]:        # only the last bar of a sheet
            continue
        nxt = stacks[g]
        gray = cv2.imread(page_images[s["sheet"] - 1], 0) if s["sheet"] - 1 < len(page_images) else None
        if gray is None:
            continue
        for pi, staves in enumerate(s["parts"]):
            m0, m1 = bars.get((g, pi)), bars.get((g + 1, pi))
            if m0 is None or m1 is None:
                continue
            for si, st in enumerate(staves):
                staff = str(si + 1)
                seq = _notes_of_staff(m0, staff, divs[(g, pi)])
                # the notes that last to the end of the bar, and of those the latest chord
                ends = []
                for n, t in seq:
                    if n.find("chord") is not None or n.find("grace") is not None or _pitch(n) is None:
                        continue
                    ends.append((t + Fraction(int(n.findtext("duration")), 4 * divs[(g, pi)]), t))
                if not ends:
                    continue
                bar_end = max(e for e, _t in ends)
                t_last = max(t for e, t in ends if e == bar_end)
                chord0 = [n for n, t in seq if t == t_last and _pitch(n) is not None]
                x = _slot_x(s, t_last)
                if x is None or not chord0:
                    continue
                arcs = arcs_at_end(gray, x, s["right"], st)
                if not arcs:
                    continue
                chord1 = [n for n, t in _notes_of_staff(m1, staff, divs[(g + 1, pi)]) if t == 0 and _pitch(n) is not None]
                if not chord1 or any(_has_arc(n) for n in chord0) or any(n.find(".//slur") is not None for n in chord1):
                    continue
                key = lambda n: (int(_pitch(n)[2]), "CDEFGAB".index(_pitch(n)[0]))
                same = {}
                for n in chord0:
                    for k in chord1:
                        if _pitch(n) == _pitch(k) and id(k) not in same.values():
                            same[id(n)] = id(k)
                            break
                by_id0 = {id(n): n for n in chord0}
                by_id1 = {id(n): n for n in chord1}
                up = [f for f in arcs if f[0] in ("above", "flat")]
                down = [f for f in arcs if f[0] == "below"]
                if same:
                    cand = sorted((by_id0[i] for i in same), key=key, reverse=True)          # high to low
                    rest = cand[len(up):]
                    pick = cand[:len(up)] + (rest[-len(down):] if down else [])
                    for n in pick:
                        k = by_id1[same[id(n)]]
                        _add_tie(n, "start")
                        _add_tie(k, "stop")
                    added.append(dict(kind="tie", part=parts[pi].get("id"), bar=int(m0.get("number")), staff=staff, notes=len(pick)))
                else:
                    top = max(chord0, key=key)
                    low = min(chord0, key=key)
                    for side, n0, c1 in (("above", top, max(chord1, key=key)), ("below", low, min(chord1, key=key))):
                        if any(f[0] == side for f in arcs) or (side == "above" and any(f[0] == "flat" for f in arcs)):
                            _add_slur(n0, "start", side)
                            _add_slur(c1, "stop", side)
                    added.append(dict(kind="slur", part=parts[pi].get("id"), bar=int(m0.get("number")), staff=staff, notes=1))
    return added
