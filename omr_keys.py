"""Correct the key signature in the exported MusicXML.

What goes wrong: Audiveris recognises a key SIGN, but C major has none.  When a piece changes to C major (the page shows
cancelling natural signs after a double barline) or a system simply starts without any key sign, Audiveris records no
change and keeps the old key for the rest of the piece.  The pitches of notes with no printed accidental are then
computed with the OLD key (an E that should be natural comes out E-flat), and in the other direction a sharp accidental
at the start of a bar can be mistaken for a key signature.

What the project file tells us for sure: the opening block of every system lists its clef and, if there is one, its
key.  So
  * a system opening with a clef but no key  ->  C major from that bar;
  * between two system openings with different keys the change happens at the first double barline in between (the
    bar after it); with no double barline, at the next system;
  * key signs that are not in a system opening are only trusted right after a double barline; otherwise they are
    stray accidentals and are ignored.

  read_header_keys(omr) -> (events, loose)
  apply_to_score(root, events, loose, offset=0) -> messages

After the key is corrected, the notes whose accidental came only from the old key (no accidental printed in that bar for
the same pitch, not tied from the previous bar) get their accidental recomputed with the new key.
"""
import re
import zipfile
import xml.etree.ElementTree as ET

SHARPS = "FCGDAEB"
FLATS = "BEADGCF"
_ATTR_BEFORE_KEY = ("footnote", "level", "divisions")


def key_alter(step, fifths):
    if fifths > 0 and step in SHARPS[:fifths]:
        return 1
    if fifths < 0 and step in FLATS[:-fifths]:
        return -1
    return 0


def read_header_keys(omr_path):
    """events[part] = [(bar, fifths)] for every system opening; loose[part] = [(bar, fifths, fraction in bar)]"""
    z = zipfile.ZipFile(omr_path)
    names = [n for n in z.namelist() if re.match(r"sheet#\d+/sheet#\d+\.xml$", n)]
    names.sort(key=lambda s: int(re.search(r"#(\d+)/", s).group(1)))
    events, loose, g = {}, {}, 0
    for name in names:
        root = ET.fromstring(z.read(name))
        keys = {}
        for el in root.iter("key"):
            if el.get("id") and el.get("fifths") is not None and el.find("bounds") is not None:
                b = el.find("bounds")
                keys[el.get("id")] = (int(el.get("fifths")), el.get("staff"), int(b.get("x")), int(b.get("w")))
        for system in root.iter("system"):
            parts = system.findall("part")
            stacks = system.findall("stack")
            for pi, part in enumerate(parts):
                staves = part.findall("staff")
                if not staves:
                    continue
                st = staves[0]                                    # the part's first staff speaks for the part
                header = st.find("header")
                if header is not None and header.find("clef") is not None:      # a normal opening block
                    fifths, readable = 0, True
                    if header.find("key") is not None:
                        hs = float(header.get("stop"))
                        cands = [v for v in keys.values() if v[1] == st.get("id") and v[2] <= hs + 2]
                        if cands:
                            fifths = cands[0][0]
                        else:
                            readable = False                      # a key is there but unreadable: say nothing
                    if readable:
                        events.setdefault(pi, []).append((g + 1, fifths))
                # Every other key sign in this part is a stray (a printed accidental taken for a key sign) unless it
                # follows a double barline.  The genuine opening keys are the ones inside an opening block that has
                # a key.  (Collected for every system, whether or not its opening block was normal.)
                my_staves = {s.get("id"): s for s in staves}
                # The staves of one part share one key.  An opening block whose key disagrees with the part's first
                # staff is a stray too (e.g. a printed flat at the very left of the bass staff).
                part_key = events[pi][-1][1] if (pi in events and events[pi] and events[pi][-1][0] == g + 1) else None
                opening_ids = set()
                for sid, s in my_staves.items():
                    h = s.find("header")
                    if h is not None and h.find("key") is not None:
                        ids = {k for k, v in keys.items() if v[1] == sid and v[2] <= float(h.get("stop")) + 2}
                        if part_key is not None and any(keys[k][0] != part_key for k in ids):
                            continue                              # disagrees with the part's key: stray, not opening
                        opening_ids |= ids
                    elif part_key not in (None, 0):
                        pass                                      # this staff shows no key while the first one does
                for j, stk in enumerate(stacks):
                    ms = part.findall("measure")
                    if j >= len(ms):
                        continue
                    ke = ms[j].find("keys")
                    if ke is None or not (ke.text or "").strip():
                        continue
                    left, right = float(stk.get("left")), float(stk.get("right"))
                    for kid in ke.text.split():
                        v = keys.get(kid)
                        if v is None or v[1] not in my_staves or kid in opening_ids:
                            continue
                        loose.setdefault(pi, []).append((g + 1 + j, v[0], ((v[2] + v[3] / 2.0) - left) / max(right - left, 1.0)))
            g += len(stacks)
    return events, loose


def _key_state(part):
    """fifths in force at the start of each bar of a part, as exported ({} entries before the first key are None)."""
    out, cur = {}, None
    for m in part.findall("measure"):
        at = m.find("attributes")
        if at is not None and at.find("key") is not None:
            f = at.find("key").findtext("fifths")
            if f is not None:
                cur = int(f)
        out[int(m.get("number"))] = cur
    return out


