#!/usr/bin/env python3
"""The verify block must fail when the world moved. It did not.

This is the defect the whole project is named against, shipped in commit one.

`dim resume` ran each command and asked only whether it exited 0. `git rev-parse
--short HEAD` exits 0 in ANY repository, so a letter written at one commit
reported "still holds" at a different one. The verify block -- the entire
differentiator, the thing that separates a letter from a summary -- could not
fail for the reason it exists.

The existing contract suite had 32 checks and did not catch it. It asserted the
block was present, that it came before Next action, and that it said "STALE" --
every property except the one that mattered. Review found it by asking whether
the comparison happened at all.

So these tests drive the real thing end to end against a real repository, and
each one asserts an OUTCOME that differs between a working verifier and a
broken one.

Run: python3 tests/test_verify_can_fail.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory.brief import Brief, Check, Declared, Observed   # noqa: E402
from dimissory.cli import main                                 # noqa: E402
from dimissory.render import render                            # noqa: E402

RAN = 0
FAILED: list = []
SKIPPED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def skip(name, why):
    SKIPPED.append(f"{name} ({why})")
    print(f"  SKIP  {name} -- {why}")


def _git(d, *args):
    return subprocess.run(["git", "-C", d, "-c", "user.email=t@t",
                           "-c", "user.name=t", *args],
                          capture_output=True, text=True, timeout=30)


def _repo():
    d = tempfile.mkdtemp(prefix="dim-verify-")
    _git(d, "init", "-q")
    _git(d, "commit", "-q", "--allow-empty", "-m", "one")
    open(os.path.join(d, "a.txt"), "w").close()
    return d


def _resume(letters):
    """Run `dim resume` and capture its exit code. 0 holds, 2 stale."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = main(["--dir", letters, "resume"])
    return rc, buf.getvalue()


def test_a_letter_goes_stale_when_HEAD_moves():
    """The exact reproduction from review, as a test."""
    if not shutil.which("git"):
        return skip("HEAD moves makes a letter stale", "git not installed")
    d = _repo()
    letters = os.path.join(d, "letters")
    cwd = os.getcwd()
    try:
        os.chdir(d)
        main(["--dir", letters, "write", "--session", "vt"])
        before = _git(d, "rev-parse", "--short", "HEAD").stdout.strip()

        rc, out = _resume(letters)
        check("an unmoved world HOLDS", rc == 0, f"exit {rc}: {out[-200:]}")
        check("and says how many checks agreed", "check(s) agreed" in out, out[-120:])

        _git(d, "commit", "-q", "--allow-empty", "-m", "two")
        after = _git(d, "rev-parse", "--short", "HEAD").stdout.strip()
        check("the world really moved", before != after, (before, after))

        rc, out = _resume(letters)
        check("a moved HEAD makes the letter STALE", rc == 2, f"exit {rc}")
        check("and it NAMES the disagreement rather than just failing",
              before in out and after in out, out[-260:])
    finally:
        os.chdir(cwd)


def test_a_letter_goes_stale_when_the_tree_changes():
    if not shutil.which("git"):
        return skip("a changed tree makes a letter stale", "git not installed")
    d = _repo()
    letters = os.path.join(d, "letters")
    cwd = os.getcwd()
    try:
        os.chdir(d)
        main(["--dir", letters, "write", "--session", "vt"])
        rc, _ = _resume(letters)
        check("holds before the edit", rc == 0, rc)
        open(os.path.join(d, "b.txt"), "w").close()
        rc, out = _resume(letters)
        check("a new untracked file makes it STALE", rc == 2, f"exit {rc}")
        check("and the difference is shown", "b.txt" in out, out[-200:])
    finally:
        os.chdir(cwd)


def test_writing_the_letter_does_not_invalidate_the_letter():
    """The observer effect, found the moment the verifier started working.

    Letters written INTO the repository dirty the working tree, so the tree
    check failed against a world nobody had touched. The tool's own output was
    moving the thing it measured.
    """
    if not shutil.which("git"):
        return skip("the tool does not invalidate its own letter", "no git")
    d = _repo()
    letters = os.path.join(d, "letters")          # deliberately INSIDE the repo
    cwd = os.getcwd()
    try:
        os.chdir(d)
        main(["--dir", letters, "write", "--session", "vt"])
        rc, out = _resume(letters)
        check("a letter written inside the repo still holds", rc == 0,
              f"exit {rc}: {out[-260:]}")
        text = open(sorted(
            os.path.join(letters, f) for f in os.listdir(letters))[0]).read()
        check("because the check excludes the letters directory",
              "exclude" in text, text[text.find("## Verify"):][:200])
    finally:
        os.chdir(cwd)


def test_a_check_with_no_recorded_expectation_is_not_a_pass():
    """Nothing to compare against is a check that cannot fail.

    Counting it as a pass is how the original bug felt fine: every command
    exited 0, so every letter held.
    """
    d = tempfile.mkdtemp(prefix="dim-verify-")
    letters = os.path.join(d, "letters")
    os.makedirs(letters)
    b = Brief(session="s", observed=Observed(head="abc1234"),
              declared=Declared(task="t"),
              checks=(Check("true", "whatever"),))
    text = render(b).replace("#   expected: whatever", "")   # strip it
    with open(os.path.join(letters, "s-1.md"), "w") as fh:
        fh.write(text)
    rc, out = _resume(letters)
    check("a check with nothing to compare is NOT a pass", rc == 2, f"exit {rc}")
    check("and says why", "no recorded expectation" in out, out[-200:])


def test_a_letter_with_no_verify_block_is_refused():
    d = tempfile.mkdtemp(prefix="dim-verify-")
    letters = os.path.join(d, "letters")
    os.makedirs(letters)
    b = Brief(session="s", observed=Observed(head="abc1234"),
              declared=Declared(task="t"), checks=())
    with open(os.path.join(letters, "s-1.md"), "w") as fh:
        fh.write(render(b))
    rc, out = _resume(letters)
    check("an unverifiable letter cannot 'hold'", rc == 2, f"exit {rc}")
    check("and says it carries no checks", "no checks" in out.lower(), out[-160:])


def main_():
    print("=" * 66)
    print(" the verify block fails when the world moved, or it is decoration")
    print("=" * 66)
    for t in (test_a_letter_goes_stale_when_HEAD_moves,
              test_a_letter_goes_stale_when_the_tree_changes,
              test_writing_the_letter_does_not_invalidate_the_letter,
              test_a_check_with_no_recorded_expectation_is_not_a_pass,
              test_a_letter_with_no_verify_block_is_refused):
        t()
    print("\n" + "=" * 66)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    if SKIPPED:
        print(f" SKIPPED {len(SKIPPED)}: {'; '.join(SKIPPED)}")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main_())
