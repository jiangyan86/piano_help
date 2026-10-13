"""Find, for a notehead in the Audiveris project file, the note of the MusicXML score that it became.

The project file knows each head's place on the page and the chord it belongs to; the score has only notes in bars.
A head is matched to a note by (a) the measure and staff that hold its position on the page, (b) the moment in the bar
(the project's time slots give each moment an x position) and (c) its rank within the chord, high to low.  The match
is only made when the chord has as many notes in the score as heads in the project, otherwise nothing is touched.

    load_sheets(omr_path) -> {sheet number: Sheet}
    Locator(root, stacks, offset).notes_of_chord(sheet, head elements of one chord) -> (measure, staff, [notes, high to low]) or None
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

from omr_ottava import _notes_of_staff

STEP = "CDEFGAB"


def box(e):
    b = e.find("bounds")
    return None if b is None else [int(float(b.get(k))) for k in "xywh"]


class Sheet:
    def __init__(self, root):
        self.root = root
        self.heads = {e.get("id"): e for e in root.iter("head")}
        self.chord_of, self.members = {}, defaultdict(list)
        self.dot_head = {}                                  # augmentation dot id -> head id
        self.chord_stem = {}                                # chord id -> stem id
        self.stem_beams = defaultdict(set)                  # stem id -> ids of the beams on it
        self.beams = {e.get("id"): e for e in root.iter("beam")}
        self.stems = {e.get("id"): e for e in root.iter("stem")}
        self.dots = {e.get("id"): e for e in root.iter("augmentation-dot")}
        for rel in root.iter("relation"):
            s, t = rel.get("source"), rel.get("target")
            if rel.find("containment") is not None and t in self.heads and s not in self.heads:
                self.chord_of[t] = s
                self.members[s].append(t)
            elif rel.find("chord-stem") is not None:
                if t in self.stems:
                    self.chord_stem[s] = t
                elif s in self.stems:
                    self.chord_stem[t] = s
            elif rel.find("beam-stem") is not None:
                if s in self.beams and t in self.stems:
                    self.stem_beams[t].add(s)
                elif t in self.beams and s in self.stems:
                    self.stem_beams[s].add(t)
            elif rel.find("augmentation") is not None:
                if s in self.dots and t in self.heads:
                    self.dot_head[s] = t
                elif t in self.dots and s in self.heads:
                    self.dot_head[t] = s

        # where each chord is: system, part, bar within the system, voice and slot (moment) of the bar
        self.chord_slot = {}
        self.voiced = set()                                 # chords that belong to a voice at all
        sysn = 0
        for page in root.findall("page"):
            for system in page.findall("system"):
                sysn += 1
                for pi, part in enumerate(system.findall("part")):
                    for j, meas in enumerate(part.findall("measure")):
                        for v in meas.findall("voice"):
                            for e in v.iter("entry"):
                                val = e.find("value")
                                if val is not None:
                                    self.voiced.add(val.get("chord"))
                                if val is not None and val.get("status") == "BEGIN" and e.findtext("key"):
                                    self.chord_slot[val.get("chord")] = (sysn, pi, j, v.get("id"), int(e.findtext("key")))

    def chord_heads(self, head_id):
        """the heads of the chord a head belongs to, high to low (the project's staff position grows downwards)"""
        c = self.chord_of.get(head_id)
        ids = self.members.get(c) or [head_id]
        return sorted((self.heads[i] for i in ids), key=lambda e: int(e.get("pitch", "0")))


def load_sheets(omr_path):
    z = zipfile.ZipFile(omr_path)
    out = {}
    for n in z.namelist():
        m = re.match(r"sheet#(\d+)/sheet#\d+\.xml$", n)
        if m:
            out[int(m.group(1))] = Sheet(ET.fromstring(z.read(n)))
    return out


def pitch_key(n):
    p = n.find("pitch")
    return int(p.findtext("octave")) * 7 + STEP.index(p.findtext("step"))


class Locator:
    def __init__(self, root, stacks, offset=0):
        self.stacks = stacks
        self.parts = root.findall("part")
        self.bars, self.divs = {}, {}
        for pi, part in enumerate(self.parts):
            div = 1
            for m in part.findall("measure"):
                at = m.find("attributes")
                if at is not None and at.find("divisions") is not None:
                    div = int(at.find("divisions").text)
                g = offset + int(m.get("number"))
                self.bars[(g, pi)] = m
                self.divs[(g, pi)] = div

    def where(self, sheet, x, y):
        """(global measure, stack, part index, staff number) of a point on a page, or None"""
        for g, s in enumerate(self.stacks, 1):
            if s["sheet"] == sheet and s["left"] <= x <= s["right"]:
                best = None
                for pi, staves in enumerate(s["parts"]):
                    for si, st in enumerate(staves):
                        u = (st[1] - st[0]) / 4.0
                        d = max(0.0, st[0] - y, y - st[1])
                        if d < 5 * u and (best is None or d < best[0]):
                            best = (d, pi, si + 1)
                if best:
                    return g, s, best[1], str(best[2])
        return None

    def notes_of_chord_id(self, sheet_obj, sheet, chord_id, heads):
        """exact match through the project file's own voice / slot bookkeeping: (measure, staff, notes high to low)"""
        info = sheet_obj.chord_slot.get(chord_id)
        if not info or not heads:
            return None
        sysn, pi, j, voice, key = info
        g = next((i for i, s in enumerate(self.stacks, 1) if s["sheet"] == sheet and s["system"] == sysn and s["idx"] == j), None)
        if g is None:
            return None
        s = self.stacks[g - 1]
        m = self.bars.get((g, pi))
        if m is None or not (1 <= key <= len(s.get("slots") or [])):
            return None
        t = s["slots"][key - 1][1]
        ys = [box(h)[1] + box(h)[3] / 2.0 for h in heads]
        best = None
        for si, st in enumerate(s["parts"][pi]):
            d = max(0.0, st[0] - sum(ys) / len(ys), sum(ys) / len(ys) - st[1])
            if best is None or d < best[0]:
                best = (d, str(si + 1))
        staff = best[1]
        seq = [n for n, tt in _notes_of_staff(m, staff, self.divs[(g, pi)])
               if tt == t and n.find("pitch") is not None and n.find("grace") is None]
        if len(seq) != len(heads):
            seq = [n for n in seq if (n.findtext("voice") or "1") == str(voice)]
        if len(seq) != len(heads):
            return None
        return m, staff, sorted(seq, key=pitch_key, reverse=True)

    def staff_chords(self, sheet_obj, number, g, pi, staff):
        """the chords of one staff of one bar that are in the score: [dict(cid, x, heads, notes high to low, voice)]"""
        s = self.stacks[g - 1]
        out = []
        for cid, (sysn, pj, j, voice, key) in sheet_obj.chord_slot.items():
            if sysn != s["system"] or j != s["idx"] or pj != pi or not sheet_obj.members.get(cid):
                continue
            hs = sheet_obj.chord_heads(sheet_obj.members[cid][0])
            f = self.notes_of_chord_id(sheet_obj, number, cid, hs)
            if not f or f[1] != staff:
                continue
            xs = sorted(box(h)[0] + box(h)[2] / 2.0 for h in hs)
            out.append(dict(cid=cid, x=xs[len(xs) // 2], heads=hs, notes=f[2], voice=voice))
        return out

    def chord_notes(self, sheet_obj, sheet, heads):
        """notes of a chord: through the project's voice / slot bookkeeping, else through its place on the page"""
        cid = sheet_obj.chord_of.get(heads[0].get("id"))
        return self.notes_of_chord_id(sheet_obj, sheet, cid, heads) or self.notes_of_chord(sheet, heads)

    def notes_of_chord(self, sheet, heads):
        xs = sorted(box(h)[0] + box(h)[2] / 2.0 for h in heads)
        ys = [box(h)[1] + box(h)[3] / 2.0 for h in heads]
        hit = self.where(sheet, xs[len(xs) // 2], sum(ys) / len(ys))
        if not hit:
            return None
        g, s, pi, staff = hit
        m = self.bars.get((g, pi))
        if m is None or not s.get("slots"):
            return None
        xmid = xs[len(xs) // 2]
        t = min(s["slots"], key=lambda sl: abs(s["left"] + sl[0] - xmid))[1]
        seq = [(n, tt) for n, tt in _notes_of_staff(m, staff, self.divs[(g, pi)]) if tt == t and n.find("pitch") is not None
               and n.find("grace") is None]
        if len(seq) != len(heads):
            return None
        return m, staff, sorted((n for n, _t in seq), key=pitch_key, reverse=True)
