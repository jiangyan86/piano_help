"""8va / 8vb lines (ottava) for homr_run.py.

homr reads an 8va line, and it does so unreliably: under a line that starts with "8va" it writes the notes an octave higher
than they are printed (and may lose one of them), under the "(8va)" that goes on in the next row it often does not.  The line is
erased from the page picture before homr reads it, so every note comes out at the pitch it is printed at.

erase_ottava(path) finds the dashed line (with its "8va" text and the hook at its end) above the upper staff or below the lower
staff of every row of a page picture and erases it, in place.
"""
import cv2
import numpy as np


def _binary(image):
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return (gray < 140).astype(np.uint8) * 255


def _groups(values, gap):
    """Sorted values split where two neighbours are more than gap apart."""
    out = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= gap:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _holes(mask):
    """Number of enclosed background regions of a binary mask (an 8 has two)."""
    contours, hierarchy = cv2.findContours(np.pad(mask, 1), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    return 0 if hierarchy is None else int(sum(1 for h in hierarchy[0] if h[3] >= 0))


def _says_eight(boxes, binary, unit):
    """x of the 8 if these glyphs (boxes x, y, w, h) read "8va" / "8vb" / "8", else None.  An 8 has two holes, but a scan
    often fills one, so it is also taken for the first of a taller glyph with a hole, a "v" (no hole) and an "a" or "b" (a
    hole) that follow it."""
    boxes = sorted(boxes)
    holes = [_holes(binary[y:y + h, x:x + w]) for x, y, w, h in boxes]
    for i in range(len(boxes) - 2):
        (x1, _, w1, h1), (x2, _, w2, h2), (x3, _, _, _) = boxes[i:i + 3]
        if (holes[i] >= 1 and holes[i + 1] == 0 and holes[i + 2] >= 1 and h1 >= 1.1 * h2
                and x2 - (x1 + w1) <= unit and x3 - (x2 + w2) <= unit):
            return boxes[i][0]
    for box, n in zip(boxes, holes):
        if n >= 2:
            return box[0]
    return None


def _regular(run):
    """Whether a chain of dash-like pieces (boxes x, y, w, h) is a dashed line: dashes of one length at one distance.  The
    strokes of a line of text (the bars of an e, a hyphen, an underscore) pass for dashes one by one but not as a chain."""
    if len(run) < 4:
        return True
    widths = np.array([d[2] for d in run], dtype=float)
    pitch = np.array([b[0] - a[0] for a, b in zip(run[:-1], run[1:])], dtype=float)   # start to start
    ok_w = np.mean(np.abs(widths - np.median(widths)) <= 0.35 * np.median(widths))
    ok_p = np.mean(np.abs(pitch - np.median(pitch)) <= 0.25 * np.median(pitch))
    return ok_w >= 0.8 and ok_p >= 0.8


def find_ottava_lines(binary, x0, x1, y_from, y_to, unit, above):
    """Dashed lines in a strip of the picture: [{"xs", "first", "last", "level", "text", "hook", "goes_on", "dashes", "erase"}];
    xs is where the "8va" text starts (None without an 8 next to the line), goes_on says that the line is still going at the
    right end of the row, dashes is how many dashes it has, erase the boxes (x, y, w, h) of its ink: dashes, hook and text."""
    ya, yb = max(0, int(y_from)), int(y_to)
    xa = max(0, int(x0 - 2 * unit))
    strip = binary[ya:yb, xa:int(x1 + unit)]
    count, _, stats, centroids = cv2.connectedComponentsWithStats(strip, connectivity=8)
    dashes, hooks, others, every = [], [], [], []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        box = (xa + x, ya + y, w, h)
        every.append(box)
        if h <= 0.27 * unit and 0.25 * unit <= w <= 3 * unit and w >= 2.2 * h:
            dashes.append(box)
        elif w <= 0.5 * unit and 0.3 * unit <= h <= 2.5 * unit and h >= 0.9 * w:
            hooks.append(box)
        elif 0.6 * unit <= h <= 2.5 * unit and w <= 3 * unit:
            others.append(box)
    found = []
    for rows in _groups([d[1] + d[3] / 2 for d in dashes], 0.8 * unit):
        level = float(np.mean(rows))
        chain = sorted((d for d in dashes if abs(d[1] + d[3] / 2 - level) <= 0.8 * unit), key=lambda d: d[0])
        run = [chain[0]]
        runs = []
        for d in chain[1:]:
            if d[0] - (run[-1][0] + run[-1][2]) <= 2.5 * unit:
                run.append(d)
            else:
                runs.append(run)
                run = [d]
        runs.append(run)
        for run in runs:
            if not _regular(run):
                continue
            first, last = run[0][0], run[-1][0] + run[-1][2]
            near = [h for h in hooks if abs(h[0] - last) <= 2 * unit
                    and (h[1] <= level + 1.3 * unit if above else h[1] + h[3] >= level - 1.3 * unit)   # a dashed hook starts a bit away
                    and h[1] <= level + 2.5 * unit and h[1] + h[3] >= level - 2.5 * unit]
            text = [o for o in others if o[0] + o[2] <= first + 0.5 * unit and o[0] >= first - 4 * unit
                    and abs(o[1] + o[3] / 2 - level) <= 1.3 * unit]
            eight = _says_eight(text, binary, unit)
            # the glyphs of the text and of the "(" before it: every piece of ink right before the first dash at the height of
            # the line
            glyphs = [b for b in every if first - 5 * unit <= b[0] + b[2] <= first + 0.5 * unit
                      and abs(b[1] + b[3] / 2 - level) <= 1.3 * unit and b[3] <= 2.5 * unit]
            # the hook is a vertical dash or two, toward the staff, at the end of the line
            bits = [b for b in every if last - 0.5 * unit <= b[0] <= last + 1.5 * unit
                    and ((b[2] <= 0.6 * unit and b[3] <= 2.5 * unit
                          and (level - 0.6 * unit <= b[1] and b[1] + b[3] <= level + 3 * unit if above
                               else level - 3 * unit <= b[1] and b[1] + b[3] <= level + 0.6 * unit))
                         or (abs(b[1] + b[3] / 2 - level) <= 0.6 * unit and b[3] <= 0.4 * unit))]
            found.append({"xs": eight, "first": first, "last": last, "level": level, "text": eight is not None,
                          "hook": bool(near), "dashes": len(run), "glyphs": len(glyphs),
                          "erase": run + near + bits + glyphs})
    # one line whose dashes were taken for two chains (the scan is a little crooked) is one line
    found.sort(key=lambda ln: ln["first"])
    merged = []
    for ln in found:
        for m in merged:
            if abs(m["level"] - ln["level"]) <= unit and ln["first"] - m["last"] <= 2.5 * unit:
                m["xs"] = min([v for v in (m["xs"], ln["xs"]) if v is not None], default=None)
                m["last"] = max(m["last"], ln["last"])
                m["text"], m["hook"] = m["text"] or ln["text"], m["hook"] or ln["hook"]
                m["dashes"] += ln["dashes"]
                m["glyphs"] = max(m["glyphs"], ln["glyphs"])
                m["erase"] = m["erase"] + ln["erase"]
                break
        else:
            merged.append(dict(ln))
    for m in merged:
        m["goes_on"] = not m["hook"] and m["last"] >= x1 - 3 * unit
    return merged


def _staves(binary):
    """The staves of a page picture, top to bottom: [(y0, y1, x0, x1, unit)] (y0 the top line, y1 the bottom line), found from
    the long horizontal lines."""
    h, w = binary.shape
    lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 12), 1)))
    rows = np.where(lines.sum(axis=1) > 0)[0]
    if len(rows) == 0:
        return []
    centres = [float(np.mean(g)) for g in _groups(rows, 1)]
    gaps = np.round(np.diff(centres))
    gaps = gaps[(gaps >= 5) & (gaps <= 60)]
    if len(gaps) == 0:
        return []
    values, counts = np.unique(gaps, return_counts=True)
    unit = float(values[np.argmax(counts)])   # the spacing of the staff lines is the most common gap
    staves, i = [], 0
    while i + 4 < len(centres):
        group = centres[i:i + 5]
        if all(0.8 * unit <= b_ - a_ <= 1.25 * unit for a_, b_ in zip(group[:-1], group[1:])):
            band = lines[int(group[0]) - 1:int(group[-1]) + 2]
            columns = np.where(band.sum(axis=0) > 0)[0]
            staves.append((group[0], group[-1], float(columns[0]), float(columns[-1]), unit))
            i += 5
        else:
            i += 1   # a stray line (a beam, a tie, an underline) in front of the staff
    return staves


