"""Automatic repair of one known Audiveris failure: triplets read as plain notes in separate voices.

Symptom: in one bar a staff holds several voices; each voice contains one or more beamed groups of exactly three
equal, undotted, un-tupleted notes (usually eighths).  Audiveris did not see the tuplet, so it wrote the groups as
plain notes (1.5 beats each instead of 1) and, to avoid overlaps, spread them over separate voices.

Repair: squeeze every such three-note group 3:2 (durations x 2/3), mark it as a tuplet, and put everything on the
staff into one voice in time order.  Notes that are not part of a group keep their written length.

It is only done when the arithmetic proves the reading:
  * the staff is irregular as written (so a correct bar is never touched);
  * after squeezing, the pieces of the staff (start from where Audiveris put each voice, groups following each
    other) tile the bar exactly: no gap, no overlap, and they end exactly at the end of the bar;
  * a final check on the rewritten XML.
Anything else is left alone.  Every repaired bar gets a small text mark so it is easy to find and verify.

    repair_triplets(root) -> list of dicts describing what was repaired (and what was skipped, with the reason)
"""
import copy
import xml.etree.ElementTree as ET

PLAIN_TYPES = {"eighth": 0.5, "16th": 0.25, "32nd": 0.125}      # beats (quarter note = 1)
MARK = "auto: triplets rebuilt - please verify"

# child order inside <note> (MusicXML): pitch/rest..., duration, tie, voice, type, dot, accidental,
# time-modification, stem, notehead, staff, beam, notations, lyric
_AFTER_TM = ("stem", "notehead", "notehead-text", "staff", "beam", "notations", "lyric", "play")


class _NeedScale(Exception):
    pass


def _staff(n):
    return n.findtext("staff") or "1"


def _dur(e):
    d = e.find("duration")
    return int(d.text) if d is not None else 0


def _timeline(items):
    """Cursor (in divisions) before every child, and the final cursor."""
    t, before = 0, []
    for e in items:
        before.append(t)
        if e.tag == "note":
            if e.find("chord") is None and e.find("grace") is None:
                t += _dur(e)
        elif e.tag == "backup":
            t -= _dur(e)
        elif e.tag == "forward":
            t += _dur(e)
    return before, t


def _scale_file(root, factor):
    for e in root.iter():
        if e.tag in ("duration", "divisions") and e.text and e.text.strip().isdigit():
            e.text = str(int(e.text) * factor)


def _insert_time_modification(note):
    tm = ET.Element("time-modification")
    ET.SubElement(tm, "actual-notes").text = "3"
    ET.SubElement(tm, "normal-notes").text = "2"
    for i, c in enumerate(list(note)):
        if c.tag in _AFTER_TM:
            note.insert(i, tm)
            return
    note.append(tm)


def _add_tuplet(note, kind):
    nots = note.find("notations")
    if nots is None:
        nots = ET.Element("notations")
        for i, c in enumerate(list(note)):
            if c.tag in ("lyric", "play"):
                note.insert(i, nots)
                break
        else:
            note.append(nots)
    t = ET.SubElement(nots, "tuplet")
    t.set("type", kind)
    t.set("number", "1")
    t.set("bracket", "no")
    t.set("show-number", "actual")


def _staff_voices(items):
    """({staff: {voice: [steps]}}, {staff: [indices of its notes]}); a step = [note, chord-notes...]."""
    out, idx = {}, {}
    for i, e in enumerate(items):
        if e.tag != "note":
            continue
        s, v = _staff(e), e.findtext("voice") or "1"
        idx.setdefault(s, []).append(i)
        steps = out.setdefault(s, {}).setdefault(v, [])
        if e.find("chord") is not None and steps:
            steps[-1].append(e)
        else:
            steps.append([e])
    return out, idx


def _beam1(note):
    for b in note.findall("beam"):
        if b.get("number", "1") == "1":
            return (b.text or "").strip()
    return None


