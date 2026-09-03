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
          when the window reopens. `_claude_wall` reads it, and it is what
          Claude had INSTEAD of a meter until the statusline was wired up.

          THE METER ITSELF ARRIVES BY STATUSLINE. Claude Code hands the
          five_hour/seven_day pair to the statusline command on stdin every
          turn -- a real percentage on a 0-100 scale with a real reset time.
          Measured live on 2.1.248: five_hour 100%, seven_day 58%. `dim
          statusline` records it and `_claude` reads it back, so Claude now
          has a before-the-wall meter like the other two.

          Note this is NOT a supervising process. The predecessor wrapped the
          CLI in a PTY to scrape the same pair off the wire; here Claude Code
          calls us, through an interface it already invokes on its own
          schedule, and we never sit between it and its terminal.

          It costs one thing: the statusline has to be installed. Without it
          nothing records, `_claude` finds no file, and Claude falls back to
          the tombstone -- at the wall rather than before it. That is a real
          precondition and `dim status` reports it rather than assuming it.

What is still refused everywhere: turning token consumption into a
percentage. Consumption IS on disk for every provider, but consumption
without a denominator is telemetry, not a fraction of a window, and
converting it would invent the number this whole project exists not to
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

# How much of a rollout's tail is scanned for a reading.
_TAIL = 2_000_000

# A reading stamped in the future is a clock the reader cannot trust. A little
# slack absorbs ordinary skew between a container and its host; beyond that the
# reading is refused rather than treated as fresh, because `is_stale` used to
# ask only `age > MAX_AGE` and a negative age sailed through it. A stamp two
# hours ahead therefore looked newer than one taken now -- the failure pointing
# the wrong way, since the whole job of the staleness gate is to refuse
# readings it cannot vouch for.
MAX_SKEW = 120.0

# A reading with no knowable window length cannot have its growth bounded, so
# it is taken at face value only while it is genuinely recent. Past this it
# answers "I do not know" rather than "there is headroom".
FRESH_FOR = 120.0

# Claude's statusline names its windows instead of giving a length, so the
# length is looked up rather than derived. These are the documented spans, and
# they are needed for the growth bound below -- without a length there is no
# arithmetic, only a guess.
CLAUDE_WINDOW_MINUTES = {"claude 5h": 300, "claude 7d": 10080,
                         "claude spend": 43200}          # a 30-day period

# When no length is knowable at all -- Grok's account-wide credit figure, or a
# window Anthropic adds under a name we do not recognise -- the SHORTEST real
# window is assumed rather than giving up.
#
# Giving up was the bug: `worst_case_percent` returned None, `should_seal`
# answered None for anything past FRESH_FOR, and None routes to the
# at-the-wall check, which only reads Claude's tombstone. So Grok could not
# seal at 84% at ANY age -- the original "an hour of 84% is plenty of room",
# wearing a third hat.
#
# Assuming the SHORTEST window is the conservative direction, because a short
# window implies the fastest burn rate and therefore the highest ceiling. And
# the error is bounded: MAX_AGE refuses anything over an hour, so the most
# this assumption can ever add is 20 points.
ASSUMED_SPAN_MINUTES = 300


def percentage(value):
    """A usable percentage, or None. Rejects what `isinstance` lets through.

    `isinstance(v, (int, float))` accepts `True`, `NaN` and `inf`, and each is
    a live defect in the number that decides when to seal:

        True    bool subclasses int, so float(True) is 1.0
        inf     `inf >= 85` is True -- an immediate seal, forever
        NaN     every comparison is False, so it reads as "plenty of room"
                while also defeating any ordering

    This lives HERE, not in statusline.py, because that is where it was and
    review found the consequence: only the Claude path validated. `_codex` and
    `_grok` still took `isinstance`, so a NaN in a Codex rollout produced
    `Window(nan%)` and `should_seal` returned False -- reported as headroom.
    Validation belongs with the type it protects.

    The ceiling is generous because a spend limit is documented as able to
    exceed 100.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pct != pct or pct in (float("inf"), float("-inf")):
        return None
    if pct < 0.0 or pct > 1000.0:
        return None
    return pct


def reset_time(value):
    """A plausible epoch, or None. Same reasoning as `percentage`.

    A NaN reset defeats the expiry check specifically: `NaN <= now` is False,
    so the window it belongs to never looks expired and never retires.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        at = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if at != at or at in (float("inf"), float("-inf")):
        return None
    if not (946_684_800 < at < 4_102_444_800):
        return None
    return at


