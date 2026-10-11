"""Put fragmented parts back together.

Audiveris decides which staves on different pages belong to the same part (a "logical part") partly from part-name
text.  Thin vertical strokes at the left edge of a staff are sometimes read as the letters "i" or "l", and then the
same part gets different names on different pages, so the export has MORE parts than any system on any page has.
(Seen on a four-hand duet: every system has 2 parts, the export had 3, the third holding four bars of the second.)

regroup_parts(root, expected) merges such a fragment into the part it belongs to, but only when that is provably
safe: the fragment and its target have the same number of staves, and in no bar do both hold notes.  Otherwise the
file is left alone and the problem is reported.
"""
import copy


def _notes_by_measure(part):
    out = {}
    for m in part.findall("measure"):
        out[m.get("number")] = sum(1 for n in m.findall("note") if n.find("rest") is None)
    return out


def _staves(part):
    for m in part.findall("measure"):
        a = m.find("attributes")
        if a is not None and a.find("staves") is not None:
            return int(a.find("staves").text)
    return 1


def regroup_parts(root, expected):
    """Merge fragment parts until there are at most `expected` parts.  Returns a list of messages."""
    msgs = []
    while True:
        parts = root.findall("part")
        if len(parts) <= expected:
            return msgs
        notes = {p.get("id"): _notes_by_measure(p) for p in parts}
        # the extra part with the fewest notes is the likeliest fragment
        frag = min(parts, key=lambda p: sum(notes[p.get("id")].values()))
        fid = frag.get("id")
        best = None
        for tgt in parts:
            if tgt is frag or _staves(tgt) != _staves(frag):
                continue
            both = [mn for mn, c in notes[fid].items() if c > 0 and notes[tgt.get("id")].get(mn, 0) > 0]
            if both:
                continue
            # prefer the target that is quiet exactly where the fragment is busy: fewest notes overall wins ties
            score = sum(notes[tgt.get("id")].values())
            if best is None or score > best[0]:
                best = (score, tgt)
        if best is None:
            msgs.append("part %s looks like a fragment but no other part is free in the same bars; left as is" % fid)
            return msgs
        tgt = best[1]
        by_num = {m.get("number"): m for m in tgt.findall("measure")}
        moved = []
        for m in frag.findall("measure"):
            if notes[fid].get(m.get("number"), 0) == 0:
                continue
            dest = by_num.get(m.get("number"))
            if dest is None:
                continue
            keep_attrs = [c for c in dest if c.tag == "attributes"]
            src = copy.deepcopy(list(m))
            has_attrs = any(c.tag == "attributes" for c in src)
            for c in list(dest):
                dest.remove(c)
            if not has_attrs:
                for c in keep_attrs:                      # keep clef/key/time changes the target already had here
                    dest.append(c)
            for c in src:
                dest.append(c)
            moved.append(m.get("number"))
        root.remove(frag)
        for sp in list(root.iter("score-part")):
            if sp.get("id") == fid:
                root.find("part-list").remove(sp)
        msgs.append("part %s was a fragment of part %s (notes in bar%s %s only): merged back"
                    % (fid, tgt.get("id"), "s" if len(moved) != 1 else "", ", ".join(moved)))
