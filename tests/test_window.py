#!/usr/bin/env python3
"""The plan window: the component that makes firing BEFORE the wall possible.

Reacting to a 429 needs no meter. Sealing at 85%, while the agent still has
budget to write something worth reading, needs one -- and it is the part a
competitor cannot skip.

Where the number comes from was measured per provider, not assumed:

    Codex   in the transcript. Rollout `token_count` events carry a full
            rate_limits object with BOTH caps: primary (5h) and secondary
            (weekly). Measured across 332 real rollouts on a live account.
    Grok    ~/.grok/logs/unified.jsonl, creditUsagePercent, account-wide.
    Claude  NOTHING ON DISK BY DEFAULT, and a real meter once `dim
            statusline` is installed -- Claude Code hands the five_hour /
            seven_day pair to the statusline command on stdin every turn.
            Transcripts also carry a `quotaLimits` object, but it is written
            only once a limit has been hit and holds no percentage: a
            tombstone, which is what Claude had instead of a meter until the
            statusline was wired up. See test_statusline.py.

What is refused everywhere is deriving a percentage from token consumption,
which IS on disk for every provider: consumption without a denominator is
telemetry, not a fraction of a window.

Several checks here exist because review found the meter wrong in ways this
file had asserted were right -- it read only the 5h window when the weekly
one is routinely closer to full, took a placeholder 0.0 as a measured zero,
and treated a future timestamp as fresh -- and one exists because this file's
claim about Claude has now been wrong twice in opposite directions.

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
    # The fixture's secondary (weekly) sits at 64%, well above the 37% primary,
    # so the BINDING window is the weekly one and that is what comes back. An
    # earlier version of this test asserted the primary and had to be corrected
    # when the meter stopped ignoring the cap that was actually closer to full.
    p = _rollout(used=37.0)
    w = W._codex(p)
    check("a window is parsed", w is not None)
    check("the binding window is the one returned",
          w and w.used_percent == 64.0, w)
    check("labelled as the weekly cap", w and w.label() == "codex 1w",
          w and w.label())
    check("and the plan", w and w.plan == "plus", w)
    check("the other window rides along", w and len(w.also) == 1, w and w.also)
    check("with the primary's own figures",
          w and w.also[0].used_percent == 37.0
          and w.also[0].window_minutes == 300
          and w.also[0].resets_at == 1788404361, w and w.also)


def test_the_binding_window_is_whichever_is_nearest_full():
    """Only `primary` was read. Measured on 332 real rollouts: 36 have the
    weekly cap more than 20 points above primary, including primary 0% against
    secondary 48%. A meter watching the wrong window reports room that is not
    there."""
    d = tempfile.mkdtemp(prefix="dim-bind-")
    for name, prim, sec, expect_pct, expect_label in (
            ("weekly binds", 5.0, 91.0, 91.0, "codex 1w"),
            ("five-hour binds", 88.0, 40.0, 88.0, "codex 5h"),
            ("the zero case seen in the wild", 0.0, 48.0, 48.0, "codex 1w")):
        p = os.path.join(d, f"{expect_pct}.jsonl")
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time()))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "event_msg", "timestamp": ts,
                "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": prim, "window_minutes": 300,
                                "resets_at": 100},
                    "secondary": {"used_percent": sec, "window_minutes": 10080,
                                  "resets_at": 200}}}}) + "\n")
        w = W._codex(p)
        check(f"{name}: {prim}/{sec} -> {expect_pct}%",
              w and w.used_percent == expect_pct, w)
        check(f"{name}: labelled {expect_label}",
              w and w.label() == expect_label, w and w.label())
    check("and 88% of the 5h window is enough to seal",
          W.should_seal(W.Window(88.0, 300, observed_at=time.time())) is True)


def test_a_placeholder_zero_does_not_erase_a_real_reading():
    """Codex writes used_percent 0.0 before it has a figure -- 16 of 300 real
    rollouts end on one. Newest-wins turned that into a confident 0%, which
    tells should_seal there is a whole window left.

    The rule, without special-casing zero: WITHIN ONE WINDOW USAGE CANNOT GO
    DOWN. Readings are grouped by resets_at, and the max in the current group
    wins."""
    d = tempfile.mkdtemp(prefix="dim-zero-")
    p = os.path.join(d, "r.jsonl")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time()))
    with open(p, "w", encoding="utf-8") as fh:
        for pct in (90.0, 0.0):            # a real reading, then a placeholder
            fh.write(json.dumps({"type": "event_msg", "timestamp": ts,
                "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": pct, "window_minutes": 300,
                                "resets_at": 555}}}}) + "\n")
    w = W._codex(p)
    check("the real 90% survives a trailing 0.0", w and w.used_percent == 90.0, w)
    check("so the letter is still sealed", W.should_seal(w, 0.85) is True)

    # And the rule must not suppress a GENUINE rollover: a new window has a
    # new resets_at, and 2% of a fresh window is a real 2%.
    p2 = os.path.join(d, "reset.jsonl")
    with open(p2, "w", encoding="utf-8") as fh:
        for pct, resets in ((97.0, 555), (2.0, 999)):
            fh.write(json.dumps({"type": "event_msg", "timestamp": ts,
                "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": pct, "window_minutes": 300,
                                "resets_at": resets}}}}) + "\n")
    w2 = W._codex(p2)
    check("but a genuine rollover to a new window is honoured",
          w2 and w2.used_percent == 2.0, w2)
    check("and does not seal", W.should_seal(w2, 0.85) is False)

    # An ALL-zero rollout is what one looks like before Codex has any figure
    # -- 5 of 332 real ones. Refused, because "0% used" is a claim.
    p3 = os.path.join(d, "allzero.jsonl")
    with open(p3, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 0.0, "window_minutes": 300,
                            "resets_at": 1},
                "secondary": {"used_percent": 0.0, "window_minutes": 10080,
                              "resets_at": 2}}}}) + "\n")
    check("an all-zero rollout is refused, not reported as 0%",
          W._codex(p3) is None, W._codex(p3))
    check("so should_seal says 'no meter', not 'plenty of room'",
          W.should_seal(W._codex(p3)) is None)

    # But a real 0% window alongside a measured one is kept, and keeps its 0.
    p4 = os.path.join(d, "onezero.jsonl")
    with open(p4, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 0.0, "window_minutes": 300,
                            "resets_at": 1},
                "secondary": {"used_percent": 48.0, "window_minutes": 10080,
                              "resets_at": 2}}}}) + "\n")
    w4 = W._codex(p4)
    check("a fresh 5h window beside a 48% weekly is still a reading",
          w4 and w4.used_percent == 48.0, w4)
    check("and the genuine 0% survives in `also`",
          w4 and w4.also and w4.also[0].used_percent == 0.0, w4 and w4.also)


def test_an_old_reading_is_not_evidence_of_headroom():
    """The staleness problem review named: "an hour of 84% is treated as live
    plenty of room."

    `should_seal` compared `used_percent` against the margin and nothing else,
    so a reading was a current fact for the whole hour MAX_AGE allows. And
    staleness here is ONE-DIRECTIONAL: usage inside a window only grows (a real
    reset changes resets_at, and an expired window is dropped before it gets
    here), so an old reading always UNDERSTATES. It cannot cause a false seal;
    it causes no seal at all, which is the only failure that matters.

    The fix is arithmetic, not a smaller MAX_AGE. Inside a window of length L,
    usage cannot exceed 100% over L, so in `age` seconds it can have grown by
    at most (age/L)*100 points -- an inarguable ceiling on burn rate.
    """
    now = time.time()

    def w(pct, age, minutes=300):
        return W.Window(pct, minutes, resets_at=now + 99999,
                        source="codex", observed_at=now - age)

    check("84% measured NOW does not seal -- it really is under the margin",
          W.should_seal(w(84, 0), 0.85) is False)
    check("84% measured 50 minutes ago DOES",
          W.should_seal(w(84, 3000), 0.85) is True,
          w(84, 3000).worst_case_percent)
    check("because its ceiling has crossed 85",
          w(84, 3000).worst_case_percent > 85.0,
          w(84, 3000).worst_case_percent)
    check("40% measured 50 minutes ago still does not",
          W.should_seal(w(40, 3000), 0.85) is False,
          w(40, 3000).worst_case_percent)
    check("and that False is earned: the ceiling is provably under the margin",
          w(40, 3000).worst_case_percent < 85.0,
          w(40, 3000).worst_case_percent)

    # A longer window moves more slowly, so the same age costs it far less.
    # This falls out of the arithmetic rather than being special-cased.
    check("58% of a WEEKLY window is barely moved by 50 minutes",
          abs(w(58, 3000, 10080).worst_case_percent - 58.5) < 0.1,
          w(58, 3000, 10080).worst_case_percent)
    check("so it does not seal", W.should_seal(w(58, 3000, 10080), 0.85) is False)

    # Already across when measured: age cannot rescue it, in either direction.
    check("a reading already past the margin stays past it",
          W.should_seal(w(90, 0), 0.85) is True)
    check("even a very old one", W.should_seal(w(90, 3500), 0.85) is True)


def test_growth_that_cannot_be_bounded_answers_i_do_not_know():
    """No window length means no arithmetic. A recent reading is still taken
    at face value; an older one must not be reported as headroom, so it
    answers None and routes to the at-the-wall check."""
    now = time.time()
    fresh = W.Window(50.0, None, source="grok", observed_at=now - 5)
    check("no length is bounded", fresh.worst_case_percent is None)
    check("but a recent reading is usable",
          W.should_seal(fresh, 0.85) is False)
    old = W.Window(50.0, None, source="grok", observed_at=now - 3000)
    check("an older unboundable reading answers None, not False",
          W.should_seal(old, 0.85) is None)
    across = W.Window(95.0, None, source="grok", observed_at=now - 3000)
    check("unless it was already across", W.should_seal(across, 0.85) is True)

    # Claude names its windows rather than giving a length, so the length is
    # looked up -- otherwise the whole Claude path would be unboundable.
    c = W.Window(84.0, None, source="claude", observed_at=now - 3000)
    c.fixed_label = "claude 5h"
    check("a claude 5h window has a known length",
          c.window_seconds == 18000.0, c.window_seconds)
    check("so an hour-old 84% seals there too",
          W.should_seal(c, 0.85) is True, c.worst_case_percent)


def test_the_bound_is_used_for_the_decision_and_never_reported():
    """A decision may be conservative. A document may not: the letter says
    what was measured, because that is what the reader is being told was
    observed."""
    now = time.time()
    w = W.Window(84.0, 300, resets_at=now + 9999, source="codex",
                 observed_at=now - 3000)
    check("the decision uses the ceiling", W.should_seal(w, 0.85) is True)
    check("but as_dict reports the MEASURED figure",
          w.as_dict()["used_percent"] == 84.0, w.as_dict())
    check("and no worst-case number appears in it",
          not any("worst" in k for k in w.as_dict()), w.as_dict())
    src = open(os.path.join(ROOT, "src", "dimissory", "window.py")).read()
    check("the module records that distinction",
          "NEVER REPORTED AS A MEASUREMENT" in src)


def test_a_reading_stamped_in_the_future_is_refused():
    """is_stale asked only `age > MAX_AGE`, so a negative age sailed through
    as fresh -- the failure pointing the wrong way, since refusing what it
    cannot vouch for is the gate's entire job."""
    ahead = W.Window(99.0, 300, source="codex",
                     observed_at=time.time() + 7200)
    check("a stamp two hours ahead is stale", ahead.is_stale, ahead.age)
    check("and read() refuses it", W.should_seal(None) is None)
    ok = W.Window(99.0, 300, source="codex", observed_at=time.time() - 5)
    check("a normal recent stamp is not", not ok.is_stale, ok.age)
    slack = W.Window(50.0, 300, source="codex",
                     observed_at=time.time() + 30)
    check("small clock skew is tolerated", not slack.is_stale, slack.age)


