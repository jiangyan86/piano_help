"""8va / 8vb lines (ottava): find them in the page image, take them out before Audiveris, and apply them to the score.

Why: Audiveris recognises the "8va" sign but only a short stub of the dashed line that follows it, so only the first
notes under a long 8va get raised an octave (and the exported octave-shift marks are inconsistent).  The pitches in a
MusicXML file are *sounding* pitches, so the notes under a long 8va come out an octave low.

  detect_and_erase(gray, u, staves, lowest) -> (image without the lines, [line dicts])
        a dashed line is a chain of >= 6 equally spaced dashes above the upper staff of a part (8va) or just below
        the lower staff (8vb).  To count as an ottava the chain must have an "8" next to it (the only common glyph
        with two holes) so that hyphen runs after words such as "rit. - - -" are never mistaken for it.
        The dashes, the sign and the end hook are erased, so Audiveris sees no ottava at all and writes plain
        written pitches.

  apply_to_score(root, stacks, lines, offset) -> summary
        for every note under a line (found from the measure time slots in the Audiveris project file) the octave is
        raised (8va) or lowered (8vb) and one octave-shift start/stop pair is written around each run of notes, so
        MuseScore shows the bracket and the notes at the written height while the pitches sound right.
"""
from fractions import Fraction
import copy
import xml.etree.ElementTree as ET

import cv2
import numpy as np


