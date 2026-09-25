"""The platform's event-topic grammar, for pattern listeners.

A topic is dot-separated segments (so `a..b` has an empty middle one). In a
pattern, `*` is exactly one whole segment, `**` is zero or more whole
segments, and anything else is literal text (`write_*` and `a**` included).
A pattern equal to the topic always matches. Because `**` may match nothing,
`a.**` matches `a` itself and `a.**.b` matches `a.b`.

The platform's delivery gate uses the same grammar to decide what reaches the
plugin at all, so the two must agree or a plugin's own routing disagrees with
what it receives. `tests/testdata/topic-match-conformance.json` is a
byte-identical copy of the platform's conformance table, and
`tests/test_topic.py` runs its subscription column against this module."""


def matches_topic(pattern: str, event_type: str) -> bool:
    """Does `event_type` match subscription `pattern`? See the module doc."""
    if pattern == event_type:
        return True
    # With no `*` anywhere every segment is literal, so the pattern names
    # exactly one topic, which the equality above already tested.
    if "*" not in pattern:
        return False
    pat = pattern.split(".")
    evt = event_type.split(".")
    # Greedy, backtracking to the most recent `**`: what lies between two
    # `**` has a fixed length, so the latest one is the only one worth
    # retrying, and the match is O(len(pat) * len(evt)) at worst.
    p = e = 0
    resume_p = resume_e = -1
    while e < len(evt):
        if p < len(pat) and pat[p] == "**":
            resume_p, resume_e = p + 1, e
            p += 1
        elif p < len(pat) and (pat[p] == "*" or pat[p] == evt[e]):
            p += 1
            e += 1
        elif resume_p >= 0:
            resume_e += 1
            p, e = resume_p, resume_e
        else:
            return False
    return all(seg == "**" for seg in pat[p:])
