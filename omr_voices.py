"""One line split into two voices by Audiveris, put back into one.

Audiveris often writes a staff that is really ONE line of notes as two voices: the second starts after a gap
(<forward>), the rests in between are missing or doubled, and MuseScore shows an extra voice with rests that have to be
fought one by one.  When the notes themselves are right and only their order in time is wrong, the line can be put back
together without retyping anything:

  * the chords of the staff (their notes and lengths as exported) are taken in the order they stand on the page,
  * the rests are taken from the project file (the rests as printed, with their dots),
  * all of it, one after the other, must fill the bar exactly; if it does not, nothing is touched.

The staff is then written as one voice: the notes as they were, the rests as printed, the beams left to MuseScore (the old
ones belonged to the old voices).  Slurs, ties, articulations stay with their notes.

    plan(root, stacks, omr_path, offset) -> plans   (before other steps move notes around)
    apply(root, plans) -> list of what was merged
"""
import xml.etree.ElementTree as ET
from fractions import Fraction

from omr_confidence import BLUE
from omr_locate import Locator, box, load_sheets

MARK = "auto: voices merged into one - please verify"
REST_Q = {"WHOLE_REST": Fraction(4), "HALF_REST": Fraction(2), "QUARTER_REST": Fraction(1), "EIGHTH_REST": Fraction(1, 2),
          "SIXTEENTH_REST": Fraction(1, 4), "THIRTY_SECOND_REST": Fraction(1, 8), "SIXTY_FOURTH_REST": Fraction(1, 16)}
REST_TYPE = {"WHOLE_REST": "whole", "HALF_REST": "half", "QUARTER_REST": "quarter", "EIGHTH_REST": "eighth",
             "SIXTEENTH_REST": "16th", "THIRTY_SECOND_REST": "32nd", "SIXTY_FOURTH_REST": "64th"}


REST_VALUES = {Fraction(4): "WHOLE_REST", Fraction(2): "HALF_REST", Fraction(1): "QUARTER_REST", Fraction(1, 2): "EIGHTH_REST",
               Fraction(1, 4): "SIXTEENTH_REST", Fraction(1, 8): "THIRTY_SECOND_REST"}
REST_NAME = dict(REST_VALUES)


def _staff_end(measure, staff, div):
    cursor, ends = 0, {}
    for e in measure:
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
        elif e.tag == "note" and e.find("chord") is None and e.find("grace") is None and e.find("duration") is not None:
            cursor += int(e.findtext("duration"))
            if (e.findtext("staff") or "1") == staff:
                v = e.findtext("voice") or "1"
                ends[v] = max(ends.get(v, 0), cursor)
    return Fraction(max(ends.values()), div) if ends else Fraction(0)


