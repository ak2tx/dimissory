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


def write(directory, session, text, when=None):
    """Claim a name and write `text` into it. Returns the path, or None."""
    path = claim(directory, session, when)
    if path is None:
        return None
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None
    return path
