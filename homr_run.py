"""Sheet music (PDF / picture / folder of pictures) -> one MusicXML file with homr, with doubtful notes marked.

Run through convert_homr.ps1, which sets up the Python 3.12 environment homr needs (this file must run inside it).

homr (https://github.com/liebharc/homr) reads every staff with a transformer that picks, note by note, the most likely
symbol.  homr throws the model's certainty away; this script keeps it.  For every symbol the transformer chose, the
probability it gave to its choice (the lowest of the rhythm, pitch and accidental choices) is attached to the note that
symbol becomes, and the notes that deserve a second look are marked, so a manual proofread can start where the doubt is:

  red     the model was unsure about this note (certainty below --low, default 0.7)
  orange  every note of a staff in a bar whose rhythm does not add up (something is missing, doubled or of the wrong
          length; it need not be the coloured note)

Output (in the output folder):  <name>.musicxml, report.txt, proofread/index.html (every marked bar with a picture of
its system), work/ (the page pictures and the per-page files homr wrote).  Bar numbers are those of the merged file.
"""
import argparse
import html
import json
import math
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from homr_merge import merge  # noqa: E402

BLUE, RED, ORANGE = "#0000FF", "#E00000", "#FF8000"
LEGEND = ("Colours: blue = added or moved by the script (a whole-bar rest in an empty staff, notes moved from the lower to the upper staff); red = homr was unsure about this note; orange = a staff in a bar whose rhythm does not add up "
          "(something is missing or too long). Clefs, key and time signatures are not marked.")
# the readings: (scale, fingering numbers erased): the page as rendered, 1.5 times larger, 0.75 times smaller, then the
# page 1.5 times larger and as rendered with the fingering numbers left. A bar the others get wrong is sometimes right there:
# whether a tenuto on a stem tip is read as a flag depends on the scale and on the numbers around it.
VARIANTS = ((1.0, True), (1.5, True), (0.75, True), (1.5, False), (1.0, False))
IMAGES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
STEP_NAME = {"C": "C", "D": "D", "E": "E", "F": "F", "G": "G", "A": "A", "B": "B"}


# ---------------------------------------------------------------- homr hooks (certainty of every note)
class Certainty(float):
    """The certainty of a note (the lowest of the model's choices of rhythm, pitch and accidental) with, as .art, its
    certainty of the articulation or ornament.  It is a float, so everything that compares or averages certainties works."""
    art = None


LAST = {"measures": None, "systems": [], "image": None}


def install_hooks():
    import homr.main as hm
    import homr.music_xml_generator as mg
    import homr.staff_parsing as sp
    import homr.transformer.decoder_inference as di
    import musicxml.xmlelement.xmlelement as mxl

    def top_probability(logits):
        a = np.asarray(logits, dtype=np.float64)
        a = a - a.max()
        e = np.exp(a)
        return float(e.max() / e.sum())

    class Net:
        """Wraps the decoder's onnx session and records how sure the model was at every step."""

        def __init__(self, net, steps):
            self._net, self._steps = net, steps

        def run_with_iobinding(self, iobinding):
            self._net.run_with_iobinding(iobinding=iobinding)
            out = iobinding.get_outputs()
            self._steps.append(tuple(top_probability(out[i].numpy()[0, -1, :]) for i in (0, 1, 2, 4)))

        def __getattr__(self, name):
            return getattr(self._net, name)

    generate = di.ScoreDecoder.generate

    def generate_with_certainty(self, *args, **kwargs):
        steps, real = [], self.net
        self.net = Net(real, steps)
        try:
            symbols = generate(self, *args, **kwargs)
        finally:
            self.net = real
        for symbol, step in zip(symbols, steps):
            symbol.conf = min(step[:3])
            symbol.art = step[3]  # how sure the model was of the articulation / ornament it chose
        return symbols

    di.ScoreDecoder.generate = generate_with_certainty

    import homr.transformer.vocabulary as vocabulary

    def keep_unisons(chord):
        """homr's own clean-up keeps one note per pitch of a chord.  That throws away a note held under a shorter one on
        the same line: a whole note with an eighth on the same pitch (two voices in unison, a stacked pair of heads that
        share no stem).  A whole note is the only length treated like that; every other pair of notes
        of one pitch is a duplicate of the model (it also writes the next chord's notes into the previous one) and
        is cleaned up as before: the longer one stays."""
        if len(chord) <= 1 or not chord[0].rhythm.startswith(("note", "rest")):
            return chord
        seen, order = {}, []
        for symbol in chord:
            length = symbol.get_duration().fraction
            key = (symbol.pitch, symbol.position)
            if key in seen:
                first = seen[key][0]
                other = first.get_duration().fraction
                if symbol.pitch not in (vocabulary.empty, vocabulary.nonote) and max(length, other) >= 1 > min(length, other):
                    seen[key].append(symbol)
                elif length > other:
                    seen[key] = [symbol]
            else:
                seen[key] = [symbol]
                order.append(key)
        return [n for k in order for n in seen[k]]

    vocabulary._remove_duplicated_piches = keep_unisons

    build_note = mg.build_note_or_rest

    def build_note_with_certainty(model_note, *args, **kwargs):
        note = build_note(model_note, *args, **kwargs)
        certainty = getattr(model_note, "conf", None)
        if certainty is not None:
            certainty = Certainty(certainty)
            certainty.art = getattr(model_note, "art", None)
        note._conf = certainty
        return note

    mg.build_note_or_rest = build_note_with_certainty

    generate_xml = mg.generate_xml

    def generate_xml_recording(*args, **kwargs):
        root = generate_xml(*args, **kwargs)
        measures = []
        for part in root.get_children_of_type(mxl.XMLPart):
            for measure in part.get_children_of_type(mxl.XMLMeasure):
                notes = [c for c in measure.get_children() if isinstance(c, mxl.XMLNote)]
                measures.append({"new_system": bool(measure.get_children_of_type(mxl.XMLPrint)),
                                 "conf": [getattr(n, "_conf", None) for n in notes]})
        LAST["measures"] = measures
        return root

    hm.generate_xml = generate_xml_recording

    parse_staff_image = sp.parse_staff_image

    def parse_staff_image_recording(debug, index, staff, image, regions, config):
        result = parse_staff_image(debug, index, staff, image, regions, config)
        if len(result) > 0:  # an empty staff is skipped by homr and is not a system of the output
            LAST["systems"].append((staff.min_x, staff.max_x, staff.min_y, staff.max_y, staff.average_unit_size))
            LAST["image"] = image
        return result

    sp.parse_staff_image = parse_staff_image_recording