def _chunks(steps):
    """Split a voice's steps into beam groups; an unbeamed step is a chunk of its own."""
    out, cur = [], None
    for st in steps:
        b = _beam1(st[0])
        if b == "begin":
            if cur:
                out.extend([[s] for s in cur])          # unterminated group: treat as singles
            cur = [st]
        elif b in ("continue", "end") and cur is not None:
            cur.append(st)
            if b == "end":
                out.append(cur)
                cur = None
        else:
            if cur:
                out.extend([[s] for s in cur])
                cur = None
            out.append([st])
    if cur:
        out.extend([[s] for s in cur])
    return out


def _triplet_chunk(chunk, div):
    """If the chunk is exactly three equal plain notes, return (type, written duration), else None."""
    if len(chunk) != 3:
        return None
    ty = dur = None
    for st in chunk:
        n = st[0]
        if n.find("rest") is not None or n.find("grace") is not None or n.find("dot") is not None \
                or n.find("time-modification") is not None:
            return None
        t = n.findtext("type")
        if t not in PLAIN_TYPES:
            return None
        d = _dur(n)
        if ty is None:
            ty, dur = t, d
        if t != ty or d != dur:
            return None
        if abs(d - PLAIN_TYPES[t] * div) > 1e-6:
            return None
    return ty, dur


def _step_len(st):
    return _dur(st[0])


def _regular_as_written(steps_by_voice, before, items, exp_units):
    """A staff is fine as written when no voice overruns the bar and some voice runs the whole bar from 0."""
    full = False
    for v, steps in steps_by_voice.items():
        start = before[items.index(steps[0][0])]
        total = sum(_step_len(s) for s in steps)
        if start + total > exp_units:
            return False
        if start == 0 and total == exp_units:
            full = True
    return full


def _plan(steps_by_voice, before, items, exp_units, div):
    """-> (events, why).  events = [(new_start, [steps], triplet_info or None)] in time order, or (None, reason)."""
    events, any_trip = [], False
    for v, steps in steps_by_voice.items():
        tau = before[items.index(steps[0][0])]
        for chunk in _chunks(steps):
            info = _triplet_chunk(chunk, div)
            if info:
                any_trip = True
                events.append((tau, chunk, info))
                tau += 2 * info[1]                       # a triplet of three notes written d long lasts 2d
            else:
                events.append((tau, chunk, None))
                tau += sum(_step_len(s) for s in chunk)
    if not any_trip:
        return None, "no beamed group of three"
    events.sort(key=lambda e: e[0])
    t = 0
    for start, chunk, info in events:
        if start != t:
            return None, "pieces would not tile the bar (gap or overlap at %d of %d)" % (start, exp_units)
        t += 2 * info[1] if info else sum(_step_len(s) for s in chunk)
    if t != exp_units:
        return None, "pieces end at %d, the bar is %d" % (t, exp_units)
    return events, None


