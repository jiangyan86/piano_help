"""Grace notes (the small notes before a main note).

Audiveris does not read them.  What it makes of one is a mess: a small notehead is sometimes taken for a real, badly
graded note (a spurious extra note, often in another voice), and the sharp or flat in front of a grace note is attached
to the main note, so the main note comes out with the wrong pitch.

Here the small noteheads are found in the page picture: an oval about 0.9 staff spaces wide and 0.8 high (a normal
head is 1.3 by 1.1), with a stem going up for about two spaces.  For each group of them before a main note:
  * the pitch comes from the staff line or space (relative to the main note, whose pitch is known),
  * the sign in front of it (found in the project file) is taken off the main note and given to the grace note,
  * any badly graded "note" the project file made out of the same noteheads is removed from the score,
  * grace notes (slashed, as acciaccaturas) are written before the main note.
Only a group whose main note can be tied to the notes of the score is touched.  The bar gets a text mark.

    find_graces(gray, staves, normal_boxes) -> [dict(cx, cy, stem_x, staff, pos, ...)]
    apply_to_score(root, stacks, omr_path, page_images, offset) -> list of what was added
"""
import xml.etree.ElementTree as ET
from fractions import Fraction

import cv2
import numpy as np

from omr_locate import Locator, box, load_sheets, pitch_key, STEP
from omr_ottava import _notes_of_staff

MARK = "auto: grace note added - please verify"
SHARPS, FLAGS_ = "FCGDAEB", "BEADGCF"
ALTER = {"SHARP": 1, "FLAT": -1, "NATURAL": 0, "DOUBLE_SHARP": 2, "DOUBLE_FLAT": -2}
ACC_NAME = {1: "sharp", -1: "flat", 0: "natural", 2: "double-sharp", -2: "flat-flat"}