def run_homr(page_path):
    """Run homr on one picture; returns (xml path, per-measure records, [system crop images])."""
    import homr.main as hm
    from homr.onnx_providers import coreml_available, cuda_available
    from homr.music_xml_generator import XmlGeneratorArguments

    gpu = cuda_available()
    config = hm.ProcessingConfig(False, False, False, False, -1, gpu, gpu or coreml_available(), False)
    LAST.update(measures=None, systems=[], image=None)
    hm.process_image(str(page_path), config, XmlGeneratorArguments(False, None, None))
    crops = []
    for x0, x1, y0, y1, unit in LAST["systems"]:
        img = LAST["image"]
        pad = int(3 * unit)
        crops.append(img[max(0, int(y0) - pad):int(y1) + pad, max(0, int(x0) - pad):int(x1) + pad].copy())
    return Path(page_path).with_suffix(".musicxml"), LAST["measures"], crops


# ---------------------------------------------------------------- fingering numbers
def erase_fingerings(path):
    """Erase the fingering numbers from a page picture, in place (the detector of convert.ps1's Audiveris preparation:
    omr_tools.clean_page).  homr takes them for ornaments (turns, trills, breath marks, arpeggios).  Returns how many."""
    from omr_tools import clean_page
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    out, _, digits, _, _ = clean_page(gray, False, True)
    if digits:
        cv2.imwrite(str(path), out)
    return digits


# ---------------------------------------------------------------- tenuto marks
def staff_unit(binary):
    """Staff-line spacing in pixels of a page (black ink = 255), from the long horizontal lines."""
    h, w = binary.shape
    lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 12), 1)))
    rows = np.where(lines.sum(axis=1) > 0)[0]
    if len(rows) == 0:
        return None, lines
    centres, start = [], rows[0]
    for a_, b_ in zip(rows[:-1], rows[1:]):
        if b_ != a_ + 1:
            centres.append((start + a_) / 2)
            start = b_
    centres.append((start + rows[-1]) / 2)
    gaps = np.round(np.diff(centres))
    gaps = gaps[(gaps >= 5) & (gaps <= 60)]
    if len(gaps) == 0:
        return None, lines
    values, counts = np.unique(gaps, return_counts=True)  # the spacing of the staff lines is the most common gap
    return float(values[np.argmax(counts)]), lines


def erase_tenutos(path):
    """Erase tenuto marks (short thick dashes over or under a note head) from a page picture, in place.

    homr takes them for beams (they have the thickness of one) and then reads the notes next to them as beamed eighths:
    a bar of four quarter-note chords comes out as a half note and eighths.  A tenuto is a component of the ink, once the
    staff lines are taken out, that is a small isolated rectangle about a staff space wide and a third of a staff space thick, with
    no other ink within a few pixels.  Ledger lines and staff-line leftovers are thinner, rests thicker, and the pieces of
    a beam that the removal of a staff line cut off have neighbours.  Returns how many were erased."""
    gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    binary = (gray < 140).astype(np.uint8) * 255
    unit, lines = staff_unit(binary)
    if not unit:
        return 0
    lines = cv2.dilate(lines, np.ones((3, 1), np.uint8))
    residue = cv2.bitwise_and(binary, cv2.bitwise_not(lines))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(residue, connectivity=8)
    erased = 0
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if not (0.8 * unit <= w <= 2.0 * unit and 0.3 * unit <= h <= 0.45 * unit and w >= 3 * h and area >= 0.75 * w * h):
            continue
        # the pieces of a beam that the removal of a staff line cut off have a stem or another piece of the beam a few pixels
        # away; a fingering digit near a tenuto is no reason to keep it
        x0, y0 = max(0, x - 6), max(0, y - 6)
        around = labels[y0:y + h + 6, x0:x + w + 6]
        if any(stats[j][2] >= 1.2 * unit or stats[j][3] >= 2 * unit for j in set(np.unique(around)) - {0, i}):
            continue
        gray[labels == i] = 255
        erased += 1
    if erased:
        cv2.imwrite(str(path), gray)
    return erased


# ---------------------------------------------------------------- input pages
def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def collect_pages(source, work, dpi, wanted, scale=1.0, tag_extra=""):
    """Page pictures in name order (a PDF is rendered at dpi * scale, a picture is resized by scale); returns [path]."""
    source = Path(source)
    work.mkdir(parents=True, exist_ok=True)
    tag = ("" if scale == 1 else f"_x{round(scale * 100)}") + tag_extra
    pages = []
    if source.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(source) as pdf:
            for i, page in enumerate(pdf, 1):
                out = work / f"p{i:02d}{tag}.png"
                page.get_pixmap(dpi=round(dpi * scale)).save(out)
                pages.append(out)
    else:
        files = [source] if source.is_file() else sorted(
            (f for f in source.iterdir() if f.suffix.lower() in IMAGES), key=lambda f: natural_key(f.name))
        for i, f in enumerate(files, 1):
            out = work / f"p{i:02d}{tag}{f.suffix.lower()}"
            if scale == 1:
                shutil.copyfile(f, out)
            else:
                img = cv2.imread(str(f))
                cv2.imwrite(str(out), cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA))
            pages.append(out)
    if wanted:
        pages = [pages[n - 1] for n in wanted if 1 <= n <= len(pages)]
    return pages