def _double_after(part):
    """set of bars whose right barline is not an ordinary one (double / final)."""
    s = set()
    for m in part.findall("measure"):
        for b in m.findall("barline"):
            if b.get("location", "right") == "right" and b.findtext("bar-style") not in (None, "regular"):
                s.add(int(m.get("number")))
    return s


def _insert_key(measure, fifths):
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
        if e.tag in _ATTR_BEFORE_KEY:
            idx = i + 1
    k = ET.Element("key")
    ET.SubElement(k, "fifths").text = str(fifths)
    at.insert(idx, k)


def _recompute_alters(measure, old, new):
    """Notes whose alteration came only from the key: redo it with the new key.  Returns how many changed."""
    notes = []
    cursor, last = 0, 0
    for e in measure:
        if e.tag == "note":
            if e.find("chord") is None:
                last = cursor
                if e.find("grace") is None:
                    cursor += int(e.findtext("duration") or 0)
            notes.append((last, e))
        elif e.tag == "backup":
            cursor -= int(e.findtext("duration"))
        elif e.tag == "forward":
            cursor += int(e.findtext("duration"))
    changed = 0
    by_staff = {}
    for t, e in notes:
        by_staff.setdefault(e.findtext("staff") or "1", []).append((t, e))
    for st, items in by_staff.items():
        state = {}                                       # (step, octave) -> alteration fixed by a printed accidental
        for t, e in sorted(items, key=lambda r: r[0]):   # stable: document order for equal times
            p = e.find("pitch")
            if p is None:
                continue
            step, octv = p.findtext("step"), p.findtext("octave")
            alter_el = p.find("alter")
            alter = int(float(alter_el.text)) if alter_el is not None else 0
            if e.find("accidental") is not None:
                state[(step, octv)] = alter
                continue
            if (step, octv) in state:
                continue                                  # follows an accidental printed earlier in this bar
            if any(t_.get("type") == "stop" for t_ in e.findall("tie")):
                continue                                  # tied from the previous bar: same pitch, keep
            if alter == key_alter(step, old) and key_alter(step, old) != key_alter(step, new):
                n = key_alter(step, new)
                if n == 0:
                    if alter_el is not None:
                        p.remove(alter_el)
                else:
                    if alter_el is None:
                        alter_el = ET.Element("alter")
                        p.insert(list(p).index(p.find("step")) + 1, alter_el)
                    alter_el.text = str(n)
                changed += 1
    return changed


def apply_to_score(root, events, loose, offset=0):
    msgs = []
    for pi, part in enumerate(root.findall("part")):
        ev = sorted(events.get(pi, []))
        if not ev:
            continue
        measures = part.findall("measure")
        nums = [int(m.get("number")) for m in measures]
        doubles = _double_after(part)
        # desired key at the start of each bar
        change = {}
        prev = None
        for (g, f) in ev:
            bar = g - offset
            if prev is None:
                change[bar] = f
            elif f != prev[1]:
                pbar = prev[0] - offset
                cand = [j + 1 for j in range(pbar, bar) if j in doubles]
                change[cand[0] if cand else bar] = f
            prev = (g, f)
        stray_bars = set()                               # bars holding a "key sign" that is really a printed accidental
        for (g, f, frac) in sorted(loose.get(pi, [])):
            bar = g - offset
            if frac < 0.12 and (bar - 1) in doubles:     # a real key sign right after a double barline
                change[bar] = f
            else:
                stray_bars.add(bar)
        if not change:
            continue
        desired, cur = {}, None
        for n in nums:
            if n in change:
                cur = change[n]
            desired[n] = cur
        old = _key_state(part)
        if all(desired[n] is None or desired[n] == old[n] for n in nums):
            continue
        # rewrite the key elements
        for m in measures:
            at = m.find("attributes")
            if at is not None:
                for k in at.findall("key"):
                    at.remove(k)
                if len(at) == 0:
                    m.remove(at)
        prev_k = None
        for m in measures:
            n = int(m.get("number"))
            if desired[n] is not None and desired[n] != prev_k:
                _insert_key(m, desired[n])
            prev_k = desired[n] if desired[n] is not None else prev_k
        # pitches that were computed with the old key
        fixed, bars = 0, []
        for m in measures:
            n = int(m.get("number"))
            o, nw = old[n], desired[n]
            # In a bar that holds a stray "key sign" that sign is a printed accidental Audiveris failed to attach, and
            # the pitches after it were right because of it: keep them.  Only the bars where the stray key lingered
            # by mistake are recomputed.
            if o is not None and nw is not None and o != nw and n not in stray_bars:
                c = _recompute_alters(m, o, nw)
                if c:
                    fixed += c
                    bars.append(n)
        runs, last = [], None
        for n in nums:
            if desired[n] != last:
                runs.append("bar %d: %s" % (n, "C major / A minor (no sharps or flats)" if desired[n] == 0
                                            else "%d %s" % (abs(desired[n]), "sharp(s)" if desired[n] > 0 else "flat(s)")))
                last = desired[n]
        msgs.append("part %d key signature: %s" % (pi + 1, "; ".join(runs)))
        if fixed:
            msgs.append("  re-pitched %d note(s) whose accidental came only from the old key (bars %s)" % (
                fixed, ", ".join(str(b) for b in bars[:14]) + (" ..." if len(bars) > 14 else "")))
    return msgs
