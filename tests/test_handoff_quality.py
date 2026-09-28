#!/usr/bin/env python3
"""What the receiving model actually gets to read, made better.

Three changes, each aimed at a letter that transfers less than it could:

  the declared half   a quality floor at `dim declare` (placeholders, bare
                      words, a next action that restates the task), and a
                      nudge after a commit asking for `--done` -- the one
                      moment a hook can see that something was finished.
  the observed half   `git diff --shortstat` and the last shell command
                      with the host's own error flag. Both free, both true.
  dead settings       `letters.keep` now prunes, and `agents.<name> = false`
                      now silences an installed hook, instead of both
                      promising behaviour nothing implemented.

Each test carries the negative control that lets it fail.

Run: python3 tests/test_handoff_quality.py
"""
from __future__ import annotations

import contextlib
import io
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
from dimissory.brief import Brief, Check, Declared, Observed     # noqa: E402
from dimissory.cli import main                                   # noqa: E402
from dimissory.config import _toml_str                           # noqa: E402
from dimissory.observe import observe                            # noqa: E402
from dimissory.render import render                              # noqa: E402
from dimissory.transcript import last_command                    # noqa: E402

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
    d = tempfile.mkdtemp(prefix="dim-q-")
    _git(d, "init", "-q")
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("one\ntwo\n")
    _git(d, "add", "a.txt")
    _git(d, "commit", "-q", "-m", "first")
    return d


def _bed(**sections):
    """A hermetic config. `sections` maps a section name to its keys."""
    home = tempfile.mkdtemp(prefix="dim-q-home-")
    letters = os.path.join(home, "letters")
    body = f'[letters]\ndir = "{_toml_str(letters)}"\n'
    for section, items in sections.items():
        if section == "letters":
            body += "".join(f"{k} = {json.dumps(v)}\n" for k, v in items.items())
            continue
        body += f"[{section}]\n" + "".join(
            f"{k} = {json.dumps(v)}\n" for k, v in items.items())
    cfg = os.path.join(home, "config.toml")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.environ["DIMISSORY_CONFIG"] = cfg
    return os.path.join(home, "journal"), letters


def _declare(jr, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = main(["--journal", jr, "declare", "--session", "s", *argv])
    return rc, buf.getvalue()


def _context(out):
    d = json.loads(out) if out else {}
    return (d.get("hookSpecificOutput") or {}).get("additionalContext", "")


def _rollout(d, used=40.0):
    """A Codex rollout under the margin, so the meter says 'plenty left'.

    Passed as the transcript so the window read is scoped to Codex: without
    one, the meter also consults ~/.grok, and a developer box with Grok past
    85% would seal instead of nudging -- a test result about the machine.
    """
    p = os.path.join(d, "rollout-q.jsonl")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 5))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 300,
                            "resets_at": time.time() + 3600}}}}) + "\n")
    return p


# -- the declared half: a floor ----------------------------------------------

def test_a_placeholder_or_a_bare_word_is_not_a_declaration():
    jr = tempfile.mkdtemp(prefix="dim-q-j-")
    for label, argv, why in (
            ("the ask's placeholder, pasted verbatim",
             ["--task", "<one line: what this session is for>"], "placeholder"),
            ("a one-word next action", ["--next", "fix"], "too short"),
            ("a two-word task", ["--task", "the parser"], "too short"),
            ("a placeholder in an accumulating field",
             ["--done", "<what this commit finished>"], "placeholder")):
        rc, out = _declare(jr, *argv)
        check(f"{label} is refused", rc == 1, f"rc={rc} {out[-120:]}")
        check(f"{label}: and says why", why in out, out[-160:])
    values, _a, _d = J.read("s", jr)
    check("and nothing at all reached the journal", values == {}, values)

    rc, out = _declare(jr, "--task", "port the plan-window meter",
                       "--next", "run the ten-task trial on two models")
    check("a real task and next action are accepted", rc == 0, out)
    rc, out = _declare(jr, "--done", "wired the hook")
    check("a short --done is fine; only task and next need three words",
          rc == 0, out)