# ----------------------------------------------------------------------------- detection (page image)
def _holes(mask):
    """Number of enclosed background regions in a binary mask."""
    cs, hier = cv2.findContours(mask.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return 0
    return sum(1 for i in range(len(cs)) if hier[0][i][3] != -1 and cv2.contourArea(cs[i]) >= 4)


def detect_and_erase(gray, u, staves, lowest):
    bw = (gray < 128).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    comps = [tuple(int(v) for v in st[i]) for i in range(1, n)]            # x, y, w, h, area (label = index + 1)
    dashes = []
    for i, (x, y, w, h, a) in enumerate(comps):
        if 0.3 * u <= w <= 0.95 * u and 1 <= h <= 0.3 * u and w >= 2.2 * h and a >= 8:
            dashes.append((x, y, w, h, i + 1))
    dashes.sort()

    chains, used = [], set()
    for i, d in enumerate(dashes):
        if i in used:
            continue
        chain, cur = [i], d
        used.add(i)
        while True:
            nxt = None
            for j in range(chain[-1] + 1, min(len(dashes), chain[-1] + 60)):
                if j in used:
                    continue
                e = dashes[j]
                gap = e[0] - (cur[0] + cur[2])
                if gap > 1.1 * u:
                    break
                if 0.2 * u <= gap <= 1.0 * u and abs((e[1] + e[3] / 2.0) - (cur[1] + cur[3] / 2.0)) <= 0.25 * u:
                    nxt = j
                    break
            if nxt is None:
                break
            chain.append(nxt)
            used.add(nxt)
            cur = dashes[nxt]
        chains.append([dashes[k] for k in chain])         # short ones (one or two dashes) count only with a hook

    kill = np.zeros(gray.shape, np.uint8)
    lines = []
    for ch in chains:
        x0, x1 = ch[0][0], ch[-1][0] + ch[-1][2]
        y = float(np.median([c[1] + c[3] / 2.0 for c in ch]))
        upper = [(s[0] - y, k) for k, s in enumerate(staves)
                 if (k == 0 or lowest[k - 1]) and 0.8 * u <= s[0] - y <= 8.5 * u]
        lower = [(y - s[1], k) for k, s in enumerate(staves) if lowest[k] and 0.8 * u <= y - s[1] <= 4.0 * u]
        cands = [("alta", d, k) for d, k in upper] + [("bassa", d, k) for d, k in lower]
        if not cands:
            continue
        kind, dist, k = min(cands, key=lambda c: c[1])
        # the sign: an "8" (two holes, 1.2-2.4 staff spaces tall) within a few spaces left of the first dash
        sign = []
        for i, (x, yy, w, h, a) in enumerate(comps):
            # the sign hangs below the dash line (its top is at the line), so look mostly downwards
            if x0 - 6.5 * u <= x and x + w <= x0 + 0.4 * u and y - 1.2 * u <= yy and yy + h <= y + 2.5 * u:
                if 0.25 * u <= h <= 2.6 * u and w <= 2.4 * u and a >= 6:
                    sign.append(i)
        has8 = False
        for i in sign:
            x, yy, w, h, a = comps[i]
            if 1.2 * u <= h <= 2.4 * u and w <= 2.4 * u and _holes(lab[yy:yy + h, x:x + w] == (i + 1)) >= 2:
                has8 = True
                break
        if not has8:
            continue
        # optional hook at the right end: a short DASHED vertical line hanging toward the staff.  Its first dash
        # merges with the last horizontal dash into a corner.
        hook_ids, hook_x = [], None
        corner = None
        for i, (x, yy, w, h, a) in enumerate(comps):
            if x1 - 0.2 * u <= x <= x1 + 1.0 * u and y - 0.7 * u <= yy <= y + 0.4 * u \
                    and w <= 1.0 * u and 0.3 * u <= h <= 1.0 * u and a >= 20:
                corner = (i, x, yy, w, h)
                break
        if corner:
            xr = corner[1] + corner[3]
            hook_ids.append(corner[0] + 1)
            for i, (x, yy, w, h, a) in enumerate(comps):
                if i == corner[0]:
                    continue
                if abs((x + w / 2.0) - (xr - 2)) <= 0.4 * u and w <= 0.45 * u and 0.1 * u <= h <= 0.9 * u \
                        and corner[2] + corner[4] - 0.3 * u <= yy <= y + 4.5 * u:
                    hook_ids.append(i + 1)
            if len(hook_ids) >= 2:                       # corner plus at least one more dash: a real hook
                hook_x = int(xr)
            else:
                hook_ids = []
        if len(ch) < 6 and hook_x is None:
            continue                                     # a short line is accepted only when it ends in a hook: "(8va)-┐"
        text_left = min(comps[i][0] for i in sign)
        for c in ch:
            kill[lab == c[4]] = 255
        for i in sign:
            kill[lab == (i + 1)] = 255
        for lid in hook_ids:
            kill[lab == lid] = 255
        s = staves[k]
        lines.append(dict(kind=kind, x0=int(x0), x1=int(x1), y=round(y, 1), text_left=int(text_left),
                          hook_x=hook_x, staff_top=round(float(s[0]), 1),
                          staff_bottom=round(float(s[1]), 1), dashes=len(ch)))
    if not lines:
        return gray, []
    out = gray.copy()
    out[cv2.dilate(kill, np.ones((3, 3), np.uint8)) > 0] = 255
    return out, lines


# ----------------------------------------------------------------------------- applying to the score
def _slot_x(stack, t):
    """x position of time t (whole-note fraction) in a measure, from the measure's time slots."""
    slots = stack.get("slots") or []
    if not slots:
        return None
    best = min(slots, key=lambda s: abs(s[1] - t))
    return stack["left"] + best[0]


def _notes_of_staff(measure, staff, div):
    """[(note element, onset in whole notes)] for one staff of one measure, in document order."""
    out, cursor, last_onset = [], 0, 0
    for e in measure:
        if e.tag == "note":
            if e.find("chord") is None:
                last_onset = cursor
                if e.find("grace") is None:
                    cursor += int(e.findtext("duration") or 0)
            if (e.findtext("staff") or "1") == staff:
                out.append((e, Fraction(last_onset, 4 * div)))
        elif e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
    return out


def _direction(kind, start, staff):
    d = ET.Element("direction", {"placement": "above" if kind == "alta" else "below"})
    ET.SubElement(d, "direction-type").append(
        ET.Element("octave-shift", {"type": ("down" if kind == "alta" else "up") if start else "stop",
                                    "size": "8", "number": "1"}))
    ET.SubElement(d, "staff").text = staff
    return d


def apply_to_score(root, stacks, lines, offset=0):
    """Raise/lower the notes under each line and add octave-shift marks.  Returns a list of summaries."""
    parts = root.findall("part")
    # Audiveris' own octave-shift marks are unreliable (a stub of a real line, or the "8" of a measure number such as
    # "28" mistaken for an 8va sign).  The real lines were taken out of the pictures, so whatever it still wrote is
    # stray: remember it and drop it once the correct marks are in.  (Pitches it may have changed are left alone.)
    stray = [(m, d) for part in parts for m in part.findall("measure") for d in m.findall("direction")
             if d.find(".//octave-shift") is not None]
    # global measure -> {part index: xml measure}
    xml_measure = {}
    divs = {}
    for pi, part in enumerate(parts):
        div = 1
        for m in part.findall("measure"):
            at = m.find("attributes")
            if at is not None and at.find("divisions") is not None:
                div = int(at.find("divisions").text)
            g = offset + int(m.get("number"))
            xml_measure[(g, pi)] = m
            divs[(g, pi)] = div

    # which (part, staff) of which global measures are under a line, and where each line starts/stops
    flagged = {}          # (pi, staff) -> {id(note): kind}
    summary = []
    for L in lines:
        sheet_stacks = [(g, s) for g, s in enumerate(stacks, 1) if s["sheet"] == L["sheet"]]
        # find the staff by its top line
        hit = None
        for g, s in sheet_stacks:
            for pi, staves in enumerate(s["parts"]):
                for si, st in enumerate(staves):
                    u = (st[1] - st[0]) / 4.0
                    if abs(st[0] - L["staff_top"]) <= 0.6 * u:
                        hit = (s["system"], pi, si + 1)
                        break
                if hit:
                    break
            if hit:
                break
        if not hit:
            summary.append(dict(line=L, notes=0, why="no staff matches this line in the project file"))
            continue
        system, pi, staff = hit
        u = (sheet_stacks[0][1]["parts"][0][0][1] - sheet_stacks[0][1]["parts"][0][0][0]) / 4.0
        xs = L["text_left"] - 0.5 * u
        xe = (L["hook_x"] + 0.7 * u) if L["hook_x"] else (L["x1"] + 0.9 * u)
        count = 0
        for g, s in sheet_stacks:
            if s["system"] != system or s["right"] < xs or s["left"] > xe:
                continue
            m = xml_measure.get((g, pi))
            if m is None:
                continue
            for note, t in _notes_of_staff(m, str(staff), divs[(g, pi)]):
                x = _slot_x(s, t)
                if x is None or not (xs <= x <= xe):
                    continue
                flagged.setdefault((pi, str(staff)), {})[id(note)] = (L["kind"], g, t, note, m)
                count += 1
        summary.append(dict(line=L, notes=count, part=pi, staff=staff))

    # transpose and bracket
    for (pi, staff), notes in flagged.items():
        for kind, g, t, note, m in notes.values():
            p = note.find("pitch")
            if p is not None:
                o = p.find("octave")
                o.text = str(int(o.text) + (1 if kind == "alta" else -1))
        # runs: consecutive notes of this staff, in score order, that are all under a line
        seq = []
        div = 1
        part = parts[pi]
        for m in part.findall("measure"):
            g = offset + int(m.get("number"))
            for note, t in _notes_of_staff(m, staff, divs[(g, pi)]):
                seq.append((g, t, note, m))
        seq.sort(key=lambda r: (r[0], r[1]))                     # stable: keeps document order for equal times
        run = []
        runs = []
        for rec in seq:
            sysid = (stacks[rec[0] - 1]["sheet"], stacks[rec[0] - 1]["system"])
            if id(rec[2]) in notes:
                # one mark pair per system, as on the page (the line is broken at each system and starts again with
                # "(8va)"): MuseScore lowers the wrong notes at the end of an 8va that runs over many bars and pages
                if run and sysid != run_sys:
                    runs.append(run)
                    run = []
                run.append(rec)
                run_sys = sysid
            else:
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)
        for run in runs:
            apply_to_score.last_runs.append((pi, staff, run[0][0], str(run[0][1]), run[-1][0], str(run[-1][1]), len(run)))
            kind = notes[id(run[0][2])][0]
            g0, t0, first, m0 = run[0]
            # the first note of the earliest onset (document order within the measure)
            first = next(r for r in run if r[0] == g0 and r[1] == t0)[2]
            m0.insert(list(m0).index(first), _direction(kind, True, staff))
            # the last note to end: stop mark after the final chord member of the latest-starting note
            gl = run[-1][0]
            tl = max(r[1] for r in run if r[0] == gl)
            last = [r for r in run if r[0] == gl and r[1] == tl][-1]
            lm, ln = last[3], last[2]
            items = list(lm)
            k = items.index(ln)
            while k + 1 < len(items) and items[k + 1].tag == "note" and items[k + 1].find("chord") is not None:
                k += 1
            lm.insert(k + 1, _direction(kind, False, staff))
    for m, d in stray:
        m.remove(d)
    apply_to_score.removed_stray = len(stray)
    return summary


apply_to_score.last_runs = []        # (part, staff, first bar, onset, last bar, onset, notes) of every run, for debugging
apply_to_score.removed_stray = 0     # how many octave-shift marks written by Audiveris were dropped
