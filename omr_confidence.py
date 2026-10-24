"""Colour the notes that deserve a second look, so a manual proofread can start where the doubt is.

MuseScore shows the `color` of a note from the MusicXML file.  Three colours, strongest first:
  blue    the script wrote or rebuilt this note (a note put back, a run rebuilt from the page, a grace note, a triplet
          group): it comes from the picture, not from Audiveris' reading, and was never checked
  red     Audiveris read this notehead with low confidence (the project file grades it below 0.6; good ones are 0.7+)
  orange  every note of a staff in a bar whose rhythm does not add up (too short or too long): something in the bar is
          missing, doubled or of the wrong length, it need not be this note

    plan(root, stacks, omr_path, offset) -> [(note element, colour)]   (before other steps move notes around)
    apply(plan)                                                         (sets the colours that are still free)
    mark_irregular(root)                                                (orange for staves whose rhythm is wrong)
LEGEND is the sentence to show the user.
"""
from omr_locate import Locator, load_sheets

BLUE, RED, ORANGE = "#0000FF", "#E00000", "#FF8000"
LEGEND = ("Colours: blue = written or rebuilt by the script, never checked; red = a notehead Audiveris read with low "
          "confidence; orange = a staff in a bar whose rhythm does not add up (something is missing or too long).")


def plan(root, stacks, omr_path, offset=0, low=0.6, sheets=None):
    sheets = sheets or load_sheets(omr_path)
    loc = Locator(root, stacks, offset)
    out = []
    for number, sh in sheets.items():
        for cid, hids in sh.members.items():
            heads = sh.chord_heads(hids[0])
            if all(float(h.get("grade", "1")) >= low for h in heads):
                continue
            found = loc.notes_of_chord_id(sh, number, cid, heads) if cid in sh.voiced else None
            if not found:
                continue
            notes = found[2]
            for h, n in zip(heads, notes):
                if float(h.get("grade", "1")) < low:
                    out.append((n, RED))
    return out


def apply(items):
    for el, colour in items:
        if el.get("color") is None:
            el.set("color", colour)


def mark_irregular(root):
    """orange for the notes of every staff whose longest voice is not as long as the bar; returns how many staves"""
    count = 0
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
                continue
            expected = beats * 4 * div // bt
            cursor, ends = 0, {}
            for e in m:
                if e.tag == "backup":
                    cursor -= int(e.findtext("duration"))
                elif e.tag == "forward":
                    cursor += int(e.findtext("duration"))
                elif e.tag == "note" and e.find("chord") is None and e.find("grace") is None \
                        and e.find("duration") is not None:
                    cursor += int(e.findtext("duration"))
                    key = (e.findtext("staff") or "1")
                    ends[key] = max(ends.get(key, 0), cursor)
            for staff, end in ends.items():
                if end != expected:
                    count += 1
                    for e in m.findall("note"):
                        if (e.findtext("staff") or "1") == staff and e.find("rest") is None and e.get("color") is None:
                            e.set("color", ORANGE)
    return count
