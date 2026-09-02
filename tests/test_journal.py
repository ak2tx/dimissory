#!/usr/bin/env python3
"""The declared half, kept current rather than asked for at the end.

Both round-25 reviews reached this independently: a handoff is a journal, not a
last-moment snapshot, because at the moment a letter is most needed the agent
has least capacity to write one.

What is tested here is the part that can go wrong quietly. The journal is
written by SEPARATE CONCURRENT PROCESSES -- every hook invocation is its own
process -- so the failure mode is not a crash, it is losing the agent's words
and never knowing. That is the one thing this module cannot do.

Run: python3 tests/test_journal.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import journal as J                              # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _root():
    return tempfile.mkdtemp(prefix="dim-journal-")


def test_no_two_processes_ever_write_the_same_file():
    """The mechanism, asserted -- because two previous ones measured as fine.

    Text-mode append justified by PIPE_BUF: wrong, PIPE_BUF is a pipe
    guarantee. A single os.write() to an O_APPEND descriptor: measured 960/960
    on Linux, then Windows CI lost 71 of 960 entries and tore one. O_APPEND is
    not an atomicity guarantee for concurrent writers to a regular file.

    So the design no longer has concurrent writers. Each process owns a
    segment, and reading merges them. This asserts that property directly,
    because an outcome test passed on both broken versions.
    """
    root = _root()
    mine = J.path_for("s", root)
    check("a segment is named for the writing process",
          str(os.getpid()) in os.path.basename(mine), mine)
    check("and is unique beyond the pid, since pids are reused",
          len(os.path.basename(mine).split("-")) >= 2, mine)
    check("path_for is stable within one process",
          J.path_for("s", root) == mine)

    # The claim must not survive as a JUSTIFICATION. It may survive as the
    # record of a correction -- that distinction is the point, and a bare
    # substring test cannot see it, so this checks the mechanism's own body.
    src = open(os.path.join(ROOT, "src", "dimissory", "journal.py")).read()
    body = src[src.index("def declare"):src.index("def read")]
    check("the append mechanism no longer cites PIPE_BUF as its guarantee",
          "PIPE_BUF" not in body, "still cited inside declare()")
    check("and the module records why that reasoning was wrong",
          "says nothing about regular files" in src, "correction not recorded")


def test_concurrent_processes_do_not_lose_or_tear_a_line():
    """The reason for append-only, asserted with real processes.

    A read-modify-write would silently drop entries when two hooks overlap.
    This runs twelve processes appending forty lines each and requires every
    one to arrive whole -- on whatever platform is running the suite, which is
    the point: this is the assumption most likely to differ on Windows.
    """
    root = _root()
    # Bigger payloads than the first version used. Review: PIPE_BUF is a pipe
    # guarantee (512 on macOS, 4096 on Linux) and says nothing about regular
    # files, and Python text mode buffers so a write is not one syscall. The
    # original 480/480 was luck. 200-byte entries exercise the real path: one
    # os.write() to an O_APPEND descriptor.
    n, per = 16, 60
    prog = ("import sys; sys.path.insert(0, %r)\n"
            "from dimissory.journal import declare\n"
            "w = sys.argv[1]\n"
            "for i in range(%d):\n"
            "    declare('s', 'decided', 'worker-%%s-entry-%%d-' %% (w, i) + 'x'*200, root=%r)\n"
            % (os.path.join(ROOT, "src"), per, root))
    procs = [subprocess.Popen([sys.executable, "-c", prog, str(w)])
             for w in range(n)]
    for p in procs:
        p.wait(timeout=120)

    segs = J.segments("s", root)
    raw = []
    for seg in segs:
        raw += open(seg, encoding="utf-8").read().splitlines()
    torn = sum(1 for ln in raw if ln.strip() and not _parses(ln))
    check(f"each of the {n} writers got its own segment",
          len(segs) == n, f"{len(segs)} segments for {n} writers")
    check(f"{n} processes x {per} appends all arrive",
          len(raw) == n * per, f"{len(raw)} lines")
    check("and not one line is torn", torn == 0, f"{torn} torn")
    values, _ages, damaged = J.read("s", root)
    check("every entry replays", len(values.get("decided", ())) == n * per,
          len(values.get("decided", ())))
    check("with nothing reported damaged", damaged == 0, damaged)


def _parses(line):
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def test_current_fields_replace_and_accumulating_fields_do_not():
    root = _root()
    J.declare("s", "task", "first task", root=root)
    J.declare("s", "task", "second task", root=root)
    J.declare("s", "next", "step one", root=root)
    J.declare("s", "next", "step two", root=root)
    J.declare("s", "decided", "A", root=root)
    J.declare("s", "decided", "B", root=root)
    values, _a, _d = J.read("s", root)
    check("task keeps only the latest", values["task"] == "second task",
          values.get("task"))
    check("next keeps only the latest", values["next"] == "step two",
          values.get("next"))
    check("decided keeps both, in order", values["decided"] == ("A", "B"),
          values.get("decided"))

    J.declare("s", "decided", "A", root=root)
    values, _a, _d = J.read("s", root)
    check("and a repeated decision is not recorded twice",
          values["decided"] == ("A", "B"), values.get("decided"))


def test_an_undeclared_field_has_no_age_rather_than_a_zero():
    """Zero would read as 'declared just now'. It means 'never declared'."""
    root = _root()
    J.declare("s", "task", "t", root=root, now=1000.0)
    d, age, _dmg = J.to_declared("s", root, sealed_at=1600.0)
    check("a declared field carries its real age", age["task"] == 600.0, age["task"])
    check("an undeclared field's age is None, not 0",
          age["next"] is None, age["next"])
    check("and the Declared reflects only what was said",
          d.task == "t" and d.next_action is None, (d.task, d.next_action))


def test_a_damaged_line_is_skipped_and_counted_not_guessed_at():
    root = _root()
    J.declare("s", "task", "good", root=root)
    with open(J.path_for("s", root), "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps({"ts": 1, "field": "nope", "value": "x"}) + "\n")
        fh.write(json.dumps({"ts": 1, "field": "task", "value": 7}) + "\n")
    values, _a, damaged = J.read("s", root)
    check("the good entry survives", values.get("task") == "good", values)
    check("and every bad line is counted", damaged == 3, damaged)


def test_an_empty_or_unknown_declaration_is_refused_at_the_call():
    root = _root()
    for bad in ("", "   ", None):
        try:
            J.declare("s", "task", bad, root=root)
            check(f"empty task {bad!r} is refused", False, "accepted")
        except J.JournalError:
            check(f"empty task {bad!r} is refused", True)
    try:
        J.declare("s", "wat", "x", root=root)
        check("an unknown field is refused", False, "accepted")
    except J.JournalError as e:
        check("an unknown field is refused", True)
        check("and lists the fields that exist", "task" in str(e), str(e)[:80])


def test_a_session_name_cannot_escape_the_journal_directory():
    """The session name comes from a payload. It is not a path."""
    root = _root()
    # Two acceptable outcomes and no third: the name is REFUSED, or it is
    # contained. What must never happen is a path outside the journal
    # directory, so the assertion is on that, not on which defence fired.
    for hostile in ("../../etc/passwd", "..", "/etc/shadow", "a/../../b",
                    "..\\..\\windows", "con", "x\x00y"):
        try:
            p = J.path_for(hostile, root)
        except J.JournalError:
            check(f"{hostile!r} is refused outright", True)
            continue
        inside = os.path.realpath(p).startswith(os.path.realpath(root) + os.sep)
        check(f"{hostile!r} is contained inside the journal directory",
              inside, p)
    try:
        J.path_for("", root)
        check("an empty session name is refused", False, "accepted")
    except J.JournalError:
        check("an empty session name is refused", True)


def test_reading_a_session_that_never_declared_is_empty_not_an_error():
    root = _root()
    values, ages, damaged = J.read("never", root)
    check("no file means no values", values == {}, values)
    check("no ages", ages == {}, ages)
    check("and nothing damaged", damaged == 0, damaged)
    d, age, _ = J.to_declared("never", root)
    check("the Declared is empty, which renders as DEGRADED",
          d.is_empty(), d)


def test_a_revoked_decision_does_not_survive_into_the_letter():
    """Review: "accumulated decisions and constraints contradict one another
    without revocation."

    Without this, a decision the agent REVERSED an hour ago is sealed into the
    letter as though it still stood, and the next agent acts on it. An
    append-only log cannot delete, so retraction has to be a record of its own.
    """
    root = _root()
    J.declare("s", "decided", "Lock around the whole request path", root=root)
    J.declare("s", "decided", "Lock around refresh only", root=root)
    J.declare("s", "constraint", "Do not touch migrations", root=root)
    values, _a, _d = J.read("s", root)
    check("both decisions are present before revocation",
          len(values["decided"]) == 2, values["decided"])

    J.declare("s", J.REVOKE, "Lock around the whole request path", root=root)
    values, _a, _d = J.read("s", root)
    check("the reversed decision is gone",
          values["decided"] == ("Lock around refresh only",), values["decided"])
    check("the one that still stands remains",
          "Lock around refresh only" in values["decided"])
    check("and an unrelated constraint is untouched",
          values["constraint"] == ("Do not touch migrations",),
          values.get("constraint"))

    # Order must not matter: revoking something declared LATER still works.
    J.declare("s", J.REVOKE, "Declared after its own revocation", root=root)
    J.declare("s", "decided", "Declared after its own revocation", root=root)
    values, _a, _d = J.read("s", root)
    check("a revocation applies regardless of append order",
          "Declared after its own revocation" not in values["decided"],
          values["decided"])

    d, _age, _dmg = J.to_declared("s", root)
    check("and the sealed Declared never sees it",
          "Lock around the whole request path" not in d.decided, d.decided)


def test_a_write_in_flight_is_not_reported_as_damage():
    """Sealing must be deterministic while hooks are still appending.

    Review: "sealing races with a concurrent declaration and produces
    nondeterministic contents." An unterminated final line is a write in
    FLIGHT, not corruption -- counting it as damage would make every seal
    during an active session report a damaged journal, and including half of it
    would put a truncated decision in the letter.
    """
    root = _root()
    J.declare("s", "task", "the real task", root=root)
    J.declare("s", "decided", "a complete decision", root=root)
    with open(J.path_for("s", root), "a", encoding="utf-8") as fh:
        fh.write('{"ts": 1, "field": "decided", "value": "half a deci')  # no \n
    values, _ages, damaged = J.read("s", root)
    check("the partial line is not counted as damage", damaged == 0, damaged)
    check("and its content does not reach the letter",
          all("half a deci" not in v for v in values["decided"]),
          values["decided"])
    check("everything complete is still there",
          values["task"] == "the real task"
          and values["decided"] == ("a complete decision",), values)


def main():
    print("=" * 64)
    print(" the agent declares as it works; the trigger only seals it")
    print("=" * 64)
    for t in (test_no_two_processes_ever_write_the_same_file,
              test_concurrent_processes_do_not_lose_or_tear_a_line,
              test_current_fields_replace_and_accumulating_fields_do_not,
              test_an_undeclared_field_has_no_age_rather_than_a_zero,
              test_a_damaged_line_is_skipped_and_counted_not_guessed_at,
              test_an_empty_or_unknown_declaration_is_refused_at_the_call,
              test_a_session_name_cannot_escape_the_journal_directory,
              test_reading_a_session_that_never_declared_is_empty_not_an_error,
              test_a_revoked_decision_does_not_survive_into_the_letter,
              test_a_write_in_flight_is_not_reported_as_damage):
        t()
    print("\n" + "=" * 64)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 64)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
