"""Fermatas.

Audiveris does not read fermatas at all (a dome with a dot inside), so they are missing from the MusicXML.  They are
easy to recognise in the page picture: a crescent about 2.4 staff spaces wide and 1.4 high, hanging down at both ends,
with a round dot under its middle; above a staff (upright) or below it (inverted).  Each one found is written as a
<fermata> on the notes or rests at that moment of the bar.

A candidate must have the right size, be thicker at the top than at the ends, have both ends reaching the bottom of the
shape, and have a dot inside it.  Dots under slurs, noteheads with ledger lines and the brackets around text are not
fooled by this.

    find_fermatas(gray, staves) -> [(kind, x centre, staff index)]
    apply_to_score(root, stacks, page_images, offset) -> list of what was added
"""
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from omr_locate import Locator
from omr_ottava import _notes_of_staff


def find_fermatas(gray, staves):
    """staves: [(top, bottom, left, right)].  -> [(kind 'upright'|'inverted', x centre, y centre, staff index)]"""
    bw = (gray < 128).astype(np.uint8)
    found = []
    for k, (top, bottom, left, right) in enumerate(staves):
        u = (bottom - top) / 4.0
        for kind in ("upright", "inverted"):
            if kind == "upright":
                y0, y1 = int(top - 6 * u), int(top - 0.3 * u)
            else:
                y0, y1 = int(bottom + 0.3 * u), int(bottom + 6 * u)
            y0 = max(0, y0)
            x0, x1 = int(left), int(right)
            reg = bw[y0:y1, x0:x1]
            if reg.size == 0:
                continue
            n, lab, st, _ = cv2.connectedComponentsWithStats(reg, connectivity=8)
            for i in range(1, n):
                x, y, w, h, a = st[i]
                if not (1.8 * u <= w <= 3.6 * u and 1.1 * u <= h <= 1.9 * u and 1.3 <= w / float(h) <= 3.2):
                    continue
                if not 0.15 <= a / float(w * h) <= 0.55:
                    continue
                sub = lab[y:y + h, x:x + w] == i
                end = max(2, w // 8)
                rows_l = np.where(sub[:, :end].any(axis=1))[0]
                rows_r = np.where(sub[:, -end:].any(axis=1))[0]
                mid = sub[:, w // 2 - 1:w // 2 + 2].any(axis=1)
                if not len(rows_l) or not len(rows_r) or not mid.any():
                    continue
                m_rows = np.where(mid)[0]
                if kind == "upright":
                    ends_ok = rows_l.max() >= 0.8 * h and rows_r.max() >= 0.8 * h and m_rows.min() <= 0.2 * h
                else:
                    ends_ok = rows_l.min() <= 0.2 * h and rows_r.min() <= 0.2 * h and m_rows.max() >= 0.8 * h
                if not ends_ok:
                    continue
                cx = x + w / 2.0
                dot = False
                for j in range(1, n):
                    if j == i:
                        continue
                    xx, yy, ww, hh, _a = st[j]
                    if 0.25 * u <= ww <= 0.85 * u and 0.25 * u <= hh <= 0.85 * u and abs(xx + ww / 2.0 - cx) <= 0.45 * u:
                        inside = (y + 0.3 * h <= yy and yy + hh <= y + h + 0.6 * u) if kind == "upright" \
                            else (y - 0.6 * u <= yy and yy + hh <= y + 0.7 * h)
                        if inside:
                            dot = True
                            break
                if dot:
                    found.append((kind, x0 + cx, y0 + y + h / 2.0, k))
    return found


def apply_to_score(root, stacks, page_images, offset=0):
    loc = Locator(root, stacks, offset)
    done = []
    for sheet in sorted({s["sheet"] for s in stacks}):
        if sheet - 1 >= len(page_images):
            continue
        gray = cv2.imread(page_images[sheet - 1], 0)
        staves, seen = [], set()
        for s in (s for s in stacks if s["sheet"] == sheet):
            for part in s["parts"]:
                for st in part:
                    if round(st[0]) not in seen:
                        seen.add(round(st[0]))
                        staves.append(st)
        for kind, x, y, k in find_fermatas(gray, staves):
            top, bottom = staves[k][0], staves[k][1]
            hit = loc.where(sheet, x, (top + bottom) / 2.0)
            if not hit:
                continue
            g, s, pi, staff = hit
            m = loc.bars.get((g, pi))
            if m is None or not s.get("slots"):
                continue
            t = min(s["slots"], key=lambda sl: abs(s["left"] + sl[0] - x))[1]
            notes = [n for n, tt in _notes_of_staff(m, staff, loc.divs[(g, pi)])
                     if tt == t and n.find("chord") is None and n.find("grace") is None]
            if not notes:
                continue
            for n in notes:
                nt = n.find("notations")
                if nt is None:
                    nt = ET.SubElement(n, "notations")
                if nt.find("fermata") is None:
                    ET.SubElement(nt, "fermata", {"type": kind})
            done.append(dict(part=loc.parts[pi].get("id"), bar=int(m.get("number")), staff=staff, kind=kind))
    return done