def find_graces(gray, staves, stack_spans, normal_boxes):
    """staves: [(top, bottom, left, right)]; stack_spans: [(left, right)] of the bars of the page."""
    bw = (gray < 128).astype(np.uint8)
    u = float(np.median([(s[1] - s[0]) / 4.0 for s in staves]))
    k = int(round(0.5 * u)) | 1
    op = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, max(3, int(0.4 * u) | 1))))
    n, lab, st, _ = cv2.connectedComponentsWithStats(op, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if not (0.5 * u <= w <= 1.1 * u and 0.78 * u <= h <= 0.95 * u and a >= 0.5 * w * h and w <= 1.3 * h):
            continue
        cx, cy = x + w / 2.0, y + h / 2.0
        if not any(l + 0.3 * u <= cx <= r - 0.8 * u for l, r in stack_spans):
            continue
        near = [j for j, s in enumerate(staves) if s[0] - 3.5 * u <= cy <= s[1] + 3.5 * u]
        if not near:
            continue
        if any(b[0] - 2 <= cx <= b[0] + b[2] + 2 and b[1] - 2 <= cy <= b[1] + b[3] + 2 for b in normal_boxes):
            continue
        xr = int(x + w - 2)
        col = bw[max(0, int(y - 2.6 * u)):int(y + h / 2), xr - 1:xr + 2].any(axis=1)
        run = best = 0
        for v in col[::-1]:
            run = run + 1 if v else 0
            best = max(best, run)
        if best < 1.5 * u:
            continue
        j = min(near, key=lambda j: abs(cy - (staves[j][0] + staves[j][1]) / 2.0))
        top, bottom = staves[j][0], staves[j][1]
        out.append(dict(cx=cx, cy=cy, w=w, h=h, stem_x=x + w - 1, ytop=int(y + h / 2) - best, staff=j, u=u,
                        pos=int(round((cy - (top + bottom) / 2.0) / (u / 2.0)))))
    out.sort(key=lambda g: (g["staff"], g["cx"]))
    return out


def _groups(graces):
    groups = []
    for g in graces:
        if groups and groups[-1][-1]["staff"] == g["staff"] and 0 < g["cx"] - groups[-1][-1]["cx"] <= 2.6 * g["u"]:
            groups[-1].append(g)
        else:
            groups.append([g])
    return groups


def _key_alter(root, pi, measure, step):
    fifths = 0
    for m in root.findall("part")[pi].findall("measure"):
        k = m.find("attributes/key/fifths")
        if k is not None:
            fifths = int(k.text)
        if m is measure:
            break
    if fifths >= 0:
        return 1 if step in SHARPS[:fifths] else 0
    return -1 if step in FLAGS_[:-fifths] else 0


def _remove_note(measure, note, keep_time=True):
    """take a note out of a bar; by default the others keep their times (a <forward> takes its place)"""
    items = list(measure)
    k = items.index(note)
    if not keep_time and note.find("chord") is None and note.find("duration") is not None:
        d = int(note.findtext("duration"))                  # the voice is shorter: so is its way back
        for e in items[k + 1:]:
            if e.tag == "backup":
                e.find("duration").text = str(max(0, int(e.findtext("duration")) - d))
                break
        measure.remove(note)
        return
    if note.find("chord") is None:
        nxt = items[k + 1] if k + 1 < len(items) else None
        if nxt is not None and nxt.tag == "note" and nxt.find("chord") is not None:
            nxt.remove(nxt.find("chord"))                  # the next note of the chord leads now
            for tag in ("beam", "notations"):
                for ch in note.findall(tag):
                    if nxt.find(tag) is None:
                        nxt.append(ch)
            measure.remove(note)
            return
        fw = ET.Element("forward")                          # nothing else here: keep the time it took
        ET.SubElement(fw, "duration").text = note.findtext("duration")
        measure.insert(k, fw)
    measure.remove(note)


def _sign_before(bw, q, stems, u):
    """the accidental in front of a grace note, looked for in the picture: 1 sharp, 0 natural, -1 flat, None
    (the project file often misses these: a small sharp is read as a stray key sign)"""
    left = q["cx"] - q["w"] / 2.0
    x0, x1 = int(left - 2.0 * u), int(left - 0.05 * u)
    y0, y1 = int(q["cy"] - 1.9 * u), int(q["cy"] + 1.9 * u)
    if x0 < 0 or y0 < 0:
        return None
    reg = bw[y0:y1, x0:x1]
    # strokes: tall thin pieces that survive a vertical opening (staff lines and the bars of a sharp do not)
    op = cv2.morphologyEx(reg, cv2.MORPH_OPEN, np.ones((max(5, int(0.9 * u)), 1), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(op, connectivity=8)
    strokes = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if h >= 1.0 * u and w <= 0.45 * u:
            cx = x0 + x + w / 2.0
            if any(sx0 - 3 <= cx <= sx1 + 3 and sy0 - 3 <= y0 + y + h / 2 <= sy1 + 3 for sx0, sy0, sx1, sy1 in stems):
                continue                                # a stem or barline of something else
            strokes.append((x + w / 2.0, y, y + h))
    strokes.sort()
    if len(strokes) >= 2 and 0.2 * u <= strokes[-1][0] - strokes[-2][0] <= 0.95 * u:
        a, b = strokes[-2], strokes[-1]
        if abs(a[1] - b[1]) <= 0.4 * u and abs(a[2] - b[2]) <= 0.4 * u:
            return 1
        if abs(a[1] - b[1]) >= 0.5 * u:
            return 0
    if len(strokes) >= 1:
        c, ytop, ybot = strokes[-1]
        xs = int(c + 2)
        belly = reg[int(ytop + 0.45 * (ybot - ytop)):ybot, xs:xs + int(0.6 * u)]
        if belly.size and belly.any(axis=0).mean() >= 0.6 and (len(strokes) == 1 or strokes[-1][0] - strokes[-2][0] > 1.0 * u):
            return -1
    return None


def _shifts_main(loc, sh, number, measure, victim, anchor, staff, main_x, u, victims):
    """Should the voice close up where the misread note was?  Yes when the note sits right before the main note in its
    voice AND the main note starts later than the chords to its left allow (the misread note pushed it): then the
    main note belongs where the misread note began.  No when the main note is already where the chords to its left
    end (the misread note only filled a gap that has to stay)."""
    if victim.find("chord") is not None or (victim.findtext("voice") or "1") != (anchor.findtext("voice") or "1"):
        return False
    items = list(measure)
    nxt = next((e for e in items[items.index(victim) + 1:] if e.tag == "note" and e.find("chord") is None
                and e.find("grace") is None), None)
    if nxt is not anchor:
        return False
    g = next((k for (k, _p), mm in loc.bars.items() if mm is measure), None)
    pi = next((p for (_k, p), mm in loc.bars.items() if mm is measure), None)
    if g is None:
        return False
    div = loc.divs[(g, pi)]
    seq = {id(n): t for n, t in _notes_of_staff(measure, staff, div)}
    if id(anchor) not in seq or id(victim) not in seq:
        return False
    end = lambda n: seq[id(n)] + Fraction(int(n.findtext("duration")), 4 * div)
    drop = {id(n) for n in victims}
    left = [c for c in loc.staff_chords(sh, number, g, pi, staff)
            if c["x"] < main_x - 0.6 * u and not any(id(n) in drop for n in c["notes"]) and all(id(n) in seq for n in c["notes"])]
    expected = max((max(end(n) for n in c["notes"]) for c in left), default=Fraction(0))
    dv = Fraction(int(victim.findtext("duration")), 4 * div)
    return expected != seq[id(anchor)] and expected == seq[id(anchor)] - dv


def _beam_rows(bw, g0, g1, u):
    """how many beams join two grace notes (1 or 2) - looked for between their stems near the stem tops"""
    x = int((g0["stem_x"] + g1["stem_x"]) / 2.0)
    y0, y1 = int(min(g0["ytop"], g1["ytop"]) - 0.2 * u), int(max(g0["ytop"], g1["ytop"]) + 1.6 * u)
    col = bw[max(0, y0):y1, x - 1:x + 2].any(axis=1)
    return int(np.sum(col[1:] & ~col[:-1])) + int(col[0])


def apply_to_score(root, stacks, omr_path, page_images, offset=0, sheets=None):
    apply_to_score.log = []
    sheets = sheets or load_sheets(omr_path)
    loc = Locator(root, stacks, offset)
    done = []
    for number in sorted({s["sheet"] for s in stacks}):
        if number - 1 >= len(page_images) or number not in sheets:
            continue
        sh = sheets[number]
        gray = cv2.imread(page_images[number - 1], 0)
        bw = (gray < 128).astype(np.uint8)
        staves, ident, seen = [], [], set()
        for s in (s for s in stacks if s["sheet"] == number):
            for pi, part in enumerate(s["parts"]):
                for si, stf in enumerate(part):
                    if round(stf[0]) not in seen:
                        seen.add(round(stf[0]))
                        staves.append(stf)
                        ident.append((pi, str(si + 1)))
        spans = [(s["left"], s["right"]) for s in stacks if s["sheet"] == number]
        normal = [box(h) for h in sh.heads.values() if float(h.get("grade", "0")) >= 0.5]
        graces = find_graces(gray, staves, spans, normal)
        sh_stems = [box(e) for e in sh.root.iter() if e.tag in ("stem", "barline") and box(e)]
        alters = [(a.get("shape"), box(a)) for a in sh.root.iter("alter") if a.get("shape") in ALTER and box(a)]
        for group in _groups(graces):
            g0, gl = group[0], group[-1]
            u = g0["u"]
            pi, staff = ident[g0["staff"]]
            top, bottom = staves[g0["staff"]][0], staves[g0["staff"]][1]

            def skip(why):
                apply_to_score.log.append((number, int(g0["cx"]), int(g0["cy"]), why))

            # the main note: the first well graded head to the right of the grace notes, in this staff
            cands = []
            for hd in sh.heads.values():
                b = box(hd)
                cxh, cyh = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
                if float(hd.get("grade", "0")) >= 0.5 and gl["cx"] + 0.4 * u < cxh <= gl["cx"] + 3.6 * u \
                        and top - 5 * u <= cyh <= bottom + 5 * u:
                    cands.append((cxh, hd))
            if not cands:
                skip("no main note to the right")
                continue
            main_hd = min(cands, key=lambda c: c[0])[1]
            chord_h = sh.chord_heads(main_hd.get("id"))
            found = loc.notes_of_chord_id(sh, number, sh.chord_of.get(main_hd.get("id")), chord_h)
            if not found:
                skip("main chord not tied to the score")
                continue
            m, staff, notes_main = found
            rb = box(main_hd)
            ref_pos = int(main_hd.get("pitch", "0"))
            if abs((rb[1] + rb[3] / 2.0 - (top + bottom) / 2.0) / (u / 2.0) - ref_pos) > 0.8:
                skip("staff position disagrees")
                continue
            ref_note = notes_main[chord_h.index(main_hd)]
            rp = ref_note.find("pitch")
            ref_d = int(rp.findtext("octave")) * 7 + STEP.index(rp.findtext("step"))
            lead = [n for n in notes_main if n.find("chord") is None] or notes_main
            anchor = lead[0]
            voice = anchor.findtext("voice") or "1"
            anchor_staff = anchor.findtext("staff")
            # notes the project file made out of the grace noteheads themselves: badly graded heads on top of them
            victims = []
            for hd in sh.heads.values():
                if float(hd.get("grade", "0")) >= 0.5:
                    continue
                b = box(hd)
                if not any(abs(b[0] + b[2] / 2.0 - q["cx"]) <= 0.9 * u and abs(b[1] + b[3] / 2.0 - q["cy"]) <= 0.9 * u
                           for q in group):
                    continue
                hs = sh.chord_heads(hd.get("id"))
                f = loc.notes_of_chord_id(sh, number, sh.chord_of.get(hd.get("id")), hs)
                if f:
                    victims.append((f[0], f[2][hs.index(hd)]))
            new_notes = []
            for q in group:
                d = ref_d + (ref_pos - q["pos"])
                step, octave = STEP[d % 7], d // 7
                left = q["cx"] - q["w"] / 2.0               # the sign in front of this grace note, if the project saw one
                sign, sign_right = None, None            # the written alteration (1, 0, -1 ...) and where the sign ends
                for shape, b in alters:
                    right = b[0] + b[2]
                    if left - 1.8 * u <= right <= left + 0.5 * u and abs(b[1] + b[3] / 2.0 - q["cy"]) <= 1.7 * u:
                        if sign_right is None or right > sign_right:
                            sign, sign_right = ALTER[shape], right
                if sign is None:                         # the project file often misses a small sign: look at the picture
                    sign = _sign_before(bw, q, [(b[0], b[1], b[0] + b[2], b[1] + b[3]) for b in sh_stems], u)
                    sign_right = left - 0.6 * u
                alt = sign if sign is not None else _key_alter(root, pi, m, step)
                new_notes.append((step, octave, alt, sign))
                if sign is not None and sign_right < rb[0] - 0.9 * u:   # the sign belongs to the grace note, not the main note
                    for n in notes_main:
                        if n.find("accidental") is not None and n.findtext("accidental") == ACC_NAME[sign]:
                            n.remove(n.find("accidental"))
                            pn = n.find("pitch")
                            kalt = _key_alter(root, pi, m, pn.findtext("step"))
                            al = pn.find("alter")
                            if kalt and al is not None:
                                al.text = str(kalt)
                            elif kalt:
                                ET.SubElement(pn, "alter").text = str(kalt)
                                pn.insert(1, pn.find("alter"))
                            elif al is not None:
                                pn.remove(al)
            main_x = rb[0] + rb[2] / 2.0
            for vm, vn in victims:
                if vn in list(vm) and vn is not anchor:
                    _remove_note(vm, vn, keep_time=not _shifts_main(loc, sh, number, vm, vn, anchor, staff, main_x, u,
                                                                    [n for _m, n in victims]))
            two_beams = len(group) > 1 and _beam_rows(bw, group[0], group[-1], u) >= 2
            elems = []
            for idx, (step, octave, alt, sign) in enumerate(new_notes):
                el = ET.Element("note")
                ET.SubElement(el, "grace", {"slash": "yes"} if idx == 0 else {})
                p = ET.SubElement(el, "pitch")
                ET.SubElement(p, "step").text = step
                if alt:
                    ET.SubElement(p, "alter").text = str(alt)
                ET.SubElement(p, "octave").text = str(octave)
                ET.SubElement(el, "voice").text = voice
                ET.SubElement(el, "type").text = "16th" if two_beams else "eighth"
                if sign is not None:
                    ET.SubElement(el, "accidental").text = ACC_NAME[sign]
                ET.SubElement(el, "stem").text = "up"
                if anchor_staff is not None:
                    ET.SubElement(el, "staff").text = anchor_staff
                if len(new_notes) > 1:
                    for beam_no in ((1, 2) if two_beams else (1,)):
                        ET.SubElement(el, "beam", {"number": str(beam_no)}).text = \
                            "begin" if idx == 0 else ("end" if idx == len(new_notes) - 1 else "continue")
                elems.append(el)
            d = ET.Element("direction", {"placement": "above"})
            ET.SubElement(ET.SubElement(d, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = MARK
            if anchor_staff is not None:
                ET.SubElement(d, "staff").text = anchor_staff
            at = list(m).index(anchor)
            for k, el in enumerate([d] + elems):
                m.insert(at + k, el)
            done.append(dict(part=loc.parts[pi].get("id"), bar=int(m.get("number")), staff=staff, notes=len(elems),
                             removed=len(victims)))
    return done
