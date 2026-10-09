"""Helpers for convert.ps1: clean a sheet-music page before Audiveris, and report on the MusicXML after.

  python omr_tools.py check
  python omr_tools.py prep   --input FILE --outdir DIR [--dpi 300] [--pages 1,3] [--keep-digits] [--keep-pedal]
  python omr_tools.py report --mxl FILE [FILE ...]

prep removes two things that Audiveris misreads on printed piano music:
  * fingering numbers (read as tuplet numbers / stray text)
  * pedal lines (a long horizontal line with upward ticks, read as volta brackets)
Everything is measured relative to the staff-line spacing, so it is not tied to one page layout.
"""
import argparse
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import cv2
import pymupdf


# ----------------------------------------------------------------------------- page loading
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")


def natural_key(name):
    """Sort key so that 2 comes before 10 and case is ignored (same as alphabetical for equal-length names)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def to_gray(bgr, keep_red):
    """Colour image -> grayscale, whiting out strongly red ink (pencil fingerings / annotations)."""
    if not keep_red:
        b, g, r = (bgr[:, :, k].astype(np.int16) for k in range(3))
        red = ((r - np.maximum(g, b)) > 70) & (r > 140)
        red = cv2.dilate(red.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0   # also its soft edge
        bgr = bgr.copy()
        bgr[red] = 255
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def load_pages(src, dpi, wanted, keep_red=False):
    """Yield (page_number, grayscale uint8 array, label) for a PDF, an image, or a folder of images."""
    if os.path.isdir(src):
        files = sorted((f for f in os.listdir(src) if f.lower().endswith(IMAGE_EXTS)), key=natural_key)
        if not files:
            raise SystemExit("no images (%s) in folder: %s" % ("/".join(IMAGE_EXTS), src))
        for i, f in enumerate(files, 1):
            if wanted and i not in wanted:
                continue
            img = cv2.imread(os.path.join(src, f), cv2.IMREAD_COLOR)
            if img is None:
                raise SystemExit("cannot read image: " + os.path.join(src, f))
            yield i, to_gray(img, keep_red), f
    elif src.lower().endswith(".pdf"):
        doc = pymupdf.open(src)
        for i in range(len(doc)):
            if wanted and (i + 1) not in wanted:
                continue
            pix = doc[i].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
            rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.stride)[:, : pix.w * 3].reshape(pix.h, pix.w, 3)
            yield i + 1, to_gray(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), keep_red), "page %d" % (i + 1)
    else:
        img = cv2.imread(src, cv2.IMREAD_COLOR)
        if img is None:
            raise SystemExit("cannot read image: " + src)
        yield 1, to_gray(img, keep_red), os.path.basename(src)


TARGET_SPACING = 19.0     # staff-line spacing (px) at which Audiveris did best here (a 300 dpi A4 page)


def normalise_scale(gray):
    """Resize so the staff spacing is about TARGET_SPACING (low-resolution scans/screenshots need enlarging)."""
    u, _ = find_staves((gray < 128).astype(np.uint8))
    if u is None or abs(u - TARGET_SPACING) / TARGET_SPACING < 0.1:
        return gray, 1.0, u
    f = TARGET_SPACING / u
    interp = cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA
    return cv2.resize(gray, None, fx=f, fy=f, interpolation=interp), f, u


# ----------------------------------------------------------------------------- skew
def estimate_skew(gray):
    """Tilt of the page in degrees (positive = lines run downhill to the right), from the long staff lines."""
    bw = (gray < 160).astype(np.uint8) * 255
    w = gray.shape[1]
    lines = cv2.HoughLinesP(bw, 1, np.pi / 1800, 300, minLineLength=int(0.35 * w), maxLineGap=15)
    if lines is None:
        return 0.0
    ang = [np.degrees(np.arctan2(y2 - y1, x2 - x1)) for x1, y1, x2, y2 in lines.reshape(-1, 4)]
    ang = [x for x in ang if abs(x) < 3]
    return float(np.median(ang)) if ang else 0.0


def deskew(gray, min_angle=0.2):
    """Rotate level only if the tilt is at least min_angle degrees (resampling blurs the page and can
    cost more than a tiny tilt does).  Returns (image, angle actually corrected)."""
    angle = estimate_skew(gray)
    if abs(angle) < min_angle:
        return gray, 0.0
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)   # cv2: positive = counter-clockwise
    out = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    return out, angle


# ----------------------------------------------------------------------------- staff geometry
def find_staves(bw):
    """Return (interline u, [ (top_y, bottom_y, left_x, right_x) per staff ]) or (None, [])."""
    h, w = bw.shape
    rows = []
    for frac in (0.4, 0.3, 0.2):                           # tolerate shorter / slightly broken staff lines
        rows = np.where(bw.sum(axis=1) > frac * w)[0]
        if len(rows) >= 5:
            break
    if len(rows) < 5:
        return None, []
    groups, start, prev = [], rows[0], rows[0]
    for r in rows[1:]:
        if r - prev > 1:
            groups.append((start, prev))
            start = r
        prev = r
    groups.append((start, prev))
    centers = np.array([(a + b) / 2.0 for a, b in groups])
    if len(centers) < 5:
        return None, []
    diffs = np.diff(centers)
    u = float(np.median(diffs))
    staves, cur = [], [centers[0]]
    for c, d in zip(centers[1:], diffs):
        if d > 1.6 * u:
            staves.append(cur)
            cur = []
        cur.append(c)
    staves.append(cur)
    out = []
    for lines in staves:
        if len(lines) < 4:
            continue
        xs = np.where(bw[int(lines[0])] > 0)[0]
        out.append((lines[0], lines[-1], int(xs.min()), int(xs.max())))
    return u, out


def system_bottoms(bw, staves):
    """For each staff: True if it is the lowest staff of its system (the left barline does not run on to the next staff)."""
    flags = []
    for k, s in enumerate(staves):
        if k == len(staves) - 1:
            flags.append(True)
            continue
        y0, y1 = int(s[1]) + 4, int(staves[k + 1][0]) - 4
        x0 = max(0, s[2] - 1)
        band = bw[y0:y1, x0:x0 + 9]
        # joined staves score ~0.75-1.0 (a thin or curvy brace in a low-res scan can leave gaps); separate systems ~0
        joined = y1 > y0 and band.any(axis=1).mean() >= 0.5
        flags.append(not joined)
    return flags


def nearest_staff(cy, staves):
    best, bd = None, 1e9
    for st in staves:
        d = max(0.0, st[0] - cy, cy - st[1])
        if d < bd:
            best, bd = st, d
    return best, bd


# ----------------------------------------------------------------------------- cleaning
def clean_page(gray, keep_digits, keep_pedal):
    bw = (gray < 128).astype(np.uint8)
    u, staves = find_staves(bw)
    if u is None:
        print("  warning: no staff lines found; page left untouched")
        return gray, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 0, 0, None

    n, lab, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    boxes = [tuple(int(v) for v in st[i][:4]) for i in range(1, n)]
    kill = np.zeros(gray.shape, np.uint8)
    over = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    digits = pedals = 0
    erased = []

    # small glyphs (letters, noteheads on their own...) used to tell digits from words
    # (letters, punctuation dots, ...) but not hyphens, which are handled separately below
    hyphens = [b for b in boxes if b[3] < 0.4 * u and 0.3 * u <= b[2] <= 0.85 * u]
    glyphs = [b for b in boxes
              if 0.15 * u <= b[3] <= 3.2 * u and 0.15 * u <= b[2] <= 3.2 * u
              and not (b[3] < 0.4 * u and b[2] >= 0.3 * u)]
    near = 1.1 * u            # a lone letter like the "a" in "a tempo" is ~0.8 staff-spaces from its word

    if not keep_digits:
        for i in range(1, n):
            x, y, w, h, a = (int(v) for v in st[i])
            # digits are ~1 staff-space tall; eighth rests are ~2.3, so cap well below that
            if not (0.7 * u <= h <= 1.6 * u and 0.35 * u <= w <= 1.4 * u and a > 0.1 * u * u):
                continue
            # digits are taller than wide (~0.7); a detached notehead is wider than tall.  Fill alone is
            # unreliable: enlarged, blurry digits come out nearly solid (0.65-0.75).
            if w > h or a / float(w * h) > 0.88:
                continue
            staff, dist = nearest_staff(y + h / 2.0, staves)
            if staff is None or dist > 5 * u or x < staff[2] + 4 * u:     # far from music / measure numbers
                continue
            crowded = False
            for (bx, by, bw_, bh) in glyphs:
                if (bx, by, bw_, bh) == (x, y, w, h):
                    continue
                gap = max(bx - (x + w), x - (bx + bw_))
                if gap <= near and min(y + h, by + bh) - max(y, by) > 0:
                    lo, hi = min(x + w, bx + bw_), max(x, bx)
                    if any(lo - 3 <= hx and hx + hw <= hi + 3 and y <= hy <= y + h for hx, hy, hw, hh in hyphens):
                        continue                              # digits joined by a hyphen, e.g. "4-1"
                    crowded = True
                    break
            if crowded:
                continue
            kill[lab == i] = 255
            erased.append((x, y, w, h))
            cv2.rectangle(over, (x - 3, y - 3), (x + w + 3, y + h + 3), (0, 0, 255), 3)
            digits += 1
        # hyphens that sit right after an erased digit ("2-1"): erase them too
        for (hx, hy, hw, hh) in hyphens:
            if any(0 <= hx - (x + w) < 0.8 * u and y <= hy <= y + h for (x, y, w, h) in erased):
                ids = lab[hy:hy + hh, hx:hx + hw][bw[hy:hy + hh, hx:hx + hw] > 0]
                if len(ids):
                    kill[lab == ids[0]] = 255

    if not keep_pedal:
        lowest = system_bottoms(bw, staves)
        found = []                                             # (label, box, centre y)
        for i in range(1, n):
            x, y, w, h, a = (int(v) for v in st[i])
            if w < 8 * u or h > 2.2 * u:
                continue
            if h > 0.5 * u and a / float(w * h) > 0.5:         # a thick solid shape, not a line
                continue
            cy = y + h / 2.0
            # a pedal line sits under the LOWEST staff of a system; anything between the two
            # staves of a piano system (hairpins) or above the top staff (voltas) is left alone
            above = [k for k, s in enumerate(staves) if s[1] < cy]
            if not above or not lowest[above[-1]]:
                continue
            sub = lab[y:y + h, x:x + w] == i
            cols = np.where(sub.any(axis=0))[0]
            if len(cols) < 0.9 * w:                            # gaps: not one continuous line
                continue
            # lowest ink pixel in every column: the line itself (pedal ticks rise above it)
            ybot = np.array([np.where(sub[:, c])[0].max() for c in cols], dtype=float)
            fit = np.polyval(np.polyfit(cols, ybot, 1), cols)  # straight but possibly tilted (scans warp slightly)
            if np.percentile(np.abs(ybot - fit), 90) > 0.15 * u:
                continue                                       # curved: a slur or tie, not a pedal line
            if (h - 1) - np.median(ybot) > 0.5 * u:            # hooks hang below the line => volta bracket: keep
                continue
            # vertical thickness per column: constant and thin for a pedal line,
            # growing steadily from a point to an opening for a hairpin (crescendo / diminuendo)
            ext = np.array([np.ptp(np.where(sub[:, c])[0]) + 1 for c in cols], dtype=float)
            if np.median(ext) > 0.35 * u:
                continue
            if ext.std() > 0 and abs(np.corrcoef(cols, ext)[0, 1]) > 0.6:
                continue
            if any(s[0] - 0.5 * u <= cy <= s[1] + 0.5 * u for s in staves):
                continue                                       # inside a staff: never touch
            found.append((i, (x, y, w, h), cy))
        for i, (x, y, w, h), cy in found:
            # two parallel straight lines close together are the split arms of one hairpin
            twin = any(j != i and abs(cy - cy2) < 1.5 * u
                       and min(x + w, x2 + w2) - max(x, x2) > 0.5 * min(w, w2)
                       for j, (x2, y2, w2, h2), cy2 in found)
            if twin:
                continue
            kill[lab == i] = 255
            cv2.rectangle(over, (x - 3, y - 3), (x + w + 3, y + h + 3), (255, 0, 0), 3)
            pedals += 1

    k = int(0.9 * u) | 1
    grown = cv2.dilate(kill, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))) > 0
    grown &= ~((bw > 0) & (kill == 0))                         # never erase other symbols' ink
    out = gray.copy()
    out[grown] = 255
    return out, over, digits, pedals, u


def erode_ink(gray, n):
    """AGGRESSIVE option: binarise and erode all black ink by n px.  Recovers more whole notes in blurry enlarged
    scans, but thins and can break staff lines, stems and slurs, and adds rhythm errors elsewhere."""
    ink = (gray < 128).astype(np.uint8)
    ink = cv2.erode(ink, np.ones((3, 3), np.uint8), iterations=n)
    return np.where(ink > 0, 0, 255).astype(np.uint8)


def line_bands(bw, frac=0.25):
    """(first_row, last_row) of every staff line: rows that are dark across a large part of the page width."""
    rows = np.where(bw.sum(axis=1) > frac * bw.shape[1])[0]
    bands, start, prev = [], None, None
    for r in rows:
        if start is None:
            start = prev = r
        elif r - prev > 1:
            bands.append((start, prev))
            start = prev = r
        else:
            prev = r
    if start is not None:
        bands.append((start, prev))
    return bands


def enhance_gentle(gray, u):
    """Enlarging a blurry scan leaves staff lines ~5 px thick (Audiveris expects ~3) and closes the small hole of
    whole notes, which it recognises by that hole.  Two careful fixes, nothing else is touched:
      * thin each staff line to 3 px, but only in columns where nothing (a notehead, a stem) touches its edge;
      * widen the small enclosed white spots (note holes)."""
    bw = (gray < 128).astype(np.uint8)
    out = bw.copy()
    target = 3
    for a, b in line_bands(bw):
        t = b - a + 1
        if t <= target:
            continue
        top = (t - target) // 2
        bottom = t - target - top
        free_above = bw[a - 1] == 0 if a > 0 else np.ones(bw.shape[1], bool)
        free_below = bw[b + 1] == 0 if b + 1 < bw.shape[0] else np.ones(bw.shape[1], bool)
        for r in range(a, a + top):
            out[r, free_above] = 0
        for r in range(b - bottom + 1, b + 1):
            out[r, free_below] = 0
    # note holes: tiny white regions fully enclosed by dark ink (found with a stricter threshold so that
    # holes the blur has almost closed still show up), widened by one pixel
    n, lab, st, _ = cv2.connectedComponentsWithStats((gray >= 110).astype(np.uint8), connectivity=4)
    h, w = gray.shape
    holes = np.zeros(gray.shape, np.uint8)
    for i in range(1, n):
        x, y, bw_, bh, a = (int(v) for v in st[i])
        if x == 0 or y == 0 or x + bw_ >= w or y + bh >= h:
            continue                                           # touches the border: it is the page background
        if 3 <= a <= 0.7 * u * u and bw_ <= 1.6 * u and bh <= 1.2 * u:
            holes[lab == i] = 1
    holes = cv2.dilate(holes, np.ones((3, 3), np.uint8), iterations=1)
    out[holes > 0] = 0
    return np.where(out > 0, 0, 255).astype(np.uint8)


def staff_gaps(bw, staff, u):
    """Horizontal gaps (>= 2.5 staff spaces) where a staff genuinely stops and restarts.

    Scans are rarely perfectly straight: across a page a staff line can drift a pixel or two and slip out of a
    one-row test, which looks like a gap.  So a column counts as 'staff' when at least 3 of the 5 lines show ink
    within +-2 px of where they should be, and a gap only counts if a substantial piece of staff (8 staff
    spaces) lies on both sides of it."""
    h, w = bw.shape
    ev = np.zeros(w, dtype=int)
    for k in range(5):
        y = int(round(staff[0] + k * (staff[1] - staff[0]) / 4.0))
        ev += bw[max(0, y - 2):min(h, y + 3)].any(axis=0)
    has = ev >= 3
    start = None
    for x in range(w + 1):                                  # braces, clefs and barlines cross the lines for a few
        on = x < w and has[x]                               # columns only; a real staff segment is long
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start < 2 * u:
                has[start:x] = False
            start = None
    xs = np.where(has)[0]
    if len(xs) == 0:
        return []
    x0, x1 = int(xs[0]), int(xs[-1])
    gaps, run = [], None
    for x in range(x0, x1 + 1):
        if not has[x]:
            if run is None:
                run = x
        elif run is not None:
            if x - run >= 2.5 * u and run - x0 >= 8 * u and x1 - x >= 8 * u:
                gaps.append((run, x - 1))
            run = None
    return gaps


def split_side_by_side(img):
    """Two systems printed side by side on one row (e.g. a Coda next to the end of the piece) make Audiveris
    see one system with a hole in it.  Cut the row at the gap and stack the pieces as separate rows."""
    bw = (img < 128).astype(np.uint8)
    u, staves = find_staves(bw)
    if u is None:
        return img, 0
    lowest = system_bottoms(bw, staves)
    systems, cur = [], []
    for k, s in enumerate(staves):
        cur.append(s)
        if lowest[k]:
            systems.append(cur)
            cur = []
    if cur:
        systems.append(cur)
    h, w = img.shape
    bounds = [0] + [int((a[-1][1] + b[0][0]) / 2) for a, b in zip(systems, systems[1:])] + [h]
    pieces, splits = [], 0
    for sysm, y0, y1 in zip(systems, bounds, bounds[1:]):
        per_staff = [staff_gaps(bw, s, u) for s in sysm]
        # a real side-by-side break shows in every staff of the system, at the same place
        common = [g for g in per_staff[0]
                  if all(any(min(g[1], o[1]) - max(g[0], o[0]) > 0 for o in others) for others in per_staff[1:])]
        strip = img[y0:y1]
        if not common:
            pieces.append(strip)
            continue
        edges = [0] + [(g[0] + g[1]) // 2 for g in common] + [w]
        for a, b in zip(edges, edges[1:]):
            piece = np.full((y1 - y0, w), 255, np.uint8)
            piece[:, : b - a] = strip[:, a:b]
            pieces.append(piece)
        splits += len(common)
    return (np.vstack(pieces), splits) if splits else (img, 0)


def cmd_prep(a):
    os.makedirs(a.outdir, exist_ok=True)
    wanted = {int(p) for p in a.pages.split(",")} if a.pages else None
    global TARGET_SPACING
    TARGET_SPACING = a.spacing
    if os.path.isdir(a.input):
        base = os.path.basename(os.path.normpath(a.input))
    else:
        base = os.path.splitext(os.path.basename(a.input))[0]
    for pno, gray, label in load_pages(a.input, a.dpi, wanted, a.keep_red):
        tilt, page = 0.0, gray
        if not a.no_deskew:
            page, tilt = deskew(gray)
        page, scale, u_in = normalise_scale(page)
        out, over, digits, pedals, u = clean_page(page, a.keep_digits, a.keep_pedal)
        if u is None and not a.no_deskew and tilt == 0.0:      # no staves found: a small tilt may be the reason
            page, tilt = deskew(gray, 0.02)
            page, scale, u_in = normalise_scale(page)
            out, over, digits, pedals, u = clean_page(page, a.keep_digits, a.keep_pedal)
        out, nsplit = split_side_by_side(out) if u is not None else (out, 0)
        mode = a.enhance if a.enhance != "auto" else ("gentle" if scale > 1.3 else "none")  # auto: enlarged pages only
        if mode == "gentle":
            out = enhance_gentle(out, TARGET_SPACING)
        elif mode == "erode":
            out = erode_ink(out, 1)
        print("  page %d = %s" % (pno, label))
        if mode == "gentle":
            print("  page %d: thinned staff lines to 3 px and reopened note holes (blur from enlarging)" % pno)
        elif mode == "erode":
            print("  page %d: eroded all ink by 1 px (aggressive)" % pno)
        if nsplit:
            print("  page %d: %d row(s) hold two systems side by side (e.g. a Coda); moved onto their own rows"
                  % (pno, nsplit))
        if tilt:
            print("  page %d: straightened a tilt of %.2f degrees" % (pno, tilt))
        if scale != 1.0:
            print("  page %d: staff spacing was %.1f px, enlarged x%.2f to reach %.0f px"
                  % (pno, u_in, scale, TARGET_SPACING))
        png = os.path.join(a.outdir, "%s_p%02d.png" % (base, pno))
        cv2.imwrite(png, out)
        cv2.imwrite(os.path.join(a.outdir, "%s_p%02d_overlay.png" % (base, pno)),
                    cv2.resize(over, None, fx=0.5, fy=0.5))
        print("PAGE|%s" % png)
        print("  page %d: staff spacing %s px, erased %d fingering digits, %d pedal lines"
              % (pno, "%.1f" % u if u else "?", digits, pedals))


def cmd_pdf(a):
    """Bundle cleaned page images into one PDF so Audiveris treats them as a single book."""
    doc = pymupdf.open()
    for png in a.pngs:
        h, w = cv2.imread(png, cv2.IMREAD_GRAYSCALE).shape
        page = doc.new_page(width=w * 72.0 / a.dpi, height=h * 72.0 / a.dpi)   # real-world size, lossless image
        page.insert_image(page.rect, filename=png)
    doc.save(a.out)
    print("wrote %s (%d pages)" % (a.out, len(doc)))


def read_mxl(path):
    if not path.lower().endswith(".mxl"):                 # plain .musicxml / .xml
        return ET.parse(path).getroot()
    z = zipfile.ZipFile(path)
    name = [n for n in z.namelist() if n.endswith(".xml") and not n.startswith("META")][0]
    return ET.fromstring(z.read(name))


def cmd_merge(a):
    """Join several score-partwise files (pages / movements) into one continuous score."""
    from math import lcm
    roots = [read_mxl(p) for p in a.inputs]
    parts = [r.findall("part") for r in roots]
    if len({len(p) for p in parts}) != 1:
        raise SystemExit("cannot merge: files have different numbers of parts")

    # The time unit (<divisions>) can change inside one file, so use the lcm of every value that occurs.
    common = 1
    for r in roots:
        for d in r.iter("divisions"):
            common = lcm(common, int(d.text))

    base = roots[0]
    base_parts = base.findall("part")
    in_force = {}                                          # (part, tag, staff number) -> signature currently in force
    for fi, root in enumerate(roots):
        last_file = fi == len(roots) - 1
        for pi, part in enumerate(root.findall("part")):
            measures = part.findall("measure")
            current = 1                                    # time unit in force; a file may change it mid-way
            for mi, m in enumerate(measures):
                at = m.find("attributes")
                d = at.find("divisions") if at is not None else None
                if d is not None:
                    current = int(d.text)
                scale = common // current
                for dur in m.iter("duration"):
                    dur.text = str(int(dur.text) * scale)
                if at is not None:
                    if d is not None:
                        if fi == 0 and mi == 0:
                            d.text = str(common)           # one time unit for the whole merged score
                        else:
                            at.remove(d)
                    for tag in ("key", "time", "clef"):
                        for el in at.findall(tag):
                            slot = (pi, tag, el.get("number"))
                            sig = ET.tostring(el)
                            if in_force.get(slot) == sig:
                                at.remove(el)              # same signature as already in force: drop the repeat
                            else:
                                in_force[slot] = sig
                    if len(at) == 0:
                        m.remove(at)
                # the closing double bar of a page/movement that is not the last becomes an ordinary bar
                if mi == len(measures) - 1 and not last_file:
                    for b in m.findall("barline"):
                        if b.find("repeat") is None and b.find("ending") is None:
                            m.remove(b)
            if fi > 0:
                base_parts[pi].extend(measures)
    for part in base_parts:                                # renumber continuously
        for n, m in enumerate(part.findall("measure"), 1):
            m.set("number", str(n))
    dropped = 0
    if not a.keep_endings:
        # a real volta bracket is labelled with a number ("1.", "2.").  Without a digit in its text it is nearly
        # always a slur or tie arc (or a pedal line) that was misread, so remove it.
        for part in base_parts:
            for m in part.findall("measure"):
                for b in list(m.findall("barline")):
                    e = b.find("ending")
                    if e is not None and not re.search(r"\d", e.text or ""):
                        b.remove(e)
                        dropped += 1
                        if b.find("bar-style") is None and b.find("repeat") is None:
                            m.remove(b)
    ET.indent(base)
    with open(a.out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
                b'<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
                b'"http://www.musicxml.org/dtds/partwise.dtd">\n')
        f.write(ET.tostring(base, encoding="utf-8"))
    n = len(base_parts[0].findall("measure"))
    print("wrote %s from %d file(s): %d measures%s"
          % (a.out, len(roots), n, ", removed %d volta bracket mark(s) with no number" % dropped if dropped else ""))


# ----------------------------------------------------------------------------- report


def cmd_report(a):
    for path in a.mxl:
        root = read_mxl(path)
        print("=" * 70)
        print(os.path.basename(path))
        for part in root.findall("part"):
            div, beats, bt = 1, 4, 4
            too_long, too_short, tuplets = [], [], set()
            measures = part.findall("measure")
            for m in measures:
                at = m.find("attributes")
                if at is not None:
                    if at.find("divisions") is not None:
                        div = int(at.find("divisions").text)
                    t = at.find("time")
                    if t is not None:
                        beats, bt = int(t.find("beats").text), int(t.find("beat-type").text)
                exp = beats * 4.0 / bt
                tot = {}
                for e in m.findall("note"):
                    if e.find("time-modification") is not None:
                        tuplets.add(m.get("number"))
                    if e.find("chord") is None and e.find("duration") is not None:
                        key = (e.findtext("staff"), e.findtext("voice"))
                        tot[key] = tot.get(key, 0) + int(e.findtext("duration")) / float(div)
                for key, d in tot.items():
                    if d > exp + 1e-6:
                        too_long.append("m%s (voice %s: %.2f of %.0f beats)" % (m.get("number"), key[1], d, exp))
                    elif d < exp - 1e-6:
                        too_short.append("m%s (voice %s: %.2f of %.0f beats)" % (m.get("number"), key[1], d, exp))
            # a staff with no note and no rest in a measure means everything there was missed (typically whole notes)
            gaps = {}
            for m in measures:
                present = {e.findtext("staff") or "1" for e in m.findall("note")}
                for s in ("1", "2"):
                    if s not in present:
                        gaps.setdefault(s, []).append(m.get("number"))
            notes = part.findall(".//note")
            rests = [n for n in notes if n.find("rest") is not None]
            print("part %s: %d measures, %d notes, %d rests"
                  % (part.get("id"), len(measures), len(notes) - len(rests), len(rests)))
            print("  DEFINITE rhythm errors (a voice overfills its measure): %s" % (", ".join(too_long) or "none"))
            print("  possible rhythm errors (a voice underfills; often just a short 2nd voice): %s"
                  % (", ".join(too_short) or "none"))
            print("  measures containing tuplets (check these, fingering often causes them): %s"
                  % (", ".join(sorted(tuplets, key=int)) or "none"))
            print("  EMPTY right hand (no notes or rests; usually missed whole notes): %s"
                  % (", ".join(gaps.get("1", [])) or "none"))
            print("  EMPTY left hand: %s" % (", ".join(gaps.get("2", [])) or "none"))
        ends = root.findall(".//ending")
        print("volta brackets / endings: %d%s" % (len(ends), "  <-- check, usually a misread pedal line" if ends else ""))
        print("pedal marks exported: %d" % len(root.findall(".//pedal")))
        credits = [c.findtext("credit-words") or "" for c in root.findall("credit")]
        junk = [c for c in credits if len(c.strip()) <= 3]
        print("text: %d credit lines, %d look like junk (<=3 chars); title: %s"
              % (len(credits), len(junk), root.findtext("movement-title") or root.findtext(".//work-title") or "(none)"))
        tempo = [(m.get("number"), mt.findtext("beat-unit"), mt.findtext("per-minute")) for m in root.iter("measure") for mt in m.iter("metronome")]
        print("tempo marks: %s" % (tempo or "none"))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    pp = sub.add_parser("prep")
    pp.add_argument("--input", required=True)
    pp.add_argument("--outdir", required=True)
    pp.add_argument("--dpi", type=int, default=300)
    pp.add_argument("--pages")
    pp.add_argument("--keep-digits", action="store_true")
    pp.add_argument("--keep-pedal", action="store_true")
    pp.add_argument("--no-deskew", action="store_true")
    pp.add_argument("--keep-red", action="store_true")
    pp.add_argument("--spacing", type=float, default=19.0, help="target staff-line spacing in px (default 19)")
    pp.add_argument("--enhance", choices=["auto", "none", "gentle", "erode"], default="auto",
                    help="repair of enlarged blurry scans: gentle = thin staff lines + reopen note holes (auto for "
                         "enlarged pages); erode = aggressive 1 px erosion of all ink; none")
    pm = sub.add_parser("merge")
    pm.add_argument("--out", required=True)
    pm.add_argument("--inputs", nargs="+", required=True)
    pm.add_argument("--keep-endings", action="store_true", help="keep volta brackets that have no number text")
    pd = sub.add_parser("pdf")
    pd.add_argument("--out", required=True)
    pd.add_argument("--dpi", type=int, default=300)
    pd.add_argument("--pngs", nargs="+", required=True)
    pr = sub.add_parser("report")
    pr.add_argument("--mxl", nargs="+", required=True)
    a = p.parse_args()
    if a.cmd == "check":
        print("ok")
    elif a.cmd == "prep":
        cmd_prep(a)
    elif a.cmd == "pdf":
        cmd_pdf(a)
    elif a.cmd == "merge":
        cmd_merge(a)
    else:
        cmd_report(a)


if __name__ == "__main__":
    main()
