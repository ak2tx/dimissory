"""Where a letter goes, and how its name is claimed.

This exists because the naming rule was fixed in ONE of the three places that
write letters. `hook.seal` learned to claim its filename with O_CREAT|O_EXCL
after review measured an upgraded letter overwriting the degraded one it was
meant to replace; `dim write` and the setup proof-letter kept a plain
`open(path, "w")` on a name with one-second resolution. Review found that too,
one round later, which is what a fix living in one caller instead of one
function looks like.

So the rule has one owner now:

  claimed, not composed   O_CREAT|O_EXCL, so two writers in the same second
                          cannot land on one path, in one process or across
                          several.
  zero-padded counter     always present, so sorting by NAME gives the same
                          order as sorting by TIME. An unpadded, sometimes-
                          absent suffix does not: "-1.md" sorts BEFORE ".md",
                          since '-' is 0x2D and '.' is 0x2E.
  0o600                   a letter carries the agent's own words about work in
                          progress. Other accounts on the host have no
                          business reading it because a umask was loose.
"""

from __future__ import annotations

import os
import time

MAX_IN_ONE_SECOND = 1000


def claim(directory, session, when=None):
    """Create and return a path nobody else holds, or None.

    Returns an OPEN-able path that this call has already created (empty), so
    the caller writes into a name that cannot be stolen between the check and
    the write.
    """
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S",
                          time.localtime(when) if when else time.localtime())
    base = f"{str(session)[:60]}-{stamp}"
    for n in range(MAX_IN_ONE_SECOND):
        path = os.path.join(directory, f"{base}-{n:03d}.md")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError:
            return None
        os.close(fd)
        return path
    return None


def _body(text):
    """A letter's content, minus the line that always differs.

    Every letter opens with "Issued by dimissory at <timestamp>", so two
    letters describing an identical world are never byte-identical. Comparing
    the rest is what makes "has anything changed?" answerable.
    """
    return "\n".join(line for line in (text or "").splitlines()
                     if not line.startswith("Issued by dimissory at "))


def latest_for(directory, session):
    """The newest letter for this session, or None."""
    prefix = f"{str(session)[:60]}-"
    try:
        names = [f for f in os.listdir(directory)
                 if f.startswith(prefix) and f.endswith(".md")]
    except OSError:
        return None
    if not names:
        return None
    return os.path.join(directory, sorted(names)[-1])


def write(directory, session, text, when=None):
    """Claim a name and write `text` into it. Returns the path, or None.

    A LETTER IDENTICAL TO THE LAST ONE IS NOT WRITTEN. Measured: one short
    Codex session produced FOUR letters. The margin guard was working
    correctly -- PostToolUse sealed exactly once -- but PreCompact and
    SessionEnd seal unconditionally, by design, because they are the
    last-chance events and a letter at compaction matters even when the window
    is nowhere near full.

    Special-casing those two events would have been the obvious fix and the
    wrong one: it would trade duplicate letters for missing ones. The real
    rule does not mention events at all. If the document we are about to write
    says exactly what the last one said, writing it adds nothing and buries
    the letter that matters under copies of itself.

    Returns the EXISTING path in that case, so a caller can still tell the
    agent where its letter is -- suppressing the write must not look like a
    failure to seal.
    """
    previous = latest_for(directory, session)
    if previous:
        try:
            with open(previous, encoding="utf-8") as fh:
                if _body(fh.read()) == _body(text):
                    return previous
        except OSError:
            pass
    path = claim(directory, session, when)
    if path is None:
        return None
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None
    return path
