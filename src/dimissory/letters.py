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


def meta(path):
    """What a letter says about itself, read back off the page.

    A letter is Markdown, not a database record, and it is deliberately the
    only artifact -- there is no sidecar index that can drift from the file it
    describes. So the few facts pickup needs (which directory, which session,
    which agent) are read from the Observed block, where render.py writes them
    at the start of a line. Missing lines come back as None, never guessed.

    `mtime` is the file's, not the "Issued by" stamp: the stamp is local time
    with no zone, and a file's age is what "sealed 12 minutes ago" means.
    """
    out = {"path": path, "session": None, "cwd": None, "branch": None,
           "agent": None, "degraded": False, "mtime": None}
    try:
        out["mtime"] = os.path.getmtime(path)
    except OSError:
        pass
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return out
    first = text.split("\n", 1)[0]
    if first.startswith("# Dimissory letter: "):
        out["session"] = first[len("# Dimissory letter: "):].strip() or None
    out["degraded"] = "> **DEGRADED" in text
    i = text.find("## Observed")
    if i == -1:
        return out
    a = text.find("```", i)
    b = text.find("```", a + 3) if a != -1 else -1
    if a == -1 or b == -1:
        return out
    for line in text[a + 3:b].splitlines():
        for key in ("cwd", "branch", "agent"):
            if line.startswith(key + " "):
                value = line[len(key):].strip()
                out[key] = value or None
    return out


def _same_dir(a, b):
    """Whether two paths name one directory, through symlinks and case."""
    try:
        return (os.path.normcase(os.path.realpath(os.path.expanduser(a)))
                == os.path.normcase(os.path.realpath(os.path.expanduser(b))))
    except (OSError, ValueError, TypeError):
        return False


def latest_for_cwd(directory, cwd):
    """The newest letter written IN this directory, or None.

    Newest by mtime with the name as tiebreak, the same rule `dim show`
    uses. Letters that record no directory -- written before it was measured,
    or with no directory to measure -- never match: handing a session the
    wrong project's letter is worse than handing it nothing.
    """
    if not cwd:
        return None
    try:
        names = [f for f in os.listdir(directory) if f.endswith(".md")]
    except OSError:
        return None
    best = None
    for name in names:
        p = os.path.join(directory, name)
        m = meta(p)
        if not m.get("cwd") or not _same_dir(m["cwd"], cwd):
            continue
        key = (m.get("mtime") or 0.0, name)
        if best is None or key > best[0]:
            best = (key, p)
    return best[1] if best else None


def ago(seconds):
    """`3m`, `1.5h`, `2.1d`. For "sealed N ago", where precision is noise."""
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


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


def _by_age(directory):
    """Every letter in `directory`, oldest first. Mtime, then name."""
    def key(p):
        try:
            return (os.path.getmtime(p), os.path.basename(p))
        except OSError:
            return (0.0, os.path.basename(p))
    return sorted((os.path.join(directory, f) for f in os.listdir(directory)
                   if f.endswith(".md")), key=key)


def prune(directory, keep, spare=None):
    """Delete the oldest letters beyond `keep`. Returns the paths removed.

    `letters.keep` was documented as "older ones are pruned" and read by
    nothing at all, so the directory grew without bound and every session
    start scanned all of it looking for a letter to hand over. A setting
    that promises something inert is the defect this project keeps a
    docstring about, one config key over.

    `keep` must be a positive integer; anything else keeps everything, and
    the template says so. Zero is NOT "delete them all": a typo in a config
    file must not be able to erase every letter on the machine. `spare` is
    never removed whatever its age -- the letter just written is the one
    the caller is about to report a path for.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep <= 0:
        return []
    try:
        files = _by_age(directory)
    except OSError:
        return []
    removed = []
    for p in (files[:-keep] if len(files) > keep else []):
        if spare and os.path.abspath(p) == os.path.abspath(spare):
            continue
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return removed


def write(directory, session, text, when=None, keep=None):
    """Claim a name and write `text` into it. Returns the path, or None.

    `keep` is `letters.keep`: after a successful write, letters beyond that
    many are pruned, oldest first -- see `prune`.

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
    prune(directory, keep, spare=path)
    return path
