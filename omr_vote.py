"""Combine several readings of the same score, staff by staff.

Audiveris is sensitive to the exact pixel scale and resampling of a page: the same bar can come out right at one scale
and wrong at another (a beamed pair with a far-away head is the typical case), and the two staves of one bar fail
independently.  Reading the page a few times at slightly different scales and keeping, for every staff of every bar,
the reading that is most likely right fixes many errors that no amount of post-processing can.

For every (part, bar, staff) each reading is judged on
  * validity  - does its rhythm add up (some voice fills the bar, none overfills it, the staff is not empty)?
  * agreement - how similar is its content to the other readings (set of events: onset, length, pitches)?
The primary reading (the first) keeps its staff unless
  * it is not valid and another reading is valid (the valid readings then vote: the one that agrees most with all the
    others wins), or
  * it is valid but the other valid readings agree with each other more than with it (a majority outvotes it).
If no reading is valid the primary is kept: replacing one broken staff with another proves nothing.
Only a staff's notes (and the directions between them) are swapped; the primary's clefs, keys, times and barlines stay.

    combine(trees, names=None) -> (tree, decisions)       trees: score-partwise roots, the first is the primary
"""
import copy
from math import lcm


# ----------------------------------------------------------------------------- time units
def _unify_divisions(trees):
    common = 1
    for t in trees:
        for d in t.iter("divisions"):
            common = lcm(common, int(d.text))
    for t in trees:
        for part in t.findall("part"):
            cur = None
            for m in part.findall("measure"):
                at = m.find("attributes")
                if at is not None and at.find("divisions") is not None:
                    cur = int(at.find("divisions").text)
                f = common // (cur or 1)
                if f != 1:
                    for e in m.iter("duration"):
                        e.text = str(int(e.text) * f)
                if at is not None and at.find("divisions") is not None:
                    at.find("divisions").text = str(common)
    return common


# ----------------------------------------------------------------------------- one staff of one bar
def _staff_of(e):
    return e.findtext("staff") or "1"


def _events(measure, staff):
    """set of (onset, duration, pitches) for the notes and rests of one staff; chords are one event."""
    out, cursor, last, lead = set(), 0, 0, None
    groups = {}
    for e in measure:
        if e.tag == "note":
            if e.find("chord") is None:
                last = cursor
                if e.find("grace") is None:
                    cursor += int(e.findtext("duration") or 0)
                lead = id(e)
                if _staff_of(e) == staff:
                    groups[lead] = [last, int(e.findtext("duration") or 0), []]
            if _staff_of(e) == staff and lead in groups:
                pit = e.find("pitch")
                if pit is not None:
                    groups[lead][2].append((pit.findtext("step"), pit.findtext("alter", "0"), pit.findtext("octave")))
        elif e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
    for t, d, ps in groups.values():
        out.add((t, d, tuple(sorted(ps))))
    return out


def _valid(measure, staff, div, beats, beat_type):
    exp = beats * 4.0 / beat_type
    voices, cursor = {}, 0                       # where each voice ends (gaps left by <forward> count as time)
    for e in measure:
        if e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
        elif e.tag == "note" and e.find("chord") is None and e.find("duration") is not None and e.find("grace") is None:
            cursor += int(e.findtext("duration"))
            if _staff_of(e) == staff:
                v = e.findtext("voice") or "1"
                voices[v] = max(voices.get(v, 0), cursor / float(div))
    if not voices:
        return False
    return abs(max(voices.values()) - exp) < 1e-6


def _jaccard(a, b):
    u = a | b
    return len(a & b) / len(u) if u else 1.0


def _block(measure, staff):
    """(first index, last index) of one staff's notes in the measure, or None if it has none / is interleaved."""
    items = list(measure)
    idx = [i for i, e in enumerate(items) if e.tag == "note" and _staff_of(e) == staff]
    if not idx:
        return None
    lo, hi = min(idx), max(idx)
    for e in items[lo:hi + 1]:
        if e.tag == "note" and _staff_of(e) != staff:
            return "interleaved"
        if e.tag == "direction" and (e.findtext("staff") or staff) != staff:
            return "interleaved"
    return lo, hi


