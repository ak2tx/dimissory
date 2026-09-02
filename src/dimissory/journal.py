"""The declared half, kept current, instead of asked for at the worst moment.

Both round-25 reviews made this correction independently:

    Codex: "You are treating handoff as a last-moment snapshot when it must be
    a journal... the final trigger should be a SEAL, not a first attempt."

    Grok: "A living declared sidecar, snapshotted at PreCompact, beats a
    solicited confession at 85%."

They are right for a reason that is easy to miss. At the moment a letter is
most needed, the agent is out of budget, mid-compaction, or wedged -- which is
precisely when it can least afford to compose a thoughtful account of its own
reasoning. Asking then produces either nothing or something worse than nothing.

So the agent declares as it goes, and the trigger SEALS what is already there.

APPEND-ONLY, one JSON object per line. Hook invocations are separate concurrent
processes, and an append of a short line opened with O_APPEND is the only write
that is safe between them without a lock -- no read-modify-write, no truncation
window, no partial state if the process dies mid-session. The cost is that
reading means replaying, which for a few dozen lines is free.

WHAT IS NOT SOLVED HERE: nothing makes an agent call `declare`. That is a
compliance problem, not a storage problem, and moving it earlier in the session
is the whole claim -- an agent with budget left is far likelier to comply than
one being asked as the window closes. Whether that is enough is an open
question the README states rather than hides.
"""

from __future__ import annotations

import json
import os
import time

# Fields that describe CURRENT STATE: the last one written wins.
CURRENT = ("task", "next")
# Fields that ACCUMULATE: every entry is kept, in order.
ACCUMULATE = ("decided", "ruled_out", "constraint")
FIELDS = CURRENT + ACCUMULATE
# Retracting one. Review: "accumulated decisions and constraints contradict one
# another without revocation" -- without this, a decision the agent REVERSED an
# hour ago is sealed into the letter as though it still stood, and the next
# agent acts on it. An append-only log needs an explicit retraction because it
# cannot delete.
REVOKE = "revoke"
WRITABLE = FIELDS + (REVOKE,)


class JournalError(ValueError):
    pass


def path_for(session, root=None):
    root = os.path.expanduser(root or "~/.dimissory/journal")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session))
    if not safe or safe.strip(".") == "":
        raise JournalError(f"unusable session name: {session!r}")
    return os.path.join(root, f"{safe[:120]}.jsonl")


def declare(session, field, value, root=None, now=None):
    """Append one declaration. Returns the path written.

    Deliberately does not read the file first. A read-modify-write between
    concurrent hook processes loses entries silently, and losing the agent's
    own words is the one failure this module cannot have.
    """
    if field not in WRITABLE:
        raise JournalError(f"unknown field {field!r}; expected one of "
                           f"{', '.join(WRITABLE)}")
    text = "" if value is None else str(value).strip()
    if not text:
        raise JournalError(f"{field}: refusing to record an empty declaration")
    p = path_for(session, root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    line = json.dumps({"ts": now if now is not None else time.time(),
                       "field": field, "value": text},
                      ensure_ascii=True) + "\n"
    # O_APPEND, one write, one line. The kernel serialises appends under
    # PIPE_BUF so concurrent hooks interleave whole lines rather than tearing
    # one. Asserted in tests/test_journal.py with real processes.
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
    return p


def read(session, root=None):
    """Replay the journal into (values, ages).

    `values` maps a field to its current value -- a string for CURRENT fields,
    a tuple for ACCUMULATE ones. `ages` maps a field to the timestamp of its
    most recent entry, which is what lets a letter say how old a declaration
    is rather than presenting a two-hour-old next action as though it were
    fresh.

    A torn or unparseable line is SKIPPED and counted, never guessed at. The
    count is returned so a caller can say the journal was damaged instead of
    quietly presenting a shorter one.
    """
    p = path_for(session, root)
    values, ages, damaged, revoked = {}, {}, 0, set()
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}, {}, 0
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    # A concurrent hook may be mid-append. An unterminated final line is NOT
    # damage -- it is a write in flight, and counting it as corruption would
    # make every seal during an active session report a damaged journal.
    # Dropping it is also what makes sealing DETERMINISTIC: the letter contains
    # exactly the entries that were complete when it was read, and a
    # declaration that arrives during the seal simply belongs to the next one.
    if lines and not text.endswith("\n"):
        lines.pop()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            field, value, ts = d["field"], d["value"], float(d["ts"])
        except (ValueError, KeyError, TypeError):
            damaged += 1
            continue
        if field not in WRITABLE or not isinstance(value, str):
            damaged += 1
            continue
        if field == REVOKE:
            revoked.add(value)
            ages[REVOKE] = max(ages.get(REVOKE, ts), ts)
            continue
        if field in CURRENT:
            values[field] = value
        else:
            values.setdefault(field, [])
            if value not in values[field]:      # a repeated decision is one
                values[field].append(value)
        ages[field] = max(ages.get(field, ts), ts)
    # Applied AFTER the replay so a revocation works regardless of the order
    # it was appended in relative to the thing it retracts.
    for f in ACCUMULATE:
        if f in values:
            values[f] = tuple(v for v in values[f] if v not in revoked)
    for f in CURRENT:
        if values.get(f) in revoked:
            values.pop(f, None)
            ages.pop(f, None)
    return values, ages, damaged


def to_declared(session, root=None, sealed_at=None):
    """Fold the journal into a Declared, plus the age of each field.

    Returns (Declared, ages_seconds, damaged). `ages_seconds` is how long
    before sealing each field was last declared -- the number a reader needs in
    order to know whether "next action" is a plan or a fossil.
    """
    from .brief import Declared
    values, ages, damaged = read(session, root)
    at = sealed_at if sealed_at is not None else time.time()
    d = Declared(
        task=values.get("task"),
        decided=values.get("decided", ()),
        ruled_out=values.get("ruled_out", ()),
        next_action=values.get("next"),
        constraints=values.get("constraint", ()),
    )
    # None, not 0, for a field never declared -- the same rule as everywhere
    # else in this project. Zero would read as "declared just now".
    age = {f: (at - ages[f]) if f in ages else None for f in FIELDS}
    return d, age, damaged
