"""Proofreading list: every bar of a converted score that looks wrong, with where it is on the page and a picture.

    build(omr, work_dir, mxl_files, out_dir, repairs=None) -> dict(summary)

Inputs
  omr        the Audiveris project file (<name>.omr): knows where every measure sits on its page
  work_dir   the cleaned page images the script gave Audiveris (<name>_p01.png, ...)
  mxl_files  the final MusicXML file(s), in score order
  repairs    optional JSON files written by the automatic triplet repair (one per MusicXML file)

Output: <out_dir>/index.html (open it in a browser) and <out_dir>/crops/*.png.

What counts as a problem (rhythm only; a wrong pitch inside a bar that still adds up cannot be seen this way)
  over      a voice holds more beats than the bar: definitely wrong
  short     no voice fills the bar: probably missing notes (a pickup bar is excluded)
  empty     a whole staff has no notes or rests in the bar: probably missed notes
  repaired  the automatic triplet repair rebuilt this bar: check it
"""
import glob
import html
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET

import cv2
import numpy as np

COLORS = {"over": (40, 40, 220), "short": (30, 140, 245), "empty": (200, 60, 150), "repaired": (200, 120, 20)}   # BGR
RANK = {"over": 0, "empty": 1, "short": 2, "repaired": 3}
LABEL = {"over": "overfull", "short": "short", "empty": "empty staff", "repaired": "auto-repaired"}


# ----------------------------------------------------------------------------- page geometry from the .omr file
def read_geometry(omr_path):
    """Every measure of the book in order: its sheet, system, x-extent and the staves of each part."""
    z = zipfile.ZipFile(omr_path)
    names = [n for n in z.namelist() if re.match(r"sheet#\d+/sheet#\d+\.xml$", n)]
    names.sort(key=lambda s: int(re.search(r"#(\d+)/", s).group(1)))
    stacks = []
    for name in names:
        sheet_no = int(re.search(r"#(\d+)/", name).group(1))
        root = ET.fromstring(z.read(name))
        pic = root.find("picture")
        w, h = int(pic.get("width")), int(pic.get("height"))
        sys_no = 0
        for page in root.findall("page"):
            for system in page.findall("system"):
                sys_no += 1
                parts = []
                for part in system.findall("part"):
                    staves = []
                    for st in part.findall("staff"):
                        lines = st.find("lines").findall("line")
                        top = min(float(p.get("y")) for p in lines[0].findall("point"))
                        bottom = max(float(p.get("y")) for p in lines[-1].findall("point"))
                        staves.append((top, bottom, float(st.get("left")), float(st.get("right"))))
                    parts.append(staves)
                sts = system.findall("stack")
                for j, stk in enumerate(sts):
                    stacks.append(dict(sheet=sheet_no, system=sys_no, idx=j, n=len(sts), parts=parts, W=w, H=h,
                                       left=float(stk.get("left")), right=float(stk.get("right")),
                                       special=stk.get("special")))
    return stacks


def page_images(work_dir):
    files = [f for f in glob.glob(os.path.join(work_dir, "*_p*.png"))
             if re.search(r"_p\d+\.png$", f)]
    files.sort(key=lambda f: int(re.search(r"_p(\d+)\.png$", f).group(1)))
    return files


# ----------------------------------------------------------------------------- what is wrong, from the MusicXML
def _beats(x):
    return ("%.2f" % x).rstrip("0").rstrip(".")