# ---------------------------------------------------------------- analysis of the merged score
def note_name(note):
    p = note.find("pitch")
    if p is None:
        return "rest"
    alter = {"1": "#", "-1": "b", "2": "x", "-2": "bb"}.get(p.findtext("alter"), "")
    return f"{p.findtext('step')}{alter}{p.findtext('octave')}"


def measure_state(m, divisions, beats):
    """Divisions and time signature in force after the attributes of this bar."""
    for a in m.findall("attributes"):
        if a.find("divisions") is not None:
            divisions = int(a.findtext("divisions"))
        t = a.find("time")
        if t is not None:
            beats = (int(t.findtext("beats")), int(t.findtext("beat-type")))
    return divisions, beats


def owner(e):
    """The staff a note belongs to.  homr gives staff 1 the voices 1-4 and staff 2 the voices 5-8, so a note of a voice of
    the upper staff that is written on the lower staff (cross-staff) still counts for the upper staff."""
    return "1" if int(e.findtext("voice", "1")) <= 4 else "2"


def rhythm_problems(m, divisions, beats, staves=1):
    """[(staff, beats found, beats wanted, why)] for every staff of the bar that is wrong, why being "sum" or "timing".
    sum:    the voices of the staff do not fill the bar (some voice must fill it exactly, none may be longer); a staff
            with nothing in it counts (a bar needs a rest at least)
    timing: seen from the order of the notes in the file a voice has a gap or an overlap, or runs past the end of the
            bar (the two hands are out of step: homr's backup steps between the staves do not match), which MuseScore
            shows as a bar that is too long or has holes although the lengths add up."""
    full = beats[0] * 4 * divisions // beats[1]
    totals, end, cursor, last, timing = {}, {}, 0, 0, set()
    for e in m:
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward" or (e.tag == "note" and e.find("grace") is None):
            dur = int(e.findtext("duration") or 0)
            if e.tag == "note" and e.find("chord") is not None:
                continue
            onset, cursor = cursor, cursor + dur
            key = (owner(e), e.findtext("voice", "1"))
            totals[key] = totals.get(key, 0) + dur
            if key in end and end[key] != onset:
                timing.add(key[0])
            end[key] = onset + dur
    for key, x in end.items():
        if x > full:
            timing.add(key[0])
    wrong = []
    for staff in sorted({k[0] for k in totals} | {str(i) for i in range(1, staves + 1)}):
        times = [v for k, v in totals.items() if k[0] == staff] or [0]
        if not (any(v == full for v in times) and all(v <= full for v in times)):
            wrong.append((staff, max(times) / divisions, full / divisions, "sum"))
        elif staff in timing:
            wrong.append((staff, max(times) / divisions, full / divisions, "timing"))
    return wrong


def analyse(root, records, low):
    """Colour the doubtful notes (in place) and return one dict per measure with what is doubtful in it."""
    measures = root.find("part").findall("measure")
    divisions, beats = 1, (4, 4)
    staves = int(root.findtext("part/measure/attributes/staves") or 1)
    out = []
    for m, rec in zip(measures, records):
        divisions, beats = measure_state(m, divisions, beats)
        notes = m.findall("note")
        conf = rec["conf"]
        cursor, last_onset = 0, 0
        doubtful = []
        for e in m:
            if e.tag == "backup":
                cursor -= int(e.findtext("duration"))
            elif e.tag == "forward":
                cursor += int(e.findtext("duration"))
            elif e.tag == "note":
                dur = int(e.findtext("duration") or 0)
                grace = e.find("grace") is not None
                if e.find("chord") is None and not grace:
                    last_onset = cursor
                    cursor += dur
                onset = last_onset if e.find("chord") is not None else cursor - dur if not grace else cursor
                idx = notes.index(e)
                c = conf[idx] if idx < len(conf) else None
                if c is not None and c < low:
                    doubtful.append({"staff": int(owner(e)), "name": note_name(e), "conf": c,
                                     "beat": onset / divisions + 1, "element": e})
        wrong_staves = rhythm_problems(m, divisions, beats, staves)
        for staff, _, _, _ in wrong_staves:
            for n in notes:
                if n.find("rest") is None and owner(n) == staff and n.get("color") is None:
                    n.set("color", ORANGE)
        for d in doubtful:
            d["element"].set("color", RED)
        out.append({"doubtful": doubtful, "wrong": wrong_staves, "new_system": rec["new_system"]})
    return out


def _attributes_key(m):
    """What the bar says about clefs, key and time (not divisions), to tell whether another reading can replace it."""
    out = []
    for a in m.findall("attributes"):
        for child in a:
            if child.tag not in ("divisions", "staves", "part-symbol"):
                out.append(ET.tostring(child, encoding="unicode"))
    return out


def combine_readings(readings, beats, labels):
    """readings: [(page root, per-bar records)], the same page read at different scales.

    The reading with the most bars whose rhythm adds up is the base.  Every bar of it that does not add up is replaced by
    the same bar of another reading where it does (that reading must have the same number of bars and divisions and say
    the same about clefs and keys; the most certain such bar wins).  Returns (root, records, beats, [(bar, label)], index of the base reading)."""
    parsed = []
    for root, records in readings:
        part = root.find("part")
        measures = part.findall("measure")
        divisions, state, bad = 1, beats, []
        staves = int(root.findtext("part/measure/attributes/staves") or 1)
        for m in measures:
            divisions, state = measure_state(m, divisions, state)
            bad.append(bool(rhythm_problems(m, divisions, state, staves)))
        first = root.find("part/measure/attributes/divisions")
        parsed.append({"root": root, "records": records, "measures": measures, "bad": bad, "state": state,
                       "div": int(first.text) if first is not None else 1})
    base_index = min(range(len(parsed)), key=lambda i: (sum(parsed[i]["bad"]), i))
    base = parsed[base_index]
    swapped = []
    part = base["root"].find("part")
    for i, is_bad in enumerate(base["bad"]):
        if not is_bad:
            continue
        options = []
        for k, other in enumerate(parsed):
            if k == base_index or len(other["measures"]) != len(base["measures"]) or other["div"] != base["div"]:
                continue
            if other["bad"][i] or _attributes_key(other["measures"][i]) != _attributes_key(base["measures"][i]):
                continue
            confs = [c for c in other["records"][i]["conf"] if c is not None]
            options.append((sum(confs) / len(confs) if confs else 0, k))
        if not options:
            continue
        k = max(options)[1]
        new = other_measure = parsed[k]["measures"][i]
        old = base["measures"][i]
        for pr in other_measure.findall("print"):
            other_measure.remove(pr)
        for pr in old.findall("print"):
            other_measure.insert(0, pr)
        new.set("number", old.get("number"))
        part[list(part).index(old)] = new
        base["records"][i] = dict(parsed[k]["records"][i], new_system=base["records"][i]["new_system"])
        swapped.append((i + 1, labels[k]))
    return base["root"], base["records"], base["state"], swapped, base_index


