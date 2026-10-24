"""Straighten staves that are bent or tilted differently across the page, before Audiveris reads them.

A scan of a book page or a screenshot of a score video is rarely flat: the staff lines of one system can run slightly
uphill while those of the next run downhill, and some bend in the middle.  One rotation for the whole page (deskew)
cannot fix that, and Audiveris wants lines that are straight to a fraction of a staff space.

Every staff is followed along the page (a 5-line comb is matched in a number of vertical strips), its centre line is
fitted with a smooth curve, and the picture is re-sampled so that each curve becomes a horizontal line: every
column is shifted up or down by how far the staff above/below it has bent there, and the shift is blended smoothly
between neighbouring staves.  Pages that are straight already are left alone.

    dewarp(gray, u) -> (picture, info)       u: staff-line spacing in pixels; info: dict(staves, max_bend, applied)
"""
import cv2
import numpy as np


def _comb_tops(prof, u):
    """[(y, score)] of 5-line combs in a row profile (dark fraction per row), best first and not overlapping"""
    n = len(prof)
    k = np.arange(5) * u
    best = []
    sm = np.maximum.reduce([np.roll(prof, s) for s in (-1, 0, 1)])
    for y in range(2, int(n - 4 * u - 3)):
        v = [sm[int(round(y + kk))] for kk in k]
        if min(v) >= 0.18 and sorted(v)[1] >= 0.3:
            best.append((y, float(sum(v))))
    best.sort(key=lambda t: -t[1])
    chosen = []
    for y, sc in best:
        if all(abs(y - c) > 3 * u for c, _ in chosen):
            chosen.append((y, sc))
    return chosen


def _refine(prof, y, u):
    """sub-pixel centre of the comb: the five line positions refined by a local centroid"""
    pts = []
    for k in range(5):
        c = y + k * u
        lo, hi = int(round(c - 0.4 * u)), int(round(c + 0.4 * u)) + 1
        w = prof[lo:hi]
        if w.sum() <= 0:
            return None
        pts.append((np.arange(lo, hi) * w).sum() / w.sum() - k * u)
    return float(np.median(pts))


def _follow(gray, u):
    """the staves of a page followed from strip to strip: [dict(pts=[(x, y of the top line)...])]"""
    H, W = gray.shape
    bw = (gray < 128).astype(np.float32)
    nstrip = max(14, int(W / (5.0 * u)))
    sw = W // nstrip
    cands = []
    for i in range(nstrip):
        x0, x1 = i * sw, min(W, (i + 1) * sw)
        prof = bw[:, x0:x1].mean(axis=1)
        row = []
        for y, _sc in _comb_tops(prof, u):
            r = _refine(prof, y, u)
            if r is not None:
                row.append(r)
        cands.append(((x0 + x1) / 2.0, sorted(row)))
    # link the staves from strip to strip
    tracks = []
    for x, ys in cands:
        used = set()
        for t in tracks:
            if t["last_x"] < x - 3 * sw:
                continue
            j = min(range(len(ys)), key=lambda j: abs(ys[j] - t["pts"][-1][1]) if j not in used else 1e9, default=None)
            if j is not None and j not in used and abs(ys[j] - t["pts"][-1][1]) <= 0.5 * u:
                t["pts"].append((x, ys[j]))
                t["last_x"] = x
                used.add(j)
        for j, y in enumerate(ys):
            if j not in used:
                tracks.append(dict(pts=[(x, y)], last_x=x))
    tracks = [t for t in tracks if len(t["pts"]) >= max(6, int(0.4 * nstrip))]
    return tracks


def locate_staves(gray, u):
    """[(top, bottom, left, right)] of every staff found by following the lines (more robust than a projection of the
    whole page when notes and beams break the lines up or the staves are bent)"""
    out = []
    for t in _follow(gray, u):
        xs = [p[0] for p in t["pts"]]
        y = float(np.median([p[1] for p in t["pts"]]))
        out.append((y, y + 4 * u, int(min(xs)), int(max(xs))))
    return sorted(out)


def dewarp(gray, u, min_bend=0.2):
    H, W = gray.shape
    tracks = _follow(gray, u)
    info = dict(staves=len(tracks), max_bend=0.0, applied=False)
    if not tracks:
        return gray, info
    curves = []
    for t in tracks:
        xs = np.array([p[0] for p in t["pts"]])
        ys = np.array([p[1] for p in t["pts"]])
        deg = 3 if len(xs) >= 10 else 2
        keep = np.ones(len(xs), bool)
        for _ in range(3):                                         # drop points that sit away from the curve
            co = np.polyfit(xs[keep], ys[keep], deg)
            res = np.abs(ys - np.polyval(co, xs))
            keep = res <= max(1.5, 3 * np.median(res[keep]))
            if keep.sum() < deg + 2:
                break
        curves.append((co, xs.min(), xs.max(), float(np.median(ys[keep]))))
    X = np.arange(W, dtype=np.float32)
    centre = []
    for co, xa, xb, target in curves:
        c = np.polyval(co, np.clip(X, xa, xb)).astype(np.float32)   # no extrapolation past the observed part
        centre.append((target, c - target))
        info["max_bend"] = max(info["max_bend"], float(np.abs(c - target).max()))
    if info["max_bend"] < min_bend * u:
        return gray, info
    centre.sort(key=lambda t: t[0])
    targets = np.array([t[0] for t in centre], np.float32)
    offs = np.stack([t[1] for t in centre])                          # staves x columns
    # displacement of every row of every column: blended between the staves, constant above the first and below the last
    ys = np.arange(H, dtype=np.float32)
    off_map = np.empty((H, W), np.float32)
    for x in range(W):
        off_map[:, x] = np.interp(ys, targets, offs[:, x])
    map_y = ys[:, None] + off_map
    map_x = np.tile(X[None, :], (H, 1))
    out = cv2.remap(gray, map_x, map_y.astype(np.float32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                    borderValue=255)
    info["applied"] = True
    return out, info
