#!/usr/bin/env python3
"""The hook, and the installer that writes it into files we do not own.

The mechanism was established by driving all three real CLIs, and these tests
hold the properties those runs depend on. The measured results:

    Claude Code   SessionStart ask   complied on haiku and sonnet-5
    Codex         SessionStart ask   complied 4 of 4
    Grok          Stop gate          complied; its SessionStart ask was ignored

Two payload conventions have to be read, because Claude Code and Codex send
snake_case and Grok sends camelCase with no transcript path at all. A hook that
understands one convention fires, learns nothing, and reports success -- this
project's recurring defect wearing a new hat.

The installer's rules come from the predecessor destroying a real
~/.claude/settings.json: refuse what will not parse, back up, ask, merge, and
be idempotent.

Run: python3 tests/test_hook.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import hook as H                                  # noqa: E402
from dimissory import install as I                               # noqa: E402
from dimissory import journal as J                               # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _json(out):
    """Parse a hook response, or None. Never raises.

    A test that does `json.loads(out)` on an empty response crashes the whole
    file instead of reporting one failure -- which is how a negative control
    stops being readable, and it is the same lesson this suite records
    elsewhere: the harness is a measurement too.
    """
    try:
        return json.loads(out) if out else None
    except ValueError:
        return None


def _root():
    return tempfile.mkdtemp(prefix="dim-hook-")


# -- the hook ---------------------------------------------------------------

def test_both_payload_conventions_are_understood():
    """Claude/Codex send snake_case; Grok sends camelCase. Both are real."""
    claude = {"hook_event_name": "SessionStart", "session_id": "s1",
              "transcript_path": "/t.jsonl", "cwd": "/w"}
    grok = {"hookEventName": "session_start", "sessionId": "s1",
            "workspaceRoot": "/w", "timestamp": "now"}
    for label, p in (("snake_case", claude), ("camelCase", grok)):
        check(f"{label}: the event is recognised",
              H.normalise_event(H.field(p, "event")) == "sessionstart",
              H.field(p, "event"))
        check(f"{label}: the session id is found", H.field(p, "session") == "s1")
        check(f"{label}: a cwd is found", H.field(p, "cwd") == "/w")
    check("a missing transcript is None, not a guess",
          H.field(grok, "transcript") is None)
    for raw in ("SessionStart", "session-start", "session_start"):
        check(f"{raw!r} normalises to one event",
              H.normalise_event(raw) == "sessionstart")


def test_the_ask_names_a_command_the_agent_can_actually_run():
    """Measured: a bare `dim` was not on the AGENT's PATH, so the hook fired,
    injected a correct-looking instruction, and nothing happened. An
    instruction the reader cannot execute is the same as no instruction."""
    cmd = H.dim_command()
    check("the command is absolute or an explicit interpreter call",
          os.path.isabs(cmd.split()[0]), cmd)
    out = H.handle({"hook_event_name": "SessionStart", "session_id": "s9"},
                   journal_root=_root())
    d = _json(out)
    check("SessionStart produced an ask at all", d is not None, repr(out)[:60])
    body = (d or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    check("the ask carries that resolved command", cmd in body, body[:120])
    check("it names the session, so declarations land under the right key",
          "s9" in body)
    check("and it leads with the imperative, not an explanation",
          body.strip().startswith("IMPORTANT"), body[:60])


def test_the_gate_blocks_only_when_nothing_was_declared():
    root = _root()
    stop = {"hookEventName": "Stop", "sessionId": "g1", "cwd": "/w"}

    d = _json(H.handle(stop, journal_root=root))
    check("an undeclared session is blocked",
          (d or {}).get("decision") == "block", repr(d)[:70])
    check("and told exactly what to run",
          "declare" in (d or {}).get("reason", ""), repr(d)[:70])

    J.declare("g1", "task", "something", root=root)
    check("once declared, the turn is allowed to end",
          H.handle(stop, journal_root=root) == "", "still blocking")


def test_the_gate_never_traps_the_agent_in_a_loop():
    """`stopHookActive` means the agent is ALREADY continuing because of a
    stop hook. Blocking again from there is how a gate becomes a trap."""
    root = _root()
    looping = {"hookEventName": "Stop", "sessionId": "never-declares",
               "stopHookActive": True}
    check("a second block is refused even with nothing declared",
          H.handle(looping, journal_root=root) == "")
    first = _json(H.handle({"hookEventName": "Stop",
                            "sessionId": "never-declares"}, journal_root=root))
    check("while the first would have blocked",
          (first or {}).get("decision") == "block", repr(first)[:70])


def test_a_session_that_is_already_declaring_is_not_nagged():
    root = _root()
    J.declare("s2", "task", "already going", root=root)
    check("SessionStart stays quiet once the journal has content",
          H.handle({"hook_event_name": "SessionStart", "session_id": "s2"},
                   journal_root=root) == "")


def test_the_hook_never_raises_and_never_blocks_the_user():
    """A hook that fails must cost one observation, not the user's work."""
    for bad in ({}, {"hook_event_name": "SessionStart"}, {"session_id": "x"},
                {"hook_event_name": 42, "session_id": ["nope"]},
                {"hookEventName": "Stop", "sessionId": None}):
        try:
            out = H.handle(bad, journal_root=_root())
            check(f"{str(bad)[:38]}: handled without raising",
                  isinstance(out, str))
        except Exception as e:                            # noqa: BLE001
            check(f"{str(bad)[:38]}: handled without raising", False,
                  f"{type(e).__name__}: {e}")
    import io
    check("main() with garbage on stdin exits 0",
          H.main(stdin=io.StringIO("not json at all")) == 0)
    check("main() with an empty stdin exits 0",
          H.main(stdin=io.StringIO("")) == 0)


