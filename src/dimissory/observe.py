"""Establishing facts, and refusing to invent them.

Everything here returns UNMEASURED rather than a plausible default. That is the
single rule this module exists to hold: a repository that is not a repository
yields no HEAD, not `"none"`; a command that never ran yields no exit code, not
`0`. The renderer omits what is unmeasured, so the honest path is also the
short one.

`git` is shelled out to rather than parsed from `.git` deliberately. The point
of the observed block is that it reports what the *tools everyone else uses*
would say, so a reader can re-run the same command and get the same answer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from .brief import UNMEASURED, Check, Observed

_TIMEOUT = 5.0


def _git(cwd, *args):
    """One git invocation, or UNMEASURED. Never raises, never guesses."""
    exe = shutil.which("git")
    if not exe or not cwd or not os.path.isdir(cwd):
        return UNMEASURED
    try:
        p = subprocess.run([exe, "-C", cwd, *args], capture_output=True,
                           text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return UNMEASURED
    if p.returncode != 0:
        # Not a repository, a detached worktree with no commits, a permission
        # problem -- all of them are "we do not know", none of them are a value.
        return UNMEASURED
    return p.stdout.strip()


def observe(cwd=None, transcript=None, window=None, session_started=None):
    """Everything establishable about the world, right now.

    `window` is the plan-window reading, if a meter supplied one. It is passed
    in rather than fetched here so that this module has no network path and
    stays testable without one.
    """
    cwd = cwd or os.getcwd()
    head = _git(cwd, "rev-parse", "--short", "HEAD")
    subject = _git(cwd, "log", "-1", "--format=%s")
    status = _git(cwd, "status", "--porcelain")

    dirty = UNMEASURED
    if status is not UNMEASURED:
        # An empty status is a MEASURED clean tree -- an empty tuple, not
        # UNMEASURED. The difference is the whole point of this module.
        dirty = tuple(ln[3:] for ln in status.splitlines() if len(ln) > 3)

    calls = UNMEASURED
    if transcript:
        from .transcript import recent_calls
        found = recent_calls(transcript)
        if found is not None:
            calls = found

    used = resets = UNMEASURED
    if window:
        used = window.get("used_percent", UNMEASURED)
        resets = window.get("resets_at", UNMEASURED)

    return Observed(
        head=head, head_subject=subject, dirty=dirty,
        calls=calls, window_used_percent=used, window_resets_at=resets,
        started_at=session_started if session_started else UNMEASURED,
        written_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


def checks_for(observed, cwd=None):
    """The verify block, derived from what was actually observed.

    A check is only emitted for a fact that was measured. Fabricating a check
    against an unmeasured value would produce the worst possible artifact: a
    verification step that always passes, which is exactly the defect class the
    predecessor kept a file about.
    """
    out = []
    k = observed.known()
    if "head" in k:
        out.append(Check(
            command="git rev-parse --short HEAD",
            expect=k["head"],
            why="the commit this letter was written against",
        ))
    if "dirty" in k and k["dirty"]:
        out.append(Check(
            command="git status --porcelain",
            expect=f"{len(k['dirty'])} modified path(s)",
            why="uncommitted work the letter assumes is still present",
        ))
    return tuple(out)
