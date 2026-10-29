"""Join the per-page MusicXML files that homr writes into one score.

homr reads one picture at a time and writes one file per page, each starting at bar 1 and restating the key and the
clefs.  The pages are joined in the order given: bars are renumbered, the title comes from the first file that has one,
and a key or clef that merely repeats the one already in force at the end of the previous page is dropped (a clef that
differs is kept, because the last bar of a page may end in the other clef).

    python homr_merge.py p01.musicxml p02.musicxml p03.musicxml -o score.musicxml

merge() takes file paths or already parsed root elements. The first bar of every page after the first starts a new row.
"""
import argparse
import math
import sys
import xml.etree.ElementTree as ET


def _clef_key(clef):
    return (clef.get("number", "1"), clef.findtext("sign"), clef.findtext("line"))


def unify_divisions(roots):
    """homr picks the divisions (the length of a quarter note in the durations) of every page by what the page holds, a page
    with triplets 24 where another has 8.  The pages are joined under the divisions of the first, so the others are first
    written in the least common multiple of all: every duration is multiplied by what is missing."""
    divisions = [int(r.findtext("part/measure/attributes/divisions") or 1) for r in roots]
    target = math.lcm(*divisions)
    for r, d in zip(roots, divisions):
        factor = target // d
        for e in r.iter("duration"):
            if factor != 1:
                e.text = str(int(e.text) * factor)
        for e in r.iter("divisions"):
            e.text = str(target)


def merge(paths):
    roots = [p if isinstance(p, ET.Element) else ET.parse(p).getroot() for p in paths]
    unify_divisions(roots)
    base = roots[0]
    part = base.find("part")
    title = base.find("work/work-title")
    for r in roots[1:]:
        if title is not None and not (title.text or "").strip():
            title.text = r.findtext("work/work-title")

    clef = {}   # staff number -> clef in force
    key = None  # fifths in force

    def track(measure, drop_repeats):
        nonlocal key
        for attrs in list(measure.findall("attributes")):
            for k in attrs.findall("key"):
                f = k.findtext("fifths")
                if drop_repeats and f == key:
                    attrs.remove(k)
                key = f
            for c in attrs.findall("clef"):
                ck = _clef_key(c)
                if drop_repeats and clef.get(ck[0]) == ck:
                    attrs.remove(c)
                clef[ck[0]] = ck
            if drop_repeats and len(attrs) == 0:
                measure.remove(attrs)

    for m in part.findall("measure"):
        track(m, False)
    for r in roots[1:]:
        for m in r.find("part").findall("measure"):
            first = m is r.find("part").find("measure")
            if first:
                # the first bar of a page of the input is the first bar of a row: without this, the rows of the score
                # run on from page to page and no longer match the rows of the original
                if not any(pr.get("new-system") == "yes" for pr in m.findall("print")):
                    m.insert(0, ET.Element("print", {"new-system": "yes"}))
                # divisions / staves / part-symbol are already set by the first file
                for attrs in list(m.findall("attributes")):
                    for tag in ("divisions", "staves", "part-symbol"):
                        for e in attrs.findall(tag):
                            attrs.remove(e)
                    if len(attrs) == 0:
                        m.remove(attrs)
            track(m, True)
            part.append(m)
    for i, m in enumerate(part.findall("measure"), 1):
        m.set("number", str(i))
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="per-page MusicXML files, in page order")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    root = merge(args.files)
    ET.indent(root)
    ET.ElementTree(root).write(args.output, encoding="UTF-8", xml_declaration=True)
    print(f"{len(root.find('part').findall('measure'))} bars from {len(args.files)} files -> {args.output}")


if __name__ == "__main__":
    sys.exit(main())
