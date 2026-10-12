"""Staccato dots that Audiveris took for augmentation dots.

A staccato dot sits right above or below a notehead; an augmentation dot sits in the space right after it, in line with
its centre.  When a staccato dot lies close to the following note of a beamed group, Audiveris can attach it to the
note before as an augmentation dot: that note becomes dotted, 50% longer, and the whole bar no longer adds up.

A dot that the project file attached to a head as an augmentation dot is rejected when it is too big to be one
(over 0.6 staff spaces; real ones are 0.4-0.55), or when
  * it is not where an augmentation dot goes (more than 1.1 staff spaces after the head's right edge, or more than 0.8
    above or below its centre), and
  * it is where a staccato dot goes for another head of the same staff (within 0.6 spaces of its centre line, 0.4-2.3
    spaces above or below it).
The note loses its dot (its length goes back to 2/3, the rest of the bar follows) and the other note gets a staccato.

    apply_to_score(root, stacks, omr_path, offset) -> list of what was changed
"""
import xml.etree.ElementTree as ET

from omr_locate import Locator, box, load_sheets


def find_bad_dots(sheets, stacks):
    """[(sheet number, chord heads of the wrongly dotted head, that head, chord heads of the staccato head, that head)]"""
    out = []
    for number, sh in sheets.items():
        if not sh.dot_head:
            continue
        lines = [st for s in stacks if s["sheet"] == number for p in s["parts"] for st in p]
        heads = list(sh.heads.values())
        for did, hid in sh.dot_head.items():
            d, h = sh.dots[did], sh.heads[hid]
            bd, bh = box(d), box(h)
            if not bd or not bh or not lines:
                continue
            cy = bh[1] + bh[3] / 2.0
            st = min(lines, key=lambda s: max(0.0, s[0] - cy, cy - s[1]))
            u = (st[1] - st[0]) / 4.0
            dcx, dcy = bd[0] + bd[2] / 2.0, bd[1] + bd[3] / 2.0
            after = dcx - (bh[0] + bh[2])
            if max(bd[2], bd[3]) > 0.6 * u:                   # too big for a dot (they are 0.4-0.55 spaces): a smudge
                out.append((number, sh.chord_heads(hid), h, None, None))
                continue
            if -0.3 * u <= after <= 1.1 * u and abs(dcy - cy) <= 0.8 * u:
                continue                                      # a normal augmentation dot
            alt = None
            for o in heads:
                if o is h:
                    continue
                bo = box(o)
                ocx, ocy = bo[0] + bo[2] / 2.0, bo[1] + bo[3] / 2.0
                if abs(dcx - ocx) <= 0.6 * u and 0.4 * u <= abs(dcy - ocy) <= 2.3 * u:
                    if alt is None or abs(dcx - ocx) < abs(dcx - (box(alt)[0] + box(alt)[2] / 2.0)):
                        alt = o
            if alt is not None:
                out.append((number, sh.chord_heads(hid), h, sh.chord_heads(alt.get("id")), alt))
    return out


def _backup_after(measure, note):
    items = list(measure)
    for e in items[items.index(note) + 1:]:
        if e.tag == "backup":
            return e
    return None


def _undot(measure, note, notes_of_chord):
    """remove the dot of a chord's notes, shorten them, and shorten the backup that follows"""
    if len(note.findall("dot")) != 1 or note.find("grace") is not None:
        return False
    old = int(note.findtext("duration"))
    new = old * 2 // 3
    if new * 3 != old * 2:
        return False
    for n in notes_of_chord:
        for dot in n.findall("dot"):
            n.remove(dot)
        n.find("duration").text = str(new)
    # a backup right after this note's voice must come back by less now
    b = _backup_after(measure, note)
    if b is not None:
        b.find("duration").text = str(max(0, int(b.findtext("duration")) - (old - new)))
    return True


def _staccato(note):
    nt = note.find("notations")
    if nt is None:
        nt = ET.SubElement(note, "notations")
    ar = nt.find("articulations")
    if ar is None:
        ar = ET.Element("articulations")
        nt.insert(0, ar)
    if ar.find("staccato") is None:
        ET.SubElement(ar, "staccato")
        return True
    return False


def apply_to_score(root, stacks, omr_path, offset=0, sheets=None):
    sheets = sheets or load_sheets(omr_path)
    bad = find_bad_dots(sheets, stacks)
    if not bad:
        return []
    loc = Locator(root, stacks, offset)
    done = []
    for number, chord, head, chord2, head2 in bad:
        a = loc.chord_notes(sheets[number], number, chord)
        b = loc.chord_notes(sheets[number], number, chord2) if chord2 else None
        if not a or (chord2 and not b):
            continue
        m, staff, notes = a
        note = notes[chord.index(head)]
        if not _undot(m, note, notes):
            continue
        if b:
            _staccato(b[2][chord2.index(head2)])
        part_id = next(p.get("id") for p in root.findall("part") if m in list(p))
        done.append(dict(part=part_id, bar=int(m.get("number")), staff=staff))
    return done
