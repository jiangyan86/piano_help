"""Dynamics read from the page picture: p, f, ff, mf, pp ..., words such as dim. and cresc., and hairpins.

homr reads none of these.  They are looked for in the picture homr worked on, in the bands between the two staves of a row,
below its lower staff and above its upper staff, and written into the score as suggestions (blue directions).  They are
placed by bar, and by beat where the note columns of the bar in the picture match the moments of the bar in the score.

  find_marks(image, staves)  -> [per row: {"u", "bounds", "words": [...], "wedges": [...]}]
  place(marks_row, geo, measures) -> [(bar index, text)]   writes the <direction> elements into the measures of one row

The letters are read with RapidOCR (a dependency of homr): every blob of ink of the size of a letter is read at a few scales
and counts when the readings agree and are a dynamic or one of a few words.  Accuracy on a three-page piano piece: about nine
letter marks in ten, six hairpins in ten, a few false marks, which is why they are suggestions to check.
"""
import re
import xml.etree.ElementTree as ET
from collections import Counter

import cv2
import numpy as np

BLUE = "#0000FF"
DYN = ("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "sf", "sfz", "fz", "fp", "rf", "rfz")
WORDS = ("dim", "cresc", "decresc", "sempre", "poco", "rit", "accel", "rall", "legato", "marcato", "dolce", "sub")
_OCR = []


def _ocr():
    if not _OCR:
        from rapidocr import RapidOCR
        _OCR.append(RapidOCR())
    return _OCR[0]


def _votes(crop):
    votes = []
    for scale, grow in ((3, 0), (4, 0), (3, 1)):
        c = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        if grow:
            c = cv2.erode(c, np.ones((2, 2), np.uint8))
        c = cv2.copyMakeBorder(c, 20, 20, 40, 40, cv2.BORDER_CONSTANT, value=255)
        result = _ocr()(cv2.cvtColor(c, cv2.COLOR_GRAY2BGR))
        if result.txts:
            votes.append("".join(result.txts).replace(" ", ""))
    return votes


def _valid(t):
    if t in DYN:
        return True
    t = t.rstrip(".")
    return t == t.lower() and any(t.startswith(w) for w in WORDS)


def _classify(votes, w, h):
    """The reading that at least two of the readings agree on, if it is a dynamic or one of the words.  The width tells the
    letters apart that the reading mixes up: an ff read as f, a pp as P."""
    ratio = w / h
    count = Counter()
    for t in votes:
        if t == "P":
            t = "pp" if ratio > 1.2 else "p"
        if t == "f" and ratio > 1.2:
            t = "ff"
        if t == "p" and ratio > 1.2:
            t = "pp"
        if _valid(t):
            count[t] += 1
    if not count:
        return None
    t, n = count.most_common(1)[0]
    return t if n >= 2 else None


def find_marks(image, staves):
    """The marks of every row (staves: homr's rows, each {"x0", "x1", "y0", "y1", "unit", "notes": [(x, y)]})."""
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bw = (image < 150).astype(np.uint8)
    rows = []
    for k, geo in enumerate(staves):
        prev_bottom = staves[k - 1]["y1"] if k else 0
        next_top = staves[k + 1]["y0"] if k + 1 < len(staves) else image.shape[0]
        try:
            rows.append(_analyse_row(bw, geo, prev_bottom, next_top))
        except Exception as e:  # a row that cannot be read has no marks
            print(f"dynamics: row {k + 1} skipped ({e!r})", flush=True)
            rows.append(None)
    return rows


def _analyse_row(bw, geo, prev_bottom, next_top):
    x0, x1 = int(geo["x0"]), int(geo["x1"])
    u = 0.55 * geo["unit"]   # a staff space
    upper_top, lower_bottom = geo["y0"], geo["y1"]
    upper_bottom, lower_top = upper_top + 4 * u, lower_bottom - 4 * u
    bands = {"between": (upper_bottom + 0.5 * u, lower_top - 0.5 * u),
             "below": (lower_bottom + 0.5 * u, min(lower_bottom + 4.5 * u, next_top - 0.5 * u)),
             "above": (max(upper_top - 4.5 * u, prev_bottom + 0.5 * u), upper_top - 0.5 * u)}
    ya, yb = int(bands["between"][0]), int(bands["between"][1])
    vertical = cv2.morphologyEx(bw[ya:yb, x0:x1], cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(0.8 * (yb - ya))))))
    xs = np.where(vertical.any(axis=0))[0]
    bars, cur = [], []
    for x in xs:
        if cur and x - cur[-1] > 3:
            bars.append(x0 + int(np.mean(cur)))
            cur = []
        cur.append(x)
    if cur:
        bars.append(x0 + int(np.mean(cur)))
    out = {"u": u, "x0": x0, "x1": x1, "bars": bars, "words": [], "wedges": []}
    for name, (ya, yb) in bands.items():
        ya, yb = int(ya), int(yb)
        if yb - ya < 1.2 * u:
            continue
        band = bw[ya:yb, x0:x1].copy()
        long_vertical = cv2.morphologyEx(band, cv2.MORPH_OPEN,
                                         cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(0.9 * (yb - ya))))))
        band[cv2.dilate(long_vertical, np.ones((1, 3), np.uint8)) > 0] = 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(band, connectivity=8)
        boxes, wedge_parts = [], []
        for i in range(1, count):
            x, y, w, h, area = (int(v) for v in stats[i])
            mask = labels[y:y + h, x:x + w] == i
            if w >= 5 * u:   # letters with a hairpin attached, or a hairpin alone: the tall columns are letters
                extent = np.array([(np.where(mask[:, c])[0].max() - np.where(mask[:, c])[0].min() + 1) if mask[:, c].any()
                                   else 0 for c in range(w)])
                tall = extent >= 1.5 * u
                segments, c = [], 0
                while c < w:
                    if tall[c]:
                        d = c
                        while d + 1 < w and (tall[d + 1] or tall[d + 1:min(w, d + 1 + int(0.6 * u))].any()):
                            d += 1
                        segments.append((c, d))
                        c = d + 1
                    else:
                        c += 1
                rest = mask.copy()
                for c0, c1 in segments:
                    boxes.append((x + c0, y, c1 - c0 + 1, h))
                    rest[:, max(0, c0 - 3):c1 + 4] = False
                if rest.any():
                    ys, xs2 = np.where(rest)
                    wedge_parts.append((x + xs2.min(), y + ys.min(), rest[ys.min():ys.max() + 1, xs2.min():xs2.max() + 1]))
            elif 0.6 * u <= h <= 3.4 * u and 0.5 * u <= w <= 6 * u and area <= 0.72 * w * h:
                boxes.append((x, y, w, h))
        for x, y, mask in wedge_parts:
            h, w = mask.shape
            if w < 3.5 * u or h > 3 * u or sum(mask[:, c].any() for c in range(w)) < 0.8 * w:
                continue
            top = np.array([np.where(mask[:, c])[0].min() if mask[:, c].any() else 0 for c in range(w)])
            bottom = np.array([np.where(mask[:, c])[0].max() if mask[:, c].any() else 0 for c in range(w)])
            thick = bottom - top + 1
            q = max(3, w // 5)
            left, right = thick[:q].mean(), thick[-q:].mean()
            columns = np.arange(w)
            slope_top, slope_bottom = np.polyfit(columns, top, 1)[0], np.polyfit(columns, bottom, 1)[0]
            if min(left, right) <= 0.45 * u and max(left, right) >= 0.55 * u and slope_top * slope_bottom < 0 \
                    and abs(slope_top) > 0.012 and abs(slope_bottom) > 0.012:
                out["wedges"].append({"band": name, "kind": "crescendo" if right > left else "diminuendo",
                                      "x0": x0 + x, "x1": x0 + x + w})
        boxes.sort()
        clusters = []
        for b in boxes:
            last = clusters[-1] if clusters else None
            if last and b[0] - (last[0] + last[2]) <= 1.0 * u \
                    and min(b[1] + b[3], last[1] + last[3]) - max(b[1], last[1]) >= 0.5 * min(b[3], last[3]):
                nx, ny = min(last[0], b[0]), min(last[1], b[1])
                clusters[-1] = [nx, ny, max(last[0] + last[2], b[0] + b[2]) - nx, max(last[1] + last[3], b[1] + b[3]) - ny]
            else:
                clusters.append(list(b))
        for x, y, w, h in clusters:
            if w < 1.1 * u or h < 1.0 * u or x < 5 * u:   # the clef and key signature at the start of a row are no marks
                continue
            crop = (255 * (1 - bw[max(0, ya + y - 6):ya + y + h + 6, max(0, x0 + x - 6):x0 + x + w + 6])).astype(np.uint8)
            if crop.size == 0:
                continue
            text = _classify(_votes(crop), w, h)
            if text:
                out["words"].append({"band": name, "text": text, "x0": x0 + x, "x1": x0 + x + w})
    return out


# ---------------------------------------------------------------- placing the marks in the score
def _onsets(m):
    """{moment in divisions: the note of the upper staff that starts there, or any note} of a measure, and its length."""
    at, last, found, end = 0, 0, {}, 0
    for e in m:
        if e.tag == "backup":
            at -= int(e.findtext("duration"))
        elif e.tag == "forward":
            at += int(e.findtext("duration"))
        elif e.tag == "note":
            if e.find("chord") is None and e.find("grace") is None:
                last = at
                at += int(e.findtext("duration") or 0)
                end = max(end, at)
                better = e.findtext("staff") == "1" and found.get(last) is not None and found[last].findtext("staff") != "1"
                if last not in found or better:
                    found[last] = e
    return found, end


def _bar_intervals(row, count):
    """The x intervals of the bars of the row, count of them, from the barlines between the staves (stray lines that make
    more bars than the row has are dropped); None when they do not fit."""
    u = row["u"]
    inner = [b for b in row["bars"] if b > row["x0"] + 3 * u and b < row["x1"] - 3 * u]
    bounds = [row["x0"]] + inner + [row["x1"]]
    # the last barline is the row's end, not a boundary inside it
    if len(bounds) >= 2 and bounds[-1] - bounds[-2] < 2 * u:
        bounds = bounds[:-2] + [bounds[-1]]
    bounds = [b for i, b in enumerate(bounds) if i == 0 or b - bounds[i - 1] >= 3 * u]
    while len(bounds) - 1 > count:  # a stray vertical line (a long stem) made a bar too many: drop the narrowest one
        widths = [b - a for a, b in zip(bounds[:-1], bounds[1:])]
        k = int(np.argmin(widths))
        if widths[k] > 0.6 * float(np.median(widths)):
            break
        del bounds[k + 1 if k == 0 else k]
    return list(zip(bounds[:-1], bounds[1:])) if len(bounds) - 1 == count else None


def place(marks_row, geo, measures):
    """Write the marks of one row into its measures (the measures of that row, in order).  Returns [(index in measures, text)]."""
    if not marks_row:
        return []
    intervals = _bar_intervals(marks_row, len(measures))
    if intervals is None:
        return [(0, f"{len(marks_row['words']) + len(marks_row['wedges'])} mark(s) found in this row but its barlines could "
                    "not be matched to the bars, so none was written")] if marks_row["words"] or marks_row["wedges"] else []
    u = marks_row["u"]
    notes = sorted(x for x, _ in geo["notes"])
    result = []

    def locate(x, edge):
        """(index of the bar, the moment in divisions) for x; edge 'end' puts a stop on the next moment or the bar's end."""
        for i, (a, b) in enumerate(intervals):
            if a <= x < b or (i == len(intervals) - 1 and x >= a):
                found, end = _onsets(measures[i])
                moments = sorted(found)
                columns = []
                for nx in (n for n in notes if a <= n < b):
                    if columns and nx - columns[-1][-1] <= 0.8 * u:
                        columns[-1].append(nx)
                    else:
                        columns.append([nx])
                centres = [float(np.mean(c)) for c in columns]
                if moments and len(centres) == len(moments):
                    k = int(np.argmin([abs(c - x) for c in centres]))
                    if edge == "end" and centres[k] < x - 0.5 * u:
                        k += 1
                    return i, (moments[k] if k < len(moments) else end)
                fraction = (x - a) / (b - a)
                k = min(range(len(moments)), key=lambda j: abs(moments[j] - fraction * end)) if moments else 0
                return i, (moments[k] if moments else 0)
        return None

    def direction(m, moment, kind, content, band):
        d = ET.Element("direction", {"placement": "above" if band == "above" else "below"})
        ET.SubElement(d, "direction-type").append(content)
        ET.SubElement(d, "staff").text = "2" if band == "below" else "1"
        found, end = _onsets(m)
        if moment in found:
            m.insert(list(m).index(found[moment]), d)
        else:
            m.append(d)

    for w in marks_row["words"]:
        spot = locate((w["x0"] + w["x1"]) / 2, "start")
        if spot is None:
            continue
        i, moment = spot
        if w["text"] in DYN:
            content = ET.Element("dynamics", {"color": BLUE})
            ET.SubElement(content, w["text"])
        else:
            content = ET.Element("words", {"color": BLUE, "font-style": "italic"})
            content.text = w["text"]
        direction(measures[i], moment, "words", content, w["band"])
        result.append((i, w["text"]))
    for w in marks_row["wedges"]:
        start, stop = locate(w["x0"], "start"), locate(w["x1"], "end")
        if start is None or stop is None:
            continue
        direction(measures[start[0]], start[1], "wedge", ET.Element("wedge", {"type": w["kind"], "color": BLUE}), w["band"])
        direction(measures[stop[0]], stop[1], "wedge", ET.Element("wedge", {"type": "stop"}), w["band"])
        result.append((start[0], "crescendo hairpin" if w["kind"] == "crescendo" else "diminuendo hairpin"))
    return result