def _fix_backups(measure):
    """After a swap: every backup that separates two staves must bring the cursor back to the start of the bar."""
    items = list(measure)
    cursor = 0
    prev_staff = None
    for i, e in enumerate(items):
        if e.tag == "note":
            if e.find("chord") is None and e.find("grace") is None:
                cursor += int(e.findtext("duration") or 0)
            prev_staff = _staff_of(e)
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
        elif e.tag == "backup":
            nxt = next((x for x in items[i + 1:] if x.tag == "note"), None)
            if nxt is not None and prev_staff is not None and _staff_of(nxt) != prev_staff:
                e.find("duration").text = str(cursor)
                cursor = 0
            else:
                cursor -= int(e.findtext("duration"))


def _swap_staff(dest, src, staff):
    """Replace the notes of `staff` in bar `dest` by those of bar `src`.  False if it cannot be done safely."""
    sb = _block(src, staff)
    if sb is None or sb == "interleaved":
        return False
    sitems = list(src)
    new = [copy.deepcopy(e) for e in sitems[sb[0]:sb[1] + 1]]
    db = _block(dest, staff)
    if db == "interleaved":
        return False
    items = list(dest)
    if db is None:
        # the primary has nothing for this staff: append after the last note, before any trailing barline
        pos = len(items)
        while pos > 0 and items[pos - 1].tag == "barline":
            pos -= 1
        for k, e in enumerate(new):
            dest.insert(pos + k, e)
        # return the cursor to the start of the bar before the new block, if it is not there already
        from xml.etree import ElementTree as ET
        b = ET.Element("backup")
        ET.SubElement(b, "duration").text = "1"
        dest.insert(pos, b)
        _fix_backups(dest)
        return True
    lo, hi = db
    for e in items[lo:hi + 1]:
        dest.remove(e)
    for k, e in enumerate(new):
        dest.insert(lo + k, e)
    _fix_backups(dest)
    return True


# ----------------------------------------------------------------------------- the vote
def combine(trees, names=None):
    names = names or ["reading %d" % i for i in range(len(trees))]
    trees = [copy.deepcopy(t) for t in trees]
    decisions = []
    prim = trees[0]
    shape = [len(p.findall("measure")) for p in prim.findall("part")]
    keep = [0]
    for i in range(1, len(trees)):
        sh = [len(p.findall("measure")) for p in trees[i].findall("part")]
        if sh == shape:
            keep.append(i)
        else:
            decisions.append(dict(kind="excluded", reading=names[i], why="different shape: bars per part %s vs %s" % (sh, shape)))
    trees = [trees[i] for i in keep]
    names = [names[i] for i in keep]
    if len(trees) < 2:
        return trees[0], decisions
    div = _unify_divisions(trees)

    for pi in range(len(shape)):
        parts = [t.findall("part")[pi] for t in trees]
        measures = [p.findall("measure") for p in parts]
        beats, bt, nst = 4, 4, 1
        for bi in range(shape[pi]):
            a0 = measures[0][bi].find("attributes")
            if a0 is not None:
                if a0.find("time") is not None:
                    beats, bt = int(a0.find("time/beats").text), int(a0.find("time/beat-type").text)
                if a0.find("staves") is not None:
                    nst = int(a0.find("staves").text)
            bars = [m[bi] for m in measures]
            for s in [str(i) for i in range(1, nst + 1)]:
                ev = [_events(b, s) for b in bars]
                ok = [_valid(b, s, div, beats, bt) for b in bars]
                valid = [i for i, x in enumerate(ok) if x]
                if not valid:
                    continue                                  # nothing to choose between: keep the primary
                if len(valid) == 1:
                    best = valid[0]
                else:
                    score = {i: sum(_jaccard(ev[i], ev[j]) for j in range(len(bars)) if j != i) for i in valid}
                    top = max(score.values())
                    best = 0 if (0 in valid and score[0] >= top - 1e-9) else max(valid, key=lambda i: score[i])
                if best == 0 or ev[best] == ev[0]:
                    continue
                if _swap_staff(bars[0], bars[best], s):
                    decisions.append(dict(kind="replaced", part=parts[0].get("id"), bar=int(bars[0].get("number")), staff=s,
                                          from_reading=names[best], primary_valid=ok[0], similarity_to_primary=round(_jaccard(ev[0], ev[best]), 2)))
    for p in trees[0].findall("part"):
        for k, m in enumerate(p.findall("measure")):
            at = m.find("attributes")
            if at is not None and at.find("divisions") is not None and k != 0:
                at.remove(at.find("divisions"))
    return trees[0], decisions