class Window:
    """One plan window, and how it was learned.

    `source` is part of the value. A reader deciding whether to trust a seal
    needs to know the number came from the agent's own transcript rather than
    from something this tool inferred.
    """

    __slots__ = ("used_percent", "window_minutes", "resets_at", "source",
                 "observed_at", "plan", "also", "fixed_label", "peak_percent",
                 "peak_at")

    def __init__(self, used_percent, window_minutes=None, resets_at=None,
                 source="", observed_at=None, plan=None, peak_percent=None,
                 peak_at=None):
        self.used_percent = float(used_percent)
        # THE HIGHEST FIGURE SEEN IN THIS WINDOW, which is not always the
        # latest one. `used_percent` is what a letter reports, because that is
        # what was last measured; `peak_percent` is what the SEAL decision
        # uses, because usage normally only grows and the peak is therefore a
        # lower bound on where the account actually is.
        #
        # Keeping them apart is the fix for a real defect: the monotonic rule
        # (max wins within a window) correctly rejects Codex's 0.0 placeholder,
        # but it also discarded a GENUINE drop -- an overage grant, a plan
        # upgrade, a credit top-up -- forever. So a letter would report "90%
        # used" when the truth was 40%. A conservative decision is fine; a
        # document that misstates a measurement is not.
        self.peak_percent = (float(peak_percent)
                             if isinstance(peak_percent, (int, float))
                             and not isinstance(peak_percent, bool)
                             else self.used_percent)
        # WHEN the peak was seen, which is not always when we last heard
        # anything. Growth has to be measured from the moment usage was known
        # to be at the peak; measuring it from a newer, lower reading would
        # understate the ceiling, which is the one direction that matters.
        # `observed_at` stays the latest contact, so freshness is judged on
        # when we last learned ANYTHING.
        self.peak_at = peak_at if isinstance(peak_at, (int, float)) \
            else observed_at
        self.window_minutes = window_minutes
        self.resets_at = resets_at
        self.source = source
        self.observed_at = observed_at
        self.plan = plan
        self.also = []          # the other windows on the same account
        # Set when the provider names its own window ("claude 5h") rather than
        # giving a length in minutes to derive one from.
        self.fixed_label = None

    @property
    def age(self):
        return None if self.observed_at is None else time.time() - self.observed_at

    @property
    def is_stale(self):
        a = self.age
        if a is None:
            return True
        # Both directions. `a > MAX_AGE` alone let a future stamp through as
        # fresh, which is the one direction that matters: it makes an old
        # reading look current to the code that decides when to seal.
        return a > MAX_AGE or a < -MAX_SKEW

    @property
    def span_is_assumed(self):
        """True when no real length was known and the shortest was assumed."""
        return not (self.window_minutes
                    or CLAUDE_WINDOW_MINUTES.get(self.fixed_label or ""))

    @property
    def window_seconds(self):
        """How long this window spans. Never None -- see ASSUMED_SPAN_MINUTES.

        Returning None here meant `should_seal` answered None for every
        unboundable reading past FRESH_FOR, which made Grok unsealable at any
        percentage under the margin.
        """
        minutes = (self.window_minutes
                   or CLAUDE_WINDOW_MINUTES.get(self.fixed_label or "")
                   or ASSUMED_SPAN_MINUTES)
        try:
            return float(minutes) * 60.0
        except (TypeError, ValueError):
            return float(ASSUMED_SPAN_MINUTES) * 60.0

    @property
    def worst_case_percent(self):
        """The most this window COULD have reached since it was measured.

        This is the answer to the staleness problem, and it is arithmetic
        rather than a guess: inside a window of length L, usage cannot exceed
        100% over L, so in `age` seconds it can have grown by at most
        (age / L) * 100 points. That is an inarguable ceiling on burn rate.

        Why it matters is that staleness here is ONE-DIRECTIONAL. Usage inside
        a window only grows -- a real reset changes `resets_at`, and an expired
        window is dropped before it reaches here -- so an old reading always
        UNDERSTATES current usage. The failure it produces is therefore never
        a false seal; it is failing to seal at all, which is the product's
        entire purpose. Review put it exactly: "an hour of 84% is treated as
        live plenty of room."

        Shrinking MAX_AGE only narrows the window in which that is wrong. This
        removes the reasoning error instead: as a reading ages, the ceiling
        rises, so it stops being evidence of headroom on its own schedule.

        NEVER REPORTED AS A MEASUREMENT. `used_percent` is what a letter says,
        because that is what was observed. This bound informs one decision --
        whether to seal -- and a decision may be conservative where a document
        may not.
        """
        span = self.window_seconds
        base_at = self.peak_at if self.peak_at is not None else self.observed_at
        if span is None or base_at is None:
            return None
        age = time.time() - base_at
        if age < 0:
            return None
        # From the PEAK and from WHEN the peak was seen: the peak is our lower
        # bound on where usage actually is, and growth accrues on top of it.
        return max(self.used_percent, self.peak_percent) + (age / span) * 100.0

    def label(self):
        if self.fixed_label:
            return self.fixed_label
        m = self.window_minutes
        if not m:
            return self.source
        # int() first: a span derived from real period bounds arrives as a
        # float, and "grok 1.0w" is a typo the arithmetic wrote for you.
        try:
            m = int(round(float(m)))
        except (TypeError, ValueError):
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
        # `source` is carried, not dropped. Without it a reader cannot tell
        # whose meter they are looking at, and `read` can legitimately answer
        # from a different provider than the session they are in.
        out = {"used_percent": self.used_percent,
               "resets_at": self.resets_at,
               "window": self.label(),
               "source": self.source}
        if self.also:
            out["also"] = [{"used_percent": w.used_percent,
                            "window": w.label(),
                            "resets_at": w.resets_at} for w in self.also]
        return out

    def __repr__(self):
        return (f"Window({self.used_percent:.0f}% {self.label()}, "
                f"age={'?' if self.age is None else int(self.age)}s)")


