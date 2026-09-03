#!/usr/bin/env python3
"""Claude's meter, which exists only because something records it.

For Codex and Grok the plan window is already on disk. For Claude Code it is
on disk NOWHERE -- but Claude Code hands it to the statusline command on stdin
every turn. Captured live from a real session, Claude Code 2.1.248:

    "rate_limits": {
      "five_hour": {"used_percentage": 100,               "resets_at": 1788416400},
      "seven_day": {"used_percentage": 57.99999999999999, "resets_at": 1788764400}
    }

That is the whole reason Claude went from at-the-wall to before-the-wall, and
it needed no supervising process: the predecessor wrapped the CLI in a PTY to
scrape the same pair off the wire, where this is a documented callback Claude
Code already invokes on its own schedule.

The fixture below is that captured payload, trimmed. Note 57.99999999999999 --
these are binary floats, so nothing here compares them for equality.

Five documented behaviours the code has to survive, each with a check:
Pro/Max-only absence, absence before the first API reply, independently
missing windows, a window DROPPED once its resets_at passes, and a third
`spend_limit` window on versions newer than the one this was measured on.

Run: python3 tests/test_statusline.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import install as I                          # noqa: E402
from dimissory import statusline as S                       # noqa: E402
from dimissory import window as W                           # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _payload(five=100, seven=57.99999999999999, spend=None, ahead=3600):
    """The real captured shape. `ahead` keeps reset times in the future."""
    now = time.time()
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five,
                               "resets_at": int(now + ahead)}
    if seven is not None:
        limits["seven_day"] = {"used_percentage": seven,
                               "resets_at": int(now + ahead * 24)}
    if spend is not None:
        limits["spend_limit"] = {"used_percentage": spend,
                                 "resets_at": int(now + ahead * 48)}
    return {"session_id": "abc", "version": "2.1.248",
            "model": {"id": "claude-opus-5", "display_name": "Opus"},
            "context_window": {"used_percentage": 37},
            "rate_limits": limits}


def _cache():
    return os.path.join(tempfile.mkdtemp(prefix="dim-sl-"), "claude.json")


def test_the_captured_payload_yields_both_windows():
    got = S.extract(_payload())
    check("both windows are read", len(got) == 2, got)
    check("the binding one is first",
          got[0]["kind"] == "five_hour", [w["kind"] for w in got])
    check("on a 0-100 scale", abs(got[0]["used_percent"] - 100.0) < 1e-9, got[0])
    check("the weekly figure survives its float noise",
          abs(got[1]["used_percent"] - 58.0) < 0.01, got[1])
    check("each window is labelled",
          [w["label"] for w in got] == ["claude 5h", "claude 7d"], got)
    check("and carries a reset time (normalised to a float)",
          all(isinstance(w["resets_at"], float) for w in got), got)


def test_a_third_window_on_a_newer_version_is_not_dropped():
    """`spend_limit` arrived in 2.1.251; this was measured on 2.1.248, which
    does not send it. The code iterates whatever keys arrive rather than naming
    two, so a window added upstream is recorded instead of silently ignored."""
    got = S.extract(_payload(five=10, seven=20, spend=99))
    check("the new window is read", len(got) == 3, [w["kind"] for w in got])
    check("and it binds, being nearest full",
          got[0]["kind"] == "spend_limit", got[0])
    unknown = S.extract({"rate_limits": {
        "some_future_window": {"used_percentage": 5, "resets_at": 1}}})
    check("an entirely unknown window is still recorded",
          len(unknown) == 1, unknown)
    check("and gets an honest label rather than a wrong one",
          unknown[0]["label"] == "claude some_future_window", unknown)


def test_absence_is_never_zero():
    """`rate_limits` is absent on plans without it and before the session's
    first API response. A tool whose whole rule is "a number nobody measured
    is omitted, never zero" cannot report 0% here."""
    for name, payload in (("no rate_limits at all", {"session_id": "x"}),
                          ("an empty rate_limits", {"rate_limits": {}}),
                          ("a null window", {"rate_limits": {"five_hour": None}}),
                          ("a window with no percentage",
                           {"rate_limits": {"five_hour": {"resets_at": 1}}}),
                          ("a non-numeric percentage",
                           {"rate_limits": {"five_hour":
                                            {"used_percentage": "lots"}}}),
                          ("a boolean, which is technically an int",
                           {"rate_limits": {"five_hour":
                                            {"used_percentage": True}}})):
        check(f"{name} -> nothing", S.extract(payload) == [],
              S.extract(payload))
    c = _cache()
    check("and nothing is written", S.record({"session_id": "x"}, c) == []
          and not os.path.exists(c))


