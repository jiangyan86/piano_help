"""Pad bars that are shorter than the time signature.

MuseScore (and perhaps other programs) keeps the positions of 8va ends and starts by adding up the lengths of the bars
before them.  A bar that holds less than its time signature (an unfinished rhythm, a note Audiveris left out) or more
throws that sum off, and from then on the end of every octave line lands a little early: the last notes under the
bracket are shown an octave too high although the bracket is drawn to the right place.  Filling every short bar with a
<forward> to the full length (it draws nothing) keeps the sums right.  A bar that is too LONG cannot be repaired this
way: it still has to be fixed in the notes.

This is done last, on the finished file, because the proofreading list and the vote look for short bars.

    pad_short_measures(root) -> (padded, overfull) lists of (part id, bar number)
"""
import xml.etree.ElementTree as ET


def pad_short_measures(root):
    padded, overfull = [], []
    for part in root.findall("part"):
        div, beats, bt = 1, 4, 4
        for m in part.findall("measure"):
            a = m.find("attributes")
            if a is not None:
                if a.find("divisions") is not None:
                    div = int(a.find("divisions").text)
                if a.find("time") is not None:
                    beats, bt = int(a.find("time/beats").text), int(a.find("time/beat-type").text)
            if m.get("implicit") == "yes":
                continue                                      # a pickup bar is meant to be short
            expected = beats * 4 * div // bt
            cursor = top = 0
            for e in m:
                if e.tag == "note":
                    if e.find("chord") is None and e.find("grace") is None and e.find("duration") is not None:
                        cursor += int(e.findtext("duration"))
                elif e.tag == "backup":
                    cursor -= int(e.findtext("duration"))
                elif e.tag == "forward":
                    cursor += int(e.findtext("duration"))
                top = max(top, cursor)
            if top > expected:
                overfull.append((part.get("id"), int(m.get("number"))))
            elif top < expected:
                items = list(m)
                at = len(items)
                while at > 0 and items[at - 1].tag in ("barline", "print"):
                    at -= 1
                for gap in ([top - cursor] if cursor < top else []) + [expected - top]:
                    f = ET.Element("forward")
                    ET.SubElement(f, "duration").text = str(gap)
                    m.insert(at, f)
                    at += 1
                padded.append((part.get("id"), int(m.get("number"))))
    return padded, overfull
