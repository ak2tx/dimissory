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

    porcelain = status
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


def _exclude_pathspec(cwd, *our_dirs):
    """A git pathspec that ignores EVERY directory dimissory writes to.

    Plural, and that is the fix. It took one argument -- the letters directory
    -- and then the journal landed inside a repository and dirtied the tree
    exactly the same way, which is this project's recurring shape: a rule that
    reached one site and not the rest. Anything dimissory writes goes in here.

    Writing a letter INTO the repository changes the working tree the letter
    attests to, so the dirty check failed the instant it was written -- the
    tool's own output moved the thing it was measuring. Observed on the first
    real run of the fixed verifier.

    Only added when the letters directory is actually inside the repo, because
    a pathspec excluding a directory that does not exist is noise in a document
    someone else has to read.
    """
    if not cwd:
        return ""
    specs = []
    for d in our_dirs:
        if not d:
            continue
        # realpath, not abspath. On macOS a temp dir is /var/folders/... while
        # getcwd() reports /private/var/folders/... -- the same directory
        # through a symlink. abspath left them looking unrelated, so no
        # exclusion was added and the tree check failed against a world nobody
        # had touched.
        try:
            rel = os.path.relpath(os.path.realpath(d), os.path.realpath(cwd))
        except ValueError:                   # different drive on Windows
            continue
        if rel.startswith(os.pardir) or os.path.isabs(rel):
            continue                         # outside the repo: nothing to hide
        specs.append(f":(exclude){rel.replace(os.sep, '/')}")
    if not specs:
        return ""
    return " -- " + " ".join(f"'{x}'" for x in specs)


def checks_for(observed, cwd=None, porcelain=None, our_dirs=()):
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
        # The expectation must be the command's ACTUAL OUTPUT, not a prose
        # description of it. `expect="2 modified path(s)"` can never be compared
        # to anything, so the check could only ever be evaluated on exit status
        # -- and `git status` exits 0 whatever it prints. A check that cannot
        # disagree is the defect this project is named after avoiding.
        spec = _exclude_pathspec(cwd, *our_dirs)
        cmd = "git status --porcelain" + spec
        recorded = porcelain
        if recorded is None:
            recorded = "\n".join(sorted(k["dirty"]))
        out.append(Check(
            command=cmd,
            expect=recorded,
            why="uncommitted work the letter assumes is still present",
        ))
    return tuple(out)
