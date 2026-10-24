"""Rebuild a bar that is one fast run of notes, from the notes the project file found.

Some scores print a run split between the hands with TWO beams (one above, one below) and a stem running through each
note, so every note belongs to both beam groups.  Audiveris cannot make sense of that: stems get linked to the wrong
beams, chords come out twice, notes end up in no voice and the times of the slots add up to more than the bar.  Its heads
and pitches are usually fine, though.

A staff whose rhythm does not add up is rebuilt as ONE voice when
  * the project file holds at least 6 well enough graded black heads in it, one at each place (no chords), each with a
    stem and beams,
  * they fill the bar with equal lengths that are a plain note value or a tuplet of it (24 notes in 2/4 are four
    sextuplets of 32nds; the beams on the stems say 32nds), the groups being as large as the tuplet allows.
The notes get their pitch from the staff position (clef, an 8va line over them, the key and the signs in front of them,
which hold for the rest of the bar).  Whatever the staff held before is replaced and the bar gets a text mark.  The
beams the page prints (one per hand) are not reproduced: the program beams by beat.

    plan(root, stacks, omr_path, ottava_lines, offset) -> list of runs found
    apply(root, runs) -> list of what was rebuilt
"""
import xml.etree.ElementTree as ET
from collections import Counter
from fractions import Fraction

import cv2
import numpy as np

from omr_confidence import BLUE
from omr_grace import ALTER, ACC_NAME, _key_alter, _sign_before
from omr_locate import Locator, box, load_sheets, STEP
from omr_repair import _scale_file

MARK = "auto: bar rebuilt from a run of notes - please verify"
BY_BEAMS = {1: ("eighth", Fraction(1, 2)), 2: ("16th", Fraction(1, 4)), 3: ("32nd", Fraction(1, 8)), 4: ("64th", Fraction(1, 16))}


def _clef_at(part, measure, staff):
    clef = {}
    for m in part.findall("measure"):
        for c in m.findall("attributes/clef"):
            n = c.get("number") or "1"
            clef[n] = (c.findtext("sign"), int(c.findtext("line") or 0), int(c.findtext("clef-octave-change") or 0))
        if m is measure:
            break
    return clef.get(staff)


def _middle_line_dia(clef):
    sign, line, oc = clef
    if sign == "G":
        base = 4 * 7 + STEP.index("B")                   # the middle line of a treble clef is B4
    elif sign == "F":
        base = 3 * 7 + STEP.index("D")                   # ... of a bass clef D3
    elif sign == "C":
        base = 4 * 7 + STEP.index("C")                   # ... of an alto clef C4
    else:
        return None
    return base + 7 * oc


def _staff_end(measure, staff, div):
    """the longest voice of a staff in quarter notes (gaps made with <forward> count)"""
    cursor, ends = 0, {}
    for e in measure:
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
        elif e.tag == "note" and e.find("chord") is None and e.find("grace") is None and e.find("duration") is not None:
            cursor += int(e.findtext("duration"))
            if (e.findtext("staff") or "1") == staff:
                v = e.findtext("voice") or "1"
                ends[v] = max(ends.get(v, 0), cursor)
    return max(ends.values()) / float(div) if ends else 0.0


def _ottava_steps(lines, sheet, staff_top, u, x):
    for L in lines or []:
        if L.get("sheet") != sheet or abs(L["staff_top"] - staff_top) > 0.6 * u:
            continue
        xs = L["text_left"] - 0.5 * u
        xe = (L["hook_x"] + 0.7 * u) if L.get("hook_x") else (L["x1"] + 0.9 * u)
        if xs <= x <= xe:
            return 7 if L["kind"] == "alta" else -7
    return 0


