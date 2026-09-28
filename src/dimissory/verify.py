"""Running a letter's Verify block, and saying plainly what it found.

One owner. `dim resume` did this inline, and then the pickup hook needed the
same answer at session start -- so it moved here rather than being written a
second time. A rule that reaches one call site and not the other is this
project's recurring defect (see letters.py and observe._exclude_pathspec for
two earlier instances), and the comparison below is the rule that matters
most: it is the only part of a letter that can fail.

Two properties, both of which were once shipped the other way:

  THE EXPECTATION IS COMPARED. The first version asked only whether each
  command exited 0. `git rev-parse --short HEAD` exits 0 in ANY repository, so
  a letter written at one commit reported "still holds" at another -- the
  verify block, the entire differentiator, could not fail for the reason it
  exists. A check with no recorded expectation is therefore NOT a pass.

  NO SHELL. cmd.exe does not strip single quotes, so the tree check's pathspec
  reached git with the quotes attached and the exclusion silently did nothing.
  And a letter is a portable document that arrives from another machine:
  `resume` executes what is written in it, and a shell would add redirection,
  chaining and expansion to anything that ever lands there. shlex.split gives
  the same argv everywhere and nothing else.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess

MARK = "## Verify first"

HOLDS, STALE, UNVERIFIABLE, UNPARSEABLE = ("holds", "stale", "unverifiable",
                                          "unparseable")


@dataclasses.dataclass
class Verdict:
    """What running the block established. `results` is (command, ok, why)."""

    status: str
    results: list = dataclasses.field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def stale(self) -> int:
        return sum(1 for _c, ok, _w in self.results if not ok)

    @property
    def holds(self) -> bool:
        return self.status == HOLDS

    @property
    def exit_code(self) -> int:
        """0 holds, 2 anything else. Deliberately nothing in between."""
        return 0 if self.holds else 2

    def sentence(self) -> str:
        """One line a reader -- or the next agent -- can act on."""
        if self.status == HOLDS:
            return (f"Its Verify block holds ({self.total} check(s) agreed), "
                    f"so the world has not moved since it was written.")
        if self.status == STALE:
            first = next(((c, w) for c, ok, w in self.results if not ok), None)
            detail = f"; first: `{first[0]}` {first[1]}" if first else ""
            return (f"Its Verify block is STALE ({self.stale} of {self.total} "
                    f"check(s) disagree{detail}), so the world moved after it "
                    f"was written -- re-derive before acting on its Next "
                    f"action.")
        if self.status == UNVERIFIABLE:
            return ("It carries no Verify block, so it is a claim rather than "
                    "a finding.")
        return ("Its Verify block could not be parsed, so nothing in it has "
                "been confirmed.")


def parse(text):
    """The (command, expectation) pairs in a letter, or None with no block.

    None and [] are different answers: a letter with no block is unverifiable
    by construction, while a block that yields no pairs is malformed.
    """
    if MARK not in text:
        return None
    after = text.split(MARK, 1)[1]
    fences = after.split("```")
    if len(fences) < 2:
        return []
    block = fences[1]

    # `command` followed by its `#   expected:` line. The expectation is
    # JSON since the format changed to survive multi-line values; older
    # letters carry a bare string, and both are accepted -- a letter written
    # by yesterday's version must still verify.
    pairs, pending = [], None
    for ln in block.splitlines():
        t = ln.strip()
        if not t:
            continue
        if t.startswith("#   expected:"):
            if pending is not None:
                raw = t.split("expected:", 1)[1].strip()
                try:
                    want = json.loads(raw)
                    if not isinstance(want, str):
                        want = raw
                except ValueError:
                    want = raw
                pairs.append((pending, want))
                pending = None
        elif t.startswith("#"):
            continue
        else:
            pending = t
    if pending is not None:
        pairs.append((pending, None))
    return pairs


def run(text, cwd=None, timeout=30):
    """Run every check in `text` and return a Verdict. Never raises.

    `cwd` is where the commands run -- the directory the letter was written
    in, when it still exists, because `git rev-parse` in the wrong repository
    answers a question nobody asked.
    """
    pairs = parse(text)
    if pairs is None:
        return Verdict(UNVERIFIABLE)
    if not pairs:
        return Verdict(UNPARSEABLE)

    results = []
    for cmd, expect in pairs:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = []
        if not argv:
            results.append((cmd, False, "unparseable command"))
            continue
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd or None)
            got = (r.stdout or "").strip()
            ran = r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            got, ran = str(e), False
        if not ran:
            ok, why = False, "command failed"
        elif expect is None:
            # Nothing to compare against is a check that cannot fail, and
            # saying so is the whole point of this tool.
            ok, why = False, "no recorded expectation to compare against"
        else:
            ok = got == expect.strip()
            why = "" if ok else f"expected {expect.strip()!r}, got {got!r}"
        results.append((cmd, ok, why))
    status = HOLDS if all(ok for _c, ok, _w in results) else STALE
    return Verdict(status, results)