def test_a_next_action_that_restates_the_task_is_refused():
    jr = tempfile.mkdtemp(prefix="dim-q-j-")
    rc, out = _declare(jr, "--task", "Fix the token refresh race",
                       "--next", "fix the token refresh race")
    check("next == task (ignoring case), given together, is refused",
          rc == 1 and "repeats the task" in out, out[-160:])
    check("and the task given alongside it was not recorded either",
          J.read("s", jr)[0] == {}, J.read("s", jr)[0])

    rc, _ = _declare(jr, "--task", "Fix the token refresh race")
    check("the task alone is recorded", rc == 0)
    rc, out = _declare(jr, "--next", "Fix the token refresh race")
    check("a later --next is compared against the task already in the "
          "journal", rc == 1 and "repeats the task" in out, out[-160:])
    rc, out = _declare(jr, "--next", "move the lock below the assignment")
    check("while a real next step is accepted", rc == 0, out)


def test_revoke_is_never_judged():
    """A revocation must match the original verbatim, whatever it was --
    including a short entry recorded before the floor existed."""
    jr = tempfile.mkdtemp(prefix="dim-q-j-")
    J.declare("s", "next", "fix", root=jr)          # written directly, pre-floor
    rc, out = _declare(jr, "--revoke", "fix")
    check("a short entry can still be revoked", rc == 0, out)
    check("and is gone", "next" not in J.read("s", jr)[0], J.read("s", jr)[0])


# -- the declared half: a nudge after a commit -------------------------------

def test_a_commit_is_followed_by_one_ask_for_done():
    jr, letters = _bed()
    d = tempfile.mkdtemp(prefix="dim-q-n-")
    roll = _rollout(d)

    def tool(command, sid="n1", event="PostToolUse"):
        return H.handle({"hook_event_name": event, "session_id": sid,
                         "transcript_path": roll, "cwd": d,
                         "tool_name": "Bash",
                         "tool_input": {"command": command}},
                        journal_root=jr, letters_dir=letters)

    out = tool("git commit -m 'lock around refresh'")
    ctx = _context(out)
    check("a commit produces a nudge", "that was a commit" in ctx, ctx[:120])
    check("naming --done with a runnable command",
          "--done" in ctx and "declare --session n1" in ctx, ctx)
    check("addressed to the event that fired",
          json.loads(out)["hookSpecificOutput"]["hookEventName"]
          == "PostToolUse")
    check("and nothing was sealed, since the window is at 40%",
          not os.path.isdir(letters) or os.listdir(letters) == [])

    check("a second commit inside the interval says nothing",
          tool("git commit -m again") == "")
    check("but a different session is nudged on its own clock",
          "that was a commit" in _context(tool("git commit -m x", sid="n2")))

    for label, command in (("git log --grep commit", "git log --grep commit"),
                           ("git --no-pager log --grep commit",
                            "git --no-pager log --grep commit"),
                           ("an echo mentioning commit", "echo commit"),
                           ("git status", "git status")):
        check(f"{label} is not a commit",
              tool(command, sid=f"neg-{label}") == "", label)
    for i, command in enumerate(("git -C /x commit -m y",
                                 "git -c user.name=t commit -m y",
                                 "git --git-dir=/x/.git commit -m y",
                                 "cd repo && git commit -am y")):
        check(f"{command!r} IS a commit",
              "that was a commit" in _context(tool(command, sid=f"flags{i}")),
              command)
    check("a commit that FAILED is not progress, so no nudge",
          tool("git commit -m z", sid="failed",
               event="PostToolUseFailure") == "")
    check("an edit with no command says nothing",
          H.handle({"hook_event_name": "PostToolUse", "session_id": "edit",
                    "transcript_path": roll, "cwd": d, "tool_name": "Edit",
                    "tool_input": {"file_path": "x"}},
                   journal_root=jr, letters_dir=letters) == "")


def test_a_seal_notice_is_never_displaced_by_a_nudge():
    """The window check decides first. A commit on the same tool call that
    crosses the margin must report the seal, not the nudge."""
    jr, letters = _bed()
    d = tempfile.mkdtemp(prefix="dim-q-s-")
    roll = _rollout(d, used=92.0)
    out = H.handle({"hook_event_name": "PostToolUse", "session_id": "seal",
                    "transcript_path": roll, "cwd": d, "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m done"}},
                   journal_root=jr, letters_dir=letters)
    ctx = _context(out)
    check("past the margin, the seal is what the agent is told",
          "handoff letter was sealed" in ctx, ctx[:160])
    check("not the nudge", "that was a commit" not in ctx, ctx[:160])