def _codex(transcript):
    """The BINDING window in a Codex rollout: whichever is closest to full.

    Two corrections here, both from review round 1 and both confirmed against
    332 real rollouts on a live account.

    NEWEST IS NOT ALWAYS CREDIBLE. Codex emits `used_percent: 0.0` before it
    has a real figure. 16 of 300 rollouts with any reading end on 0.0. Taking
    the newest reading meant reporting a number nobody measured as a
    confident 0% -- and 0% tells `should_seal` there is a whole window left.
    That is exactly what UNMEASURED exists to prevent, and it slipped past it
    because `Window.__init__` calls `float()` on whatever it is handed.

    The rule that fixes it without special-casing zero: WITHIN ONE WINDOW,
    USAGE CANNOT GO DOWN. Readings are grouped by `resets_at` -- the window's
    identity -- and the maximum within the current group wins. A genuine
    window rollover changes `resets_at`, so a real drop to 0% is still
    honoured; a placeholder inside an unchanged window is not.

    BOTH WINDOWS COUNT. Only `primary` (5h) was read. 36 of those rollouts
    have `secondary` (the weekly cap) more than 20 points above primary --
    including primary 0% against secondary 48%. The weekly window is
    routinely the binding one, and a meter watching the wrong window reports
    room that does not exist. Whichever is nearer full is returned; the other
    rides along in `also` for the letter.
    """
    if not transcript or not os.path.exists(transcript):
        return None
    # Grouped by window identity, because a percentage is only comparable
    # against others measured in the SAME window.
    groups: dict = {}          # window identity -> the HIGHEST reading seen
    latest_seen: dict = {}     # window identity -> the LAST reading seen
    plan = None
    try:
        with open(transcript, "rb") as fh:
            size = os.path.getsize(transcript)
            if size > _TAIL:
                # A tail, not a backwards scan -- the previous docstring
                # claimed backwards and the code has always read forward from
                # a seek. Said plainly because the consequence is real: if a
                # single record at EOF is larger than the tail, the readline()
                # that realigns to a record boundary can consume past every
                # remaining line and the meter goes blind.
                fh.seek(size - _TAIL)
                fh.readline()
            for raw in fh:
                if b"used_percent" not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                payload = d.get("payload") or d
                rl = payload.get("rate_limits")
                if not isinstance(rl, dict):
                    continue
                at = _epoch(d.get("timestamp"))
                plan = rl.get("plan_type") or plan
                for slot, minutes_default in (("primary", None),
                                              ("secondary", None)):
                    part = rl.get(slot)
                    if not isinstance(part, dict):
                        continue
                    # Validated, not merely isinstance'd. `_codex` was the
                    # path review found still accepting NaN -- which produced
                    # Window(nan%) and a should_seal of False, i.e. reported
                    # as headroom.
                    pct = percentage(part.get("used_percent"))
                    if pct is None:
                        continue
                    key = (slot, part.get("resets_at"))
                    prev = groups.get(key)
                    # The LATEST reading in this window -- what a letter
                    # reports, so a genuine drop (an overage grant, a plan
                    # upgrade) stops being invisible.
                    #
                    # Except an exact 0.0 following a real figure, which is
                    # Codex's documented placeholder rather than a
                    # measurement: 16 of 300 real rollouts end on one. Letting
                    # it through would print "0% used" in a document whose
                    # first rule is that a number nobody measured is omitted,
                    # never zero. A wholly zero rollout is refused separately.
                    latest = latest_seen.get(key)
                    placeholder = (pct == 0.0 and latest is not None
                                   and latest[0] > 0.0)
                    newer = (latest is None or latest[1] is None or at is None
                             or at >= latest[1])
                    if newer and not placeholder:
                        latest_seen[key] = (pct, at)
                    if prev is None or pct > prev[0]:
                        # THE TIMESTAMP OF THE WINNING READING, not the newest
                        # one in the file. `latest_at = max(timestamp)` took
                        # the stamp of every token_count event, including the
                        # 0.0 placeholders that do NOT win the percentage. So
                        # an 84% from an hour ago followed by a placeholder now
                        # came back looking one second old, its growth ceiling
                        # collapsed, and should_seal said False. Measured by
                        # review: the MAX_AGE fix was bypassed on Codex too.
                        groups[key] = (pct, part.get("window_minutes"),
                                       part.get("resets_at"), at)
    except OSError:
        return None
    if not groups:
        return None

    # The current group for each slot is the one with the latest reset time;
    # an older group belongs to a window that has already turned over.
    #
    # `resets_at` is Optional, and the first version compared `(value[2] or 0)`
    # -- which sorts an ABSENT reset time below every real one. So a reading
    # from a newer window that happened to carry no resets_at lost to the old
    # window's peak, pinning a stale high figure across a rollover and sealing
    # forever after. A non-numeric value also made the comparison raise, and
    # `handle` swallows exceptions, so the seal became a silent no-op.
    #
    # Order deliberately: readings WITH a reset time rank by it; a reading
    # without one is only used when nothing better exists for that slot, since
    # "I do not know which window this belongs to" cannot outrank "I do".
    def rank(value):
        r = value[2]
        return (1, float(r)) if isinstance(r, (int, float)) else (0, 0.0)

    current = {}
    for (slot, _resets), value in groups.items():
        held = current.get(slot)
        if held is None or rank(value) > rank(held):
            current[slot] = value

    ranked = sorted(current.values(), key=lambda v: v[0], reverse=True)
    if ranked[0][0] == 0.0:
        # EVERY window in this rollout reads exactly 0.0, which is what a
        # rollout looks like before Codex has a figure at all -- 5 of 332 real
        # ones. Indistinguishable from no measurement, so it is refused rather
        # than reported: "0% used" is a claim, and a number nobody measured is
        # omitted, never zero.
        #
        # Note this is not a blanket rule against zero. A single window at 0%
        # alongside a non-zero one is a genuinely fresh window on an active
        # account, and `ranked` puts the non-zero one first, so that reading
        # survives and keeps its real 0% in `also`.
        return None
    # `groups` is keyed by (slot, resets_at) and so is `latest_seen`, so each
    # winning entry is matched back to the latest reading of the SAME window.
    by_entry = {id(v): k for k, v in groups.items()}

    def build(entry):
        peak, minutes, resets, peak_at = entry
        latest, latest_at = latest_seen.get(by_entry.get(id(entry)),
                                            (peak, peak_at))
        return Window(latest, minutes, resets, source="codex",
                      observed_at=latest_at if latest_at is not None else peak_at,
                      plan=plan, peak_percent=peak, peak_at=peak_at)

    win = build(ranked[0])
    win.also = [build(e) for e in ranked[1:]]
    return win


