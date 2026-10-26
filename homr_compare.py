"""Put every row of the original page above the same row of the converted score, so the two can be compared at a glance.

    python homr_compare.py OUTDIR            (the folder convert_homr.ps1 wrote)

Needs MuseScore 4 (found in its usual folders, or set HOMR_MUSESCORE to MuseScore4.exe).  It renders the MusicXML file,
finds the rows (systems) of the rendering, and writes OUTDIR\\compare\\index.html: for every row of the original, its picture
(OUTDIR\\systems, written by homr_run.py), the rendering of the same bars, the marks of report.txt for those bars, and a
warning when MuseScore breaks the rows somewhere else than the original does.  The rows of the score follow the original
(a row starts where the original's does); MuseScore may still add a break where a row is too full for the page.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np

CANDIDATES = (r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe", r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
              "/Applications/MuseScore 4.app/Contents/MacOS/mscore", "mscore4portable", "mscore", "musescore")


def find_musescore():
    for c in (os.environ.get("HOMR_MUSESCORE"),) + CANDIDATES:
        if c and (Path(c).exists() or not os.path.isabs(c)):
            try:
                subprocess.run([c, "--version"], capture_output=True, timeout=60)
                return c
            except (OSError, subprocess.SubprocessError):
                continue
    sys.exit("MuseScore not found: set HOMR_MUSESCORE to the path of MuseScore4.exe")


def run(ms, args):
    subprocess.run([ms] + args, capture_output=True, timeout=600)


def rows_of_rendering(exported):
    """[(page, first bar, last bar)] of MuseScore's own layout, read from the MusicXML it exports (it writes a
    print element at every row and page start)."""
    root = ET.parse(exported).getroot()
    rows, page = [], 1
    for i, m in enumerate(root.find("part").findall("measure"), 1):
        prints = m.findall("print")
        new_page = any(p.get("new-page") == "yes" for p in prints)
        if i == 1 or new_page or any(p.get("new-system") == "yes" for p in prints):
            if i > 1 and new_page:
                page += 1
            rows.append([page, i, i])
        else:
            rows[-1][2] = i
    return [tuple(r) for r in rows]


def systems_of_picture(path):
    """The rows of a MuseScore page picture: [(y0, y1, x0, x1)].  Every row starts with a long thin vertical line at the left
    (the barline that joins the two staves); those lines, one per row and all at the same x, give the rows."""
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    h, w = gray.shape
    dark = (gray < 160).astype(np.uint8)
    longest = max(40, h // 25)
    lines = cv2.morphologyEx(dark[:, :w // 3], cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, longest)))
    count, _, stats, _ = cv2.connectedComponentsWithStats(lines, connectivity=8)
    found = [tuple(int(v) for v in stats[i][:4]) for i in range(1, count) if stats[i][2] <= 5]   # x, y, w, h
    if not found:
        return []
    rows = []  # [x, y, w, h]: one per row, the leftmost of the vertical lines that cover the same height (the first row
    for f in sorted(found, key=lambda f: f[1]):  # of the first page starts further right, after the instrument name)
        for r in rows:
            overlap = min(r[1] + r[3], f[1] + f[3]) - max(r[1], f[1])
            if overlap > 0.5 * min(r[3], f[3]):
                if f[0] < r[0]:
                    r[:] = f
                break
        else:
            rows.append(list(f))
    barlines = sorted(rows, key=lambda r: r[1])
    out = []
    for x, y, bw, bh in barlines:
        pad = int(0.4 * bh)
        out.append((max(0, y - pad), min(h, y + bh + pad), max(0, x - 40), w - 10))
    return out


def marks_of_report(report):
    """{bar number: [text lines]} from report.txt (the 'bar N (page ..., system ...):' blocks)."""
    marks, bar = {}, None
    for line in Path(report).read_text(encoding="utf8").splitlines():
        m = re.match(r"bar (\d+) \(page", line)
        if m:
            bar = int(m.group(1))
            marks[bar] = []
        elif bar is not None and line.startswith("    "):
            marks[bar].append(line.strip())
        else:
            bar = None
    return marks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", help="the folder convert_homr.ps1 wrote")
    ap.add_argument("--dpi", type=int, default=110, help="resolution of the rendering")
    args = ap.parse_args()
    out = Path(args.outdir).resolve()
    score = next(iter(sorted(out.glob("*.musicxml"))), None)
    systems_json = out / "systems" / "systems.json"
    if score is None or not systems_json.exists():
        sys.exit("No score or systems\\systems.json in that folder: convert again with the current homr_run.py.")
    original_rows = json.loads(systems_json.read_text(encoding="utf8"))
    ms = find_musescore()
    work = out / "compare"
    work.mkdir(exist_ok=True)
    print("rendering with MuseScore ...", flush=True)
    run(ms, ["-r", str(args.dpi), "-o", str(work / "render.png"), str(score)])
    run(ms, ["-o", str(work / "layout.musicxml"), str(score)])
    ours = rows_of_rendering(work / "layout.musicxml")
    pictures = sorted(work.glob("render-*.png"), key=lambda p: int(re.search(r"-(\d+)\.png$", p.name).group(1)))
    crops, k = [], 0
    for number, picture in enumerate(pictures, 1):
        image = cv2.imread(str(picture))
        for y0, y1, x0, x1 in systems_of_picture(picture):
            name = f"row{k + 1:02d}.png"
            cv2.imwrite(str(work / name), image[y0:y1, x0:x1])
            crops.append(name)
            k += 1
    if len(crops) != len(ours):
        print(f"warning: found {len(crops)} rows in the pictures but {len(ours)} in the layout; the pictures are matched by order")
    marks = marks_of_report(out / "report.txt") if (out / "report.txt").exists() else {}

    blocks, mismatches = [], 0
    for row in original_rows:
        a, b = row["first_bar"], row["last_bar"]
        holding = [i for i, (_, f, l) in enumerate(ours) if f <= b and l >= a]
        exact = len(holding) == 1 and ours[holding[0]][1] == a and ours[holding[0]][2] == b
        mismatches += 0 if exact else 1
        warn = "" if exact else (f'<p class="warn">MuseScore breaks this row differently: it shows bars '
                                 + ", ".join(f"{ours[i][1]}-{ours[i][2]}" for i in holding) + f" for the original's {a}-{b}.</p>")
        original = f'<img src="../systems/{row["picture"]}">' if row["picture"] else "<p>(no picture)</p>"
        render = "".join(f'<img src="{crops[i]}">' for i in holding if i < len(crops))
        notes = [f"bar {bar}: " + "; ".join(marks[bar]) for bar in range(a, b + 1) if bar in marks]
        notes_html = "<ul>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>" if notes else ""
        blocks.append(f'<section><h2>Page {row["page"]}, row {row["system"]} <small>bars {a}-{b}</small></h2>{warn}'
                      f'<div class="label">original</div>{original}<div class="label">converted score (MuseScore)</div>'
                      f'{render}{notes_html}</section>')
    page = ("<!doctype html><meta charset='utf-8'><title>Original and converted score</title><style>"
            "body{font:15px system-ui;max-width:1200px;margin:1em auto;padding:0 1em}section{border-top:2px solid #888;"
            "padding:.6em 0}img{max-width:100%;display:block;margin:.2em 0}small{color:#666;font-weight:normal}"
            ".label{font-size:12px;color:#888;margin-top:.4em}.warn{background:#fff3cd;padding:.3em .6em}</style>"
            f"<h1>Original and converted score</h1><p>{len(original_rows)} rows of the original; "
            f"{'every row is a row of the converted score too' if not mismatches else str(mismatches) + ' rows are broken differently by MuseScore'}."
            " Compare: bar count, rests, clefs, accidentals, chord notes, stems and beams, voices.</p>" + "".join(blocks))
    (work / "index.html").write_text(page, encoding="utf8")
    print(f"{len(original_rows)} rows, {mismatches} broken differently")
    print(f"Comparison: {work / 'index.html'}")


if __name__ == "__main__":
    main()