# -- the installer ----------------------------------------------------------

def test_detection_keys_match_the_install_targets():
    """The predecessor detected into one key space and queried another, so the
    step that mattered never ran while setup reported success."""
    check("detect() and TARGETS agree exactly",
          set(I.detect()) == set(I.TARGETS),
          f"{sorted(I.detect())} vs {sorted(I.TARGETS)}")


def test_an_unparseable_or_wrongly_shaped_file_is_never_rewritten():
    """The bug that destroyed a real settings.json."""
    for label, body in (("trailing garbage", '{"theme":"dark","OOPS"}'),
                        ("not an object", '["a","b"]'),
                        ("truncated", '{"hooks": {'),
                        ("hooks is a list", '{"hooks": []}'),
                        ("hooks is a string", '{"hooks": "nope"}')):
        d = tempfile.mkdtemp(prefix="dim-inst-")
        p = os.path.join(d, "settings.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        before = open(p, encoding="utf-8").read()
        try:
            I.plan("claude", "dim hook", p)
            check(f"{label}: refused", False, "it planned an edit")
        except I.InstallRefused as e:
            check(f"{label}: refused", True)
            check(f"{label}: says nothing was changed",
                  "Nothing was changed" in str(e) or "Refusing" in str(e),
                  str(e)[:70])
        check(f"{label}: file untouched", open(p, encoding="utf-8").read() == before)


def test_unrelated_keys_and_existing_hooks_survive():
    d = tempfile.mkdtemp(prefix="dim-inst-")
    p = os.path.join(d, "settings.json")
    original = {"theme": "dark", "agentPushNotifEnabled": True,
                "hooks": {"PreToolUse": [{"hooks": [{"type": "command",
                                                     "command": "other-tool"}]}]}}
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(original, fh)
    _existing, merged, added = I.plan("claude", "dim hook", p)
    for k in ("theme", "agentPushNotifEnabled"):
        check(f"`{k}` survives", merged.get(k) == original[k])
    check("somebody else's PreToolUse hook survives",
          merged["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"],
          merged["hooks"].get("PreToolUse"))
    check("and ours were added", "SessionStart" in added, added)


def test_installing_twice_adds_nothing_the_second_time():
    d = tempfile.mkdtemp(prefix="dim-inst-")
    p = os.path.join(d, "settings.json")
    out = []
    I.install("claude", path=p, assume_yes=True, out=out.append,
                  verify=False)
    first = json.load(open(p, encoding="utf-8"))
    I.install("claude", path=p, assume_yes=True, out=out.append,
                  verify=False)
    second = json.load(open(p, encoding="utf-8"))
    check("the file is unchanged by a second install", first == second)
    n = sum(len(v) for v in second["hooks"].values())
    check("and hooks are not doubled",
          n == len(I.TARGETS["claude"]["events"]), n)


def test_declining_changes_nothing_and_is_not_success():
    d = tempfile.mkdtemp(prefix="dim-inst-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)
    before = open(p, encoding="utf-8").read()
    path, added = I.install("claude", path=p, assume_yes=False, verify=False,
                            out=lambda *_a: None, ask=lambda _p: "n")
    check("declining returns no path", path is None, path)
    check("and nothing was added", added == [], added)
    check("and the file is byte-identical",
          open(p, encoding="utf-8").read() == before)


def test_the_previous_file_is_kept_and_never_overwritten():
    """The predecessor's second install copied its own output over the first
    backup, destroying the only thing worth keeping."""
    d = tempfile.mkdtemp(prefix="dim-inst-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "THE ORIGINAL"}, fh)
    I.install("claude", path=p, assume_yes=True, out=lambda *_a: None,
              verify=False)
    b1 = p + ".dim-backup"
    check("a backup exists", os.path.exists(b1))
    check("holding what was there", "THE ORIGINAL" in open(b1, encoding="utf-8").read())

    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "edited since"}, fh)
    I.install("claude", path=p, assume_yes=True, out=lambda *_a: None,
              verify=False)
    check("the first backup still holds the original",
          "THE ORIGINAL" in open(b1, encoding="utf-8").read(),
          open(b1, encoding="utf-8").read()[:60])


def test_a_leftover_backup_name_cannot_cost_the_original():
    """The case that got past the test above.

    When `<path>.dim-backup` was ALREADY taken -- a leftover from a failed run
    or a copy the operator made -- the fallback name was stamped to the second
    with no existence check, and shutil.copy2 overwrote it. Two installs inside
    one second then left no backup that predated dimissory at all: measured
    `recoverable pristine original: NONE`. The docstring promised "a name that
    is never reused" while the code chose one that could be.
    """
    d = tempfile.mkdtemp(prefix="dim-leftover-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "THE ORIGINAL"}, fh)
    # The reserved name is occupied by something unrelated.
    with open(p + ".dim-backup", "w", encoding="utf-8") as fh:
        json.dump({"leftover": "unrelated junk"}, fh)

    for _ in range(3):                     # same second, deliberately
        I.install("claude", path=p, assume_yes=True, out=lambda *_a: None,
              verify=False)

    backups = [os.path.join(d, n) for n in os.listdir(d) if ".dim-backup" in n]
    pristine = [b for b in backups
                if "THE ORIGINAL" in open(b, encoding="utf-8").read()
                and "dim hook" not in open(b, encoding="utf-8").read()]
    check("the unrelated leftover is not clobbered",
          "unrelated junk" in open(p + ".dim-backup", encoding="utf-8").read())
    check("and a backup predating dimissory survives",
          bool(pristine), f"{len(backups)} backups, none pristine")
    check("every backup has its own name",
          len(backups) == len(set(backups)), backups)


def test_a_file_edited_while_the_prompt_waits_is_not_silently_reverted():
    """`merged` is computed BEFORE the operator is asked. Writing it after a
    yes would revert anything that landed during the question -- an agent
    editing permissions, the CLI saving a preference -- and the only copy would
    be a backup nobody knows to look in. Refusing costs one re-run."""
    d = tempfile.mkdtemp(prefix="dim-toctou-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)

    def meddle_then_agree(_prompt):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"theme": "dark", "permissions": {"allow": ["Bash"]}}, fh)
        return "y"

    try:
        I.install("claude", path=p, ask=meddle_then_agree,
                  out=lambda *_a: None, verify=False)
        check("the concurrent edit is refused, not overwritten", False,
              "install proceeded and reverted it")
    except I.InstallRefused as e:
        check("the concurrent edit is refused, not overwritten", True)
        check("and the reason says the file changed",
              "changed while waiting" in str(e), str(e)[:70])
    check("the edit is still on disk",
          "permissions" in open(p, encoding="utf-8").read())

    # A file that does NOT change must still install, or this guard has just
    # broken every normal install.
    d2 = tempfile.mkdtemp(prefix="dim-toctou2-")
    p2 = os.path.join(d2, "settings.json")
    with open(p2, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)
    path, added = I.install("claude", path=p2, ask=lambda _p: "y",
                            out=lambda *_a: None, verify=False)
    check("an untouched file installs normally", bool(added), added)


def test_the_statusline_install_also_refuses_a_file_changed_mid_prompt():
    """Found by mutation testing, not by review or by reading.

    `install` had this test; `install_statusline` did not. Disabling the
    fingerprint check left all 501 checks green, because the mutation landed on
    whichever copy of the guard came first in the file and nothing exercised
    that one. Two functions, one rule, one test -- the same shape as the O_EXCL
    fix that lived in hook.seal and not in `dim write`.
    """
    d = tempfile.mkdtemp(prefix="dim-sl-toctou-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)

    def meddle_then_agree(_prompt):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"theme": "dark", "permissions": {"allow": ["Bash"]}}, fh)
        return "y"

    try:
        I.install_statusline(command="dim statusline", path=p,
                             ask=meddle_then_agree, out=lambda *_a: None,
                             verify=False)
        check("a statusline install refuses a changed file", False,
              "it proceeded and would have reverted the edit")
    except I.InstallRefused as e:
        check("a statusline install refuses a changed file", True)
        check("and says the file changed", "changed while waiting" in str(e),
              str(e)[:70])
    check("the concurrent edit survives",
          "permissions" in open(p, encoding="utf-8").read())

    # And an untouched file must still install, or the guard broke the feature.
    d2 = tempfile.mkdtemp(prefix="dim-sl-ok-")
    p2 = os.path.join(d2, "settings.json")
    with open(p2, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)
    got, _note = I.install_statusline(command="dim statusline", path=p2,
                                      ask=lambda _p: "y", out=lambda *_a: None,
                                      verify=False)
    check("an untouched file installs normally", got == p2, got)


