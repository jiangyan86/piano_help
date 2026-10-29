"""Put every row of the original page above the same row of the converted score, so the two can be compared at a glance.

    python homr_compare.py OUTDIR            (the folder convert_homr.ps1 wrote)

Needs MuseScore 4 (found in its usual folders, or set HOMR_MUSESCORE to MuseScore4.exe).  It renders the MusicXML file,
finds the rows (systems) of the rendering, and writes OUTDIR\\compare\\index.html: for every row of the original, its picture
(OUTDIR\\systems, written by homr_run.py), the rendering of the same bars, the marks of report.txt for those bars, and a
warning when MuseScore breaks the rows somewhere else than the original does.  Every row has a place for your review notes
(looks right / has problems / unsure, and free text): they are kept in the browser as you type and can be saved as
review-notes.md or copied.  The rows of the score follow the original
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
    # a long line inside a row that is no barline of the grand staff (the stem of a low note, a long tie) is not a row: it
    # lies for the most part inside the picture of a row already found
    rows = []
    for r in sorted(out, key=lambda r: r[0] - r[1]):   # the tallest first
        if any(min(r[1], q[1]) - max(r[0], q[0]) > 0.5 * (r[1] - r[0]) for q in rows):
            continue
        rows.append(r)
    return sorted(rows)


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


PAGE_HEAD = """<!doctype html><meta charset='utf-8'><title>Original and converted score</title><style>
body{font:15px system-ui;max-width:1200px;margin:0 auto;padding:0 1em 3em}
section{border-top:2px solid #888;padding:.6em 0}img{width:100%;display:block;margin:.2em 0}
small{color:#666;font-weight:normal}.label{font-size:12px;color:#888;margin-top:.4em}
.warn{background:#fff3cd;padding:.3em .6em}
#bar{position:sticky;top:0;background:#fff;border-bottom:1px solid #aaa;padding:.5em 0;z-index:5}
#bar button{margin-right:.4em}#count{margin-left:.6em;color:#555}
.review{margin-top:.5em;display:flex;gap:.6em;align-items:flex-start;flex-wrap:wrap}
.review textarea{flex:1;min-width:260px;font:14px system-ui}
.status button{margin-right:.3em;padding:.25em .6em;cursor:pointer;border:1px solid #888;background:#f4f4f4}
.status button.on[data-status=ok]{background:#c8e6c9}.status button.on[data-status=bad]{background:#ffcdd2}
.status button.on[data-status=unsure]{background:#fff3cd}
section.ok{background:#f6fbf6}section.bad{background:#fff6f6}
</style><div id="bar"><button id="save">Save notes (review-notes.md)</button><button id="copy">Copy notes</button>
<button id="clear">Clear all</button><span id="count"></span></div>
<h1>Original and converted score</h1><p>@SUMMARY@ Compare: bar count, rests, clefs, accidentals, chord notes, stems and beams,
voices. Your notes are kept in this browser as you type; save them when you are done.</p>"""

PAGE_SCRIPT = """<script>
const FOLDER = @FOLDER@, KEY = 'homr-review:' + location.pathname;
const sections = [...document.querySelectorAll('section[data-row]')];
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { state = {}; }
function store() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} count(); }
function count() {
  const done = sections.filter(s => (state[s.dataset.row] || {}).status).length;
  const noted = sections.filter(s => ((state[s.dataset.row] || {}).text || '').trim()).length;
  document.getElementById('count').textContent = done + ' of ' + sections.length + ' rows marked, ' + noted + ' with notes';
}
function paint(section) {
  const r = state[section.dataset.row] || {};
  section.querySelectorAll('.status button').forEach(b => b.classList.toggle('on', b.dataset.status === r.status));
  section.classList.toggle('ok', r.status === 'ok'); section.classList.toggle('bad', r.status === 'bad');
  section.querySelector('textarea').value = r.text || '';
}
sections.forEach(section => {
  const id = section.dataset.row; paint(section);
  section.querySelectorAll('.status button').forEach(b => b.addEventListener('click', () => {
    const r = state[id] = state[id] || {}; r.status = (r.status === b.dataset.status) ? '' : b.dataset.status;
    paint(section); store();
  }));
  section.querySelector('textarea').addEventListener('input', e => {
    (state[id] = state[id] || {}).text = e.target.value; store();
  });
});
function notes() {
  const label = {ok: 'looks right', bad: 'HAS PROBLEMS', unsure: 'unsure'};
  const lines = ['# Review notes: ' + FOLDER, ''];
  sections.forEach(s => {
    const r = state[s.dataset.row] || {};
    if (!r.status && !(r.text || '').trim()) return;
    lines.push('## ' + s.dataset.title + (r.status ? ' - ' + label[r.status] : ''));
    if ((r.text || '').trim()) lines.push(r.text.trim());
    lines.push('');
  });
  if (lines.length === 2) lines.push('(no notes yet)');
  return lines.join('\\n');
}
document.getElementById('save').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([notes()], {type: 'text/markdown'}));
  a.download = 'review-notes.md'; a.click();
});
document.getElementById('copy').addEventListener('click', () => {
  navigator.clipboard.writeText(notes()).then(() => { document.getElementById('count').textContent = 'copied'; setTimeout(count, 1500); });
});
document.getElementById('clear').addEventListener('click', () => {
  if (confirm('Delete all notes and marks on this page?')) { state = {}; sections.forEach(paint); store(); }
});
count();
</script>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir", help="the folder convert_homr.ps1 wrote")
    ap.add_argument("--dpi", type=int, default=170, help="resolution of the rendering")
    args = ap.parse_args()
    out = Path(args.outdir).resolve()
    # the score is the newest .musicxml of the folder that is not a single page (p01.musicxml ... are left over from older runs)
    scores = [p for p in out.glob("*.musicxml") if not re.fullmatch(r"p\d+\.musicxml", p.name)]
    score = max(scores, key=lambda p: p.stat().st_mtime, default=None)
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
    for old in work.glob("row*.png"):   # the rows of an earlier run (there may have been more of them)
        old.unlink()
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
        title = f'Page {row["page"]}, row {row["system"]} (bars {a}-{b})'
        blocks.append(f'<section data-row="{row["page"]}-{row["system"]}" data-title="{html.escape(title)}">'
                      f'<h2>Page {row["page"]}, row {row["system"]} <small>bars {a}-{b}</small></h2>{warn}'
                      f'<div class="label">original</div>{original}<div class="label">converted score (MuseScore)</div>'
                      f'{render}{notes_html}'
                      '<div class="review"><span class="status"><button data-status="ok">looks right</button>'
                      '<button data-status="bad">has problems</button><button data-status="unsure">unsure</button></span>'
                      '<textarea rows="2" placeholder="Your notes for this row, e.g. bar 12: upper staff an octave lower"></textarea>'
                      '</div></section>')
    summary = (f"{len(original_rows)} rows of the original; "
               + ("every row is a row of the converted score too" if not mismatches
                  else str(mismatches) + " rows are broken differently by MuseScore") + ".")
    page = (PAGE_HEAD.replace("@SUMMARY@", summary).replace("@FOLDER@", html.escape(out.name)) + "".join(blocks)
            + PAGE_SCRIPT.replace("@FOLDER@", json.dumps(out.name)))
    (work / "index.html").write_text(page, encoding="utf8")
    print(f"{len(original_rows)} rows, {mismatches} broken differently")
    print(f"Comparison: {work / 'index.html'}")


if __name__ == "__main__":
    main()
