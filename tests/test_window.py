#!/usr/bin/env python3
"""The plan window: the component that makes firing BEFORE the wall possible.

Reacting to a 429 needs no meter. Sealing at 85%, while the agent still has
budget to write something worth reading, needs one -- and it is the part a
competitor cannot skip.

Where the number comes from was measured per provider, not assumed:

    Codex   in the transcript. Rollout `token_count` events carry a full
            rate_limits object. Verified against a real rollout: 4% of a
            300-minute window, resets_at as epoch, plan_type "plus".
    Grok    ~/.grok/logs/unified.jsonl, creditUsagePercent, account-wide.
    Claude  NOT ON DISK. The utilization pair exists only in a live stream
            event. The predecessor needed a supervising process to catch it.

The last one is a limitation this suite pins down rather than papers over,
because the tempting fix -- deriving a percentage from token consumption,
which IS on disk -- would invent the number the whole project exists not to
invent.

Run: python3 tests/test_window.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import window as W                                # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _rollout(used=42.0, minutes=300, when=None, extra_lines=0):
    """A Codex rollout, shaped exactly like the real one this was built from."""
    d = tempfile.mkdtemp(prefix="dim-win-")
    p = os.path.join(d, "rollout.jsonl")
    # The real shape: UTC with a Z suffix and milliseconds, exactly as a
    # Codex rollout writes it ("2026-09-03T01:25:22.541Z"). The first version
    # of this fixture wrote a NAIVE timestamp, which is read as local time --
    # so a deliberately-stale reading looked five hours fresher and the
    # staleness test passed for the wrong reason.
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                       time.gmtime(when if when is not None else time.time()))
    with open(p, "w", encoding="utf-8") as fh:
        for i in range(extra_lines):
            fh.write(json.dumps({"type": "response_item", "ordinal": i,
                                 "payload": {"type": "message"}}) + "\n")
        fh.write(json.dumps({
            "type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "rate_limits": {
                "limit_id": "codex", "plan_type": "plus",
                "primary": {"used_percent": used, "window_minutes": minutes,
                            "resets_at": 1788404361},
                "secondary": {"used_percent": 64.0, "window_minutes": 10080,
                              "resets_at": 1788788408}}}}) + "\n")
    return p


def test_a_codex_rollout_yields_the_window():
    p = _rollout(used=37.0)
    w = W._codex(p)
    check("a window is parsed", w is not None)
    check("with the percentage", w and w.used_percent == 37.0, w)
    check("the window length", w and w.window_minutes == 300, w)
    check("a reset time", w and w.resets_at == 1788404361, w)
    check("and the plan", w and w.plan == "plus", w)
    check("labelled in human terms", w and w.label() == "codex 5h", w and w.label())


def test_the_newest_reading_wins():
    """A session emits these repeatedly; the last is the current state."""
    d = tempfile.mkdtemp(prefix="dim-win-")
    p = os.path.join(d, "r.jsonl")
    now = time.time()
    with open(p, "w", encoding="utf-8") as fh:
        for pct in (10.0, 55.0, 91.0):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
            fh.write(json.dumps({"type": "event_msg", "timestamp": ts,
                                 "payload": {"type": "token_count",
                                             "rate_limits": {"primary": {
                                                 "used_percent": pct,
                                                 "window_minutes": 300}}}}) + "\n")
    w = W._codex(p)
    check("the last reading is the one used", w and w.used_percent == 91.0, w)


def test_a_stale_reading_is_refused_not_returned():
    """The predecessor served a cached percentage 119 hours old and wrong by
    fifty points. A confidently wrong meter is worse than none, because it is
    the thing deciding when to seal."""
    old = _rollout(used=88.0, when=time.time() - (W.MAX_AGE + 600))
    w = W._codex(old)
    check("the old reading still parses", w is not None)
    check("but it knows it is stale", w and w.is_stale, w and w.age)
    check("and read() refuses it", W.read(transcript=old, provider="codex") is None)

    fresh = _rollout(used=88.0, when=time.time() - 60)
    check("a fresh one is returned", W.read(transcript=fresh, provider="codex") is not None)


def test_an_undateable_reading_is_refused():
    """"I do not know how old this is" and "this is fresh" are different
    answers, and only one is safe to seal on."""
    d = tempfile.mkdtemp(prefix="dim-win-")
    p = os.path.join(d, "r.jsonl")
    with open(p, "w", encoding="utf-8") as fh:          # no timestamp at all
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count",
            "rate_limits": {"primary": {"used_percent": 99.0}}}}) + "\n")
    w = W._codex(p)
    check("it parses", w is not None)
    check("its age is unknown, not zero", w and w.age is None, w and w.age)
    check("unknown age counts as stale", w and w.is_stale)
    check("so read() refuses it", W.read(transcript=p, provider="codex") is None)


def test_no_reading_is_not_the_same_as_plenty_of_room():
    """should_seal returns None with no meter -- never False. Collapsing them
    lets a session with no window data run to the wall reporting it is fine."""
    check("no window -> None", W.should_seal(None) is None)
    w = W.Window(50.0, 300, source="codex", observed_at=time.time())
    check("below the margin -> False", W.should_seal(w, 0.85) is False)
    w2 = W.Window(90.0, 300, source="codex", observed_at=time.time())
    check("above the margin -> True", W.should_seal(w2, 0.85) is True)
    check("exactly at the margin counts as crossed",
          W.should_seal(W.Window(85.0, observed_at=time.time()), 0.85) is True)


def test_claude_has_no_on_disk_window_and_says_so():
    """The temptation is to derive a percentage from token consumption, which
    IS on disk. That is telemetry with no denominator, and turning it into a
    fraction of a window would be inventing the number."""
    check("read() offers no claude provider",
          W.read(transcript="/nonexistent", provider="claude") is None)
    src = open(os.path.join(ROOT, "src", "dimissory", "window.py")).read()
    check("and the module records why, so nobody adds it later",
          "NOT AVAILABLE ON DISK" in src)
    flat = " ".join(src.split())          # the phrase wraps across a line
    check("naming consumption as the trap",
          "consumption without a denominator" in flat)


def test_a_missing_or_unreadable_transcript_is_none_not_an_error():
    for bad in (None, "", "/definitely/not/here.jsonl"):
        check(f"{bad!r} yields None", W._codex(bad) is None)
    d = tempfile.mkdtemp(prefix="dim-win-")
    p = os.path.join(d, "junk.jsonl")
    with open(p, "wb") as fh:
        fh.write(b"not json\n\x00\xff binary \n{used_percent")
    check("garbage yields None rather than raising", W._codex(p) is None)


def test_grok_reads_its_own_billing_log():
    d = tempfile.mkdtemp(prefix="dim-grok-")
    os.makedirs(os.path.join(d, "logs"))
    p = os.path.join(d, "logs", "unified.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "msg": "noise"}) + "\n")
        fh.write(json.dumps({"ts": time.time(),
                             "ctx": {"billing": {"creditUsagePercent": 73.5}}}) + "\n")
    w = W._grok(d)
    check("the nested percentage is found", w and w.used_percent == 73.5, w)
    check("and attributed to grok", w and w.source == "grok", w)
    empty = tempfile.mkdtemp(prefix="dim-grok-")
    check("no log means None", W._grok(empty) is None)


def main():
    print("=" * 66)
    print(" the meter: the only reason 'before the wall' is possible")
    print("=" * 66)
    for t in (test_a_codex_rollout_yields_the_window,
              test_the_newest_reading_wins,
              test_a_stale_reading_is_refused_not_returned,
              test_an_undateable_reading_is_refused,
              test_no_reading_is_not_the_same_as_plenty_of_room,
              test_claude_has_no_on_disk_window_and_says_so,
              test_a_missing_or_unreadable_transcript_is_none_not_an_error,
              test_grok_reads_its_own_billing_log):
        t()
    print("\n" + "=" * 66)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