def test_one_window_missing_does_not_hide_the_other():
    check("only weekly present", [w["kind"] for w in
                                  S.extract(_payload(five=None))]
          == ["seven_day"])
    check("only five-hour present", [w["kind"] for w in
                                     S.extract(_payload(seven=None))]
          == ["five_hour"])


def test_a_window_past_its_reset_is_dropped_not_reported():
    """Claude Code removes a window once its resets_at passes, so a cached
    reading for an expired window describes usage that no longer counts. Left
    in, a 100% five-hour figure would keep sealing after the window reopened."""
    c = _cache()
    now = time.time()
    with open(c, "w", encoding="utf-8") as fh:
        json.dump({"observed_at": now - 10, "source": "claude", "windows": [
            {"kind": "five_hour", "label": "claude 5h",
             "used_percent": 100.0, "resets_at": now - 60},      # expired
            {"kind": "seven_day", "label": "claude 7d",
             "used_percent": 40.0, "resets_at": now + 86400}]}, fh)
    w = W._claude(c)
    check("the expired window is gone", w and w.label() == "claude 7d", w)
    check("and the live one is reported",
          w and abs(w.used_percent - 40.0) < 1e-9, w)
    check("so it no longer seals", W.should_seal(w, 0.85) is False)

    with open(c, "w", encoding="utf-8") as fh:
        json.dump({"observed_at": now - 10, "windows": [
            {"kind": "five_hour", "label": "claude 5h",
             "used_percent": 100.0, "resets_at": now - 60}]}, fh)
    check("every window expired means no reading at all",
          W._claude(c) is None, W._claude(c))
    check("which is 'no meter', not 'plenty of room'",
          W.should_seal(W._claude(c)) is None)


def test_a_window_keeps_its_own_age_when_another_is_updated():
    """The R4 finding, and the reason `seen_at` exists.

    The merge deliberately keeps a window the newest payload did not mention,
    so one session cannot erase another's reading. But it then rewrote the
    FILE's timestamp, and `_claude` dated every window by the file -- so an
    hour-old 84% five-hour reading came back looking one second old, its
    growth ceiling collapsed to 84%, and `should_seal` said False.

    The MAX_AGE work was not replaced by that. It was BYPASSED: dead on the
    one meter it was written for.
    """
    c = _cache()
    now = time.time()
    S.record({"rate_limits": {"five_hour": {"used_percentage": 84,
              "resets_at": int(now + 3600)}}}, c)
    blob = json.load(open(c, encoding="utf-8"))
    blob["observed_at"] = now - 3000
    for w in blob["windows"]:
        w["seen_at"] = now - 3000
    with open(c, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)
    check("an hour-old 84% seals on its own",
          W.should_seal(W._claude(c), 0.85) is True)

    # A weekly-only report arrives. The five-hour reading is KEPT, and it must
    # keep its age with it.
    S.record({"rate_limits": {"seven_day": {"used_percentage": 41,
              "resets_at": int(now + 86400)}}}, c)
    w = W._claude(c)
    check("the kept reading is still the binding one",
          w and abs(w.used_percent - 84.0) < 1e-9, w)
    check("and it did NOT become one second old",
          w and w.age > 2500, w and w.age)
    check("so it still seals", W.should_seal(w, 0.85) is True,
          w and w.worst_case_percent)

    # A late LOWER sample of the same window does the same thing without a
    # second session: the higher value is kept, so its age must be too.
    c2 = _cache()
    S.record({"rate_limits": {"five_hour": {"used_percentage": 84,
              "resets_at": int(now + 3600)}}}, c2)
    blob = json.load(open(c2, encoding="utf-8"))
    blob["observed_at"] = now - 3000
    for w in blob["windows"]:
        w["seen_at"] = now - 3000
    with open(c2, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)
    S.record({"rate_limits": {"five_hour": {"used_percentage": 12,
              "resets_at": int(now + 3600)}}}, c2)
    w2 = W._claude(c2)
    check("a late lower sample keeps the higher value",
          w2 and abs(w2.used_percent - 84.0) < 1e-9, w2)
    check("and does not reset its age", w2 and w2.age > 2500, w2 and w2.age)