def test_codex_is_not_given_a_turn_end_gate_it_does_not_have():
    """Codex's event registry has no bare Stop. Installing one would put a
    hook in the file that can never fire -- configuration that looks like
    coverage and is not."""
    check("claude gets the gate", "Stop" in I.TARGETS["claude"]["events"])
    check("grok gets the gate", "Stop" in I.TARGETS["grok"]["events"])
    check("codex does NOT", "Stop" not in I.TARGETS["codex"]["events"],
          I.TARGETS["codex"]["events"])
    check("but codex still gets the ask",
          "SessionStart" in I.TARGETS["codex"]["events"])


def main():
    print("=" * 66)
    print(" the hook: three CLIs, one contract, and a gate that is not a request")
    print("=" * 66)
    for t in (test_both_payload_conventions_are_understood,
              test_the_ask_names_a_command_the_agent_can_actually_run,
              test_the_gate_blocks_only_when_nothing_was_declared,
              test_the_gate_never_traps_the_agent_in_a_loop,
              test_a_session_that_is_already_declaring_is_not_nagged,
              test_the_hook_never_raises_and_never_blocks_the_user,
              test_detection_keys_match_the_install_targets,
              test_an_unparseable_or_wrongly_shaped_file_is_never_rewritten,
              test_unrelated_keys_and_existing_hooks_survive,
              test_installing_twice_adds_nothing_the_second_time,
              test_declining_changes_nothing_and_is_not_success,
              test_the_previous_file_is_kept_and_never_overwritten,
              test_a_leftover_backup_name_cannot_cost_the_original,
              test_a_file_edited_while_the_prompt_waits_is_not_silently_reverted,
              test_the_statusline_install_also_refuses_a_file_changed_mid_prompt,
              test_codex_is_not_given_a_turn_end_gate_it_does_not_have):
        t()
    print("\n" + "=" * 66)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
