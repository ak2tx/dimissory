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

ONE SEGMENT FILE PER WRITER, append-only, one JSON object per line.

The first two designs shared a single file between processes and were both
wrong. Text-mode append was justified with PIPE_BUF, which is a pipe guarantee
and says nothing about regular files. Replacing it with a single os.write() to
an O_APPEND descriptor measured 960/960 on Linux -- and then Windows CI lost 71
of 960 entries and tore one. O_APPEND is simply not an atomicity guarantee for
concurrent writers to a regular file on every platform.

So no two processes ever write the same file. Each writer owns
`<session>/<pid>-<unique>.jsonl`, and reading merges every segment and orders
by timestamp. There is no contention to get wrong, no lock to deadlock, and no
platform-specific behaviour to depend on -- which matters because losing the
agent's own words is the one failure this module cannot have.

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
import uuid

# This process's segment name, chosen once. See path_for.
_SEGMENT: dict = {}

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


def default_root():
    """Where the journal lives: DIMISSORY_HOME/journal, else ~/.dimissory/journal.

    DIMISSORY_HOME exists because the two halves of this tool do not always run
    in the same sandbox. Measured on Codex: the HOOK runs outside the sandbox
    and writes ~/.dimissory happily, while the AGENT's own tool call runs
    inside it, where $HOME is read-only. So the agent could not record what the
    hook was waiting to read, and the letter came out DEGRADED with no
    explanation anywhere.

    One variable, honoured by both halves, is what lets an operator point them
    at a directory the agent can actually write.
    """
    home = os.environ.get("DIMISSORY_HOME")
    if home:
        return os.path.join(os.path.expanduser(home), "journal")
    return os.path.expanduser("~/.dimissory/journal")


def _session_dir(session, root=None):
    root = os.path.expanduser(root or default_root())
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session))
    if not safe or safe.strip(".") == "":
        raise JournalError(f"unusable session name: {session!r}")
    return os.path.join(root, safe[:120])


def path_for(session, root=None):
    """This process's own segment. Nothing else ever writes to it.

    The pid alone is not enough -- pids are reused, and a later process
    inheriting a dead one's segment would append to a stranger's entries. The
    random suffix makes collision a non-question.
    """
    global _SEGMENT
    d = _session_dir(session, root)
    key = (d, os.getpid())
    if _SEGMENT.get("key") != key:
        _SEGMENT["key"] = key
        _SEGMENT["name"] = f"{os.getpid()}-{uuid.uuid4().hex[:8]}.jsonl"
    return os.path.join(d, _SEGMENT["name"])


def segments(session, root=None):
    """Every writer's segment for this session, in a stable order."""
    d = _session_dir(session, root)
    try:
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if f.endswith(".jsonl"))
    except OSError:
        return []


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
    # Appending to a file NO OTHER PROCESS WRITES. That is the whole
    # concurrency strategy, and it is the third one -- the first two shared a
    # file between writers and both measured fine before failing. See the
    # module docstring for what they were and how each was disproved.
    #
    # os.write to an O_APPEND descriptor is still used, but as ordinary
    # care rather than as the guarantee: it keeps a crash mid-session from
    # leaving a partially buffered entry. The guarantee is the segment.
    data = line.encode("utf-8")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(fd, data)
        if written != len(data):
            # A short write is not a crash and must not be silent: the entry is
            # now half in the file, and the reader will drop it. Saying so is
            # the difference between a lost declaration and an unexplained one.
            raise JournalError(
                f"short write to {p}: {written} of {len(data)} bytes. The "
                f"entry was not recorded intact.")
    finally:
        os.close(fd)
    return p


def _is_complete(line):
    """Whether an unterminated final line is nonetheless a whole entry.

    The test is the same one `read` applies to every other line: it parses,
    and it carries the three fields an entry needs. Anything less is torn.
    """
    try:
        d = json.loads(line.strip())
    except (ValueError, TypeError):
        return False
    if not isinstance(d, dict):
        return False
    try:
        float(d["ts"])
    except (KeyError, TypeError, ValueError):
        return False
    return isinstance(d.get("value"), str) and d.get("field") in WRITABLE


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
    values, ages, damaged, revoked = {}, {}, 0, set()
    entries = []
    for seg in segments(session, root):
        try:
            with open(seg, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        # An unterminated final line used to be dropped silently as "a write
        # in FLIGHT, not corruption". That conflated two different things, and
        # the difference is decidable:
        #
        #   it parses      the write COMPLETED and only lost its newline (a
        #                  short write, a copy, an editor). The data is all
        #                  there, and discarding it threw away a real
        #                  declaration -- usually the newest one, which is
        #                  exactly the `next` action a reader needs most.
        #
        #   it does not    genuinely truncated. Dropping it silently made a
        #                  crash mid-write look identical to "the agent never
        #                  declared", and the letter then presented the
        #                  PREVIOUS next action as current, carrying its older
        #                  timestamp. A stale plan presented as the live one is
        #                  the worst thing this file can produce, so it is now
        #                  counted and the caller can say so.
        if lines and not text.endswith("\n"):
            tail = lines.pop()
            if _is_complete(tail):
                lines.append(tail)
            elif tail.strip():
                damaged += 1
        for i, line in enumerate(lines):
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
            # (ts, segment, line) so a merge across writers is total and
            # deterministic. Two processes can stamp the same instant; without
            # a tiebreak, "last write wins" would depend on directory order.
            entries.append((ts, seg, i, field, value))

    for ts, _seg, _i, field, value in sorted(entries, key=lambda e: e[:3]):
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

    # Applied AFTER the replay so a revocation works regardless of which
    # segment or order it was written in.
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