def test_a_stale_recording_is_refused_like_any_other():
    c = _cache()
    S.record(_payload(), c)
    blob = json.load(open(c))
    old = time.time() - (W.MAX_AGE + 600)
    # BOTH, and the per-window one is what actually counts now. Ageing only
    # the file's timestamp used to age the reading, because `_claude` dated
    # every window by the file. That was the laundering bug: this test aged
    # the wrong field and still passed.
    blob["observed_at"] = old
    for w in blob["windows"]:
        w["seen_at"] = old
    with open(c, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)
    w = W._claude(c)
    check("the old reading still parses", w is not None)
    check("but knows it is stale", w and w.is_stale, w and w.age)
    check("and read() refuses it",
          W.read(transcript="/h/.claude/p/s.jsonl", claude_root=c) is None)


def test_the_round_trip_a_real_session_actually_takes():
    """statusline in, cache file, meter out, seal decision. End to end."""
    c = _cache()
    rc = S.main(["--cache", c], stdin=io.StringIO(json.dumps(_payload())))
    check("the statusline exits 0", rc == 0)
    w = W.read(transcript="/home/u/.claude/projects/p/s.jsonl", claude_root=c)
    check("the meter reads it back", w is not None, w)
    check("as the binding window", w and w.label() == "claude 5h", w)
    check("with the other one alongside",
          w and [o.label() for o in w.also] == ["claude 7d"], w and w.also)
    check("and 100% is past the margin", W.should_seal(w, 0.85) is True)
    check("the source is carried, so a letter can attribute it",
          w.as_dict().get("source") == "claude", w.as_dict())
    check("and the letter names WHICH window",
          w.as_dict().get("window") == "claude 5h", w.as_dict())


def test_a_claude_session_still_never_borrows_groks_meter():
    """The R1 defect must stay fixed now that claude has a source of its own:
    an EMPTY claude cache must mean no window, not Grok's account figure."""
    empty = _cache()
    got = W.read(transcript="/home/u/.claude/projects/p/s.jsonl",
                 claude_root=empty)
    check("no recording means no window", got is None, got)


def test_the_status_bar_is_never_broken_by_us():
    """Whatever this prints is what the user stares at all day, and a
    statusline that errors is an error they cannot dismiss from inside Claude
    Code."""
    for name, raw in (("empty stdin", ""),
                      ("not json", "<<<garbage>>>"),
                      ("a json array", "[1,2,3]"),
                      ("json null", "null"),
                      ("a bare number", "42")):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            rc = S.main(["--cache", _cache()], stdin=io.StringIO(raw))
        finally:
            sys.stdout = real
        check(f"{name}: exits 0", rc == 0)
        check(f"{name}: still prints something", out.getvalue().strip() != "")


def test_it_prints_something_worth_looking_at():
    line = S.describe(_payload(), S.extract(_payload()))
    check("the model is named", "Opus" in line, line)
    check("the binding window is shown", "5h 100%" in line, line)
    check("the other one too", "7d 58%" in line, line)
    check("with a human reset time", "resets" in line, line)
    check("and it stays short enough for a status bar", len(line) < 60, line)
    bare = S.describe({}, [])
    check("with nothing to say it still says who it is",
          bare.strip() != "", repr(bare))


