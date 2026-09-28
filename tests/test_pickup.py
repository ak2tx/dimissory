#!/usr/bin/env python3
"""The receiving side: does the next session actually get the letter?

Everything before this file was about WRITING a letter before the window
closed. None of it got the letter READ. It landed in ~/.dimissory/letters and
waited for a person to remember it, find it, and paste it into the next model
-- at which point the project got explained from scratch anyway, which is the
one outcome the tool exists to prevent.

So these tests drive the whole loop: a session seals a letter in a directory,
a different session starts in that directory, and the second session is handed
the first one's letter with its Verify block already run. Each test carries
the negative control that makes it able to fail: a different directory, a
letter too old, the feature switched off, a session's own letter.

Run: python3 tests/test_pickup.py
"""
from __future__ import annotations

import io
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import hook as H                                  # noqa: E402
from dimissory import journal as J                               # noqa: E402
from dimissory import letters as L                               # noqa: E402
from dimissory import verify as V                                # noqa: E402
from dimissory.brief import Brief, Check, Declared, Observed     # noqa: E402
from dimissory.cli import main                                   # noqa: E402
from dimissory.config import _toml_str                           # noqa: E402
from dimissory.render import render                              # noqa: E402

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


def _repo(name="one"):
    d = tempfile.mkdtemp(prefix="dim-pick-")
    _git(d, "init", "-q")
    _git(d, "checkout", "-q", "-b", "work")
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# a project\n")
    _git(d, "add", "README.md")
    _git(d, "commit", "-q", "-m", name)
    return d


def _bed(**pickup):
    """A hermetic home: own config, own letters, own journal.

    NOT OPTIONAL. SessionStart reads the configured letters directory, and
    without a config pointing somewhere private these tests would read the
    developer's real ~/.dimissory/letters -- a test whose result is about the
    machine it runs on. test_setup_and_config records the same rule.
    """
    home = tempfile.mkdtemp(prefix="dim-pick-home-")
    letters = os.path.join(home, "letters")
    cfg = os.path.join(home, "config.toml")
    body = f'[letters]\ndir = "{_toml_str(letters)}"\n'
    if pickup:
        body += "[pickup]\n" + "".join(
            f"{k} = {json.dumps(v)}\n" for k, v in pickup.items())
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.environ["DIMISSORY_CONFIG"] = cfg
    return os.path.join(home, "journal"), letters


def _seal_from(session, cwd, jr, letters, task="fix the race",
               nxt="move the release below the assignment", **more):
    """A previous session that declared as it worked, then sealed."""
    J.declare(session, "task", task, root=jr)
    J.declare(session, "next", nxt, root=jr)
    for field, value in more.items():
        J.declare(session, field, value, root=jr)
    return H.seal(session, {"cwd": cwd}, jr, letters)


def _context(out):
    d = json.loads(out) if out else {}
    return (d.get("hookSpecificOutput") or {}).get("additionalContext", "")


def _run(argv, cwd):
    buf = io.StringIO()
    here = os.getcwd()
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main(argv)
    finally:
        os.chdir(here)
    return rc, buf.getvalue()


# -- the letter says where it was written ------------------------------------