def _midi(note):
    p = note.find("pitch")
    if p is None:
        return None
    step = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[p.findtext("step")]
    return 12 * (int(p.findtext("octave")) + 1) + step + int(float(p.findtext("alter") or 0))


def _octave_chord(group):
    """The top note of a group of notes that is an octave chord (two or more pitched notes of one voice, the highest an
    octave above another), else None."""
    pitched = [n for n in group if n.find("rest") is None]
    if len(pitched) != len(group) or len(pitched) < 2 or len({n.findtext("voice") for n in group}) != 1:
        return None
    top = max(pitched, key=_midi)
    return top if (_midi(top) - 12) in {_midi(n) for n in pitched} else None


def _set_stem(note, direction):
    """Give a note an explicit stem direction (MusicXML puts <stem> after the type, the dots and the time modification)."""
    for e in note.findall("stem"):
        note.remove(e)
    stem = ET.Element("stem")
    stem.text = direction
    after = max((i for i, e in enumerate(note) if e.tag in ("type", "dot", "accidental", "time-modification")), default=0)
    note.insert(after + 1, stem)


def separate_cross_staff_stems(m):
    """The right hand's notes written on the lower staff get stems up, so their beam is above them, and the left hand's notes
    of that staff get stems down with their beam below, as on the page: otherwise both sets of stems and beams are drawn
    through each other.  Only in a bar that has such cross-staff notes."""
    crossing = [e for e in m.findall("note") if e.findtext("staff") == "2" and owner(e) == "1"]
    if not crossing:
        return
    for e in m.findall("note"):
        if e.findtext("staff") != "2" or e.find("rest") is not None or e.findtext("type") == "whole":
            continue
        _set_stem(e, "up" if owner(e) == "1" else "down")


def move_cross_staff_notes(m, record, full):
    """The right hand sometimes plays notes written on the lower staff: a beat at the start of the bar, or the whole bar,
    that doubles the left hand an octave higher.  homr puts them into the lower staff as the upper notes of octave chords,
    so the upper staff is short (or empty) and its notes start late.  The top notes of those chords are given to the
    upper staff's voice (blue) but stay written on the lower staff, as on the page (MusicXML cross-staff notes).
      * a run of octave chords of the lower staff from the start of the bar to exactly where the first note of the upper
        staff begins;
      * or, when the upper staff has nothing, octave chords and rests all through the bar (a rest becomes a gap of the
        upper voice).
    Returns how many notes were moved (the conf list of the record is kept in step)."""
    cursor, last, events, first_upper = 0, 0, {}, None
    for e in m:
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
        elif e.tag == "note":
            dur = int(e.findtext("duration") or 0)
            if e.find("chord") is None and e.find("grace") is None:
                last = cursor
                cursor += dur
            if e.find("grace") is not None:
                continue
            if owner(e) == "2":
                events.setdefault(last, []).append(e)
            elif first_upper is None or last < first_upper:
                first_upper = last
    if first_upper == 0:
        return 0
    limit = full if first_upper is None else first_upper
    chain, at = [], 0
    while at < limit:
        group = events.get(at)
        if not group:
            return 0
        top = _octave_chord(group)
        if top is not None:
            chain.append((group, top))
            at += int(top.findtext("duration"))
        elif first_upper is None and len(group) == 1 and group[0].find("rest") is not None:
            chain.append((group, None))
            at += int(group[0].findtext("duration"))
        else:
            return 0
    if at != limit or not any(top is not None for _, top in chain):
        return 0
    notes = m.findall("note")
    conf = dict(zip(map(id, notes), record["conf"]))
    insert_at = next(i for i, e in enumerate(m) if e.tag == "note")
    new_elements, moved = [], 0
    for group, top in chain:
        if top is None:  # a rest of both hands: the upper voice just waits
            gap = ET.Element("forward")
            ET.SubElement(gap, "duration").text = group[0].findtext("duration")
            ET.SubElement(gap, "voice").text = "1"
            new_elements.append(gap)
            continue
        if top.find("chord") is None:  # the next note of the chord becomes its first
            following = group[group.index(top) + 1]
            for c in following.findall("chord"):
                following.remove(c)
        m.remove(top)
        for c in top.findall("chord"):
            top.remove(c)
        top.find("voice").text = "1"  # the voice of the upper staff, still written on the lower staff (cross-staff)
        top.set("color", BLUE)
        new_elements.append(top)
        moved += 1
    back = ET.Element("backup")
    ET.SubElement(back, "duration").text = str(at)
    new_elements.append(back)
    for k, el in enumerate(new_elements):
        m.insert(insert_at + k, el)
    record["conf"] = [conf.get(id(n)) for n in m.findall("note")]
    record["moved"] = moved
    separate_cross_staff_stems(m)
    return moved


