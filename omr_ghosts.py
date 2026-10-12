"""Extra notes that are not on the page.

Next to a real note, Audiveris sometimes finds a second notehead that is only a smudge: typically the bowl of a flat
(or the blur around a note) beside a chord note, one step above or below it, read as the other half of a "second".
It is a poor match to a notehead (the project file grades it far below the real ones), it is joined to a real head of
the same chord, and the picture has hardly any ink where it sits.  The rhythm still adds up, so nothing else notices.

A head is taken as a ghost only when all of these hold:
  * its own grade is below 0.35 (real heads are 0.6-0.8),
  * it is in a chord with a head graded 0.55 or better,
  * in its box, leaving out the parts covered by other symbols (stems, other heads, accidentals), the ink covers
    less than 30% (a real open head, however thin its outline, covers more).
The note is removed from the score and the bar gets a small text mark so it is easy to check.

    apply_to_score(root, stacks, omr_path, page_images, offset) -> list of what was removed
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

import cv2
import numpy as np

from omr_ottava import _notes_of_staff, _slot_x

MARK = "auto: extra note removed - please verify"
STEP = "CDEFGAB"


def _box(e):
    b = e.find("bounds")
    return None if b is None else [int(float(b.get(k))) for k in "xywh"]


def find_ghosts(omr_path, page_images):
    """[dict(sheet, x, y, neighbour_x, pos, chord_positions, index)] for every suspected ghost head."""
    z = zipfile.ZipFile(omr_path)
    names = sorted((n for n in z.namelist() if re.match(r"sheet#\d+/sheet#\d+\.xml$", n)),
                   key=lambda s: int(re.search(r"#(\d+)/", s).group(1)))
    out = []
    for name in names:
        sheet = int(re.search(r"#(\d+)/", name).group(1))
        if sheet - 1 >= len(page_images):
            continue
        root = ET.fromstring(z.read(name))
        low = [h for h in root.iter("head") if float(h.get("grade", "1")) < 0.35]
        if not low:
            continue
        img = cv2.imread(page_images[sheet - 1], 0) < 128
        heads = {e.get("id"): e for e in root.iter("head")}
        chord_of, members = {}, defaultdict(list)
        for rel in root.iter("relation"):
            if rel.find("containment") is not None and rel.get("target") in heads and rel.get("source") not in heads:
                chord_of[rel.get("target")] = rel.get("source")
                members[rel.get("source")].append(rel.get("target"))
        boxes = []
        for e in root.iter():
            if e.tag in ("head", "stem", "alter") and _box(e):
                boxes.append((e, _box(e)))
        for h in low:
            c = chord_of.get(h.get("id"))
            mates = [m for m in members.get(c, []) if m != h.get("id")]
            good = [m for m in mates if float(heads[m].get("grade", "0")) >= 0.55]
            if not good:
                continue
            x, y, w, hh = _box(h)
            free = np.ones((hh, w), bool)
            for o, (a, b, cw, ch) in boxes:
                if o is h:
                    continue
                x0, x1, y0, y1 = max(x, a), min(x + w, a + cw), max(y, b), min(y + hh, b + ch)
                if x1 > x0 and y1 > y0:
                    free[y0 - y:y1 - y, x0 - x:x1 - x] = False
            if free.mean() < 0.5:
                continue                                      # too much of it is hidden behind other symbols to judge
            sub = img[max(0, y):y + hh, max(0, x):x + w]
            if sub.shape != free.shape:
                continue
            if (sub & free).sum() / float(free.sum()) >= 0.30:
                continue
            chord = sorted((heads[m] for m in members[c]), key=lambda e: int(e.get("pitch", "0")))   # high to low
            nb = heads[good[0]]
            nbx = _box(nb)
            out.append(dict(sheet=sheet, x=x + w / 2.0, y=y + hh / 2.0, nb_x=nbx[0] + nbx[2] / 2.0,
                            index=[e.get("id") for e in chord].index(h.get("id")), size=len(chord)))
    return out


def _pitch_key(n):
    p = n.find("pitch")
    return int(p.findtext("octave")) * 7 + STEP.index(p.findtext("step"))


def apply_to_score(root, stacks, omr_path, page_images, offset=0):
    ghosts = find_ghosts(omr_path, page_images)
    if not ghosts:
        return []
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
    done = []
    for gh in ghosts:
        hit = None
        for g, s in enumerate(stacks, 1):
            if s["sheet"] == gh["sheet"] and s["left"] <= gh["nb_x"] <= s["right"]:
                best = None
                for pi, staves in enumerate(s["parts"]):
                    for si, st in enumerate(staves):
                        u = (st[1] - st[0]) / 4.0
                        d = max(0.0, st[0] - gh["y"], gh["y"] - st[1])
                        if d < 5 * u and (best is None or d < best[0]):
                            best = (d, pi, si + 1)
                if best:
                    hit = (g, s, best[1], str(best[2]))
                    break
        if not hit:
            continue
        g, s, pi, staff = hit
        m = bars.get((g, pi))
        if m is None or not s.get("slots"):
            continue
        # the moment: the time slot nearest to the real head the ghost sits against
        t = min(s["slots"], key=lambda sl: abs(s["left"] + sl[0] - gh["nb_x"]))[1]
        seq = [(n, tt) for n, tt in _notes_of_staff(m, staff, divs[(g, pi)]) if tt == t and n.find("pitch") is not None]
        if len(seq) != gh["size"]:
            continue                                          # the file's chord is not the project's chord: leave it
        notes = sorted((n for n, _t in seq), key=_pitch_key, reverse=True)
        victim = notes[gh["index"]]
        items = list(m)
        k = items.index(victim)
        if victim.find("chord") is None:                      # it led the chord: the next note takes over
            nxt = next((e for e in items[k + 1:] if e.tag == "note"), None)
            if nxt is None or nxt.find("chord") is None:
                continue
            nxt.remove(nxt.find("chord"))
            for tag in ("beam", "notations"):
                for ch in victim.findall(tag):
                    if nxt.find(tag) is None:
                        nxt.append(ch)
        m.remove(victim)
        first = next(e for e in m if e.tag == "note" and (e.findtext("staff") or "1") == staff)
        d = ET.Element("direction", {"placement": "above"})
        ET.SubElement(ET.SubElement(d, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = MARK
        ET.SubElement(d, "staff").text = staff
        m.insert(list(m).index(first), d)
        done.append(dict(part=parts[pi].get("id"), bar=int(m.get("number")), staff=staff))
    return done
