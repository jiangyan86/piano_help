"""Correct clef changes in the exported MusicXML using what Audiveris recorded in its project file.

Audiveris finds clef signs correctly (a small bass clef before a barline, the treble clef that follows ...), but the
MusicXML export puts an end-of-bar clef change at the START of the bar that contains it (one bar early) and drops
some changes altogether.  A bar in the wrong clef is a bar of wrong-height rests (every rest is exported with an
explicit display position that assumes the right clef) and wrong-looking notes.

  read_clefs(omr)             -> {(part index, staff in part): [(bar, kind, 'header' | 'end' | 'mid', x_fraction)]}
  apply_to_score(root, clefs, offset=0) -> messages

Rules (a clef sign belongs to the bar it sits in):
  * in the opening block of a system (clef/key/time): the clef in force from that bar's start;
  * near the right end of a bar (the usual courtesy change before a barline): in force from the NEXT bar;
  * in the middle of a bar: not handled (reported), the staff is left as exported.
The staff is only rewritten when the clef state per bar differs from what was exported.
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from fractions import Fraction

# kind -> (sign, line, clef-octave-change)
KINDS = {
    "TREBLE": ("G", "2", None), "BASS": ("F", "4", None), "ALTO": ("C", "3", None), "TENOR": ("C", "4", None),
    "TREBLE_8VA": ("G", "2", "1"), "TREBLE_8VB": ("G", "2", "-1"), "BASS_8VB": ("F", "4", "-1"), "BASS_8VA": ("F", "4", "1"),
}
_ATTR_ORDER = ["footnote", "level", "divisions", "key", "time", "staves", "part-symbol", "instruments", "clef"]


def read_clefs(omr_path):
    z = zipfile.ZipFile(omr_path)
    names = [n for n in z.namelist() if re.match(r"sheet#\d+/sheet#\d+\.xml$", n)]
    names.sort(key=lambda s: int(re.search(r"#(\d+)/", s).group(1)))
    out, g = {}, 0
    for name in names:
        root = ET.fromstring(z.read(name))
        inters = {}
        for el in root.iter("clef"):
            if el.get("id") and el.find("bounds") is not None:
                b = el.find("bounds")
                inters[el.get("id")] = (el.get("kind"), el.get("shape") or "", el.get("staff"), int(b.get("x")), int(b.get("w")))
        for system in root.iter("system"):
            parts = system.findall("part")
            staff_of, header_end = {}, {}
            for pi, part in enumerate(parts):
                for si, st in enumerate(part.findall("staff")):
                    staff_of[st.get("id")] = (pi, si + 1)
                    h = st.find("header")
                    header_end[st.get("id")] = float(h.get("stop")) if h is not None and h.get("stop") else None
            for j, stk in enumerate(system.findall("stack")):
                g += 1
                left, right = float(stk.get("left")), float(stk.get("right"))
                for pi, part in enumerate(parts):
                    ms = part.findall("measure")
                    if j >= len(ms):
                        continue
                    cl = ms[j].find("clefs")
                    if cl is None or not (cl.text or "").strip():
                        continue
                    for cid in cl.text.split():
                        if cid not in inters:
                            continue
                        kind, shape, staff, x, w = inters[cid]
                        if staff not in staff_of:
                            continue
                        cx = x + w / 2.0
                        frac = (cx - left) / max(right - left, 1.0)
                        he = header_end.get(staff)
                        if he is not None and cx <= he + 2:
                            where = "header"
                        elif frac >= 0.85:
                            where = "end"
                        else:
                            where = "mid"
                        out.setdefault(staff_of[staff], []).append((g, kind, where, round(frac, 2), x + w))
    return out


def _clef_elem(kind, number):
    sign, line, octv = KINDS[kind]
    c = ET.Element("clef", {"number": str(number)})
    ET.SubElement(c, "sign").text = sign
    ET.SubElement(c, "line").text = line
    if octv:
        ET.SubElement(c, "clef-octave-change").text = octv
    return c


def _same(el, kind):
    sign, line, octv = KINDS[kind]
    return (el.findtext("sign"), el.findtext("line"), el.findtext("clef-octave-change")) == (sign, line, octv)


def _insert_clef(measure, clef):
    at = measure.find("attributes")
    if at is None:
        at = ET.Element("attributes")
        pos = 0
        for i, e in enumerate(list(measure)):
            if e.tag in ("print", "barline"):
                pos = i + 1
            else:
                break
        measure.insert(pos, at)
    idx = 0
    for i, e in enumerate(list(at)):
        if e.tag in _ATTR_ORDER:
            idx = i + 1
    at.insert(idx, clef)
    # keep the clefs of a measure in staff order (1, 2, ...)
    clefs = sorted(at.findall("clef"), key=lambda c: int(c.get("number") or 1))
    first = next(i for i, e in enumerate(list(at)) if e.tag == "clef")
    for c in clefs:
        at.remove(c)
    for k, c in enumerate(clefs):
        at.insert(first + k, c)


def _insert_clef_mid(measure, clef, staff, t_target, div):
    """Put a clef change into the bar, just before the first note of the staff that starts at or after t_target
    (whole-note fraction).  Returns False if there is no such note."""
    cursor, last = 0, 0
    for e in list(measure):
        if e.tag == "note":
            if e.find("chord") is None:
                last = cursor
                if e.find("grace") is None:
                    cursor += int(e.findtext("duration") or 0)
            if (e.findtext("staff") or "1") == str(staff) and e.find("chord") is None \
                    and Fraction(last, 4 * div) >= t_target:
                at = ET.Element("attributes")
                at.append(clef)
                measure.insert(list(measure).index(e), at)
                return True
        elif e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
    return False


def apply_to_score(root, clefs, offset=0, stacks=None):
    """stacks: the measure geometry (omr_proofread.read_geometry), needed only for mid-bar clef changes."""
    msgs = []
    parts = root.findall("part")
    for (pi, staff), events in sorted(clefs.items()):
        if pi >= len(parts):
            continue
        part = parts[pi]
        measures = part.findall("measure")
        # desired clef kind in force at the start of every bar
        change_at = {}                                    # bar (file numbering) -> kind
        mid_at = {}                                       # bar -> (kind, x of the clef's right edge)
        for g, kind, where, frac, xr in sorted(events):
            if kind not in KINDS:
                continue
            bar = g - offset
            if where == "header":
                change_at[bar] = kind
            elif where == "end":
                change_at[bar + 1] = kind
            else:
                mid_at[bar] = (kind, xr)
        if not change_at and not mid_at:
            continue
        desired, end_state, cur = {}, {}, None
        for m in measures:
            n = int(m.get("number"))
            if n in change_at:
                cur = change_at[n]
            desired[n] = cur
            if n in mid_at:
                cur = mid_at[n][0]
            end_state[n] = cur
        # clef in force at the start / end of every bar, as exported (a mid-bar change counts for the end state)
        actual, actual_end, cur = {}, {}, None
        for m in measures:
            n = int(m.get("number"))
            at = m.find("attributes")
            if at is not None:
                for c in at.findall("clef"):
                    if (c.get("number") or "1") == str(staff):
                        cur = next((k for k, v in KINDS.items() if _same(c, k)), "?")
            actual[n] = cur
            for at2 in m.findall("attributes"):
                if at2 is not at:
                    for c in at2.findall("clef"):
                        if (c.get("number") or "1") == str(staff):
                            cur = next((k for k, v in KINDS.items() if _same(c, k)), "?")
            actual_end[n] = cur
        wrong = [n for n in desired if desired[n] and (actual.get(n) != desired[n] or actual_end.get(n) != end_state[n])]
        if not wrong:
            continue
        # rewrite: drop this staff's clef elements everywhere, add one wherever the state changes
        prev, div, done_mid, skipped = None, 1, 0, []
        for m in measures:
            n = int(m.get("number"))
            for at in m.findall("attributes"):
                if at.find("divisions") is not None:
                    div = int(at.find("divisions").text)
                for c in list(at.findall("clef")):
                    if (c.get("number") or "1") == str(staff):
                        at.remove(c)
                if len(at) == 0:
                    m.remove(at)
            if desired[n] and desired[n] != prev:
                _insert_clef(m, _clef_elem(desired[n], staff))
            if n in mid_at:
                ok = False
                g = n + offset
                if stacks and 0 < g <= len(stacks):
                    st = stacks[g - 1]
                    later = [(st["left"] + xo, t) for xo, t in st["slots"] if st["left"] + xo > mid_at[n][1] - 2]
                    if later:
                        t_target = min(later)[1]
                        ok = _insert_clef_mid(m, _clef_elem(mid_at[n][0], staff), staff, t_target, div)
                if ok:
                    done_mid += 1
                else:
                    skipped.append(n)
            prev = end_state[n] or prev
        text = "part %d staff %d: corrected the clef in bar%s %s" % (
            pi + 1, staff, "s" if len(wrong) != 1 else "",
            ", ".join(str(n) for n in wrong[:12]) + (" ..." if len(wrong) > 12 else ""))
        if done_mid:
            text += "; %d clef change(s) inside a bar placed" % done_mid
        if skipped:
            text += "; a clef change inside bar %s could not be placed" % ", ".join(map(str, skipped))
        msgs.append(text)
    return msgs