def find_issues(root, offset, stacks, repairs):
    """-> {(global_measure, part_index): [issue dicts]}"""
    issues = {}
    rep = {}
    skip = {}
    for r in repairs or []:
        key = (r.get("part"), int(r["measure"]), r["staff"])
        (rep if r["kind"] == "repaired" else skip)[key] = r
    for pi, part in enumerate(root.findall("part")):
        pid = part.get("id")
        div, beats, bt = 1, 4, 4
        nstaves = 1
        for m in part.findall("measure"):
            at = m.find("attributes")
            if at is not None:
                if at.find("divisions") is not None:
                    div = int(at.find("divisions").text)
                if at.find("time") is not None:
                    t = at.find("time")
                    beats, bt = int(t.find("beats").text), int(t.find("beat-type").text)
                if at.find("staves") is not None:
                    nstaves = int(at.find("staves").text)
            mn = int(m.get("number"))
            g = offset + mn                                       # global measure number (1-based)
            stack = stacks[g - 1] if 0 <= g - 1 < len(stacks) else None
            pickup = bool(stack and stack["special"] == "PICKUP")
            exp = beats * 4.0 / bt
            per = {}
            for e in m.findall("note"):
                s = e.findtext("staff") or "1"
                per.setdefault(s, {})
                if e.find("chord") is None and e.find("duration") is not None:
                    v = e.findtext("voice") or "1"
                    per[s][v] = per[s].get(v, 0) + int(e.findtext("duration")) / float(div)
            for s in [str(i) for i in range(1, max(nstaves, 1) + 1)]:
                voices = per.get(s)
                key = (pid, mn, s)
                found = []
                if not voices:
                    found.append(dict(kind="empty", staff=s, text="nothing is written here (no notes, no rests)"))
                else:
                    worst = max(voices.items(), key=lambda kv: kv[1])
                    if worst[1] > exp + 1e-6:
                        found.append(dict(kind="over", staff=s, text="voice %s holds %s beats in a %s-beat bar (over by %s)"
                                          % (worst[0], _beats(worst[1]), _beats(exp), _beats(worst[1] - exp))))
                    elif worst[1] < exp - 1e-6 and not pickup:
                        found.append(dict(kind="short", staff=s, text="the fullest voice holds %s of %s beats (short by %s): "
                                          "notes are probably missing" % (_beats(worst[1]), _beats(exp), _beats(exp - worst[1]))))
                if key in rep:
                    r = rep[key]
                    found.insert(0, dict(kind="repaired", staff=s, text="rebuilt automatically: %d group(s) of three %s notes "
                                         "were read as plain notes and are now triplets. Check the notes and the '3' marks."
                                         % (r.get("groups", 0), r.get("note_value", "eighth"))))
                if key in skip and not found:
                    found.append(dict(kind="short", staff=s, text="looks like a missed triplet, but the arithmetic did not prove it "
                                      "(%s); left as read" % skip[key].get("why", "")))
                elif key in skip:
                    found[-1]["text"] += "  (also looked like a missed triplet: %s; left as read)" % skip[key].get("why", "")
                if found:
                    issues.setdefault((g, pi), []).extend(found)
    return issues