def erase_ottava(path):
    """Erase the 8va / 8vb lines (dashes, hook and the text) from a page picture, in place.  A line is looked for above the
    upper staff and below the lower staff of every row (a grand staff is two staves less than nine staff spaces apart), up to
    6.5 staff spaces away and not farther than half way to the next staff.  A regular chain of at least four dashes is a
    line, and so are fewer dashes with an "8va" before and a hook after them.
    Returns how many lines were erased."""
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    binary = _binary(gray)
    staves = _staves(binary)
    erased = 0
    for i, (y0, y1, x0, x1, unit) in enumerate(staves):
        upper = i == 0 or staves[i - 1][1] + 9 * unit < y0   # the first staff of a row
        lower = i == len(staves) - 1 or y1 + 9 * unit < staves[i + 1][0]   # the last staff of a row
        strips = []
        if upper:
            top = staves[i - 1][1] + (y0 - staves[i - 1][1]) / 2 if i else 0
            strips.append((max(top, y0 - 6.5 * unit), y0 - 0.2 * unit, True))
        if lower:
            bottom = y1 + (staves[i + 1][0] - y1) / 2 if i + 1 < len(staves) else gray.shape[0]
            strips.append((y1 + 0.2 * unit, min(bottom, y1 + 6.5 * unit), False))
        for y_from, y_to, above in strips:
            for line in find_ottava_lines(binary, x0, x1, y_from, y_to, unit, above):
                if line["dashes"] < 4 and not (line["hook"] and line["text"]):
                    # the short "(8va)-" at the start of a row: text before it and a hook after
                    continue
                for x, y, w, h in line["erase"]:
                    gray[max(0, y - 1):y + h + 1, max(0, x - 1):x + w + 1] = 255
                erased += 1
    if erased:
        cv2.imwrite(str(path), gray)
    return erased