def repair_lone_sixteenth(m, divisions, beats, record):
    """A voice that is short by exactly the difference between an eighth and a sixteenth and holds exactly one sixteenth:
    the sixteenth is an eighth (homr sees the second beam stub of two stacked beam groups).  The note (and the notes of its
    chord) become eighths (blue); the backup that rewinds that voice is lengthened to match.  Returns how many."""
    full = beats[0] * 4 * divisions // beats[1]
    delta = divisions // 4
    if delta < 1:
        return 0
    totals, sixteenths = {}, {}
    for e in m:
        if e.tag == "note" and e.find("chord") is None and e.find("grace") is None:
            key = (owner(e), e.findtext("voice", "1"))
            totals[key] = totals.get(key, 0) + int(e.findtext("duration") or 0)
            if e.findtext("type") == "16th" and e.find("dot") is None and e.find("time-modification") is None:
                sixteenths.setdefault(key, []).append(e)
    fixed = 0
    for key, total in totals.items():
        if total != full - delta or len(sixteenths.get(key, [])) != 1:
            continue
        children = list(m)
        note = sixteenths[key][0]
        index = children.index(note)
        start = index
        while start > 0 and children[start - 1].tag != "backup":
            start -= 1
        end = next((j for j in range(index, len(children)) if children[j].tag == "backup"), None)
        chain = sum(int(e.findtext("duration") or 0) for e in children[start:end]
                    if e.tag in ("note", "forward") and e.find("chord") is None and e.find("grace") is None)
        members = [note]
        k = index + 1
        while k < len(children) and children[k].tag == "note" and children[k].find("chord") is not None:
            members.append(children[k])
            k += 1
        for n in members:
            n.find("type").text = "eighth"
            n.find("duration").text = str(int(n.find("duration").text) + delta)
            n.set("color", BLUE)
        if end is not None and int(children[end].findtext("duration")) == chain:
            children[end].find("duration").text = str(chain + delta)
        fixed += 1
    if fixed:
        record["lengthened"] = record.get("lengthened", 0) + fixed
    return fixed


def _voice_events(m):
    """The notes of a bar per voice, as {(staff, voice): [event]}, an event being
    {"onset", "dur", "notes": [note elements of one chord], "rest": bool}.  The first event of a voice starts where the
    file's backup steps put it, every other event follows the one before (inside a voice the file is contiguous where the
    bar is valid; where it is not, the order of the voice is still right).  None when the bar has something this does not
    handle (grace notes).  Under the key None: the clef or key changes inside the bar, [(onset, attributes element)]."""
    events, cursor, seen_note, changes = {}, 0, False, []
    for e in m:
        if e.tag == "attributes" and seen_note:
            changes.append((cursor, e))
            continue
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag in ("forward", "note"):
            if e.tag == "note":
                seen_note = True
                if e.find("grace") is not None:
                    return None
            dur = int(e.findtext("duration") or 0)
            if e.tag == "note" and e.find("chord") is not None:
                key = (owner(e), e.findtext("voice", "1"))
                events[key][-1]["notes"].append(e)
                continue
            key = (owner(e), e.findtext("voice", "1"))
            voice = events.setdefault(key, [])
            onset = voice[-1]["onset"] + voice[-1]["dur"] if voice else cursor
            voice.append({"onset": onset, "dur": dur, "notes": [e] if e.tag == "note" else [],
                          "rest": e.tag == "forward" or e.find("rest") is not None})
            cursor += dur
    events[None] = changes  # clef and key changes inside the bar: (onset, attributes element)
    return events


def merge_voices(m, record, full):
    """One voice per staff, as the default.  homr writes a note held while others move (a half note under quarter notes, a
    whole note under eighths) as a second voice, and the two voices then fall out of step.  When a staff has several
    voices and one of them fills the bar, the notes of the others that start together with a note of that one become members
    of its chords, as long as that note (a single voice cannot hold them longer); their rests are dropped.  A staff whose
    other voices do not line up with the main one is left alone.  The bar is written out again voice by voice with backup
    steps, so the timing no longer depends on homr's interleaving of the staves.  Notes changed are blue.  Returns the number
    of staves merged."""
    events = _voice_events(m)
    if not events:
        return 0
    changes = events.pop(None)
    merged = 0
    for staff in sorted({k[0] for k in events}):
        keys = [k for k in events if k[0] == staff]
        if len(keys) < 2:
            continue
        total = {k: sum(ev["dur"] for ev in events[k]) for k in keys}
        base_key = max(keys, key=lambda k: (total[k], sum(1 for ev in events[k] if not ev["rest"])))
        if total[base_key] != full:
            continue
        base = {ev["onset"]: ev for ev in events[base_key]}
        plan = []  # (note, base event) for every note to be added to a chord
        ok = True
        for k in keys:
            if k == base_key:
                continue
            for ev in events[k]:
                if ev["rest"]:
                    continue
                target = base.get(ev["onset"])
                if target is None or target["rest"] or ev["dur"] < target["dur"]:
                    ok = False
                    break
                plan += [(n, target) for n in ev["notes"]]
            if not ok:
                break
        if not ok:
            continue
        for note, target in plan:
            if any(_midi(note) == _midi(t) for t in target["notes"]):
                continue  # the same pitch twice (a unison): one note
            first = target["notes"][0]
            note.find("duration").text = first.findtext("duration")
            note.find("type").text = first.findtext("type")
            note.find("voice").text = first.findtext("voice")
            for tag in ("dot", "tie", "time-modification"):
                for t in note.findall(tag):
                    note.remove(t)
            for n in note.findall("notations"):
                for t in n.findall("tied"):
                    n.remove(t)
            at = list(note).index(note.find("duration")) + 1
            for _ in first.findall("dot"):
                note.insert(at, ET.Element("dot"))
            if note.find("chord") is None:
                note.insert(0, ET.Element("chord"))
            note.set("color", BLUE)
            target["notes"].append(note)
        for k in keys:
            if k != base_key:
                del events[k]
        merged += 1
    if not merged:
        return 0
    _write_voices(m, events, changes, record)
    record["merged"] = merged
    return merged