def test_a_letter_records_where_and_what_it_was_written_for():
    """Without a directory, a letter cannot be matched to a project; without
    the guides, a Codex session does not know CLAUDE.md exists."""
    if not shutil.which("git"):
        return skip("a letter records its directory", "git not installed")
    d = _repo()
    with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("read me\n")
    jr, letters = _bed()
    path = _seal_from("prev", d, jr, letters)
    text = open(path, encoding="utf-8").read()
    m = L.meta(path)
    check("the observed block records cwd", "\ncwd         " in text, text[:400])
    check("and meta() reads it back as this directory",
          m["cwd"] and L._same_dir(m["cwd"], d), m)
    check("the branch is recorded", m["branch"] == "work", m)
    check("and is a check the reader runs",
          "git rev-parse --abbrev-ref HEAD" in text
          and '#   expected: "work"' in text, text[text.find("## Verify"):][:300])
    check("the project's guide files are listed, in reading order",
          "guides      CLAUDE.md, README.md" in text, text)
    check("the session name is read back", m["session"] == "prev", m)
    check("and the Resume prompt tells the reader to open the guides",
          "guides it lists under Observed" in text)

    # Negative controls: nothing measured, nothing claimed. Scoped to the
    # fenced block, since the Resume prose below it mentions the guides.
    bare = render(Brief(session="s", observed=Observed(head="abc1234"),
                        declared=Declared(task="t"),
                        checks=(Check("true", "x"),)))
    i = bare.find("## Observed")
    a = bare.find("```", i)
    block = bare[a:bare.find("```", a + 3)]
    for absent in ("cwd", "branch", "guides", "agent"):
        check(f"an unmeasured `{absent}` line is omitted", f"\n{absent} " not in block,
              block)
    p = os.path.join(tempfile.mkdtemp(), "bare.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(bare)
    check("and meta() on it says None, not a guess", L.meta(p)["cwd"] is None)


def test_the_newest_letter_for_a_directory_is_found_and_no_other():
    if not shutil.which("git"):
        return skip("newest letter per directory", "git not installed")
    a, b = _repo("a"), _repo("b")
    jr, letters = _bed()
    first_a = _seal_from("pa1", a, jr, letters, task="older task in a")
    time.sleep(1.05)                     # names and mtimes are second-resolution
    later_a = _seal_from("pa2", a, jr, letters, task="newer task in a")
    time.sleep(1.05)
    only_b = _seal_from("pb", b, jr, letters, task="the task in b")
    check("three letters exist", len(os.listdir(letters)) == 3, os.listdir(letters))
    check("directory a gets ITS newest letter, not the newest overall",
          L.latest_for_cwd(letters, a) == later_a,
          (L.latest_for_cwd(letters, a), later_a, only_b))
    check("directory b gets its own", L.latest_for_cwd(letters, b) == only_b)
    check("an unrelated directory gets nothing",
          L.latest_for_cwd(letters, tempfile.mkdtemp()) is None)
    check("and the older letter for a is not chosen",
          L.latest_for_cwd(letters, a) != first_a)
    link = os.path.join(tempfile.mkdtemp(), "link")
    try:
        os.symlink(a, link)
        check("the same directory through a symlink still matches",
              L.latest_for_cwd(letters, link) == later_a)
    except (OSError, NotImplementedError, AttributeError):
        skip("symlinked directory matches", "no symlink support")


# -- the hook hands it over ---------------------------------------------------

def test_a_new_session_in_the_directory_is_handed_the_letter_once():
    if not shutil.which("git"):
        return skip("a new session is handed the letter", "git not installed")
    d = _repo()
    jr, letters = _bed()
    path = _seal_from("prev", d, jr, letters,
                      done="ported the meter", learned="tests need FOO=1",
                      ruled_out="optimistic versioning: needs a migration")
    start = {"hook_event_name": "SessionStart", "session_id": "next", "cwd": d}
    out = H.handle(start, journal_root=jr, letters_dir=letters)
    ctx = _context(out)
    check("SessionStart hands over a letter", "handoff letter" in ctx, ctx[:120])
    check("it says whose it was", "(prev)" in ctx, ctx[:200])
    check("its Verify block was run and holds",
          "Verify block holds" in ctx, ctx[:300])
    check("the letter itself is inline", "----- begin letter -----" in ctx
          and "# Dimissory letter: prev" in ctx)
    for phrase in ("fix the race", "move the release below the assignment",
                   "ported the meter", "tests need FOO=1",
                   "optimistic versioning"):
        check(f"and carries {phrase!r}", phrase in ctx)
    check("the path is named too, for the rest", path in ctx)
    check("the ask to declare still follows, because this session's next "
          "action is its own", "IMPORTANT: before you finish" in ctx)
    check("the letter comes before the ask",
          ctx.index("begin letter") < ctx.index("IMPORTANT"))
    check("the response is named for the event that fired",
          json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart")

    again = _context(H.handle(start, journal_root=jr, letters_dir=letters))
    check("a second SessionStart of the same session is NOT handed it again",
          "begin letter" not in again, again[:120])
    check("but still gets the ask", "IMPORTANT" in again)

    # Negative controls.
    elsewhere = {"hook_event_name": "SessionStart", "session_id": "other",
                 "cwd": tempfile.mkdtemp()}
    ctx2 = _context(H.handle(elsewhere, journal_root=jr, letters_dir=letters))
    check("a session in a different directory gets no letter",
          "begin letter" not in ctx2, ctx2[:120])

    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    ctx3 = _context(H.handle({"hook_event_name": "SessionStart",
                              "session_id": "late", "cwd": d},
                             journal_root=jr, letters_dir=letters))
    check("a letter past pickup.max_age is not offered",
          "begin letter" not in ctx3, ctx3[:120])
    now = time.time()
    os.utime(path, (now, now))

    jr2, letters2 = _bed(enabled=False)
    os.makedirs(letters2)
    shutil.copy(path, letters2)
    check("the disabled bed really holds the letter, so the control can fail",
          L.latest_for_cwd(letters2, d) is not None)
    ctx4 = _context(H.handle({"hook_event_name": "SessionStart",
                              "session_id": "off", "cwd": d},
                             journal_root=jr2, letters_dir=letters2))
    check("pickup.enabled = false switches it off",
          "begin letter" not in ctx4, ctx4[:120])
    check("while the ask is unaffected", "IMPORTANT" in ctx4)


def test_a_stale_letter_is_handed_over_as_stale_not_withheld():
    """A moved world does not make the previous session's Decided and Ruled
    out worthless. It makes Next action suspect, and the reader is told which
    check moved rather than being left to find out."""
    if not shutil.which("git"):
        return skip("a stale letter is reported stale", "git not installed")
    d = _repo()
    jr, letters = _bed()
    _seal_from("prev", d, jr, letters)
    _git(d, "commit", "-q", "--allow-empty", "-m", "moved")
    ctx = _context(H.handle({"hook_event_name": "SessionStart",
                             "session_id": "next", "cwd": d},
                            journal_root=jr, letters_dir=letters))
    check("the letter is still delivered", "begin letter" in ctx, ctx[:120])
    check("and reported STALE", "is STALE" in ctx, ctx[:300])
    check("naming the check that moved",
          "git rev-parse --short HEAD" in ctx.split("begin letter")[0], ctx[:400])
    check("with the advice to re-derive", "re-derive" in ctx)


def test_a_newer_letter_is_delivered_after_a_compaction_restart():
    """PreCompact seals; SessionStart(source=compact) fires with the SAME
    session id. The marker is per letter, not per session, so the letter
    that was just sealed is handed back rather than suppressed."""
    if not shutil.which("git"):
        return skip("a newer letter after compaction", "git not installed")
    d = _repo()
    jr, letters = _bed()
    _seal_from("prev", d, jr, letters, task="the previous session's task")
    start = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": d}
    first = _context(H.handle(start, journal_root=jr, letters_dir=letters))
    check("first start: the previous session's letter",
          "the previous session's task" in first, first[:120])

    J.declare("s", "task", "what THIS session is doing", root=jr)
    J.declare("s", "next", "the step after compaction", root=jr)
    time.sleep(1.05)
    H.handle({"hook_event_name": "PreCompact", "session_id": "s", "cwd": d},
             journal_root=jr, letters_dir=letters)
    check("PreCompact sealed a second letter", len(os.listdir(letters)) == 2,
          os.listdir(letters))
    second = _context(H.handle(start, journal_root=jr, letters_dir=letters))
    check("restart after compaction: handed ITS OWN fresh letter",
          "what THIS session is doing" in second
          and "the step after compaction" in second, second[:200])
    check("not the old one", "the previous session's task" not in second)
    check("and no ask, since this session has declared",
          "IMPORTANT" not in second)
    third = H.handle(start, journal_root=jr, letters_dir=letters)
    check("a third start with nothing newer says nothing", third == "", third[:80])


def test_userpromptsubmit_never_reads_a_session_its_own_letter_back():
    """Registered on no host by default, but the branch exists, and if it is
    ever wired up it must not turn every seal into a re-read on the next
    prompt."""
    if not shutil.which("git"):
        return skip("UserPromptSubmit and own letters", "git not installed")
    d = _repo()
    jr, letters = _bed()
    _seal_from("u", d, jr, letters, task="u's own work")
    mine = _context(H.handle({"hook_event_name": "UserPromptSubmit",
                              "session_id": "u", "cwd": d},
                             journal_root=jr, letters_dir=letters))
    check("a session is not handed its own letter on a prompt",
          "begin letter" not in mine, mine[:120])
    out = H.handle({"hook_event_name": "UserPromptSubmit", "session_id": "v",
                    "cwd": d}, journal_root=jr, letters_dir=letters)
    theirs = _context(out)
    check("while a different session is", "u's own work" in theirs, theirs[:120])
    check("and the response is named UserPromptSubmit",
          json.loads(out)["hookSpecificOutput"]["hookEventName"]
          == "UserPromptSubmit")
    again = _context(H.handle({"hook_event_name": "UserPromptSubmit",
                               "session_id": "v", "cwd": d},
                              journal_root=jr, letters_dir=letters))
    check("once", "begin letter" not in again, again[:120])


def test_the_hook_survives_a_letters_directory_it_cannot_read():
    jr, letters = _bed()
    with open(letters, "w", encoding="utf-8") as fh:      # a FILE, not a dir
        fh.write("not a directory\n")
    try:
        out = H.handle({"hook_event_name": "SessionStart", "session_id": "x",
                        "cwd": tempfile.mkdtemp()},
                       journal_root=jr, letters_dir=letters)
        check("an unreadable letters dir costs the pickup, not the session",
              isinstance(out, str) and "IMPORTANT" in _context(out), out[:80])
    except Exception as e:                                  # noqa: BLE001
        check("an unreadable letters dir costs the pickup, not the session",
              False, f"{type(e).__name__}: {e}")


# -- by hand ------------------------------------------------------------------

def test_dim_pickup_prints_the_letter_and_its_verdict():
    if not shutil.which("git"):
        return skip("dim pickup", "git not installed")
    d = _repo()
    jr, letters = _bed()
    _seal_from("prev", d, jr, letters)
    rc, out = _run(["--dir", letters, "pickup"], cwd=d)
    check("pickup in the directory exits 0 while the world is unmoved",
          rc == 0, f"exit {rc}: {out[-200:]}")
    check("it prints the letter", "# Dimissory letter: prev" in out, out[:120])
    check("then the verification", "still holds" in out, out[-160:])
    check("letter before verdict", out.index("Dimissory letter") < out.index("holds"))

    _git(d, "commit", "-q", "--allow-empty", "-m", "moved")
    rc, out = _run(["--dir", letters, "pickup"], cwd=d)
    check("a moved world makes pickup exit 2", rc == 2, f"exit {rc}")
    check("and it still printed the letter first",
          "# Dimissory letter: prev" in out and "STALE" in out, out[-200:])

    rc, out = _run(["--dir", letters, "pickup"], cwd=tempfile.mkdtemp())
    check("pickup somewhere with no letter exits 1, never another project's",
          rc == 1, f"exit {rc}")
    check("and says so", "no letter records this directory" in out, out[-160:])


def test_dim_resume_prefers_the_letter_for_this_directory():
    """`dim resume` used to take the newest letter from ANYWHERE, so standing
    in project A it happily verified project B's letter and, since git ran in
    A, reported B's letter stale. Wrong project, confident verdict."""
    if not shutil.which("git"):
        return skip("resume prefers this directory", "git not installed")
    a, b = _repo("a"), _repo("b")
    jr, letters = _bed()
    pa = _seal_from("pa", a, jr, letters)
    time.sleep(1.05)
    pb = _seal_from("pb", b, jr, letters)
    rc, out = _run(["--dir", letters, "resume"], cwd=a)
    check("standing in a, resume verifies a's letter", pa in out and pb not in out,
          out[-200:])
    check("and it holds", rc == 0, f"exit {rc}: {out[-200:]}")
    rc, out = _run(["--dir", letters, "resume"], cwd=b)
    check("standing in b, b's", pb in out and rc == 0, out[-200:])
    rc, out = _run(["--dir", letters, "resume"], cwd=tempfile.mkdtemp())
    check("with no letter for this directory it falls back to the newest, "
          "and says where it is running the checks",
          pb in out and "recorded in the letter" in out, out[-260:])
    check("which still holds, because the checks ran in b", rc == 0, rc)


# -- the declared half grew two fields ---------------------------------------

def test_done_and_learned_are_declared_rendered_and_attributed():
    jr = tempfile.mkdtemp(prefix="dim-pick-j-")
    rc, out = _run(["--journal", jr, "declare", "--session", "s",
                    "--done", "ported the meter", "--done", "wired the hook",
                    "--learned", "the flaky test is test_x; ignore it"],
                   cwd=ROOT)
    check("`dim declare --done/--learned` is accepted", rc == 0, out)
    d, _ages, _dmg = J.to_declared("s", jr)
    check("done accumulates in order",
          d.done == ("ported the meter", "wired the hook"), d.done)
    check("learned is recorded", d.learned == ("the flaky test is test_x; ignore it",))
    check("a brief with only these is not degraded", not d.is_empty())
    text = render(Brief(session="s", observed=Observed(head="abc1234"),
                        declared=d, checks=(Check("true", "x"),)))
    for label in ("Done", "Learned"):
        i = text.find(f"## {label}")
        check(f"`{label}` is rendered and attributed on its heading line",
              i != -1 and "the agent's own words" in text[i:text.find("\n", i)],
              text[i:i + 60] if i != -1 else "absent")
    check("done is rendered before learned",
          text.find("## Done") < text.find("## Learned"), text)
    check("the ask now names both",
          "--done" in H.ASK and "--learned" in H.ASK)


# -- the verifier, now one owner ---------------------------------------------

def test_the_verifier_answers_in_exactly_four_ways():
    check("no block is unverifiable",
          V.run("# a letter\n\nno checks here\n").status == V.UNVERIFIABLE)
    check("a block with nothing in it is unparseable",
          V.run("## Verify first\n\n```\n\n```\n").status == V.UNPARSEABLE)
    if not shutil.which("git"):
        return skip("the verifier holds and fails", "git not installed")
    # A git command rather than an interpreter path: shlex.split is POSIX
    # everywhere by design, and a Windows path in the command would lose its
    # backslashes. Letters only ever carry git commands, so that is what is
    # tested.
    d = _repo()
    head = _git(d, "rev-parse", "--short", "HEAD").stdout.strip()
    good = render(Brief(session="s", observed=Observed(head=head),
                        declared=Declared(task="t"),
                        checks=(Check("git rev-parse --short HEAD", head),)))
    v = V.run(good, cwd=d)
    check("a check whose output matches holds",
          v.status == V.HOLDS and v.exit_code == 0 and v.total == 1, v)
    check("and says so in a sentence", "holds" in v.sentence(), v.sentence())
    bad = render(Brief(session="s", observed=Observed(head=head),
                       declared=Declared(task="t"),
                       checks=(Check("git rev-parse --short HEAD", "nope"),)))
    v = V.run(bad, cwd=d)
    check("a mismatch is stale, exit 2",
          v.status == V.STALE and v.exit_code == 2 and v.stale == 1, v)
    check("and the sentence names what was expected and what came back",
          "'nope'" in v.sentence() and f"'{head}'" in v.sentence(),
          v.sentence())
    check("cwd is honoured: the same letter fails in another repository",
          V.run(good, cwd=_repo("other")).status == V.STALE)
    stripped = "\n".join(l for l in good.splitlines()
                         if not l.startswith("#   expected:"))
    v = V.run(stripped, cwd=d)
    check("no recorded expectation is NOT a pass", v.status == V.STALE, v)
    check("and it says why", "no recorded expectation" in v.results[0][2])

    src = open(os.path.join(ROOT, "src", "dimissory", "verify.py"),
               encoding="utf-8").read()
    check("checks never run through a shell", "shell=True" not in src)
    check("they are split with shlex", "shlex.split" in src)


def main_():
    print("=" * 66)
    print(" the receiving side: the next session is handed the letter")
    print("=" * 66)
    for t in (test_a_letter_records_where_and_what_it_was_written_for,
              test_the_newest_letter_for_a_directory_is_found_and_no_other,
              test_a_new_session_in_the_directory_is_handed_the_letter_once,
              test_a_stale_letter_is_handed_over_as_stale_not_withheld,
              test_a_newer_letter_is_delivered_after_a_compaction_restart,
              test_userpromptsubmit_never_reads_a_session_its_own_letter_back,
              test_the_hook_survives_a_letters_directory_it_cannot_read,
              test_dim_pickup_prints_the_letter_and_its_verdict,
              test_dim_resume_prefers_the_letter_for_this_directory,
              test_done_and_learned_are_declared_rendered_and_attributed,
              test_the_verifier_answers_in_exactly_four_ways):
        t()
    os.environ.pop("DIMISSORY_CONFIG", None)
    print("\n" + "=" * 66)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    if SKIPPED:
        print(f" SKIPPED {len(SKIPPED)}: {'; '.join(SKIPPED)}")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main_())