def _apply(m, items, staff, events, exp_units, div, target_voice):
    """Rewrite the staff's block in measure m.  Returns False (caller restores the measure) if a check fails."""
    notes = {id(n) for _, chunk, _ in events for st in chunk for n in st}
    pos = [i for i, e in enumerate(items) if id(e) in notes]
    first_idx, last_idx = min(pos), max(pos)
    orig_before, _ = _timeline(items)
    c0 = orig_before[first_idx]
    if c0 != 0:
        return False
    # the backup that follows the block returns the cursor to where the next staff starts
    follow = None
    for j in range(last_idx + 1, len(items)):
        if items[j].tag == "backup":
            follow = items[j]
            break
        if items[j].tag == "note":
            break
    c_next = (orig_before[items.index(follow)] - _dur(follow)) if follow is not None else None

    # new note sequence in time order, with squeezed durations and tuplet marks
    seq, first_note = [], None
    for start, chunk, info in events:
        for si, st in enumerate(chunk):
            for n in st:
                if info:
                    n.find("duration").text = str(info[1] * 2 // 3)
                    _insert_time_modification(n)
                n.find("voice").text = target_voice
            if info and si == 0:
                _add_tuplet(st[0], "start")
            if info and si == 2:
                _add_tuplet(st[-1], "stop")
            seq.extend(st)
    first_note = seq[0]

    d = ET.Element("direction", {"placement": "above"})
    ET.SubElement(ET.SubElement(d, "direction-type"), "words", {"font-style": "italic", "font-size": "8"}).text = MARK
    ET.SubElement(d, "staff").text = staff

    new_children = []
    for i, e in enumerate(items):
        if i == first_idx:
            new_children.append(d)
            new_children.extend(seq)
        if first_idx <= i <= last_idx and (id(e) in notes or e.tag in ("backup", "forward")):
            continue                                    # the block is replaced by seq
        new_children.append(e)
    m[:] = new_children

    if follow is not None:
        new_amt = exp_units - c_next                    # cursor is at the end of the bar after the block
        if new_amt <= 0:
            return False
        follow.find("duration").text = str(new_amt)

    # final check on the rewritten bar
    items2 = list(m)
    b2, _ = _timeline(items2)
    if min(b2) < 0:
        return False
    k = 0
    for start, chunk, info in events:
        if b2[items2.index(chunk[0][0])] != start:
            return False
    tot = {}
    for e in items2:
        if e.tag == "note" and _staff(e) == staff and e.find("chord") is None:
            tot[e.findtext("voice")] = tot.get(e.findtext("voice"), 0) + _dur(e)
    return list(tot.keys()) == [target_voice] and tot[target_voice] == exp_units


def _repair_measure(m, part_state, div, report):
    items = list(m)
    before, _ = _timeline(items)
    sv, idx = _staff_voices(items)
    beats, bt = part_state
    exp_units = round(beats * 4.0 / bt * div)
    for staff, sbv in sv.items():
        if _regular_as_written(sbv, before, items, exp_units):
            continue
        events, why = _plan(sbv, before, items, exp_units, div)
        if events is None:
            if why != "no beamed group of three":
                report.append(dict(kind="skipped", measure=m.get("number"), staff=staff, why=why))
            continue
        if any(info and (info[1] * 2) % 3 for _, _, info in events):
            raise _NeedScale()
        lo, hi = min(idx[staff]), max(idx[staff])
        if [e for e in items[lo:hi + 1] if e.tag == "note" and _staff(e) != staff]:
            report.append(dict(kind="skipped", measure=m.get("number"), staff=staff, why="other staves are interleaved"))
            continue
        saved = copy.deepcopy(list(m))
        target = events[0][1][0][0].findtext("voice") or "1"
        if _apply(m, items, staff, events, exp_units, div, target):
            report.append(dict(kind="repaired", measure=m.get("number"), staff=staff,
                               groups=sum(1 for _, _, i in events if i),
                               note_value=[i for _, _, i in events if i][0][0]))
        else:
            m[:] = saved
            report.append(dict(kind="skipped", measure=m.get("number"), staff=staff, why="check after rewrite failed"))
        items = list(m)
        before, _ = _timeline(items)
        sv, idx = _staff_voices(items)


def repair_triplets(root):
    """Repair all qualifying bars in a score-partwise tree (in place).  Returns the list of actions."""
    snapshot = copy.deepcopy(root)
    for attempt in (0, 1):
        report = []
        try:
            for part in root.findall("part"):
                div, beats, bt = 1, 4, 4
                for m in part.findall("measure"):
                    at = m.find("attributes")
                    if at is not None:
                        if at.find("divisions") is not None:
                            div = int(at.find("divisions").text)
                        t = at.find("time")
                        if t is not None:
                            beats, bt = int(t.find("beats").text), int(t.find("beat-type").text)
                    n0 = len(report)
                    _repair_measure(m, (beats, bt), div, report)
                    for r in report[n0:]:
                        r["part"] = part.get("id")
            return report
        except _NeedScale:
            # 2/3 of the written duration is not a whole number of divisions: use a finer time unit for the whole
            # file (all durations x3, divisions x3) and start over from the untouched copy
            if attempt == 1:
                raise
            root[:] = copy.deepcopy(list(snapshot))
            _scale_file(root, 3)
    return []
