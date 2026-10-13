"""Notes the project file has but the score does not.

When Audiveris cannot make the rhythm of a bar add up it leaves out the chords it cannot place (they stay in the project
file, with their heads, stems and beams, but belong to no voice) and the MusicXML simply lacks them: a bar comes out
short, or a triplet group is missing and the triplet repair cannot prove its arithmetic.

Such a chord is put back when it is clear what it is:
  * its heads are well graded and it has a stem,
  * its length follows from the beams on its stem (1 beam an eighth, 2 a 16th ...); with no beam it is a quarter (black
    head) or a half (open head), and then only when that fills the voice exactly,
  * the chord just to its left in the same staff is the last one of its voice, so the new note continues that voice,
  * its pitch comes from the staff position relative to that left neighbour, the sign in front of it from the project
    file (else the key signature).
Chords that stand next to each other under one beam are written as a beamed group.  The bar gets a text mark.

    plan(root, stacks, omr_path, offset) -> plans   (before other steps move notes around: the score is tied to the
                                                      project file through the times of its slots)
    apply(root, plans) -> list of what was added
"""
import xml.etree.ElementTree as ET
from collections import defaultdict

from omr_grace import ALTER, ACC_NAME, _key_alter
from omr_locate import Locator, box, load_sheets, pitch_key, STEP