def _grok(root=None):
    """Grok logs its own billing fetch; the newest one it wrote is the truth.

    Account-wide rather than per-host, which is the point: an agent running
    somewhere else is invisible to local session files and fully counted here.

    THE WINDOW IS ON THE SAME LINE and this used to throw it away. The billing
    record carries `currentPeriod.start`/`.end` -- a WEEKLY period on the
    account measured -- and dropping it left `window_minutes` None, which made
    the growth bound assume the shortest window instead. Grok ran that number
    against itself and reported the result: a 56-hour-old 35% reading produced
    a worst case of about 1154%, so a direct `should_seal(_grok())` said seal
    on a reading that was nearly three days stale.

    AND A WARNING THAT COST NOTHING TO LEARN AND WOULD HAVE COST A LOT TO
    MISS. Grok 1.0.13 has a statusline whose payload is Claude-shaped, right
    down to a `used_percentage` field. It is the CONTEXT window, not the plan:
    measured at 3% while the weekly plan sat at 80%. Pointing `dim statusline`
    at Grok would seal on the wrong number entirely.

    What this cannot do is REFRESH the row. Measured on a real box: `grok -p`
    never writes billing, no matter how much work it does -- only interactive
    pager startup on a TTY does. So the file can sit for days while the
    account drains, which is why the staleness gate matters more here than
    anywhere else. It said 35%; the account was at 80%.
    """
    path = os.path.join(os.path.expanduser(root or "~/.grok"),
                        "logs", "unified.jsonl")
    latest = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            # A tail, not the whole file. Measured at 4.3 MB on a live box,
            # with the interesting line 164 fetches from the end.
            if size > _TAIL:
                fh.seek(size - _TAIL)
                fh.readline()
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

    def dig(o, key):
        if isinstance(o, dict):
            if key in o:
                return o[key]
            for v in o.values():
                got = dig(v, key)
                if got is not None:
                    return got
        elif isinstance(o, list):
            for v in o:
                got = dig(v, key)
                if got is not None:
                    return got
        return None

    pct = percentage(dig(d, "creditUsagePercent"))
    if pct is None:
        return None

    # The period the percentage is a fraction OF. Without it the bound has no
    # denominator and falls back to assuming five hours, which is wrong by a
    # factor of 34 for a weekly window.
    minutes = resets = None
    period = dig(d, "currentPeriod")
    if isinstance(period, dict):
        start, end = _epoch(period.get("start")), _epoch(period.get("end"))
        resets = end
        if start is not None and end is not None and end > start:
            minutes = (end - start) / 60.0
    return Window(pct, minutes, resets, source="grok",
                  observed_at=_epoch(d.get("ts")))