def test_installing_wraps_an_existing_statusline_instead_of_taking_it():
    """`statusLine` is single-valued, so installing REPLACES it. Silently
    costing somebody the bar they built is not acceptable, so theirs is run
    and its output printed verbatim."""
    d = tempfile.mkdtemp(prefix="dim-sl-inst-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark",
                   "statusLine": {"type": "command",
                                  "command": "/opt/my bar/bar.sh --fancy"}}, fh)
    _existing, merged, note = I.plan_statusline("dim statusline", p)
    cmd = merged["statusLine"]["command"]
    check("ours runs first", cmd.startswith("dim statusline"), cmd)
    check("and theirs is wrapped", "--wrap" in cmd, cmd)
    check("quoted, so a path with a space survives",
          "'/opt/my bar/bar.sh --fancy'" in cmd, cmd)
    check("the note says what happened", "wrapping" in note, note)
    check("unrelated keys are untouched", merged["theme"] == "dark")

    # And the wrap actually runs their command, with our recording done first.
    c = _cache()
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        S.main(["--cache", c, "--wrap", "printf THEIR-BAR"],
               stdin=io.StringIO(json.dumps(_payload())))
    finally:
        sys.stdout = real
    check("their output is what shows", out.getvalue() == "THEIR-BAR",
          repr(out.getvalue()))
    check("and ours was still recorded", os.path.exists(c))


