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

RED, ORANGE = "#E00000", "#FF8000"
LEGEND = ("Colours: red = homr was unsure about this note; orange = a staff in a bar whose rhythm does not add up "
          "(something is missing or too long). Clefs, key and time signatures are not marked.")
IMAGES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
STEP_NAME = {"C": "C", "D": "D", "E": "E", "F": "F", "G": "G", "A": "A", "B": "B"}


# ---------------------------------------------------------------- homr hooks (certainty of every note)
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
            self._steps.append(tuple(top_probability(out[i].numpy()[0, -1, :]) for i in (0, 1, 2)))

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
            symbol.conf = min(step)
        return symbols

    di.ScoreDecoder.generate = generate_with_certainty

    build_note = mg.build_note_or_rest

    def build_note_with_certainty(model_note, *args, **kwargs):
        note = build_note(model_note, *args, **kwargs)
        note._conf = getattr(model_note, "conf", None)
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


# ---------------------------------------------------------------- input pages
def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def collect_pages(source, work, dpi, wanted):
    """Page pictures in name order (a PDF is rendered); returns [path]."""
    source = Path(source)
    work.mkdir(parents=True, exist_ok=True)
    pages = []
    if source.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(source) as pdf:
            for i, page in enumerate(pdf, 1):
                out = work / f"p{i:02d}.png"
                page.get_pixmap(dpi=dpi).save(out)
                pages.append(out)
    else:
        files = [source] if source.is_file() else sorted(
            (f for f in source.iterdir() if f.suffix.lower() in IMAGES), key=lambda f: natural_key(f.name))
        for i, f in enumerate(files, 1):
            out = work / f"p{i:02d}{f.suffix.lower()}"
            shutil.copyfile(f, out)
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


def analyse(root, records, low):
    """Colour the doubtful notes (in place) and return one dict per measure with what is doubtful in it."""
    measures = root.find("part").findall("measure")
    divisions, beats = 1, (4, 4)
    out = []
    for m, rec in zip(measures, records):
        attrs = m.findall("attributes")
        for a in attrs:
            if a.find("divisions") is not None:
                divisions = int(a.findtext("divisions"))
            t = a.find("time")
            if t is not None:
                beats = (int(t.findtext("beats")), int(t.findtext("beat-type")))
        full = beats[0] * 4 * divisions // beats[1]
        notes = m.findall("note")
        conf = rec["conf"]
        cursor, last_onset = 0, 0
        voice_time = {}
        doubtful = []
        for i, e in enumerate(m):
            if e.tag == "backup":
                cursor -= int(e.findtext("duration"))
            elif e.tag == "forward":
                cursor += int(e.findtext("duration"))
                key = (e.findtext("staff", "1"), e.findtext("voice", "1"))
                voice_time[key] = voice_time.get(key, 0) + int(e.findtext("duration"))
            elif e.tag == "note":
                dur = int(e.findtext("duration") or 0)
                grace = e.find("grace") is not None
                onset = last_onset if e.find("chord") is not None else cursor
                if e.find("chord") is None and not grace:
                    last_onset = cursor
                    onset = cursor
                    cursor += dur
                    key = (e.findtext("staff", "1"), e.findtext("voice", "1"))
                    voice_time[key] = voice_time.get(key, 0) + dur
                idx = notes.index(e)
                c = conf[idx] if idx < len(conf) else None
                if c is not None and c < low:
                    doubtful.append({"staff": int(e.findtext("staff", "1")), "name": note_name(e), "conf": c,
                                     "beat": onset / divisions + 1, "element": e})
        wrong_staves = []
        for staff in sorted({k[0] for k in voice_time}):
            totals = [v for k, v in voice_time.items() if k[0] == staff]
            if not (any(v == full for v in totals) and all(v <= full for v in totals)):
                wrong_staves.append((staff, max(totals) / divisions, full / divisions))
        for staff, _, _ in wrong_staves:
            for n in notes:
                if n.find("rest") is None and n.findtext("staff", "1") == staff and n.get("color") is None:
                    n.set("color", ORANGE)
        for d in doubtful:
            d["element"].set("color", RED)
        out.append({"doubtful": doubtful, "wrong": wrong_staves, "new_system": rec["new_system"]})
    return out


def describe(info):
    parts = []
    for staff, got, want in info["wrong"]:
        parts.append(f"rhythm of staff {staff} does not add up ({got:g} of {want:g} beats)")
    if info["doubtful"]:
        notes = ", ".join(f"{d['name']} (staff {d['staff']}, beat {d['beat']:g}, certainty {d['conf']:.2f})"
                          for d in info["doubtful"])
        parts.append(f"{len(info['doubtful'])} unsure note(s): {notes}")
    return parts


# ---------------------------------------------------------------- output
def write_outputs(out_dir, name, root, infos, page_of_measure, system_of_measure, crops_by_page, low):
    flagged = [(i, info) for i, info in enumerate(infos) if info["wrong"] or info["doubtful"]]
    total = len(infos)
    lines = [f"{name}: {total} bars, {len(flagged)} marked ({low:g} = certainty below which a note is marked red).",
             LEGEND, ""]
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
    args = ap.parse_args()

    source = Path(args.input).resolve()
    name = source.stem if source.is_file() else source.name
    out_dir = Path(args.outdir).resolve() if args.outdir else source.parent / (name + "_homr")
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [int(x) for x in args.pages.split(",")] if args.pages else None
    pages = collect_pages(source, out_dir / "work", args.dpi, wanted)
    if not pages:
        sys.exit("No pages found.")

    install_hooks()
    from homr.main import download_weights
    from homr.onnx_providers import coreml_available, cuda_available
    gpu = cuda_available()
    download_weights(gpu or coreml_available(), gpu, False)

    xmls, records, page_of_measure, system_of_measure = [], [], [], []
    crops_by_page = {}
    for n, page in enumerate(pages, 1):
        print(f"--- page {n} of {len(pages)}: {page.name}", flush=True)
        try:
            xml_path, measures, crops = run_homr(page)
        except Exception as e:  # keep the other pages
            print(f"homr failed on {page.name}: {e}", flush=True)
            continue
        xmls.append(xml_path)
        records += measures
        system = -1
        for m in measures:
            system += 1 if (m["new_system"] or system < 0) else 0
            page_of_measure.append(n)
            system_of_measure.append(system)
        starts = sum(1 for m in measures if m["new_system"]) + 1
        crops_by_page[n] = dict(enumerate(crops)) if starts == len(crops) else {}
    if not xmls:
        sys.exit("homr read no page.")

    root = merge([str(p) for p in xmls])
    measures = root.find("part").findall("measure")
    if len(measures) != len(records):
        sys.exit(f"internal error: {len(measures)} bars in the merged file but {len(records)} recorded.")
    infos = analyse(root, records, args.low)
    flagged = write_outputs(out_dir, name, root, infos, page_of_measure, system_of_measure, crops_by_page, args.low)
    print(f"\n{len(measures)} bars, {len(flagged)} marked.")
    print(f"MusicXML : {out_dir / (name + '.musicxml')}")
    print(f"Report   : {out_dir / 'report.txt'}")
    print(f"Proofread: {out_dir / 'proofread' / 'index.html'}")
    print(LEGEND)


if __name__ == "__main__":
    main()