def _write_voices(m, events, changes, record):
    """Write the bar out again voice by voice: the leading elements (attributes, print), then every voice in order with a
    forward where it starts late and a backup to the start of the bar before the next voice, then the clef and key changes
    inside the bar in front of the first note of their staff at or after their time, then the closing elements (barline).
    The timing of a voice is then its own contiguous run of notes, whatever the backup steps of the file were."""
    notes = m.findall("note")
    conf = dict(zip(map(id, notes), record["conf"]))
    late = {id(e) for _, e in changes}
    head = [e for e in m if e.tag in ("attributes", "print", "direction") and id(e) not in late]
    tail = [e for e in m if e.tag not in ("attributes", "print", "direction", "note", "backup", "forward")]
    for e in list(m):
        m.remove(e)
    for e in head:
        m.append(e)
    order = sorted(events, key=lambda k: (int(k[0]), int(k[1])))
    placed = []
    for i, key in enumerate(order):
        voice = events[key]
        if voice and voice[0]["onset"] > 0:
            gap = ET.SubElement(m, "forward")
            ET.SubElement(gap, "duration").text = str(voice[0]["onset"])
            ET.SubElement(gap, "voice").text = key[1]
        first_of_staff = key == min((k for k in order if k[0] == key[0]), key=lambda k: int(k[1]))
        for ev in voice:
            if first_of_staff:
                for at, change in [c for c in changes if c[1].find("clef") is None
                                   or c[1].find("clef").get("number", "1") == key[0]]:
                    if at <= ev["onset"] and change not in placed:
                        m.append(change)
                        placed.append(change)
            for n in ev["notes"]:
                m.append(n)
        if i < len(order) - 1 and voice:
            back = ET.SubElement(m, "backup")
            ET.SubElement(back, "duration").text = str(voice[-1]["onset"] + voice[-1]["dur"])
    for _, change in changes:
        if change not in placed:
            m.append(change)
    for e in tail:
        m.append(e)
    record["conf"] = [conf.get(id(n)) for n in m.findall("note")]


def repair_timing(m, divisions, beats, staves, record):
    """A bar whose lengths add up but whose two hands are out of step (a gap, an overlap, or running past the end of the
    bar, which MuseScore shows as a bar of 7/4) is written out again voice by voice (_write_voices): each voice is then
    its own contiguous run of notes starting where the file puts its first note.  Returns 1 when the bar was rewritten."""
    problems = rhythm_problems(m, divisions, beats, staves)
    if not problems or any(why == "sum" for *_, why in problems):
        return 0
    events = _voice_events(m)
    if not events:
        return 0
    changes = events.pop(None)
    _write_voices(m, events, changes, record)
    record["retimed"] = True
    return 1


ORNAMENT_TAGS = ("trill-mark", "turn", "inverted-turn", "delayed-turn", "mordent", "inverted-mordent", "shake", "schleifer",
                 "tremolo", "wavy-line", "arpeggiate", "non-arpeggiate", "breath-mark", "caesura")


def find_ornaments(root, records):
    """{bar index: [(tag, certainty of the model or None), ...]} of the ornaments, arpeggios and breath marks homr wrote.
    Left over fingering numbers are often read as these (a 5 as a trill, a stack of 5-2-1 as an arpeggio)."""
    found = {}
    for i, (m, record) in enumerate(zip(root.find("part").findall("measure"), records)):
        notes = m.findall("note")
        for note, certainty in zip(notes, record["conf"]):
            for e in note.iter():
                if e.tag in ORNAMENT_TAGS:
                    found.setdefault(i, []).append((e.tag, getattr(certainty, "art", None)))
    return found


def drop_ornaments(root, records, below=None):
    """Remove the ornaments, arpeggios and breath marks of the notes whose articulation certainty is under `below` (all of
    them when None).  Fermatas, accents, staccato and the like stay.  Returns {bar index: [tag, ...]} of what was removed."""
    removed = {}
    for i, (m, record) in enumerate(zip(root.find("part").findall("measure"), records)):
        for note, certainty in zip(m.findall("note"), record["conf"]):
            art = getattr(certainty, "art", None)
            if below is not None and (art is None or art >= below):
                continue
            for parent in list(note.iter()):
                for child in list(parent):
                    if child.tag in ORNAMENT_TAGS:
                        parent.remove(child)
                        removed.setdefault(i, []).append(child.tag)
            notations = note.find("notations")
            if notations is not None:
                for group in list(notations):
                    if group.tag in ("ornaments", "articulations", "technical") and len(group) == 0:
                        notations.remove(group)
                if len(notations) == 0:
                    note.remove(notations)
    return removed


def fill_empty_staves(root, beats):
    """Put a whole-bar rest (blue) into every staff that has nothing in a bar: homr leaves out the rest of a bar
    that is silent in one hand, and a bar with an empty staff has no length for MuseScore.  Returns [(bar index, staff)];
    bars are counted from 0 within this root."""
    part = root.find("part")
    divisions, staves, added = 1, int(root.findtext("part/measure/attributes/staves") or 1), []
    for index, m in enumerate(part.findall("measure")):
        divisions, beats = measure_state(m, divisions, beats)
        used = {owner(e) for e in m if e.tag == "note"}
        full = beats[0] * 4 * divisions // beats[1]
        cursor = 0
        for e in m:
            if e.tag in ("note", "forward") and e.find("chord") is None and e.find("grace") is None:
                cursor += int(e.findtext("duration") or 0)
            elif e.tag == "backup":
                cursor -= int(e.findtext("duration"))
        for staff in range(1, staves + 1):
            if str(staff) in used:
                continue
            backup = ET.SubElement(m, "backup")
            ET.SubElement(backup, "duration").text = str(cursor)
            rest = ET.SubElement(m, "note", {"color": BLUE})
            ET.SubElement(rest, "rest", {"measure": "yes"})
            ET.SubElement(rest, "duration").text = str(full)
            ET.SubElement(rest, "voice").text = "1" if staff == 1 else "5"
            ET.SubElement(rest, "type").text = "whole"
            ET.SubElement(rest, "staff").text = str(staff)
            cursor = full
            added.append((index, staff))
    return added