MARK = "auto: missed note(s) added - please verify"
QUARTERS = {"whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25, "32nd": 0.125}
BY_BEAMS = {1: "eighth", 2: "16th", 3: "32nd"}


def _bar_length(part, measure):
    beats, bt = 4, 4
    for m in part.findall("measure"):
        t = m.find("attributes/time")
        if t is not None:
            beats, bt = int(t.findtext("beats")), int(t.findtext("beat-type"))
        if m is measure:
            break
    return beats * 4.0 / bt                                  # in quarter notes


def _voice_end(measure, staff, voice, div):
    """where a voice of a staff ends, in quarter notes (gaps made with <forward> count)"""
    cursor = end = 0
    for e in measure:
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
        elif e.tag == "note" and e.find("chord") is None and e.find("grace") is None and e.find("duration") is not None:
            cursor += int(e.findtext("duration"))
            if (e.findtext("staff") or "1") == staff and (e.findtext("voice") or "1") == voice:
                end = max(end, cursor)
    return end / float(div)


def _sheet_orphans(sh, loc, number):
    """{(g, pi, staff): [orphan dicts sorted by x]}"""
    found = defaultdict(list)
    for cid, hids in sh.members.items():
        if cid in sh.voiced:
            continue
        heads = sorted((sh.heads[h] for h in hids), key=lambda e: int(e.get("pitch", "0")))
        if not heads or min(float(h.get("grade", "0")) for h in heads) < 0.5:
            continue
        xs = sorted(box(h)[0] + box(h)[2] / 2.0 for h in heads)
        ys = [box(h)[1] + box(h)[3] / 2.0 for h in heads]
        hit = loc.where(number, xs[len(xs) // 2], sum(ys) / len(ys))
        if not hit:
            continue
        g, s, pi, staff = hit
        found[(g, pi, staff)].append(dict(cid=cid, heads=heads, x=xs[len(xs) // 2], y=sum(ys) / len(ys)))
    for v in found.values():
        v.sort(key=lambda o: o["x"])
    return found


def _basic_type(sh, o):
    """(note type, number of beams, dots) of an orphan chord, or None when it cannot be told"""
    stem = sh.chord_stem.get(o["cid"])
    beams = len(sh.stem_beams.get(stem, ())) if stem else 0
    shapes = {h.get("shape") for h in o["heads"]}
    dots = max(sum(1 for d, hid in sh.dot_head.items() if hid == h.get("id")) for h in o["heads"])
    if beams >= 1:
        return BY_BEAMS.get(beams), beams, dots
    if shapes <= {"WHOLE_NOTE"}:
        return "whole", 0, dots
    if not stem:
        return None
    if shapes <= {"NOTEHEAD_VOID"}:
        return "half", 0, dots
    if shapes <= {"NOTEHEAD_BLACK"}:
        return "quarter", 0, dots
    return None


def plan(root, stacks, omr_path, offset=0, sheets=None):
    sheets = sheets or load_sheets(omr_path)
    loc = Locator(root, stacks, offset)
    plans = []
    for number in sorted(sheets):
        sh = sheets[number]
        alters = [(a.get("shape"), box(a)) for a in sh.root.iter("alter") if a.get("shape") in ALTER and box(a)]
        for (g, pi, staff), orphans in _sheet_orphans(sh, loc, number).items():
            m = loc.bars.get((g, pi))
            if m is None:
                continue
            s = stacks[g - 1]
            st = next((stf for si, stf in enumerate(s["parts"][pi]) if str(si + 1) == staff), None)
            existing = loc.staff_chords(sh, number, g, pi, staff)
            if st is None or not existing:
                continue
            u = (st[1] - st[0]) / 4.0
            groups, last_beams = [], None
            for o in orphans:
                stem = sh.chord_stem.get(o["cid"])
                bm = set(sh.stem_beams.get(stem, ())) if stem else set()
                if groups and bm and last_beams and bm & last_beams:
                    groups[-1].append(o)
                else:
                    groups.append([o])
                last_beams = bm
            for grp in groups:
                left = [e for e in existing if e["x"] < grp[0]["x"] - 0.5 * u]
                if not left:
                    continue
                nb = max(left, key=lambda e: e["x"])
                ref = nb["notes"][0]
                rp = ref.find("pitch")
                d_ref = int(rp.findtext("octave")) * 7 + STEP.index(rp.findtext("step"))
                ref_pos = int(nb["heads"][0].get("pitch", "0"))
                items = []
                for o in grp:
                    ty = _basic_type(sh, o)
                    if ty is None or ty[0] is None:
                        break
                    stem = sh.stems.get(sh.chord_stem.get(o["cid"]))
                    sb = box(stem) if stem is not None else None
                    top_head = min(box(h)[1] for h in o["heads"])
                    heads = []
                    for h in sorted(o["heads"], key=lambda e: -int(e.get("pitch", "0"))):       # low to high
                        d = d_ref + (ref_pos - int(h.get("pitch", "0")))
                        hb = box(h)
                        sign = None
                        for shape, b in alters:
                            right = b[0] + b[2]
                            if hb[0] - 1.8 * u <= right <= hb[0] + 0.3 * u and \
                                    abs(b[1] + b[3] / 2.0 - (hb[1] + hb[3] / 2.0)) <= 1.3 * u:
                                if sign is None or right > sign[1]:
                                    sign = (ALTER[shape], right)
                        heads.append((STEP[d % 7], d // 7, sign[0] if sign else None))
                    items.append(dict(type=ty[0], beams=ty[1], dots=ty[2], heads=heads,
                                      direction="down" if (sb and sb[1] + sb[3] / 2.0 > top_head + 2 * u) else "up"))
                if items:
                    plans.append(dict(measure=m, pi=pi, staff=staff, neighbour=nb["notes"], items=items,
                                      voice=ref.findtext("voice") or "1"))
    return plans


def apply(root, plans):
    done = {}
    for pl in plans:
        m, staff, voice, pi = pl["measure"], pl["staff"], pl["voice"], pl["pi"]
        part = root.findall("part")[pi]
        div = 1
        for mm in part.findall("measure"):                                # the divisions in force at this bar
            if mm.find("attributes/divisions") is not None:
                div = int(mm.find("attributes/divisions").text)
            if mm is m:
                break
        last_notes = pl["neighbour"]
        n_items = len(pl["items"])
        for idx, it in enumerate(pl["items"]):
            voice_notes = [e for e in m.findall("note") if (e.findtext("staff") or "1") == staff
                           and (e.findtext("voice") or "1") == voice]
            if not voice_notes or voice_notes[-1] not in last_notes:       # the neighbour does not close its voice any more
                break
            qdur = QUARTERS[it["type"]] * (1.5 if it["dots"] == 1 else 1.0)
            if it["beams"] == 0 and abs(_voice_end(m, staff, voice, div) + qdur - _bar_length(part, m)) > 1e-6:
                break                                                     # an unbeamed note must fill the voice exactly
            dur = int(round(qdur * div))
            new = []
            for hi, (step, octave, sign) in enumerate(it["heads"]):
                alt = sign if sign is not None else _key_alter(root, pi, m, step)
                el = ET.Element("note")
                if hi:
                    ET.SubElement(el, "chord")
                p = ET.SubElement(el, "pitch")
                ET.SubElement(p, "step").text = step
                if alt:
                    ET.SubElement(p, "alter").text = str(alt)
                ET.SubElement(p, "octave").text = str(octave)
                ET.SubElement(el, "duration").text = str(dur)
                ET.SubElement(el, "voice").text = voice
                ET.SubElement(el, "type").text = it["type"]
                if it["dots"] == 1:
                    ET.SubElement(el, "dot")
                if sign is not None:
                    ET.SubElement(el, "accidental").text = ACC_NAME[sign]
                ET.SubElement(el, "stem").text = it["direction"]
                ET.SubElement(el, "staff").text = staff
                if n_items > 1 and it["beams"] >= 1:
                    for b in range(1, it["beams"] + 1):
                        ET.SubElement(el, "beam", {"number": str(b)}).text = \
                            "begin" if idx == 0 else ("end" if idx == n_items - 1 else "continue")
                new.append(el)
            at = list(m).index(last_notes[-1]) + 1
            key = (id(m), staff)
            if key not in done:
                d = ET.Element("direction", {"placement": "above"})
                ET.SubElement(ET.SubElement(d, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = MARK
                ET.SubElement(d, "staff").text = staff
                m.insert(at, d)
                at += 1
                done[key] = dict(part=part.get("id"), bar=int(m.get("number")), staff=staff, notes=0)
            for k, el in enumerate(new):
                m.insert(at + k, el)
            for e in list(m)[at + len(new):]:                              # the way back to the bar's start is longer now
                if e.tag == "backup":
                    e.find("duration").text = str(int(e.findtext("duration")) + dur)
                    break
            done[key]["notes"] += 1
            last_notes = sorted(new, key=pitch_key, reverse=True)
    return list(done.values())