def _claude(cache_root=None):
    """Claude's plan window, from whatever the statusline last wrote down.

    Claude Code never puts this on disk itself -- but it HANDS it to the
    statusline command on stdin every turn, and `dim statusline` writes it
    where this can find it. Measured live on 2.1.248: five_hour 100%,
    seven_day 58%, both with real reset times. See statusline.py.

    So the earlier verdict in this module -- Claude can only be sealed AFTER
    the wall -- held only while nothing was recording. It is a real
    before-the-wall meter now, and it required no supervising process: the
    predecessor wrapped the CLI in a PTY to scrape this same pair off the
    wire, where this is a documented callback Claude Code already invokes.

    A window whose `resets_at` has PASSED is dropped rather than reported.
    Claude Code does the same thing upstream, and for the same reason: once
    the window turns over, the percentage measured inside it describes usage
    that no longer counts against anything.
    """
    path = os.path.expanduser(cache_root or "~/.dimissory/window/claude.json")
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    # VALIDATED HERE TOO, not only where it was written. This is a file on
    # disk that becomes OBSERVED in a letter -- under a heading saying it was
    # established by dimissory rather than claimed by the agent -- so the
    # reader cannot assume the writer was this version of this program. An
    # edited, corrupted or poisoned cache could otherwise assert 1000000%
    # (seal now, forever), `inf` (the same), or NaN (every comparison false,
    # so it reads as budget to spare while breaking any ordering).
    if blob.get("source") not in (None, "claude"):
        return None
    written_at = reset_time(blob.get("observed_at"))
    now = time.time()
    live = []
    for w in blob.get("windows") or []:
        if not isinstance(w, dict):
            continue
        pct = percentage(w.get("used_percent"))
        if pct is None:
            continue
        resets = reset_time(w.get("resets_at"))
        if resets is not None and resets <= now:
            continue                  # this window has already turned over
        label = w.get("label")
        if not isinstance(label, str) or not label or len(label) > 64 \
                or any(c < " " for c in label):
            label = "claude"          # never put control characters in a letter
        # EACH WINDOW CARRIES ITS OWN AGE, and this is the whole point.
        #
        # The file's `observed_at` is when the file was last WRITTEN, which is
        # not when this percentage was MEASURED. The R3 merge keeps a window
        # that the newest payload did not mention -- deliberately, so one
        # session cannot erase another's reading -- and then rewrote the
        # file's timestamp. So an hour-old 84% five-hour reading came back
        # looking one second old, its growth ceiling collapsed to 84%, and
        # `should_seal` said False.
        #
        # That made the entire MAX_AGE fix dead on the meter it was written
        # for: not replaced, BYPASSED. Review measured it. `seen_at` is
        # stamped per window by `record`, and reading it is what makes the
        # ceiling mean anything.
        seen = reset_time(w.get("seen_at"))
        live.append((pct, label, resets, seen if seen is not None else written_at))
    if not live:
        return None
    # Sorted by an EXPLICIT key. `live.sort(reverse=True)` compared whole
    # tuples, so two windows with equal percentage and label -- possible in a
    # hand-edited or duplicated cache -- fell through to comparing a None
    # resets_at against a float and raised TypeError. `handle` swallows every
    # exception, so the seal became a silent no-op: the loudest possible bug
    # reduced to the quietest. This is the same mixed-type comparison `rank()`
    # was written to kill in `_codex`, reintroduced on the Claude path.
    live.sort(key=lambda row: row[0], reverse=True)
    pct, label, resets, seen = live[0]
    win = Window(pct, None, resets, source="claude", observed_at=seen)
    win.fixed_label = label
    for p, lb, r, s_at in live[1:]:
        other = Window(p, None, r, source="claude", observed_at=s_at)
        other.fixed_label = lb
        win.also.append(other)
    return win


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


