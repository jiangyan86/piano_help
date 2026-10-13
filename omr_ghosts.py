"""Extra notes that are not on the page.

Next to a real note, Audiveris sometimes finds a second notehead that is only a smudge: typically the bowl of a flat or
the whole of a natural / sharp sign beside a chord note, read as a notehead one step away from it (or on the very same
line, as an unison).  It is a poor match to a notehead (the project file grades it far below the real ones) and it is
joined to a real head of the same chord.  The rhythm still adds up, so nothing else notices.

A head is taken as a ghost when its own grade is below 0.35, the chord has a head graded 0.55 or better, and
  * it sits on the same staff line or space as another, better head of the chord (two notes on one pitch in one chord:
    this is an accidental that was read as a notehead), or
  * in its box, leaving out the parts covered by other symbols (stems, other heads, accidentals), the ink covers less
    than 30% (a real open head, however thin its outline, covers more).
The note is removed from the score and the bar gets a small text mark so it is easy to check.  In the first case the
ghost was the accidental of the real head: when the key signature alters that note (a B in a key with B flat), a
natural sign is written on it, since that is the only thing the sign can have been.

    apply_to_score(root, stacks, omr_path, page_images, offset) -> list of what was removed
"""
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from omr_locate import Locator, box, load_sheets, STEP
from omr_ottava import _notes_of_staff

MARK = "auto: extra note removed - please verify"
SHARPS, FLATS = "FCGDAEB", "BEADGCF"


def find_ghosts(sheets, page_images):
    """[(sheet number, [heads of the chord, high to low], ghost head, same-line partner head or None)]"""
    out = []
    for number, sh in sheets.items():
        if number - 1 >= len(page_images):
            continue
        low = [h for h in sh.heads.values() if float(h.get("grade", "1")) < 0.35]
        if not low:
            continue
        img = cv2.imread(page_images[number - 1], 0) < 128
        boxes = [(e, box(e)) for e in sh.root.iter() if e.tag in ("head", "stem", "alter") and box(e)]
        for h in low:
            chord = sh.chord_heads(h.get("id"))
            good = [m for m in chord if m is not h and float(m.get("grade", "0")) >= 0.55]
            if not good:
                continue
            twin = next((m for m in good if m.get("pitch") == h.get("pitch")), None)
            if twin is None:
                x, y, w, hh = box(h)
                free = np.ones((hh, w), bool)
                for o, (a, b, cw, ch) in boxes:
                    if o is h:
                        continue
                    x0, x1, y0, y1 = max(x, a), min(x + w, a + cw), max(y, b), min(y + hh, b + ch)
                    if x1 > x0 and y1 > y0:
                        free[y0 - y:y1 - y, x0 - x:x1 - x] = False
                if free.mean() < 0.5:
                    continue                                  # too much of it is hidden behind other symbols to judge
                sub = img[max(0, y):y + hh, max(0, x):x + w]
                if sub.shape != free.shape or (sub & free).sum() / float(free.sum()) >= 0.30:
                    continue
            out.append((number, chord, h, twin))
    return out


def _key_alters(root, part_index, measure):
    """{step: alter} the key signature gives in force at a measure"""
    fifths = 0
    for m in root.findall("part")[part_index].findall("measure"):
        k = m.find("attributes/key/fifths")
        if k is not None:
            fifths = int(k.text)
        if m is measure:
            break
    if fifths >= 0:
        return {s: 1 for s in SHARPS[:fifths]}
    return {s: -1 for s in FLATS[:-fifths]}


def _make_natural(note, key, measure, staff, div):
    p = note.find("pitch")
    if p is None or int(p.findtext("alter", "0")) == 0 or key.get(p.findtext("step")) != int(p.findtext("alter", "0")):
        return False
    old = int(p.findtext("alter"))
    p.remove(p.find("alter"))
    # an accidental holds for the rest of the bar: later notes of this staff on the same line or space follow it
    seq = _notes_of_staff(measure, staff, div)
    t0 = next((t for n, t in seq if n is note), None)
    for n, t in seq:
        q = n.find("pitch")
        if t0 is not None and t > t0 and q is not None and n.find("accidental") is None                 and q.findtext("step") == p.findtext("step") and q.findtext("octave") == p.findtext("octave")                 and q.find("alter") is not None and int(q.findtext("alter")) == old:
            q.remove(q.find("alter"))
    if note.find("accidental") is None:
        acc = ET.Element("accidental")
        acc.text = "natural"
        at = max((i for i, e in enumerate(note) if e.tag in ("type", "dot")), default=-1)
        note.insert(at + 1, acc)
    return True


def apply_to_score(root, stacks, omr_path, page_images, offset=0, sheets=None):
    sheets = sheets or load_sheets(omr_path)
    ghosts = find_ghosts(sheets, page_images)
    if not ghosts:
        return []
    loc = Locator(root, stacks, offset)
    done = []
    for number, chord, ghost, twin in ghosts:
        found = loc.chord_notes(sheets[number], number, chord)
        if not found:
            continue
        m, staff, notes = found
        victim = notes[chord.index(ghost)]
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
        natural = False
        if twin is not None:
            pi = next(i for i, p in enumerate(root.findall("part")) if m in list(p))
            partner = notes[chord.index(twin)]
            natural = _make_natural(partner, _key_alters(root, pi, m), m, staff, loc.divs[(next(g for (g, pj), mm in loc.bars.items() if mm is m), pi)])
        first = next(e for e in m if e.tag == "note" and (e.findtext("staff") or "1") == staff)
        d = ET.Element("direction", {"placement": "above"})
        ET.SubElement(ET.SubElement(d, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = \
            MARK + (" (natural sign added)" if natural else "")
        ET.SubElement(d, "staff").text = staff
        m.insert(list(m).index(first), d)
        part_id = next(p.get("id") for p in root.findall("part") if m in list(p))
        done.append(dict(part=part_id, bar=int(m.get("number")), staff=staff, natural=natural))
    return done