def test_a_claude_session_never_borrows_another_products_meter():
    """`read` tried Codex then fell through to Grok. Grok's figure is
    account-wide and present on any box with Grok installed, so a CLAUDE
    session -- which has no percentage of its own -- picked up GROK's number,
    and as_dict dropped `source`, so it was presented unlabelled."""
    check("a claude transcript path is recognised",
          W.provider_for("/home/u/.claude/projects/x/abc.jsonl") == "claude")
    check("a codex rollout path is recognised",
          W.provider_for("/home/u/.codex/sessions/2026/rollout-x.jsonl")
          == "codex")
    check("an unknown path stays unknown", W.provider_for("/tmp/x.jsonl") is None)
    check("a claude session with no recording gets NOTHING, not grok's figure",
          W.read(transcript="/home/u/.claude/projects/x/a.jsonl",
                 claude_root=_no_claude_cache()) is None)
    w = W.Window(50.0, 300, source="codex", observed_at=time.time())
    check("and every window carries its source",
          w.as_dict().get("source") == "codex", w.as_dict())


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


def _no_claude_cache():
    """A claude cache path that definitely holds nothing.

    Passed EXPLICITLY by every test below, because the default is
    ~/.dimissory/window/claude.json -- the developer's own live reading. Three
    checks here started failing the moment this machine had one, which is the
    correct outcome for the product and an isolation bug in the test: a test
    whose result depends on the author's home is not measuring the code.
    """
    return os.path.join(tempfile.mkdtemp(prefix="dim-noclaude-"), "none.json")