def provider_for(transcript):
    """Which agent wrote this transcript, from where it lives. None if unclear.

    Necessary because `read` used to try Codex and then fall through to Grok
    whenever the first came back empty. Grok's figure is account-wide and
    always available on a box with Grok installed, so a CLAUDE session -- which
    has no percentage of its own -- would pick up GROK's number and, since
    `as_dict` dropped `source`, present it as its own. Another product's meter,
    unlabelled, in the one field the seal decision is made on.
    """
    if not transcript:
        return None
    p = str(transcript).replace("\\", "/").lower()
    # The HOME-directory markers are checked first, all of them, before the
    # weaker filename hint. `/rollout-` used to be tested alongside `/.codex/`
    # and therefore before `/.claude/`, so a real Claude transcript under a
    # directory containing "rollout-" classified as codex.
    for marker, name in (("/.claude/", "claude"),
                         ("/.codex/", "codex"),
                         ("/.grok/", "grok")):
        if marker in p:
            return name
    if "/rollout-" in p:
        return "codex"          # a rollout filename, outside any known home
    return None


def read(transcript=None, provider=None, grok_root=None, claude_root=None):
    """The current plan window, or None when it cannot be established.

    None, not a zero and not an estimate. Every caller renders an absent
    window as an omitted line, which is the same rule the rest of this project
    holds: a number nobody measured is not reported.

    A provider is never guessed past what the transcript says. When the
    transcript identifies the agent, ONLY that agent's source is consulted --
    so a Claude session reports no window rather than borrowing one.
    """
    known = provider or provider_for(transcript)
    candidates = []
    if known == "claude":
        # Only what the statusline recorded. Never Grok's account-wide figure,
        # which is how a Claude session used to end up reporting another
        # product's meter as its own.
        candidates.append(_claude(claude_root))
    else:
        if known in (None, "codex"):
            candidates.append(_codex(transcript))
        if known in (None, "grok"):
            candidates.append(_grok(grok_root))
    for w in candidates:
        if w is not None and not w.is_stale:
            return w
    return None


