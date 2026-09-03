"""The plan window: how much is left, and when it resets.

This is the component that makes firing BEFORE the wall possible, and it is the
only part a competitor cannot skip. Reacting to a 429 needs no meter. Writing a
letter at 85% -- while the agent still has budget to think -- needs one.

WHERE THE NUMBER COMES FROM, measured per provider rather than assumed:

  Codex   IN THE TRANSCRIPT. Rollout files carry `token_count` events with a
          full rate_limits object: primary (300-minute window) and secondary
          (10080-minute = weekly), each with used_percent and resets_at, plus
          plan_type. The hook already hands us the transcript path, so this
          costs one file read and no network.

  Grok    ~/.grok/logs/unified.jsonl records the CLI's own billing fetch, with
          creditUsagePercent. Account-wide -- every device and product, not
          just this host -- which is the number that actually matters.

  Claude  NO UTILIZATION PERCENTAGE ON DISK -- but not nothing, and the
          earlier blanket claim here was wrong. Claude Code transcripts DO
          carry a structural `quotaLimits` object:

              {"status": "rejected", "resetsAt": 1788398400,
               "rateLimitType": "five_hour", "isUsingOverage": false, ...}

          Measured across 81 records in 81 transcripts on a real machine:
          every single one has status "rejected", and NO key in any of them
          holds a percentage, a utilization or a used figure. It is written
          when the limit has ALREADY been hit.

          So it is a tombstone, not a meter: it tells you the wall was hit and
          when the window reopens, which is worth putting in a letter, and it
          cannot tell you that you are at 85%. `_claude_wall` reads it for the
          reset time; `read` still returns no Window for Claude, because there
          is no percentage to return.

The five_hour/seven_day UTILIZATION pair is emitted only in a stream-json
rate_limit event, never written to the transcript -- the predecessor needed a
supervising process wrapping the CLI to catch it in flight. So Claude has no
before-the-wall meter, and that is reported as UNMEASURED rather than
estimated. Token consumption IS on disk, but consumption without a
denominator is telemetry, not a fraction of a window, and turning it into a
percentage would be inventing the number this whole project exists not to
invent.

STALENESS. A reading whose age cannot be established, or which is older than
MAX_AGE, is REFUSED rather than returned. The predecessor shipped a cached
percentage that was 119 hours old and wrong by fifty points; a confidently
wrong meter is worse than no meter, because it is the thing deciding when to
seal.
"""

from __future__ import annotations

import glob
import json
import os
import time

# How old a reading may be and still be reported. An hour: long enough that a
# session which has not just started still has a number, short enough that a
# five-hour window cannot have turned over inside it.
MAX_AGE = 3600.0


class Window:
    """One plan window, and how it was learned.

    `source` is part of the value. A reader deciding whether to trust a seal
    needs to know the number came from the agent's own transcript rather than
    from something this tool inferred.
    """

    __slots__ = ("used_percent", "window_minutes", "resets_at", "source",
                 "observed_at", "plan")

    def __init__(self, used_percent, window_minutes=None, resets_at=None,
                 source="", observed_at=None, plan=None):
        self.used_percent = float(used_percent)
        self.window_minutes = window_minutes
        self.resets_at = resets_at
        self.source = source
        self.observed_at = observed_at
        self.plan = plan

    @property
    def age(self):
        return None if self.observed_at is None else time.time() - self.observed_at

    @property
    def is_stale(self):
        a = self.age
        return a is None or a > MAX_AGE

    def label(self):
        m = self.window_minutes
        if not m:
            return self.source
        if m % 10080 == 0:
            span = f"{m // 10080}w"
        elif m % 1440 == 0:
            span = f"{m // 1440}d"
        elif m % 60 == 0:
            span = f"{m // 60}h"
        else:
            span = f"{m}m"
        return f"{self.source} {span}"

    def as_dict(self):
        return {"used_percent": self.used_percent,
                "resets_at": self.resets_at,
                "window": self.label()}

    def __repr__(self):
        return (f"Window({self.used_percent:.0f}% {self.label()}, "
                f"age={'?' if self.age is None else int(self.age)}s)")


