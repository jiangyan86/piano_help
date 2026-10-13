"""Tuplets that are not on the page.

A "3" is easy to find in text between the staves: a Japanese character, a caption, a fingering mark Audiveris did not
lose.  When one sits near notes, Audiveris can read it as a tuplet number and turns the notes under it into a tuplet.
A real tuplet is a bracket over notes of ONE voice of ONE staff, from its first to its last note, that fill whole
groups: three eighths in the time of two for a triplet, five for a quintuplet.  A group that
  * has a start but no end, or an end with no start,
  * runs from one voice or staff into another, or
  * does not hold a multiple of its note count
is not a tuplet.  Its notes get their normal lengths back (the bar's timing follows) and lose the tuplet mark.

    drop_false_tuplets(root) -> list of what was undone
"""


def _tuplet_types(n):
    return [(t.get("type"), t.get("number") or "1") for t in n.findall("notations/tuplet")]


def _undo(measure, notes):
    for n in notes:
        tm = n.find("time-modification")
        if tm is None:
            continue
        actual, normal = int(tm.findtext("actual-notes")), int(tm.findtext("normal-notes"))
        old = int(n.findtext("duration"))
        new = old * actual // normal if (old * actual) % normal == 0 else old
        n.find("duration").text = str(new)
        n.remove(tm)
        nt = n.find("notations")
        if nt is not None:
            for t in nt.findall("tuplet"):
                nt.remove(t)
            if len(nt) == 0:
                n.remove(nt)
        if new != old and n.find("chord") is None:               # the voice is longer now: so is its way back
            items = list(measure)
            for e in items[items.index(n) + 1:]:
                if e.tag == "backup":
                    e.find("duration").text = str(int(e.findtext("duration")) + new - old)
                    break


def drop_false_tuplets(root):
    done = []
    for part in root.findall("part"):
        for m in part.findall("measure"):
            notes = [e for e in m.findall("note") if e.find("grace") is None]
            open_groups = {}                                    # number -> [notes]
            bad = []
            for n in notes:
                tt = _tuplet_types(n)
                for kind, num in tt:
                    if kind == "start":
                        if num in open_groups:                  # a new start before the old one ended
                            bad.append(open_groups.pop(num))
                        open_groups[num] = [n]
                if not any(k == "start" for k, _ in tt):
                    for num, g in open_groups.items():
                        if n not in g and n.find("time-modification") is not None:
                            g.append(n)
                for kind, num in tt:
                    if kind == "stop":
                        g = open_groups.pop(num, None)
                        if g is None:
                            bad.append([n])                     # an end with no start
                        else:
                            if n not in g:
                                g.append(n)
                            heads = [x for x in g if x.find("chord") is None]
                            same = len({(x.findtext("staff") or "1", x.findtext("voice") or "1") for x in g}) == 1
                            tm = g[0].find("time-modification")
                            actual = int(tm.findtext("actual-notes")) if tm is not None else 3
                            if not same or len(heads) % actual != 0:
                                bad.append(g)
            bad.extend(open_groups.values())                    # started, never ended
            for g in bad:
                _undo(m, g)
                done.append(dict(part=part.get("id"), bar=int(m.get("number")), notes=len(g)))
    return done