def test_installing_twice_does_not_wrap_ourselves():
    d = tempfile.mkdtemp(prefix="dim-sl-idem-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)
    _e, merged, note = I.plan_statusline("dim statusline", p)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(merged, fh)
    _e2, merged2, note2 = I.plan_statusline("dim statusline", p)
    check("the first install sets it", "statusline" in
          merged["statusLine"]["command"], note)
    check("the second is a no-op", note2 == "already installed", note2)
    check("and does not nest a wrap",
          merged2["statusLine"]["command"].count("--wrap") == 0,
          merged2["statusLine"]["command"])


def test_an_unreadable_settings_file_is_refused_not_rewritten():
    d = tempfile.mkdtemp(prefix="dim-sl-bad-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"theme":"dark",OOPS}')
    before = open(p, encoding="utf-8").read()
    try:
        I.plan_statusline("dim statusline", p)
        check("an unparseable file is refused", False, "it proceeded")
    except I.InstallRefused as e:
        check("an unparseable file is refused", True)
        check("and says nothing was changed", "Nothing was changed" in str(e),
              str(e))
    check("the file is byte-identical", open(p, encoding="utf-8").read()
          == before)


def test_the_marker_does_not_match_someone_elses_statusline():
    """Both R3 reviewers found this independently, and it was the highest
    severity in the tree: the marker was the bare word "statusline", so
    `~/.claude/statusline.sh` -- THE PATH IN CLAUDE CODE'S OWN DOCS -- was read
    as "already installed". Install did nothing, reported success, and the
    meter was never recorded. Then `dim status` said to run --install, and
    running it did nothing again."""
    for theirs in ("~/.claude/statusline.sh", "claude-code-statusline",
                   "/usr/bin/powerline-statusline", "~/bin/my-statusline.py",
                   "starship prompt --statusline"):
        d = tempfile.mkdtemp(prefix="dim-mk-")
        p = os.path.join(d, "settings.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"statusLine": {"type": "command", "command": theirs}}, fh)
        _e, merged, note = I.plan_statusline("dim statusline", p)
        check(f"{theirs[:34]:34} is not mistaken for ours",
              note != "already installed", note)
        check(f"{theirs[:34]:34} gets wrapped",
              "--wrap" in merged["statusLine"]["command"],
              merged["statusLine"]["command"])
    for ours in ("dim statusline", "/x/bin/dim statusline --wrap 'foo'",
                 '"/py" -m dimissory.cli statusline'):
        d = tempfile.mkdtemp(prefix="dim-mk2-")
        p = os.path.join(d, "settings.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"statusLine": {"type": "command", "command": ours}}, fh)
        _e, _m, note = I.plan_statusline("dim statusline", p)
        check(f"but OUR own form is recognised: {ours[:30]}",
              note == "already installed", note)


def test_claude_codes_own_statusline_settings_are_not_thrown_away():
    """`padding` and `refreshInterval` belong to Claude Code, not us.
    refreshInterval matters most: it is the only knob that re-samples while
    the session is idle, which is exactly this meter's weak spot."""
    d = tempfile.mkdtemp(prefix="dim-keep-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"statusLine": {"type": "command", "command": "bar.sh",
                                  "padding": 0, "refreshInterval": 5000}}, fh)
    _e, merged, _n = I.plan_statusline("dim statusline", p)
    block = merged["statusLine"]
    check("padding survives", block.get("padding") == 0, block)
    check("their refreshInterval survives, not ours",
          block.get("refreshInterval") == 5000, block)

    # And with nothing set, we ask for one, because otherwise the sample only
    # updates when Claude Code re-renders -- which is NOT on tool calls.
    d2 = tempfile.mkdtemp(prefix="dim-keep2-")
    p2 = os.path.join(d2, "settings.json")
    with open(p2, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)
    _e2, merged2, _n2 = I.plan_statusline("dim statusline", p2)
    check("a refresh interval is requested when none was set",
          isinstance(merged2["statusLine"].get("refreshInterval"), int),
          merged2["statusLine"])


def test_a_number_nobody_could_have_measured_is_refused():
    """`isinstance(v, (int, float))` admits True, NaN and inf. Each is a live
    defect in the one number that decides when to seal: inf seals forever, NaN
    reads as budget while breaking every ordering, and bool subclasses int."""
    for bad in (float("nan"), float("inf"), float("-inf"), True, False,
                -1, 1001, "85", None, [85]):
        check(f"{bad!r} is not a percentage", S.percentage(bad) is None,
              S.percentage(bad))
    for good in (0, 0.0, 58.0, 100, 100.0, 250.5):
        check(f"{good!r} is", S.percentage(good) == float(good))
    check("a NaN reset is refused (NaN <= now is False, so it never expires)",
          S.reset_time(float("nan")) is None)
    for bad in (0, -1, 1, 99, float("inf"), True, "soon"):
        check(f"reset {bad!r} is refused", S.reset_time(bad) is None)

    # And the READER validates too, because the cache is a file that a letter
    # then presents as OBSERVED.
    c = _cache()
    for pct in (float("inf"), 1e12, float("nan")):
        with open(c, "w", encoding="utf-8") as fh:
            json.dump({"observed_at": time.time(), "source": "claude",
                       "windows": [{"kind": "five_hour", "label": "claude 5h",
                                    "used_percent": pct,
                                    "resets_at": time.time() + 3600}]}, fh)
        check(f"a cache claiming {pct} yields no window",
              W._claude(c) is None, W._claude(c))


def test_two_windows_with_the_same_percentage_do_not_crash_the_sort():
    """`live.sort(reverse=True)` compared whole tuples, so equal percentage and
    equal label fell through to comparing None against a float -- TypeError,
    swallowed by handle(), seal silently skipped. The same mixed-type
    comparison `rank()` was written to kill in `_codex`."""
    c = _cache()
    with open(c, "w", encoding="utf-8") as fh:
        json.dump({"observed_at": time.time(), "source": "claude", "windows": [
            {"kind": "a", "label": "claude", "used_percent": 50.0,
             "resets_at": None},
            {"kind": "b", "label": "claude", "used_percent": 50.0,
             "resets_at": time.time() + 3600}]}, fh)
    try:
        w = W._claude(c)
        check("a duplicated percentage and label does not raise", True)
        check("and a window still comes back", w is not None, w)
    except TypeError as e:
        check("a duplicated percentage and label does not raise", False, str(e))


def test_one_session_cannot_erase_anothers_window():
    """Each window can be absent on its own, and two sessions on one account
    do not always report the same pair. Whole-file replace let session B, which
    only saw the weekly cap, delete session A's 95% five-hour reading -- the
    one about to stop the work."""
    c = _cache()
    now = time.time()
    S.record({"rate_limits": {
        "five_hour": {"used_percentage": 95, "resets_at": int(now + 3600)},
        "seven_day": {"used_percentage": 40, "resets_at": int(now + 86400)}}}, c)
    S.record({"rate_limits": {
        "seven_day": {"used_percentage": 41, "resets_at": int(now + 86400)}}}, c)
    kinds = {w["kind"]: w["used_percent"]
             for w in json.load(open(c, encoding="utf-8"))["windows"]}
    check("the five-hour reading survives a weekly-only report",
          abs(kinds.get("five_hour", -1) - 95.0) < 1e-9, kinds)
    check("and the weekly one is updated", abs(kinds["seven_day"] - 41.0) < 1e-9,
          kinds)

    # Within one window, usage cannot go down -- a lower reading for the SAME
    # resets_at is a late sample, not a decrease.
    S.record({"rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": int(now + 3600)}}}, c)
    kinds = {w["kind"]: w["used_percent"]
             for w in json.load(open(c, encoding="utf-8"))["windows"]}
    check("a late lower sample does not undo a higher one",
          abs(kinds["five_hour"] - 95.0) < 1e-9, kinds)
    # But a real rollover does.
    S.record({"rate_limits": {
        "five_hour": {"used_percentage": 3, "resets_at": int(now + 9999)}}}, c)
    kinds = {w["kind"]: w["used_percent"]
             for w in json.load(open(c, encoding="utf-8"))["windows"]}
    check("a genuine rollover does", abs(kinds["five_hour"] - 3.0) < 1e-9, kinds)