def _codex(transcript):
    """The newest rate_limits object in a Codex rollout.

    Newest wins: a session emits these repeatedly and the last one is the
    current state. Read backwards so a long rollout costs a tail, not a scan.
    """
    if not transcript or not os.path.exists(transcript):
        return None
    best = None
    try:
        with open(transcript, "rb") as fh:
            size = os.path.getsize(transcript)
            if size > 2_000_000:
                fh.seek(size - 2_000_000)
                fh.readline()
            for raw in fh:
                if b"used_percent" not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                p = d.get("payload") or d
                rl = p.get("rate_limits") or {}
                prim = rl.get("primary") or {}
                if "used_percent" not in prim:
                    continue
                ts = d.get("timestamp")
                best = (prim, rl, _epoch(ts))
    except OSError:
        return None
    if not best:
        return None
    prim, rl, at = best
    return Window(prim["used_percent"], prim.get("window_minutes"),
                  prim.get("resets_at"), source="codex", observed_at=at,
                  plan=rl.get("plan_type"))


def _grok(root=None):
    """Grok logs its own billing fetch; the newest one it wrote is the truth.

    Account-wide rather than per-host, which is the point: an agent running
    somewhere else is invisible to local session files and fully counted here.
    """
    path = os.path.join(os.path.expanduser(root or "~/.grok"),
                        "logs", "unified.jsonl")
    latest = None
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b"creditUsagePercent" in raw:
                    latest = raw
    except OSError:
        return None
    if not latest:
        return None
    try:
        d = json.loads(latest)
    except ValueError:
        return None

    def dig(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "creditUsagePercent" and isinstance(v, (int, float)):
                    return float(v)
                got = dig(v)
                if got is not None:
                    return got
        elif isinstance(o, list):
            for v in o:
                got = dig(v)
                if got is not None:
                    return got
        return None

    pct = dig(d)
    if pct is None:
        return None
    return Window(pct, source="grok", observed_at=_epoch(d.get("ts")))


def _claude_wall(transcript):
    """Claude's quota tombstone: the wall was hit, and when it reopens.

    Deliberately NOT a Window and deliberately not wired into `read`. There is
    no percentage in this object, so it cannot say "you are at 85%" -- only
    "you were refused". Returning it as a Window would put a fabricated
    used_percent into the one number the seal decision is made on.

    What it is good for is the letter: "your five_hour window reopens at
    20:20" is a real observed fact, and it is the first question the person
    reading a handoff actually has.
    """
    if not transcript or not os.path.exists(transcript):
        return None
    latest = None
    try:
        with open(transcript, "rb") as fh:
            size = os.path.getsize(transcript)
            if size > 2_000_000:
                fh.seek(size - 2_000_000)
                fh.readline()
            for raw in fh:
                if b'"quotaLimits"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                q = d.get("quotaLimits")
                if isinstance(q, dict) and q.get("resetsAt"):
                    latest = (q, _epoch(d.get("timestamp")))
    except OSError:
        return None
    if not latest:
        return None
    q, at = latest
    return {"kind": q.get("rateLimitType"),
            "status": q.get("status"),
            "resets_at": q.get("resetsAt"),
            "observed_at": at,
            "source": "claude"}


def _epoch(value):
    """A timestamp as epoch seconds, or None. Never a guess.

    None matters: a reading whose age cannot be established is refused by
    `read`, because "I do not know how old this is" and "this is fresh" are
    different answers and only one of them is safe to seal on.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime, timezone
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError, ImportError):
        return None
    if dt.tzinfo is None:
        # A NAIVE timestamp is assumed UTC, not local. Every provider observed
        # so far stamps UTC ("2026-09-03T01:25:22.541Z"), and `fromisoformat`
        # would otherwise read a naive one as local time -- which on this
        # machine made a reading five hours OLDER look five hours FRESHER.
        # Getting that backwards defeats the staleness gate in the direction
        # that matters, since the gate is what decides when to seal.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read(transcript=None, provider=None, grok_root=None):
    """The current plan window, or None when it cannot be established.

    None, not a zero and not an estimate. Every caller renders an absent
    window as an omitted line, which is the same rule the rest of this project
    holds: a number nobody measured is not reported.
    """
    candidates = []
    if provider in (None, "codex"):
        candidates.append(_codex(transcript))
    if provider in (None, "grok"):
        candidates.append(_grok(grok_root))
    # Claude is deliberately absent. See the module docstring: the number is
    # not on disk, and inventing one from token consumption would be the exact
    # failure this project is built against.
    for w in candidates:
        if w is not None and not w.is_stale:
            return w
    return None


def should_seal(window, at=0.85):
    """Whether the window has crossed the margin where a letter is due.

    Returns None when there is no usable reading -- NOT False. "No meter" and
    "plenty of room" are different states, and collapsing them would let a
    session with no window data run to the wall while reporting that it was
    fine.
    """
    if window is None:
        return None
    return window.used_percent >= at * 100.0