# -- the observed half --------------------------------------------------------

def test_the_size_of_uncommitted_work_is_measured():
    if not shutil.which("git"):
        return skip("diff --shortstat is measured", "git not installed")
    d = _repo()
    clean = observe(cwd=d)
    check("a clean tree measures an empty diff, not an unmeasured one",
          clean.diff_stat == "", repr(clean.diff_stat))
    block = render(Brief(session="s", observed=clean,
                         declared=Declared(task="t"),
                         checks=(Check("true", "x"),)))
    check("and the letter prints no 'changes' line for it",
          "\nchanges " not in block, block)

    with open(os.path.join(d, "a.txt"), "a", encoding="utf-8") as fh:
        fh.write("three\nfour\n")
    dirty = observe(cwd=d)
    check("a modified file is measured in git's own words",
          "1 file changed" in dirty.diff_stat
          and "2 insertions" in dirty.diff_stat, dirty.diff_stat)
    text = render(Brief(session="s", observed=dirty,
                        declared=Declared(task="t"),
                        checks=(Check("true", "x"),)))
    check("and printed in the observed block",
          f"changes     {dirty.diff_stat}" in text, text)

    outside = observe(cwd=tempfile.mkdtemp(prefix="dim-q-nogit-"))
    check("outside a repository it is not measured at all",
          "diff_stat" not in outside.known(), outside.known())


def test_the_last_command_and_whether_it_failed():
    d = tempfile.mkdtemp(prefix="dim-q-t-")
    t = os.path.join(d, "session.jsonl")

    def use(i, command, description=None):
        args = {"command": command}
        if description:
            args["description"] = description
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash",
             "input": args}]}}

    def result(i, error):
        return {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}",
             "is_error": error, "content": "..."}]}}

    def write(*records):
        with open(t, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    write(use(1, "ls"), result(1, False),
          use(2, "pytest -x tests/", "run the unit tests"), result(2, True),
          {"type": "assistant", "message": {"content": [
              {"type": "tool_use", "id": "t3", "name": "Read",
               "input": {"file_path": "x.py"}}]}})
    got = last_command(t)
    check("the last COMMAND is found, not the last tool call",
          got and got["hint"] == "run the unit tests", got)
    check("with the host's error flag", got and got["failed"] is True, got)

    write(use(1, "pytest"), result(1, True), use(2, "make build"))
    got = last_command(t)
    check("a command still in flight is reported without a verdict",
          got and got["hint"] == "make build" and got["failed"] is None, got)

    write(use(1, "git status"), result(1, False))
    check("a clean result reads as not failed",
          last_command(t)["failed"] is False, last_command(t))

    write({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "r", "name": "Read",
         "input": {"file_path": "x"}}]}})
    check("a transcript with no commands is None, not an empty command",
          last_command(t) is None, last_command(t))
    check("an unreadable path is None",
          last_command(os.path.join(d, "absent.jsonl")) is None)

    write(use(1, "npm test", "run the tests"), result(1, True))
    o = observe(cwd=d, transcript=t)
    text = render(Brief(session="s", observed=o, declared=Declared(task="t"),
                        checks=(Check("true", "x"),)))
    check("the letter says the last command FAILED",
          "last cmd    run the tests  -> FAILED" in text, text)
    check("and never invents an exit code",
          "exit" not in text.split("## Observed")[1].split("## Resume")[0],
          text)

    write(use(1, "npm test", "run the tests"))
    o = observe(cwd=d, transcript=t)
    text = render(Brief(session="s", observed=o, declared=Declared(task="t"),
                        checks=(Check("true", "x"),)))
    check("an in-flight command is named with no verdict at all",
          "last cmd    run the tests\n" in text, text)


# -- the settings that did nothing --------------------------------------------