def test_the_cache_is_not_world_readable():
    """It holds the numbers a letter presents as OBSERVED. Letters and the
    journal are 0o600; this was whatever the umask happened to be."""
    c = _cache()
    S.record(_payload(), c)
    mode = os.stat(c).st_mode & 0o777
    check(f"mode is 0o600, not 0o{mode:o}", mode == 0o600, oct(mode))


def test_a_wrapped_shell_command_still_means_what_it_meant():
    """Claude Code runs `statusLine.command` through a shell, and the example
    in its own documentation is a `jq` pipeline. `shlex.split` with no shell
    silently changed the meaning of every command with a pipe, a redirect or
    an `&&`: measured, `echo hello && echo world` printed the second half as a
    literal argument."""
    for cmd, want in (("echo hello && echo world", "hello\nworld\n"),
                      ("printf 'a b' | tr ' ' '-'", "a-b"),
                      ("echo one; echo two", "one\ntwo\n")):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            S.main(["--cache", _cache(), "--wrap", cmd],
                   stdin=io.StringIO(json.dumps(_payload())))
        finally:
            sys.stdout = real
        check(f"{cmd!r} runs as a shell command",
              out.getvalue() == want, repr(out.getvalue()))

    # A command that fails silently must not leave an empty bar with no clue.
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        S.main(["--cache", _cache(), "--wrap", "exit 7"],
               stdin=io.StringIO(json.dumps(_payload())))
    finally:
        sys.stdout = real
    check("a silent non-zero exit is reported, not swallowed",
          "exited 7" in out.getvalue(), repr(out.getvalue()))


def main():
    print("=" * 68)
    print(" claude's meter: recorded off the statusline, not scraped off a PTY")
    print("=" * 68)
    for t in (test_the_captured_payload_yields_both_windows,
              test_a_third_window_on_a_newer_version_is_not_dropped,
              test_absence_is_never_zero,
              test_one_window_missing_does_not_hide_the_other,
              test_a_window_past_its_reset_is_dropped_not_reported,
              test_a_window_keeps_its_own_age_when_another_is_updated,
              test_a_stale_recording_is_refused_like_any_other,
              test_the_round_trip_a_real_session_actually_takes,
              test_a_claude_session_still_never_borrows_groks_meter,
              test_the_status_bar_is_never_broken_by_us,
              test_it_prints_something_worth_looking_at,
              test_installing_wraps_an_existing_statusline_instead_of_taking_it,
              test_installing_twice_does_not_wrap_ourselves,
              test_an_unreadable_settings_file_is_refused_not_rewritten,
              test_the_marker_does_not_match_someone_elses_statusline,
              test_claude_codes_own_statusline_settings_are_not_thrown_away,
              test_a_number_nobody_could_have_measured_is_refused,
              test_two_windows_with_the_same_percentage_do_not_crash_the_sort,
              test_one_session_cannot_erase_anothers_window,
              test_the_cache_is_not_world_readable,
              test_a_wrapped_shell_command_still_means_what_it_meant):
        t()
    print("\n" + "=" * 68)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