def describe(info):
    parts = []
    for staff, got, want, why in info["wrong"]:
        parts.append(f"rhythm of staff {staff} does not add up ({got:g} of {want:g} beats)" if why == "sum" else
                     f"staff {staff}: the notes of the two hands are out of step (gap, overlap or past the end of the bar)")
    if info["doubtful"]:
        notes = ", ".join(f"{d['name']} (staff {d['staff']}, beat {d['beat']:g}, certainty {d['conf']:.2f})"
                          for d in info["doubtful"])
        parts.append(f"{len(info['doubtful'])} unsure note(s): {notes}")
    return parts


# ---------------------------------------------------------------- output
def write_outputs(out_dir, name, root, infos, page_of_measure, system_of_measure, crops_by_page, low, swaps=(), rests=(),
                  records=(), ornaments=None, dropped=None):
    flagged = [(i, info) for i, info in enumerate(infos) if info["wrong"] or info["doubtful"]]
    total = len(infos)
    lines = [f"{name}: {total} bars, {len(flagged)} marked ({low:g} = certainty below which a note is marked red).",
             LEGEND, ""]
    if swaps:
        lines.append("bars whose rhythm did not add up in the first reading and were taken from another reading: "
                     + ", ".join(f"{i + 1} ({label})" for i, label in swaps))
        lines.append("")
    moved = [(i, r["moved"]) for i, r in enumerate(records) if r.get("moved")]
    if moved:
        lines.append("notes the right hand plays on the lower staff, now part of the upper staff but still written on the lower one (blue): "
                     + ", ".join(f"bar {i + 1} ({n} note{'s' if n > 1 else ''})" for i, n in moved))
        lines.append("")
    lengthened = [i for i, r in enumerate(records) if r.get("lengthened")]
    if lengthened:
        lines.append("a lone sixteenth in a bar that was short by exactly that much was made an eighth (blue): "
                     + ", ".join(f"bar {i + 1}" for i in lengthened))
        lines.append("")
    if dropped:
        lines.append("ornaments, arpeggios and breath marks homr read without being sure (a left over fingering number or a "
                     "smudge) and that were removed: "
                     + ", ".join(f"bar {i + 1} ({', '.join(sorted(set(t)))})" for i, t in sorted(dropped.items())))
        lines.append("")
    if ornaments:
        lines.append("ornaments, arpeggios and breath marks homr read and was sure of, kept (the certainty is the model's; "
                     "--drop-ornaments removes every one): "
                     + ", ".join(f"bar {i + 1} ({', '.join(sorted({f'{tag} {art:.2f}' if art is not None else tag for tag, art in t}))})"
                                 for i, t in sorted(ornaments.items())))
        lines.append("")
    retimed = [i for i, r in enumerate(records) if r.get("retimed")]
    if retimed:
        lines.append("bars whose hands were out of step although the lengths add up, written out again voice by voice: "
                     + ", ".join(f"bar {i + 1}" for i in retimed))
        lines.append("")
    merged = [i for i, r in enumerate(records) if r.get("merged")]
    if merged:
        lines.append("staves written with one voice (a note held longer than the notes it starts with is only as long as "
                     "they are; the changed notes are blue): " + ", ".join(f"bar {i + 1}" for i in merged))
        lines.append("")
    if rests:
        lines.append("empty staff, a whole-bar rest was added (blue): "
                     + ", ".join(f"bar {i + 1} staff {staff}" for i, staff in rests))
        lines.append("")
    for i, info in flagged:
        lines.append(f"bar {i + 1} (page {page_of_measure[i]}, system {system_of_measure[i] + 1}):")
        lines += ["    " + p for p in describe(info)]
    (out_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    proof = out_dir / "proofread"
    proof.mkdir(exist_ok=True)
    cards, seen = [], set()
    bars_in_system = {}
    for i in range(total):
        bars_in_system.setdefault((page_of_measure[i], system_of_measure[i]), []).append(i)
    for i, info in flagged:
        key = (page_of_measure[i], system_of_measure[i])
        picture = ""
        crop = crops_by_page.get(key[0], {}).get(key[1])
        if crop is not None and key not in seen:
            seen.add(key)
            fname = f"p{key[0]:02d}_system{key[1] + 1}.png"
            cv2.imwrite(str(proof / fname), crop)
            picture = f'<img src="{fname}" alt="page {key[0]}, system {key[1] + 1}">'
        elif key in seen:
            picture = f'<div class="same">(picture: page {key[0]}, system {key[1] + 1}, shown above)</div>'
        position = bars_in_system[key].index(i) + 1
        reasons = "".join(f"<li>{html.escape(p)}</li>" for p in describe(info))
        cards.append(f'<section><h2>Bar {i + 1} <small>page {key[0]}, system {key[1] + 1}, bar {position} from the '
                     f'left</small></h2>{picture}<ul>{reasons}</ul></section>')
    page = ("<!doctype html><meta charset='utf-8'><title>Proofreading list</title><style>"
            "body{font:15px system-ui;max-width:1100px;margin:1em auto;padding:0 1em}"
            "section{border-top:1px solid #ccc;padding:.5em 0}img{max-width:100%}small{color:#666;font-weight:normal}"
            ".same{color:#888}</style>"
            f"<h1>{html.escape(name)}: {len(flagged)} of {total} bars to check</h1><p>{html.escape(LEGEND)}</p>"
            + "".join(cards))
    (proof / "index.html").write_text(page, encoding="utf-8")
    ET.indent(root)
    ET.ElementTree(root).write(out_dir / f"{name}.musicxml", encoding="UTF-8", xml_declaration=True)
    return flagged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="PDF, picture, or folder of page pictures (name order)")
    ap.add_argument("--outdir")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--pages", help="page numbers to use, e.g. 1,3")
    ap.add_argument("--low", type=float, default=0.7, help="notes with certainty below this are marked red")
    ap.add_argument("--one-voice", action="store_true",
                    help="write every staff with one voice: a note held while others move becomes a member of their "
                         "chords, only as long as they are (by default homr's voices are kept)")
    ap.add_argument("--ornament-certainty", type=float, default=0.8,
                    help="ornaments, arpeggios and breath marks the model was less sure of than this are removed (0 keeps "
                         "all)")
    ap.add_argument("--drop-ornaments", action="store_true",
                    help="remove every ornament, arpeggio and breath mark homr read, however sure it was; fermatas and "
                         "articulations stay")
    ap.add_argument("--keep-fingering", action="store_true",
                    help="do not erase the fingering numbers from the pictures (homr takes them for ornaments)")
    ap.add_argument("--keep-tenuto", action="store_true",
                    help="do not erase tenuto marks from the pictures (homr takes them for beams)")
    ap.add_argument("--readings", type=int, default=4, choices=range(1, len(VARIANTS) + 1),
                    help="read every page this many times at different scales and take, for each bar whose rhythm does "
                         "not add up, the same bar from a reading where it does (1 = once, faster)")
    args = ap.parse_args()
    one_voice = args.one_voice

    source = Path(args.input).resolve()
    name = source.stem if source.is_file() else source.name
    out_dir = Path(args.outdir).resolve() if args.outdir else source.parent / (name + "_homr")
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [int(x) for x in args.pages.split(",")] if args.pages else None
    variants = VARIANTS[:args.readings]
    pages_by_scale = [collect_pages(source, out_dir / "work", args.dpi, wanted, sc, "" if erase else "_raw")
                      for sc, erase in variants]
    if not args.keep_fingering:
        erased = sum(erase_fingerings(path) for (sc, erase), pages in zip(variants, pages_by_scale) if erase
                     for path in pages) // max(1, sum(1 for _, erase in variants if erase))
        print(f"erased about {erased} fingering numbers", flush=True)
    if not args.keep_tenuto:
        erased = sum(erase_tenutos(path) for pages in pages_by_scale for path in pages) // len(variants)
        print(f"erased about {erased} tenuto marks", flush=True)
    labels = [(f"{round(args.dpi * sc)} dpi" if source.suffix.lower() == ".pdf" else f"scale {sc:g}")
              + ("" if erase else ", fingering numbers kept") for sc, erase in variants]
    scales = variants
    if not pages_by_scale[0]:
        sys.exit("No pages found.")

    install_hooks()
    from homr.main import download_weights
    from homr.onnx_providers import coreml_available, cuda_available
    gpu = cuda_available()
    download_weights(gpu or coreml_available(), gpu, False)

    page_roots, records, page_of_measure, system_of_measure = [], [], [], []
    crops_by_page, swaps, rests = {}, [], []
    beats = (4, 4)
    for n in range(len(pages_by_scale[0])):
        readings, crops_of, used = [], [], []
        for k, pages in enumerate(pages_by_scale):
            print(f"--- page {n + 1} of {len(pages_by_scale[0])}, reading {k + 1} of {len(scales)} ({labels[k]})",
                  flush=True)
            try:
                xml_path, measures, crops = run_homr(pages[n])
            except Exception as e:  # keep the other readings and pages
                print(f"homr failed on {pages[n].name}: {e}", flush=True)
                continue
            page_root = ET.parse(xml_path).getroot()
            div, state = 1, beats
            staves = int(page_root.findtext("part/measure/attributes/staves") or 1)
            for m, rec in zip(page_root.find("part").findall("measure"), measures):
                div, state = measure_state(m, div, state)
                move_cross_staff_notes(m, rec, state[0] * 4 * div // state[1])
                repair_lone_sixteenth(m, div, state, rec)
                if one_voice:
                    merge_voices(m, rec, state[0] * 4 * div // state[1])
                repair_timing(m, div, state, staves, rec)
            if not variants[k][1]:
                # the fingering numbers were left on this page: its ornaments are the numbers (the other readings, from
                # which the numbers were erased, do not have them)
                drop_ornaments(page_root, measures)
            readings.append((page_root, measures))
            crops_of.append(crops)
            used.append(labels[k])
        if not readings:
            continue
        root, measures, beats, swapped, base = combine_readings(readings, beats, used)
        for index, staff in fill_empty_staves(root, beats):
            measures[index]["conf"] = measures[index]["conf"] + [None]
            rests.append((len(records) + index, staff))
        crops = crops_of[base]
        swaps += [(len(records) + bar - 1, label) for bar, label in swapped]
        page_roots.append(root)
        records += measures
        system = -1
        for m in measures:
            system += 1 if (m["new_system"] or system < 0) else 0
            page_of_measure.append(n + 1)
            system_of_measure.append(system)
        starts = sum(1 for m in measures if m["new_system"]) + 1
        crops_by_page[n + 1] = dict(enumerate(crops)) if starts == len(crops) else {}
    if not page_roots:
        sys.exit("homr read no page.")

    root = merge(page_roots)
    measures = root.find("part").findall("measure")
    if len(measures) != len(records):
        sys.exit(f"internal error: {len(measures)} bars in the merged file but {len(records)} recorded.")
    # an ornament the model was unsure of (under --ornament-certainty) is almost always a fingering number or a smudge: the
    # ones it read for real (a trill, a turn) it is sure of
    dropped = drop_ornaments(root, records, None if args.drop_ornaments else args.ornament_certainty)
    ornaments = find_ornaments(root, records)
    infos = analyse(root, records, args.low)
    flagged = write_outputs(out_dir, name, root, infos, page_of_measure, system_of_measure, crops_by_page, args.low,
                            swaps, rests, records, ornaments, dropped)
    print(f"\n{len(measures)} bars, {len(flagged)} marked"
          + (f", {len(swaps)} taken from another reading" if swaps else "") + ".")
    print(f"MusicXML : {out_dir / (name + '.musicxml')}")
    print(f"Report   : {out_dir / 'report.txt'}")
    print(f"Proofread: {out_dir / 'proofread' / 'index.html'}")
    print(LEGEND)


if __name__ == "__main__":
    main()