def test_letters_keep_prunes_the_oldest_and_nothing_else():
    d = tempfile.mkdtemp(prefix="dim-q-k-")
    now = time.time()
    made = []
    for i in range(4):
        p = L.write(d, f"s{i}", f"# Dimissory letter: s{i}\nbody {i}\n")
        os.utime(p, (now - 400 + i * 100, now - 400 + i * 100))
        made.append(p)
    newest = L.write(d, "s4", "# Dimissory letter: s4\nbody 4\n", keep=3)
    left = sorted(os.listdir(d))
    check("keep = 3 leaves exactly three letters", len(left) == 3, left)
    check("the two oldest are the ones removed",
          not os.path.exists(made[0]) and not os.path.exists(made[1]), left)
    check("the one just written survives", os.path.exists(newest), left)

    for bad in (0, -1, True, "5", None, 2.5):
        d2 = tempfile.mkdtemp(prefix="dim-q-k2-")
        for i in range(4):
            L.write(d2, f"s{i}", f"# Dimissory letter: s{i}\nbody {i}\n",
                    keep=bad)
        check(f"keep = {bad!r} keeps everything, never deletes them all",
              len(os.listdir(d2)) == 4, os.listdir(d2))

    d3 = tempfile.mkdtemp(prefix="dim-q-k3-")
    p = L.write(d3, "only", "# Dimissory letter: only\n")
    os.utime(p, (now - 9999, now - 9999))
    check("the just-written letter is spared even when it is oldest",
          L.prune(d3, 1, spare=p) == [] and os.path.exists(p))


def test_the_hook_prunes_to_the_configured_keep():
    if not shutil.which("git"):
        return skip("the hook honours letters.keep", "git not installed")
    jr, letters = _bed(letters={"keep": 2})
    d = _repo()
    now = time.time()
    for i in range(4):
        J.declare(f"k{i}", "task", f"task number {i} of four", root=jr)
        p = H.seal(f"k{i}", {"cwd": d}, jr, None)
        os.utime(p, (now - 400 + i * 100, now - 400 + i * 100))
    left = sorted(os.listdir(letters))
    check("the hook, with no letters_dir passed, pruned to keep = 2",
          len(left) == 2, left)
    check("and kept the two newest",
          left and all(n.startswith(("k2-", "k3-")) for n in left), left)


def test_a_disabled_agent_is_silent_even_with_its_hook_installed():
    codex_t = "/home/u/.codex/sessions/2026/rollout-abc.jsonl"
    claude_t = "/home/u/.claude/projects/p/abc.jsonl"

    jr, letters = _bed(agents={"codex": False})
    start = {"hook_event_name": "SessionStart", "session_id": "off",
             "transcript_path": codex_t, "cwd": tempfile.mkdtemp()}
    check("agents.codex = false: a Codex SessionStart says nothing",
          H.handle(start, journal_root=jr, letters_dir=letters) == "")
    stop = dict(start, hook_event_name="Stop")
    check("and its Stop gate does not block",
          H.handle(stop, journal_root=jr, letters_dir=letters) == "")
    check("while Claude, still enabled, is asked as usual",
          "IMPORTANT" in _context(H.handle(
              dict(start, transcript_path=claude_t, session_id="on"),
              journal_root=jr, letters_dir=letters)))
    check("and a payload with no transcript is never guessed at",
          "IMPORTANT" in _context(H.handle(
              {"hook_event_name": "SessionStart", "session_id": "anon"},
              journal_root=jr, letters_dir=letters)))

    jr2, letters2 = _bed()
    check("with the default config, Codex is asked (the control)",
          "IMPORTANT" in _context(H.handle(
              dict(start, session_id="default"),
              journal_root=jr2, letters_dir=letters2)))


def main_():
    print("=" * 66)
    print(" what the receiving model gets: a better letter, and honest settings")
    print("=" * 66)
    for t in (test_a_placeholder_or_a_bare_word_is_not_a_declaration,
              test_a_next_action_that_restates_the_task_is_refused,
              test_revoke_is_never_judged,
              test_a_commit_is_followed_by_one_ask_for_done,
              test_a_seal_notice_is_never_displaced_by_a_nudge,
              test_the_size_of_uncommitted_work_is_measured,
              test_the_last_command_and_whether_it_failed,
              test_letters_keep_prunes_the_oldest_and_nothing_else,
              test_the_hook_prunes_to_the_configured_keep,
              test_a_disabled_agent_is_silent_even_with_its_hook_installed):
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
