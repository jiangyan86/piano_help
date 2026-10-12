"""Trill lines ("tr" followed by a wavy line).

Audiveris does not read them, so the trill is missing from the MusicXML.  A wavy line is easy to find in the page
picture: a long thin stroke above a staff whose centre line goes up and down many times (a slur has one hump, a
crescendo wedge none).  The "tr" sign sits at its left end.  The first note under the line (from the "tr" to the end of
the wave) gets a trill mark and the start of a wavy line, the last one the end of it.

    find_trills(gray, staves) -> [dict(x0, x1, left, staff)]      staves: [(top, bottom, left, right)]
    apply_to_score(root, stacks, page_images, offset) -> list of what was added
"""
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from omr_locate import Locator
from omr_ottava import _notes_of_staff, _slot_x


def find_trills(gray, staves):
    bw = (gray < 128).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    comps = [tuple(int(v) for v in st[i]) for i in range(1, n)]
    out = []
    for i, (x, y, w, h, a) in enumerate(comps):
        k = None
        for j, (top, bottom, _l, _r) in enumerate(staves):
            u = (bottom - top) / 4.0
            if top - 7 * u <= y + h <= top - 0.2 * u:
                k, uu = j, u
                break
        if k is None or not (3 * uu <= w and 0.3 * uu <= h <= 1.2 * uu and w >= 4 * h):
            continue
        sub = lab[y:y + h, x:x + w] == i + 1
        cy = np.array([np.mean(np.where(sub[:, c])[0]) for c in range(w) if sub[:, c].any()])
        sm = np.convolve(cy, np.ones(3) / 3.0, mode="valid")
        d = np.diff(sm)
        sign = np.sign(d[np.abs(d) > 0.05])
        if int((np.diff(sign) != 0).sum()) < 8:                   # a slur or a wedge has a few turns at most
            continue
        left = x
        for (cx, cy2, cw, ch, _a) in comps:                      # the "tr": a bold italic blob left of the wave
            if x - 3.5 * uu <= cx and cx + cw <= x + 0.6 * uu and abs((cy2 + ch / 2.0) - (y + h / 2.0)) <= 1.0 * uu \
                    and 0.5 * uu <= ch <= 1.8 * uu and 0.4 * uu <= cw <= 2.6 * uu:
                left = min(left, cx)
        out.append(dict(x0=x, x1=x + w, left=left, staff=k))
    return out


def apply_to_score(root, stacks, page_images, offset=0):
    loc = Locator(root, stacks, offset)
    done = []
    for sheet in sorted({s["sheet"] for s in stacks}):
        if sheet - 1 >= len(page_images):
            continue
        gray = cv2.imread(page_images[sheet - 1], 0)
        staves, ident, seen = [], [], set()
        for s in (s for s in stacks if s["sheet"] == sheet):
            for pi, part in enumerate(s["parts"]):
                for si, stf in enumerate(part):
                    if round(stf[0]) not in seen:
                        seen.add(round(stf[0]))
                        staves.append(stf)
                        ident.append((pi, str(si + 1)))
        for t in find_trills(gray, staves):
            pi, staff = ident[t["staff"]]
            u = (staves[t["staff"]][1] - staves[t["staff"]][0]) / 4.0
            xs, xe = t["left"] - 0.7 * u, t["x1"] + 0.5 * u
            ymid = (staves[t["staff"]][0] + staves[t["staff"]][1]) / 2.0
            first = last = None
            seq = []
            for g, s in enumerate(stacks, 1):
                if s["sheet"] != sheet or s["right"] < xs or s["left"] > xe:
                    continue
                if not any(abs(stf[0] - staves[t["staff"]][0]) < 2 for part in s["parts"] for stf in part):
                    continue                                    # another system
                m = loc.bars.get((g, pi))
                if m is None:
                    continue
                for n, tt in _notes_of_staff(m, staff, loc.divs[(g, pi)]):
                    x = _slot_x(s, tt)
                    if x is not None and xs <= x <= xe and n.find("chord") is None and n.find("grace") is None \
                            and n.find("pitch") is not None:
                        seq.append((g, tt, n, m))
            if not seq:
                continue
            seq.sort(key=lambda r: (r[0], r[1]))
            first, last = seq[0][2], seq[-1][2]
            nt = first.find("notations")
            if nt is None:
                nt = ET.SubElement(first, "notations")
            orn = ET.SubElement(nt, "ornaments")
            ET.SubElement(orn, "trill-mark")
            ET.SubElement(orn, "wavy-line", {"type": "start", "number": "1"})
            nt2 = last.find("notations")
            if nt2 is None:
                nt2 = ET.SubElement(last, "notations")
            orn2 = ET.SubElement(nt2, "ornaments")
            ET.SubElement(orn2, "wavy-line", {"type": "stop", "number": "1"})
            done.append(dict(part=loc.parts[pi].get("id"), bar=int(seq[0][3].get("number")), staff=staff, notes=len(seq)))
    return done
