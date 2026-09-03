#!/usr/bin/env python3
"""Can the trigger actually fire in a real installation?

This file exists because of a defect external review found and this suite did
not. The window check -- the entire "seal a letter BEFORE the wall" claim, the
one thing a competitor cannot skip -- lives on the PostToolUse event in
hook.py. No target in install.py registered PostToolUse. So in every real
installation the check was unreachable and no letter was ever sealed at the
margin.

It measured green because the test fed `handle()` a PostToolUse payload
directly. That bypasses the installer, and the installer is the only thing
that decides whether the event ever arrives. A trigger tested by invoking it
yourself is a check that cannot fail -- the exact defect class this project
was built to hunt, built by the person hunting it.

So every test here goes THROUGH `install.plan()` and fires only what the
installer actually registered, and each carries the negative control that
makes it capable of failing.

Run: python3 tests/test_trigger_is_reachable.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import hook as H                             # noqa: E402
from dimissory import install as I                          # noqa: E402
from dimissory.config import seconds                        # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _rollout(dirpath, used=92.0, resets_at=1788404361, age=30):
    """A Codex rollout whose window is well past the 85% margin."""
    p = os.path.join(dirpath, "rollout.jsonl")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - age))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "rate_limits": {
                "plan_type": "plus",
                "primary": {"used_percent": used, "window_minutes": 300,
                            "resets_at": resets_at}}}}) + "\n")
    return p


def _bed(reseal="10m"):
    """A hermetic session: own journal, own letters, own config."""
    d = tempfile.mkdtemp(prefix="dim-trig-")
    cfg = os.path.join(d, "config.toml")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(f'[window]\nwrite_at = 0.85\nreseal_after = "{reseal}"\n')
    os.environ["DIMISSORY_CONFIG"] = cfg
    return d, os.path.join(d, "journal"), os.path.join(d, "letters")


def _letters(path):
    return sorted(f for f in os.listdir(path)) if os.path.isdir(path) else []


def test_every_target_registers_the_event_the_check_lives_on():
    for name, spec in I.TARGETS.items():
        check(f"{name} registers {I.WINDOW_EVENT}",
              I.WINDOW_EVENT in spec["events"], spec["events"])
    check("and the hook really does branch on that event",
          I.WINDOW_EVENT.lower() in
          open(os.path.join(ROOT, "src", "dimissory", "hook.py")).read())


# PreCompact and SessionEnd seal unconditionally, with no reference to the
# window at all. Firing them proves nothing about the margin, and including
# them in the test below is how the first version of this file stayed GREEN
# with PostToolUse unregistered -- a letter appeared, just not for the reason
# the test claimed. So they are excluded by name: what is under test is
# whether a letter can be sealed MID-SESSION, on budget, which is the claim.
SEALS_REGARDLESS = ("precompact", "sessionend")


def test_a_letter_is_sealed_mid_session_using_only_registered_events():
    """The test that would have caught it. Nothing here names PostToolUse --
    the event list comes out of the installer."""
    for target in I.TARGETS:
        d, jr, letters = _bed()
        roll = _rollout(d)
        _existing, merged, _added = I.plan(target, "dim hook",
                                           os.path.join(d, "conf.json"))
        registered = [e for e in merged[I.TARGETS[target]["root_key"]]
                      if e.lower() not in SEALS_REGARDLESS]
        for event in registered:
            H.handle({"hook_event_name": event, "session_id": f"s-{target}",
                      "transcript_path": roll, "cwd": d},
                     journal_root=jr, letters_dir=letters)
        got = _letters(letters)
        check(f"{target}: sealed mid-session from {registered}",
              bool(got), f"{registered} -> nothing")

    # THE NEGATIVE CONTROL. Drop the window event from what we fire and the
    # letter at the margin must disappear -- otherwise the check above is
    # passing because something else seals, and it would have stayed green
    # through the very bug it is meant to catch.
    d, jr, letters = _bed()
    roll = _rollout(d)
    _e, merged, _a = I.plan("claude", "dim hook", os.path.join(d, "c.json"))
    without = [e for e in merged["hooks"]
               if e.lower() not in SEALS_REGARDLESS + ("posttooluse",)]
    for event in without:
        H.handle({"hook_event_name": event, "session_id": "neg",
                  "transcript_path": roll, "cwd": d},
                 journal_root=jr, letters_dir=letters)
    check("without the window event, nothing is sealed at the margin",
          _letters(letters) == [], f"fired {without} -> {_letters(letters)}")


def test_crossing_the_margin_seals_once_not_once_per_tool_call():
    """Crossing is not an event that happens once. The window stays past the
    margin for the rest of the session, so an unguarded trigger seals a letter
    on every following tool call -- each shelling out to git."""
    d, jr, letters = _bed(reseal="10m")
    roll = _rollout(d)
    for _ in range(40):
        H.handle({"hook_event_name": "PostToolUse", "session_id": "once",
                  "transcript_path": roll, "cwd": d},
                 journal_root=jr, letters_dir=letters)
    got = _letters(letters)
    check("40 tool calls past the margin seal exactly one letter",
          len(got) == 1, f"{len(got)} letters")

    # And the guard must not be a blanket "never seal twice".
    d2, jr2, letters2 = _bed(reseal="10m")
    for i, resets in enumerate((1788404361, 1788422361)):
        roll2 = _rollout(d2, resets_at=resets)
        os.replace(roll2, os.path.join(d2, "rollout.jsonl"))
        H.handle({"hook_event_name": "PostToolUse", "session_id": "two-win",
                  "transcript_path": os.path.join(d2, "rollout.jsonl"),
                  "cwd": d2}, journal_root=jr2, letters_dir=letters2)
        time.sleep(1.05)          # letter names are second-resolution
    check("but a genuinely NEW window seals again",
          len(_letters(letters2)) == 2, _letters(letters2))


def test_the_refresh_interval_is_honoured():
    """A letter written at 85% and never touched again describes a session
    that has since run to 99%."""
    d, jr, letters = _bed(reseal="1s")
    roll = _rollout(d)
    args = {"hook_event_name": "PostToolUse", "session_id": "refresh",
            "transcript_path": roll, "cwd": d}
    H.handle(args, journal_root=jr, letters_dir=letters)
    first = len(_letters(letters))
    H.handle(args, journal_root=jr, letters_dir=letters)
    check("an immediate second call does not reseal",
          len(_letters(letters)) == first, _letters(letters))
    time.sleep(1.3)
    H.handle(args, journal_root=jr, letters_dir=letters)
    check("once the interval passes, the letter is refreshed",
          len(_letters(letters)) > first, _letters(letters))


def test_below_the_margin_nothing_is_sealed_at_all():
    d, jr, letters = _bed()
    roll = _rollout(d, used=40.0)
    for _ in range(5):
        H.handle({"hook_event_name": "PostToolUse", "session_id": "low",
                  "transcript_path": roll, "cwd": d},
                 journal_root=jr, letters_dir=letters)
    check("40% of the window seals nothing", _letters(letters) == [],
          _letters(letters))


def test_a_bad_interval_does_not_become_zero():
    """0 means reseal on every tool call -- the setting inverting into the bug
    it exists to prevent."""
    check('"10m" parses', seconds("10m") == 600.0, seconds("10m"))
    check('"90s" parses', seconds("90s") == 90.0)
    check('"2h" parses', seconds("2h") == 7200.0)
    check("a bare number is seconds", seconds("45") == 45.0)
    for bad in ("", None, "soon", "-5m", "m", True, [], {}):
        check(f"{bad!r} falls back to the default, not 0",
              seconds(bad, 600.0) == 600.0, seconds(bad, 600.0))


def main():
    print("=" * 66)
    print(" can the trigger fire in a real installation?")
    print("=" * 66)
    for t in (test_every_target_registers_the_event_the_check_lives_on,
              test_a_letter_is_sealed_mid_session_using_only_registered_events,
              test_crossing_the_margin_seals_once_not_once_per_tool_call,
              test_the_refresh_interval_is_honoured,
              test_below_the_margin_nothing_is_sealed_at_all,
              test_a_bad_interval_does_not_become_zero):
        t()
    os.environ.pop("DIMISSORY_CONFIG", None)
    print("\n" + "=" * 66)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
