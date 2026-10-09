"""Graphical (HTML) practice report for piano_check.py."""
import html

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
WHITE = {0, 2, 4, 5, 7, 9, 11}


def note_name(m):
    return f"{NAMES[m % 12]}{m // 12 - 1}"


def nm(ms):
    return " ".join(map(note_name, ms))


def judge(chords, i, ev, t=None):
    """Compare what was heard at one onset with chord i of the score."""
    c = chords[i]
    r = dict(i=i, measure=c.measure, beat=c.beat + 1, expected=list(c.notes), missing=[], extra=[],
             subs=[], status="ok", t=t)
    missing = [m for m in c.notes if not ev.present(m)]
    neighbours = set()
    for k in (i - 1, i + 1):
        if 0 <= k < len(chords):
            neighbours.update(chords[k].notes)
    # ignore likely overtone/octave ghosts and bleed from adjacent chords (hands not exactly together)
    extra = [m for m in ev.extras(c.notes)
             if m not in neighbours and not any(abs(m - e) in (12, 19, 24, 28, 31, 34, 36) for e in c.notes)]
    for e in list(extra):
        near = [m for m in missing if abs(m - e) <= 2]
        if near:
            m = min(near, key=lambda z: abs(z - e))
            r["subs"].append((e, m))
            missing.remove(m)
            extra.remove(e)
    for e in list(extra):  # a wrong key struck in place of an expected one that is silent
        pool = missing
        if pool:
            m = min(pool, key=lambda z: abs(z - e))
            r["subs"].append((e, m))
            pool.remove(m)
            extra.remove(e)
    r["missing"], r["extra"] = missing, extra
    if missing or extra or r["subs"]:
        r["status"] = "bad"
    return r


def analyse(chords, evs, times, pairs, last_idx, first_idx):
    """-> list of dicts, one per compared chord."""
    recs = []
    for i in range(first_idx, last_idx):
        if i not in pairs:
            c = chords[i]
            recs.append(dict(i=i, measure=c.measure, beat=c.beat + 1, expected=list(c.notes), missing=[],
                             extra=[], subs=[], status="skipped", t=None))
        else:
            recs.append(judge(chords, i, evs[pairs[i]], times[pairs[i]]))
    return recs


def describe(r):
    if r["status"] == "skipped":
        return "not heard"
    msg = [f"played {note_name(e)} instead of {note_name(m)}" for e, m in r["subs"]]
    if r["missing"]:
        msg.append("missing " + nm(r["missing"]))
    if r["extra"]:
        msg.append("extra " + nm(r["extra"]))
    return "; ".join(msg)


def keyboard_svg(r):
    wrong = set(r["extra"]) | {e for e, _ in r["subs"]}
    should = set(r["missing"]) | {m for _, m in r["subs"]}
    played = set(r["expected"]) - should
    notes = r["expected"] + list(wrong)
    lo, hi = min(notes) - 2, max(notes) + 2
    while lo % 12 not in WHITE:
        lo -= 1
    while hi % 12 not in WHITE:
        hi += 1

    def fill(m, base):
        if m in wrong:
            return "#e5484d"
        if m in should:
            return "#f5a524"
        if m in played:
            return "#30a46c"
        return base

    kw, kh = 14, 56
    whites = [m for m in range(lo, hi + 1) if m % 12 in WHITE]
    x = {m: i * kw for i, m in enumerate(whites)}
    w = len(whites) * kw
    out = [f'<svg width="{w}" height="{kh + 16}" viewBox="0 0 {w} {kh + 16}">']
    for m in whites:
        out.append(f'<rect x="{x[m]}" y="0" width="{kw}" height="{kh}" fill="{fill(m, "#fff")}" stroke="#555"/>')
        if m % 12 == 0:
            out.append(f'<text x="{x[m] + 2}" y="{kh + 12}" font-size="10" fill="#777">{note_name(m)}</text>')
    for m in range(lo, hi + 1):
        if m % 12 not in WHITE and m - 1 in x:
            out.append(f'<rect x="{x[m - 1] + kw * 0.65}" y="0" width="{kw * 0.7}" height="{kh * 0.6}" '
                       f'fill="{fill(m, "#222")}" stroke="#000"/>')
    out.append("</svg>")
    return "".join(out)


CSS = """body{font-family:system-ui,sans-serif;margin:24px auto;max-width:900px;padding:0 16px;color:#222}
h1{margin-bottom:4px}.sub{color:#666}.tiles{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0}
.tile{width:46px;height:46px;border-radius:6px;display:flex;flex-direction:column;align-items:center;
justify-content:center;font-size:12px;color:#fff;text-decoration:none}
.tile b{font-size:15px}.ok{background:#30a46c}.warn{background:#f5a524}.err{background:#e5484d}
.card{border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:10px 0;display:flex;gap:18px;
align-items:center;flex-wrap:wrap}.card h3{margin:0;font-size:15px;min-width:110px}
.legend span{display:inline-block;width:12px;height:12px;border-radius:2px;margin:0 4px 0 12px;vertical-align:-1px}
.msg{font-size:14px}"""