def test_claude_has_no_percentage_UNTIL_THE_STATUSLINE_RECORDS_ONE():
    """This test has now been wrong twice, in opposite directions.

    First it pinned "NOT AVAILABLE ON DISK", which was too strong: Claude
    transcripts do carry a structural quotaLimits object (with no percentage
    in it). Corrected to say the PERCENTAGE was what was missing.

    That is still not the whole truth. Claude Code hands the five_hour /
    seven_day pair to the STATUSLINE on stdin every turn, so the percentage is
    obtainable -- it simply is not written down until something writes it
    down. Nothing is on disk by default; `dim statusline` is what puts it
    there. See statusline.py and test_statusline.py.

    What survives unchanged is the refusal underneath: token consumption IS on
    disk and is still never turned into a percentage, because consumption
    without a denominator is telemetry, not a fraction of a window.
    """
    check("with nothing recorded, there is no claude window",
          W.read(transcript="/nonexistent", provider="claude",
                 claude_root=_no_claude_cache()) is None)
    src = open(os.path.join(ROOT, "src", "dimissory", "window.py")).read()
    flat = " ".join(src.split())
    check("the module no longer claims the object is absent",
          "NOT AVAILABLE ON DISK" not in src)
    check("it names the statusline as where the meter comes from",
          "STATUSLINE" in src.upper() and "_claude" in src)
    check("and still names consumption as the trap",
          "consumption without a denominator" in flat)
    check("and says plainly that the statusline must be installed",
          "has to be installed" in flat, "precondition not recorded")