# ----------------------------------------------------------------------------- pictures
def _crop(img, x0, y0, x1, y1, max_w=None):
    h, w = img.shape[:2]
    x0, x1 = int(max(0, x0)), int(min(w, x1))
    y0, y1 = int(max(0, y0)), int(min(h, y1))
    c = img[y0:y1, x0:x1]
    if max_w and c.shape[1] > max_w:
        f = max_w / float(c.shape[1])
        c = cv2.resize(c, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return c


def make_crops(img_bgr, stack, part_index, flagged, crop_dir, stem):
    """-> (measure_png, context_png) file names (relative), or (None, None)."""
    parts = stack["parts"]
    staves = parts[min(part_index, len(parts) - 1)]
    u = (staves[0][1] - staves[0][0]) / 4.0
    # --- zoom on the measure, all staves of the part, flagged staves outlined
    x0, x1 = stack["left"] - 6, stack["right"] + 6
    y0, y1 = staves[0][0] - 2.6 * u, staves[-1][1] + 2.6 * u
    vis = img_bgr.copy()
    for s, kind in flagged.items():
        si = int(s) - 1
        if 0 <= si < len(staves):
            top, bottom = staves[si][0] - 0.9 * u, staves[si][1] + 0.9 * u
            cv2.rectangle(vis, (int(x0 + 3), int(top)), (int(x1 - 3), int(bottom)), COLORS[kind], max(2, int(u / 7)))
    zoom = _crop(vis, x0, y0, x1, y1, max_w=1100)
    a = stem + "_measure.png"
    cv2.imwrite(os.path.join(crop_dir, a), zoom)
    # --- the whole system with this measure boxed, for orientation
    allst = [st for p in parts for st in p]
    sx0, sx1 = min(s[2] for s in allst) - 40, max(s[3] for s in allst) + 20
    sy0, sy1 = min(s[0] for s in allst) - 2.8 * u, max(s[1] for s in allst) + 2.8 * u
    ctx = img_bgr.copy()
    cv2.rectangle(ctx, (int(x0), int(sy0 + u)), (int(x1), int(sy1 - u)), (60, 60, 230), max(3, int(u / 4)))
    ctx = _crop(ctx, sx0, sy0, sx1, sy1, max_w=1100)
    b = stem + "_system.png"
    cv2.imwrite(os.path.join(crop_dir, b), ctx)
    return "crops/" + a, "crops/" + b


# ----------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#fafafa;--fg:#222;--card:#fff;--line:#ddd;--over:#dc2828;--short:#f58c1e;--empty:#963cc8;--repaired:#1478c8;--mute:#667}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8e8ea;--card:#20232a;--line:#3a3f4a;--mute:#9aa}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--fg);line-height:1.45}
header{padding:16px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{margin:0 0 4px;font-size:20px}.sub{color:var(--mute);font-size:14px}
.filters{margin-top:8px;display:flex;gap:14px;flex-wrap:wrap;font-size:14px}.filters label{cursor:pointer}
main{max-width:1180px;margin:0 auto;padding:16px 20px 60px}
.pagelist{font-size:14px;margin:8px 0 18px}.pagelist b{display:inline-block;min-width:3.2em}
.card{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 14px}
.card.over{border-left-color:var(--over)}.card.empty{border-left-color:var(--empty)}.card.short{border-left-color:var(--short)}.card.repaired{border-left-color:var(--repaired)}
.card h2{margin:0;font-size:17px}.where{color:var(--mute);font-size:13px;margin:2px 0 8px}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:10px;color:#fff;margin-left:6px;vertical-align:middle}
.b-over{background:var(--over)}.b-short{background:var(--short)}.b-empty{background:var(--empty)}.b-repaired{background:var(--repaired)}
ul{margin:4px 0 10px;padding-left:20px;font-size:14px}.imgs{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}
.imgs figure{margin:0;max-width:100%}.imgs img{max-width:100%;border:1px solid var(--line);background:#fff}
figcaption{font-size:12px;color:var(--mute);margin-top:2px}.note{font-size:13px;color:var(--mute);margin:6px 0 14px}
"""

JS = """
const boxes=[...document.querySelectorAll('.filters input')];
function apply(){const on=new Set(boxes.filter(b=>b.checked).map(b=>b.value));
 document.querySelectorAll('.card').forEach(c=>{c.style.display=on.has(c.dataset.sev)?'':'none'});}
boxes.forEach(b=>b.addEventListener('change',apply));apply();
"""


def _where(stack, pages_for_sheet):
    return "page %d, system %d, bar %d of %d in that system" % (stack["sheet"], stack["system"], stack["idx"] + 1, stack["n"])


def write_html(path, title, cards, counts, notes, per_page):
    esc = html.escape
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           "<title>%s</title><style>%s</style></head><body>" % (esc(title), CSS),
           "<header><h1>%s</h1>" % esc(title),
           '<div class="sub">%d bar(s) to check. Bar numbers are the numbers in the converted file; the numbers printed '
           "in the score may differ.</div>" % len(cards),
           '<div class="filters">']
    for k in ("over", "empty", "short", "repaired"):
        out.append('<label><input type="checkbox" value="%s" checked> <span class="badge b-%s">%s</span> %d</label>'
                   % (k, k, LABEL[k], counts.get(k, 0)))
    out.append("</div></header><main>")
    for n in notes:
        out.append('<div class="note">%s</div>' % esc(n))
    out.append('<div class="note">Colours: <b>overfull</b> = a voice has more beats than the bar (certainly wrong). '
               "<b>empty staff</b> = nothing written. <b>short</b> = no voice fills the bar (probably missing notes). "
               "<b>auto-repaired</b> = I rebuilt triplets here; please verify. The picture is the cleaned page the converter "
               "saw (fingering numbers and pedal lines erased), with the staff in question outlined.</div>")
    out.append('<div class="pagelist"><b>By page</b><br>')
    for pg, nums in sorted(per_page.items()):
        out.append("<div><b>p.%d</b> bars %s</div>" % (pg, ", ".join(str(n) for n in sorted(nums))))
    out.append("</div>")
    for c in cards:
        out.append('<section class="card %s" data-sev="%s" id="m%d_p%d">' % (c["sev"], c["sev"], c["measure"], c["part"] + 1))
        out.append("<h2>Bar %d &middot; part %d%s</h2>" % (c["measure"], c["part"] + 1, "".join(
            ' <span class="badge b-%s">%s</span>' % (k, LABEL[k]) for k in c["kinds"])))
        out.append('<div class="where">%s</div>' % esc(c["where"]))
        out.append("<ul>%s</ul>" % "".join("<li><b>%s</b>: %s</li>" % (esc(i["staffname"]), esc(i["text"])) for i in c["issues"]))
        if c["img1"]:
            out.append('<div class="imgs"><figure><img src="%s" alt="bar %d"><figcaption>the bar (outlined staff = the problem)'
                       "</figcaption></figure>" % (c["img1"], c["measure"]))
            out.append('<figure><img src="%s" alt="system"><figcaption>where it sits in its system</figcaption></figure></div>' % c["img2"])
        out.append("</section>")
    out.append("<script>%s</script></main></body></html>" % JS)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def build(omr, work_dir, mxl_files, out_dir, repairs=None, title="Bars to proofread"):
    from omr_tools import read_mxl
    os.makedirs(os.path.join(out_dir, "crops"), exist_ok=True)
    stacks = read_geometry(omr)
    images = page_images(work_dir)
    cache = {}
    notes = []
    total_xml = 0
    all_issues = {}
    offset = 0
    for fi, f in enumerate(mxl_files):
        root = read_mxl(f)
        rep = None
        if repairs and fi < len(repairs) and repairs[fi] and os.path.exists(repairs[fi]):
            with open(repairs[fi], encoding="utf-8") as fh:
                rep = json.load(fh)
        n_meas = len(root.find("part").findall("measure"))
        for key, val in find_issues(root, offset, stacks, rep).items():
            all_issues.setdefault(key, []).extend(val)
        offset += n_meas
        total_xml += n_meas
    if total_xml != len(stacks):
        notes.append("Warning: the converted file has %d bars but the page layout lists %d, so pictures may be off for later bars."
                     % (total_xml, len(stacks)))
    if len(images) < max((s["sheet"] for s in stacks), default=0):
        notes.append("Warning: fewer page images (%d) than pages in the project; some pictures are missing." % len(images))

    cards = []
    per_page = {}
    counts = {}
    for (g, pi), found in sorted(all_issues.items()):
        stack = stacks[g - 1] if 0 <= g - 1 < len(stacks) else None
        kinds_by_staff = {}
        for it in found:
            cur = kinds_by_staff.get(it["staff"])
            if cur is None or RANK[it["kind"]] < RANK[cur]:
                kinds_by_staff[it["staff"]] = it["kind"]
        kinds = sorted({it["kind"] for it in found}, key=lambda k: RANK[k])
        sev = kinds[0]
        for k in kinds:
            counts[k] = counts.get(k, 0) + 1
        nst = max((int(i["staff"]) for i in found), default=1)
        part_staves = len(stack["parts"][min(pi, len(stack["parts"]) - 1)]) if stack else 1
        for it in found:
            it["staffname"] = ("upper staff" if it["staff"] == "1" else "lower staff") if part_staves > 1 else "staff"
        img1 = img2 = None
        if stack:
            per_page.setdefault(stack["sheet"], set()).add(g)
            si = stack["sheet"] - 1
            if si < len(images):
                if si not in cache:
                    cache[si] = cv2.imread(images[si], cv2.IMREAD_COLOR)
                img = cache[si]
                # Audiveris may trim an odd edge row/column, so the sizes can differ by a pixel or so
                if img is not None and abs(img.shape[1] - stack["W"]) <= 3 and abs(img.shape[0] - stack["H"]) <= 3:
                    img1, img2 = make_crops(img, stack, pi, kinds_by_staff, os.path.join(out_dir, "crops"),
                                            "m%03d_part%d" % (g, pi + 1))
        cards.append(dict(measure=g, part=pi, sev=sev, kinds=kinds, issues=found, img1=img1, img2=img2,
                          where=_where(stack, None) if stack else "position unknown"))
    cards.sort(key=lambda c: (c["measure"], c["part"]))
    write_html(os.path.join(out_dir, "index.html"), title, cards, counts, notes, per_page)
    return dict(path=os.path.join(out_dir, "index.html"), cards=len(cards), counts=counts, notes=notes)