def should_seal(window, at=0.85):
    """Whether a letter is due. True, False, or None for "no usable answer".

    None is not False. "No meter" and "plenty of room" are different states,
    and collapsing them lets a session with no window data run to the wall
    while reporting that it was fine.

    THE AGE OF THE READING IS PART OF THE DECISION. The old version compared
    `used_percent` against the margin and nothing else, so a reading was
    treated as a current fact for the whole hour MAX_AGE allows -- and since
    staleness here only ever understates usage, an hour-old 84% was reported
    as headroom while the window could already be full. That is a failure to
    write the letter, which is the one failure this project exists to prevent.

    So three questions, in order:

      already across?   usage only grows inside a window, so a reading that
                        was at or past the margin when measured is still past
                        it now. Age cannot rescue it.
      could it be?      `worst_case_percent` bounds how far it can have moved
                        since. If the ceiling crosses the margin, seal: being
                        early costs a letter, being late costs the run.
      provably not?     if the ceiling is still under the margin, there is
                        real headroom and that is a measured False.

    When the growth cannot be bounded at all -- no window length -- a recent
    reading is taken at face value and an older one answers None, because
    "I cannot tell" is the honest response and it routes to the at-the-wall
    check rather than to silence.
    """
    if window is None:
        return None
    margin = at * 100.0
    if max(window.used_percent, window.peak_percent) >= margin:
        return True
    ceiling = window.worst_case_percent
    if ceiling is None:
        age = window.age
        if age is not None and 0 <= age <= FRESH_FOR:
            return False
        return None
    return ceiling >= margin