def test_claudes_quota_tombstone_is_read_but_never_used_as_a_meter():
    """quotaLimits says the wall WAS hit and when it reopens. Measured on 81
    real records: every one is status "rejected" and none carries a
    percentage. Useful in a letter, useless as a trigger."""
    d = tempfile.mkdtemp(prefix="dim-cl-")
    p = os.path.join(d, "t.jsonl")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": ts}) + "\n")
        fh.write(json.dumps({
            "timestamp": ts,
            "quotaLimits": {"status": "rejected", "resetsAt": 1788398400,
                            "rateLimitType": "five_hour",
                            "isUsingOverage": False}}) + "\n")
    wall = W._claude_wall(p)
    check("the tombstone is read", wall is not None)
    check("with the reset time", wall and wall["resets_at"] == 1788398400, wall)
    check("and which window it was", wall and wall["kind"] == "five_hour", wall)
    check("it is NOT a Window object", not isinstance(wall, W.Window))
    check("it carries no percentage to be mistaken for one",
          wall and not any("percent" in k for k in wall), wall)
    check("and with nothing recorded, read() still yields no claude window",
          W.read(transcript=p, provider="claude",
                 claude_root=_no_claude_cache()) is None)
    check("no tombstone means None", W._claude_wall(
        os.path.join(d, "absent.jsonl")) is None)


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
              test_the_binding_window_is_whichever_is_nearest_full,
              test_a_placeholder_zero_does_not_erase_a_real_reading,
              test_an_old_reading_is_not_evidence_of_headroom,
              test_growth_that_cannot_be_bounded_answers_i_do_not_know,
              test_the_bound_is_used_for_the_decision_and_never_reported,
              test_a_reading_stamped_in_the_future_is_refused,
              test_a_claude_session_never_borrows_another_products_meter,
              test_the_newest_reading_wins,
              test_a_stale_reading_is_refused_not_returned,
              test_an_undateable_reading_is_refused,
              test_no_reading_is_not_the_same_as_plenty_of_room,
              test_claude_has_no_percentage_UNTIL_THE_STATUSLINE_RECORDS_ONE,
              test_claudes_quota_tombstone_is_read_but_never_used_as_a_meter,
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