def write_html(path, recs, title, xml_text=None, measure_nums=None):
    bad = [r for r in recs if r["status"] != "ok"]
    o = [f"<!doctype html><meta charset=utf-8><title>Practice report</title><style>{CSS}</style>",
         f"<h1>Practice report</h1><div class=sub>{html.escape(title)} &middot; {len(recs)} positions compared, "
         f"{len(bad)} with problems</div>",
         '<p class=legend><span style="background:#30a46c"></span>correct '
         '<span style="background:#e5484d"></span>wrong key played '
         '<span style="background:#f5a524"></span>should have played</p>',
         "<h2>Measures</h2><div class=tiles>"]
    runs = []  # consecutive chords of one measure = one played measure
    for r in recs:
        if runs and runs[-1][0] == r["measure"] and r["beat"] >= runs[-1][1][-1]["beat"]:
            runs[-1][1].append(r)
        else:
            runs.append((r["measure"], [r]))
    for mn, rs in runs:
        n = sum(r["status"] != "ok" for r in rs)
        cls = "ok" if n == 0 else ("warn" if n == 1 else "err")
        first_bad = next((r["i"] for r in rs if r["status"] != "ok"), None)
        href = f' href="#c{first_bad}"' if first_bad is not None else ""
        o.append(f'<a class="tile {cls}"{href}><b>{html.escape(str(mn))}</b>{"&#10003;" if n == 0 else n}</a>')
    o.append("</div>")
    if xml_text:
        o.append(sheet_block(recs, xml_text, measure_nums))
    o.append("<h2>Mistakes</h2>")
    if not bad:
        o.append("<p>No mistakes detected.</p>")
    for r in bad:
        o.append(f'<div class=card id="c{r["i"]}"><h3>m.{html.escape(str(r["measure"]))} beat {r["beat"]:g}</h3>')
        if r["status"] == "skipped":
            o.append(f'<div class=msg>expected {nm(r["expected"])} &mdash; not heard</div>')
        else:
            o.append(keyboard_svg(r))
            o.append(f'<div class=msg>expected <b>{nm(r["expected"])}</b><br>{html.escape(describe(r))}</div>')
        o.append("</div>")
    open(path, "w", encoding="utf8").write("\n".join(o))


def sheet_block(recs, xml_text, nums):
    """OpenSheetMusicDisplay block: noteheads coloured, wrong keys written above the staff."""
    import json
    issues = {}
    for r in recs:
        if r["status"] == "ok" or r["status"] == "skipped" or r["measure"] not in nums:
            continue
        key = (nums.index(r["measure"]), round(r["beat"] - 1, 3))
        d = issues.setdefault(key, dict(m=key[0], onset=key[1], missing=set(), subs=set(), extra=set()))
        d["missing"] |= set(r["missing"])
        d["subs"] |= set(r["subs"])
        d["extra"] |= set(r["extra"])
    data = [dict(m=d["m"], onset=d["onset"], missing=sorted(d["missing"]),
                 subs=[[note_name(e), mm] for e, mm in sorted(d["subs"])],
                 extra=[note_name(e) for e in sorted(d["extra"])]) for d in issues.values()]
    return ("<h2>On the sheet music</h2><p class=legend><span style=\"background:#f5a524\"></span>key not played "
            "<span style=\"background:#e5484d\"></span>wrong key played (written above the note)</p>"
            "<div id=sheet></div>"
            "<script src=\"https://cdn.jsdelivr.net/npm/opensheetmusicdisplay@1.8.9/build/opensheetmusicdisplay.min.js\">"
            "</script><script>const ISSUES=" + json.dumps(data) + ";const XML=" + json.dumps(xml_text).replace("</", "<\\/")
            + ";" + SHEET_JS + "</script>")


SHEET_JS = """
(async()=>{
 const osmd=new opensheetmusicdisplay.OpenSheetMusicDisplay("sheet",{autoResize:false,drawTitle:false,drawPartNames:false});
 window.OSMD=osmd; await osmd.load(XML);
 const ms=osmd.Sheet.SourceMeasures;
 for(const is of ISSUES){
  const sm=ms[is.m]; if(!sm) continue;
  const notes=[];
  for(const c of sm.VerticalSourceStaffEntryContainers){
   if(Math.abs(c.Timestamp.RealValue*4-is.onset)>0.01) continue;
   for(const se of c.StaffEntries) if(se) for(const ve of se.VoiceEntries) for(const n of ve.Notes)
    if(n.Pitch) notes.push(n);
  }
  if(!notes.length) continue;
  const lab=[];
  for(const n of notes){
   const mid=n.Pitch.getHalfTone()+12;
   if(is.missing.includes(mid)){ n.NoteheadColor="#f5a524"; }
   for(const [played,m2] of is.subs) if(m2===mid){ n.NoteheadColor="#e5484d"; lab.push("→"+played); }
  }
  for(const e of is.extra) lab.push("+"+e);
  if(lab.length){ notes[0]._lab=(notes[0]._lab||[]).concat(lab); if(!notes[0].NoteheadColor) notes[0].NoteheadColor="#e5484d"; }
 }
 osmd.render();
 const NS="http://www.w3.org/2000/svg";
 for(const staves of osmd.GraphicSheet.MeasureList) for(const gm of staves) if(gm) for(const se of gm.staffEntries)
  for(const gve of se.graphicalVoiceEntries) for(const gn of gve.notes){
   const lab=gn.sourceNote._lab; if(!lab) continue;
   const g=gn.getSVGGElement(); if(!g) continue;
   const b=g.getBBox(); const t=document.createElementNS(NS,"text");
   t.setAttribute("x",b.x-2); t.setAttribute("y",b.y-14); t.setAttribute("fill","#e5484d");
   t.setAttribute("font-size","14"); t.setAttribute("font-weight","bold"); t.textContent=lab.join(" ");
   g.appendChild(t);
  }
})().catch(e=>{document.getElementById("sheet").textContent="Could not draw sheet music: "+e;});
"""