def _refine_dy(bw, b, u):
    """how far (pixels, + = lower) the real head sits from the box the project file gave a poorly graded head: the
    blob that is left of the picture after thin lines and stems are opened away"""
    x0, x1 = max(0, b[0] - 3), b[0] + b[2] + 3
    y0, y1 = max(0, int(b[1] - 0.9 * u)), int(b[1] + b[3] + 0.9 * u)
    win = bw[y0:y1, x0:x1]
    if win.size == 0:
        return 0.0
    k = max(5, int(0.28 * u) | 1)
    op = cv2.morphologyEx(win, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, lab, st, cen = cv2.connectedComponentsWithStats(op, connectivity=8)
    best = None
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if 0.5 * b[2] <= w <= 1.5 * b[2] and 0.5 * b[3] <= h <= 1.8 * b[3]:
            d = abs((y0 + cen[i][1]) - (b[1] + b[3] / 2.0))
            if best is None or d < best[0]:
                best = (d, y0 + cen[i][1])
    return 0.0 if best is None else best[1] - (b[1] + b[3] / 2.0)


def _tuplet_shape(d, nb, n):
    """(note type, actual, normal) for n equal notes of d quarters under nb beams, or None"""
    if nb not in BY_BEAMS:
        return None
    typ, v = BY_BEAMS[nb]
    r = Fraction(d).limit_denominator(24) / v                       # normal / actual
    if r == 1:
        return typ, 1, 1
    for actual in ((3, 6, 5, 7, 9, 12, 10) if typ in ("eighth", "16th") else (6, 3, 5, 7, 9, 12, 10)):
        normal = r * actual
        if normal.denominator == 1 and 2 <= normal < actual and n % actual == 0:
            return typ, actual, int(normal)
    return None


def plan(root, stacks, omr_path, ottava_lines=None, offset=0, sheets=None, page_images=None):
    sheets = sheets or load_sheets(omr_path)
    loc = Locator(root, stacks, offset)
    runs = []
    plan.log = []
    for g, s in enumerate(stacks, 1):
        sh = sheets.get(s["sheet"])
        if sh is None:
            continue
        for pi, staves in enumerate(s["parts"]):
            m = loc.bars.get((g, pi))
            if m is None:
                continue
            part = loc.parts[pi]
            div = loc.divs[(g, pi)]
            beats, bt = 4, 4
            for mm in part.findall("measure"):
                t = mm.find("attributes/time")
                if t is not None:
                    beats, bt = int(t.findtext("beats")), int(t.findtext("beat-type"))
                if mm is m:
                    break
            exp = beats * 4.0 / bt
            for si, st in enumerate(staves):
                staff = str(si + 1)
                if abs(_staff_end(m, staff, div) - exp) < 1e-6:
                    continue                                          # the rhythm adds up: leave it
                plan.log.append((g, staff, "invalid, looking"))
                u = (st[1] - st[0]) / 4.0
                heads = []
                for h in sh.heads.values():
                    b = box(h)
                    cx, cy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
                    if not (s["left"] <= cx <= s["right"] and st[0] - 9 * u <= cy <= st[1] + 9 * u):
                        continue
                    near = min(range(len(staves)), key=lambda k: abs(cy - (staves[k][0] + staves[k][1]) / 2.0))
                    if near != si or float(h.get("grade", "0")) < 0.15:
                        continue
                    stem0 = sh.chord_stem.get(sh.chord_of.get(h.get("id")))
                    if h.get("shape") != "NOTEHEAD_BLACK" and not sh.stem_beams.get(stem0):
                        continue                                      # an open head with no beam is not part of a run
                    heads.append((cx, cy, h, b))
                heads.sort(key=lambda t: t[0])
                heads = [t for t in heads if not (float(t[2].get("grade", "0")) < 0.35 and any(
                    o is not t and abs(o[0] - t[0]) <= 1.7 * u and float(o[2].get("grade", "0")) >= 0.55
                    and o[2].get("pitch") == t[2].get("pitch") for o in heads))]
                uniq = []
                for t in heads:                                         # the same head listed twice: keep one
                    if uniq and abs(t[0] - uniq[-1][0]) <= 6:
                        if t[2].get("pitch") != uniq[-1][2].get("pitch"):
                            uniq = None                                 # two different notes at one place: a chord
                            break
                        if float(t[2].get("grade", "0")) > float(uniq[-1][2].get("grade", "0")):
                            uniq[-1] = t
                    else:
                        uniq.append(t)
                if not uniq or len(uniq) < 6:
                    plan.log.append((g, staff, "fewer than 6 usable heads (%s)" % (len(uniq) if uniq else "chord")))
                    continue
                n = len(uniq)
                # only when Audiveris clearly lost the thread: notes in no voice, or a staff with a quite different number
                # of notes than heads (a bar it read note for note is the business of the triplet repair and the rest)
                orphans = sum(1 for _cx, _cy, h, _b in uniq if sh.chord_of.get(h.get("id")) not in sh.voiced)
                have = sum(1 for e in m.findall("note") if (e.findtext("staff") or "1") == staff and e.find("rest") is None
                           and e.find("chord") is None and e.find("grace") is None)
                if orphans < 2 and abs(have - n) < 3:
                    plan.log.append((g, staff, "read note for note (%d heads, %d notes)" % (n, have)))
                    continue
                nbs = []
                for cx, cy, h, b in uniq:
                    stem = sh.chord_stem.get(sh.chord_of.get(h.get("id")))
                    nbs.append(len(sh.stem_beams.get(stem, ())) if stem else 0)
                if sum(1 for x in nbs if x >= 1) < 0.5 * n:
                    plan.log.append((g, staff, "heads without beams: %d of %d" % (sum(1 for x in nbs if x < 1), n)))
                    continue
                shape = None
                for level in range(1, 5):                    # the shape closest to a plain run wins
                    sh_ = _tuplet_shape(exp / n, level, n)
                    if sh_ is not None and (shape is None or Fraction(sh_[2], sh_[1]) > Fraction(shape[2], shape[1])):
                        shape = sh_
                if shape is None:
                    plan.log.append((g, staff, "%d notes of %.3f quarters fit no tuplet" % (n, exp / n)))
                    continue
                clef = _clef_at(part, m, staff)
                # a mid-bar change to the treble clef is easily missed by Audiveris: a run under an 8va line (above the
                # staff) is written in the treble clef, whatever the clef recorded for the bar
                new_clef = False
                if clef and clef[0] == "F" and any(_ottava_steps(ottava_lines, s["sheet"], st[0], u, t[0]) > 0 for t in uniq):
                    clef, new_clef = ("G", 2, 0), True
                mid = _middle_line_dia(clef) if clef else None
                if mid is None:
                    continue
                # the signs in front of the heads: each sign goes to the head right of it that is nearest
                first_x = uniq[0][3][0]
                signs = [(a.get("shape"), box(a)) for a in sh.root.iter() if a.tag in ("alter", "key-alter")
                         and a.get("shape") in ALTER and box(a) and (a.tag == "alter" or box(a)[0] > first_x - 2 * u)]
                owner = {}
                for shape_name, sb in signs:
                    right, scy = sb[0] + sb[2], sb[1] + sb[3] / 2.0
                    best = None
                    for k, (cx, cy, h, b) in enumerate(uniq):
                        gap = b[0] - right
                        if -0.4 * u <= gap <= 1.9 * u and abs(scy - cy) <= 1.6 * u and (best is None or gap < best[0]):
                            best = (gap, k)
                    if best is not None and (best[1] not in owner or best[0] < owner[best[1]][0]):
                        owner[best[1]] = (best[0], ALTER[shape_name])
                bw = (cv2.imread(page_images[s["sheet"] - 1], 0) < 128).astype(np.uint8) if page_images and                     s["sheet"] - 1 < len(page_images) else None
                stems_boxes = [(e_b[0], e_b[1], e_b[0] + e_b[2], e_b[1] + e_b[3]) for e_b in
                               (box(e) for e in sh.root.iter() if e.tag in ("stem", "barline")) if e_b]
                for k, (cx, cy, h, b) in enumerate(uniq):          # a sign the project file missed: look at the picture
                    if k not in owner and bw is not None:
                        alt_seen = _sign_before(bw, dict(cx=cx, cy=cy, w=b[2]), stems_boxes, u, min_h=2.0, max_h=3.6)
                        if alt_seen in (0, 1):
                            owner[k] = (9.0, alt_seen)
                        elif alt_seen == -1:                         # something with a stem and a belly: a sign of a kind
                            owner[k] = (9.0, "?")                    # the picture is too coarse to tell which; see below
                notes, state = [], {}
                for k, (cx, cy, h, b) in enumerate(uniq):
                    pos = int(h.get("pitch", "0"))
                    if float(h.get("grade", "0")) < 0.5 and bw is not None:       # a poorly graded head: check its place
                        pos += int(round(_refine_dy(bw, b, u) / (u / 2.0)))
                    shift = _ottava_steps(ottava_lines, s["sheet"], st[0], u, cx)
                    dia = mid - pos + shift
                    step, octave = STEP[dia % 7], dia // 7
                    sign = owner[k][1] if k in owner else None
                    if sign == "?":
                        # an unreadable sign: a natural when the note is already altered (by the key or by an earlier sign
                        # in the bar), else a sharp
                        before = state.get((step, octave), _key_alter(root, pi, m, step))
                        sign = 0 if before != 0 else 1
                    if sign is not None:
                        state[(step, octave)] = sign
                    notes.append(dict(step=step, octave=octave, sign=sign, shift=shift))
                # another staff of the bar with no head of its own in the project file but notes in the score: those notes
                # are the run's heads, handed to the wrong staff
                empty = []
                for sj, st2 in enumerate(staves):
                    if sj == si:
                        continue
                    own = 0
                    for h in sh.heads.values():
                        b2 = box(h)
                        cx2, cy2 = b2[0] + b2[2] / 2.0, b2[1] + b2[3] / 2.0
                        if s["left"] <= cx2 <= s["right"] and min(
                                range(len(staves)), key=lambda k: abs(cy2 - (staves[k][0] + staves[k][1]) / 2.0)) == sj                                 and st2[0] - 9 * u <= cy2 <= st2[1] + 9 * u:
                            own += 1
                    if own == 0:
                        empty.append(str(sj + 1))
                runs.append(dict(measure=m, pi=pi, staff=staff, part=part, notes=notes, shape=shape, exp=exp, div=div,
                                 bar=int(m.get("number")), empty=empty, new_clef=new_clef))
    return runs


def apply(root, runs):
    done = []
    for r in runs:
        m, staff, notes, part = r["measure"], r["staff"], r["notes"], r["part"]
        typ, actual, normal = r["shape"]
        n = len(notes)
        div = 1
        for mm in part.findall("measure"):                                  # the divisions in force at this bar
            if mm.find("attributes/divisions") is not None:
                div = int(mm.find("attributes/divisions").text)
            if mm is m:
                break
        unit = Fraction(r["exp"]).limit_denominator(24) / n * div           # divisions per note
        if unit.denominator != 1:                                          # make the divisions fine enough
            _scale_file(root, unit.denominator)
            div *= unit.denominator
            unit = unit * unit.denominator
        dur = int(unit)
        if r.get("new_clef"):
            from omr_clefs import _clef_elem, _insert_clef
            at = m.find("attributes")
            if at is not None:
                for c in at.findall("clef"):
                    if (c.get("number") or "1") == staff:
                        at.remove(c)
            _insert_clef(m, _clef_elem("TREBLE", staff))
        items = list(m)
        idx = [i for i, e in enumerate(items) if e.tag == "note" and (e.findtext("staff") or "1") == staff]
        if not idx:
            continue
        lo, hi = min(idx), max(idx)
        if any(e.tag == "note" and (e.findtext("staff") or "1") != staff for e in items[lo:hi + 1]):
            continue                                                       # interleaved with the other staff: leave it
        voices = [e.findtext("voice") for e in items if e.tag == "note" and (e.findtext("staff") or "1") == staff]
        voice = voices[0] if voices and voices[0] else ("1" if staff == "1" else "5")
        # octave-shift marks inside the old block were placed around OLD notes: take them out and put them around the
        # new ones (a start before the first, a stop after the last)
        starts, stops = [], []
        for e in items[lo:hi + 1]:
            o = e.find(".//octave-shift") if e.tag == "direction" else None
            if o is not None and (e.findtext("staff") or "1") == staff:
                (stops if o.get("type") == "stop" else starts).append(e)
                m.remove(e)
            elif e.tag in ("note", "forward", "backup"):
                m.remove(e)
        # the signs hold for the rest of the bar
        pi = list(root.findall("part")).index(part)
        state = {}
        new = []
        nbeam = {"eighth": 1, "16th": 2, "32nd": 3, "64th": 4}[typ]
        for k, nt in enumerate(notes):
            key = (nt["step"], nt["octave"])
            if nt["sign"] is not None:
                state[key] = nt["sign"]
            alt = state.get(key, _key_alter(root, pi, m, nt["step"]))
            el = ET.Element("note", {"color": BLUE})
            p = ET.SubElement(el, "pitch")
            ET.SubElement(p, "step").text = nt["step"]
            if alt:
                ET.SubElement(p, "alter").text = str(alt)
            ET.SubElement(p, "octave").text = str(nt["octave"])
            ET.SubElement(el, "duration").text = str(dur)
            ET.SubElement(el, "voice").text = voice
            ET.SubElement(el, "type").text = typ
            if nt["sign"] is not None:
                ET.SubElement(el, "accidental").text = ACC_NAME[nt["sign"]]
            if actual > 1:
                tm = ET.SubElement(el, "time-modification")
                ET.SubElement(tm, "actual-notes").text = str(actual)
                ET.SubElement(tm, "normal-notes").text = str(normal)
            ET.SubElement(el, "staff").text = staff
            pos = k % actual if actual > 1 else k % 4
            size = actual if actual > 1 else 4
            for b in range(1, nbeam + 1):
                ET.SubElement(el, "beam", {"number": str(b)}).text = \
                    "begin" if pos == 0 else ("end" if pos == size - 1 else "continue")
            if actual > 1 and pos in (0, size - 1):
                nt_el = ET.SubElement(el, "notations")
                ET.SubElement(nt_el, "tuplet", {"type": "start" if pos == 0 else "stop", "number": "1",
                                                "bracket": "no", "show-number": "actual"})
            new.append(el)
        d = ET.Element("direction", {"placement": "above"})
        ET.SubElement(ET.SubElement(d, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = MARK
        ET.SubElement(d, "staff").text = staff
        # an 8va / 8vb line starts before its first note and stops after its last one (the line may end in the middle of
        # the bar: the notes after it are not under it)
        shifted = [k for k, nt in enumerate(notes) if nt["shift"]]
        marks_before, marks_after = [], []
        if shifted and (starts or stops):
            from omr_ottava import _direction
            kind = "alta" if notes[shifted[0]]["shift"] > 0 else "bassa"
            if starts:
                marks_before.append((shifted[0], _direction(kind, True, staff)))
            if stops:
                marks_after.append((shifted[-1], _direction(kind, False, staff)))
        block = [d]
        for k, el in enumerate(new):
            block += [mk for at_, mk in marks_before if at_ == k]
            block.append(el)
            block += [mk for at_, mk in marks_after if at_ == k]
        for k, el in enumerate(block):
            m.insert(lo + k, el)
        after = list(m)[lo + len(block):]
        for e in after:                                                    # the way back to the bar's start
            if e.tag == "backup":
                e.find("duration").text = str(dur * n)
                break
        # the other staff of the bar: when the project file has no head for it, its notes are junk (the run's heads handed
        # to the wrong staff): a rest for the whole bar takes their place; when it is one rest already, give it the bar's length
        for other in {e.findtext("staff") or "1" for e in m if e.tag == "note"} - {staff}:
            theirs = [e for e in m if e.tag == "note" and (e.findtext("staff") or "1") == other]
            if other in r.get("empty", []) and any(e.find("rest") is None for e in theirs):
                its = list(m)
                idx2 = [i for i, e in enumerate(its) if e in theirs]
                lo2, hi2 = min(idx2), max(idx2)
                if not any(e.tag == "note" and (e.findtext("staff") or "1") != other for e in its[lo2:hi2 + 1]):
                    voice2 = theirs[0].findtext("voice") or "5"
                    for e in its[lo2:hi2 + 1]:
                        if e.tag in ("note", "forward", "backup"):
                            m.remove(e)
                    rest = ET.Element("note")
                    ET.SubElement(rest, "rest", {"measure": "yes"})
                    ET.SubElement(rest, "duration").text = str(dur * n)
                    ET.SubElement(rest, "voice").text = voice2
                    ET.SubElement(rest, "type").text = "whole"
                    ET.SubElement(rest, "staff").text = other
                    m.insert(lo2, rest)
                    continue
            if len(theirs) == 1 and theirs[0].find("rest") is not None and theirs[0].find("duration") is not None:
                theirs[0].find("duration").text = str(dur * n)
                theirs[0].find("rest").set("measure", "yes")
        done.append(dict(part=part.get("id"), bar=r["bar"], staff=staff, notes=n, shape=r["shape"]))
    return done