def plan(root, stacks, omr_path, offset=0, sheets=None):
    sheets = sheets or load_sheets(omr_path)
    loc = Locator(root, stacks, offset)
    plans = []
    for g, s in enumerate(stacks, 1):
        sh = sheets.get(s["sheet"])
        if sh is None:
            continue
        for pi, staves in enumerate(s["parts"]):
            m = loc.bars.get((g, pi))
            if m is None:
                continue
            part = loc.parts[pi]
            div = loc.divs[(g, pi)]
            beats, bt = 4, 4
            for mm in part.findall("measure"):
                t = mm.find("attributes/time")
                if t is not None:
                    beats, bt = int(t.findtext("beats")), int(t.findtext("beat-type"))
                if mm is m:
                    break
            exp = Fraction(beats * 4, bt)
            for si, st in enumerate(staves):
                staff = str(si + 1)
                if _staff_end(m, staff, div) == exp:
                    continue
                voices = {e.findtext("voice") or "1" for e in m.findall("note") if (e.findtext("staff") or "1") == staff}
                if len(voices) < 2:
                    continue                                              # only a line split in voices is our business
                u = (st[1] - st[0]) / 4.0
                chords = loc.staff_chords(sh, number=s["sheet"], g=g, pi=pi, staff=staff) if False else \
                    loc.staff_chords(sh, s["sheet"], g, pi, staff)
                events = []
                ok = True
                seen = set()
                for c in chords:
                    notes = c["notes"]
                    durs = {n.findtext("duration") for n in notes}
                    if len(durs) != 1 or any(n.find("grace") is not None for n in notes):
                        ok = False
                        break
                    seen.update(id(n) for n in notes)
                    events.append((c["x"], Fraction(int(durs.pop()), div), "chord", notes))
                if not ok or not events:
                    continue
                # every pitched note of the staff must be accounted for by a chord (else the mapping is not safe)
                allnotes = [e for e in m.findall("note") if (e.findtext("staff") or "1") == staff and e.find("rest") is None]
                if any(id(n) not in seen for n in allnotes):
                    continue
                # the rests as printed
                dots = [box(e) for e in sh.dots.values() if box(e)]
                for r in sh.root.iter("rest"):
                    shape = r.get("shape")
                    b = box(r)
                    if shape not in REST_Q or not b:
                        continue
                    cx, cy = b[0] + b[2] / 2.0, b[1] + b[3] / 2.0
                    if not (s["left"] <= cx <= s["right"]):
                        continue
                    near = min(range(len(staves)), key=lambda k: abs(cy - (staves[k][0] + staves[k][1]) / 2.0))
                    if near != si or not (st[0] - 4 * u <= cy <= st[1] + 4 * u):
                        continue
                    d = REST_Q[shape]
                    if any(0 <= db[0] - (b[0] + b[2]) <= 1.4 * u and abs(db[1] + db[3] / 2.0 - cy) <= 1.2 * u for db in dots):
                        d = d * Fraction(3, 2)
                    events.append((cx, d, shape, None))
                events.sort(key=lambda t: t[0])
                if any(b[0] - a[0] < 0.5 * u for a, b in zip(events, events[1:])):
                    continue                                              # two things at one place: not a single line
                missing = exp - sum(e[1] for e in events)
                if missing > 0:
                    # Audiveris sometimes does not see one rest at all.  A single gap that is much wider than the others
                    # and a missing length that is one plain rest value: the rest stands in that gap
                    gaps = [(b[0] - a[0], k) for k, (a, b) in enumerate(zip(events, events[1:]))]
                    typical = sorted(g_ for g_, _k in gaps)[len(gaps) // 2] if gaps else 0
                    if gaps and missing in REST_VALUES and max(gaps)[0] > 1.6 * typical and                             sum(1 for g_, _k in gaps if g_ > 1.6 * typical) == 1:
                        k = max(gaps)[1]
                        x = (events[k][0] + events[k + 1][0]) / 2.0
                        events.insert(k + 1, (x, missing, REST_NAME[missing], None))
                if sum(e[1] for e in events) != exp:
                    continue
                plans.append(dict(measure=m, pi=pi, staff=staff, events=events, part=part, exp=exp))
    return plans


def apply(root, plans):
    done = []
    for pl in plans:
        m, staff, events, part, exp = pl["measure"], pl["staff"], pl["events"], pl["part"], pl["exp"]
        div = 1
        for mm in part.findall("measure"):
            if mm.find("attributes/divisions") is not None:
                div = int(mm.find("attributes/divisions").text)
            if mm is m:
                break
        if _staff_end(m, staff, div) == exp:
            continue
        items = list(m)
        mine = [e for e in items if e.tag == "note" and (e.findtext("staff") or "1") == staff]
        if any(e not in items for ev in events if ev[3] for e in ev[3]):
            continue
        idx = [i for i, e in enumerate(items) if e in mine]
        lo, hi = min(idx), max(idx)
        if any(e.tag == "note" and (e.findtext("staff") or "1") != staff for e in items[lo:hi + 1]):
            continue
        voice = (events[0][3][0].findtext("voice") if events[0][3] else mine[0].findtext("voice")) or "1"
        marks = [e for e in items[lo:hi + 1] if e.tag == "direction"]
        for e in items[lo:hi + 1]:
            if e.tag in ("note", "forward", "backup", "direction"):
                m.remove(e)
        block = [d for d in marks]
        mark = ET.Element("direction", {"placement": "above"})
        ET.SubElement(ET.SubElement(mark, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = MARK
        ET.SubElement(mark, "staff").text = staff
        block.append(mark)
        order = {id(e): i for i, e in enumerate(items)}
        for x, dur, kind, notes in events:
            if notes:
                for n in sorted(notes, key=lambda e: order[id(e)]):     # the leader of a chord comes first, as exported
                    n.find("voice").text = voice
                    for b in n.findall("beam"):                              # MuseScore beams by beat
                        n.remove(b)
                    block.append(n)
            else:
                r = ET.Element("note", {"color": BLUE})
                ET.SubElement(r, "rest")
                ET.SubElement(r, "duration").text = str(int(dur * div))
                ET.SubElement(r, "voice").text = voice
                ET.SubElement(r, "type").text = REST_TYPE[kind]
                if dur != REST_Q[kind]:
                    ET.SubElement(r, "dot")
                ET.SubElement(r, "staff").text = staff
                block.append(r)
        for k, el in enumerate(block):
            m.insert(lo + k, el)
        for e in list(m)[lo + len(block):]:                                   # the way back to the start of the bar
            if e.tag == "backup":
                e.find("duration").text = str(int(exp * div))
                break
        done.append(dict(part=part.get("id"), bar=int(m.get("number")), staff=staff, events=len(events)))
    return done
