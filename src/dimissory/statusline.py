"""Claude's meter, which does not exist on disk until something records it.

Codex and Grok write their plan window to a file, so dimissory can just read
it. Claude Code writes its window NOWHERE -- but it HANDS it to the statusline
command on stdin, every turn. Measured on a live session, Claude Code 2.1.248:

    "rate_limits": {
      "five_hour": {"used_percentage": 100,               "resets_at": 1788416400},
      "seven_day": {"used_percentage": 57.99999999999999, "resets_at": 1788764400}
    }

A real percentage on a 0-100 scale with a real reset time. That is the meter
the rest of this project needs and the reason Claude could previously only be
sealed AFTER the wall: the numbers were never on disk, so nothing could read
them. This module is the missing half -- it catches them as they go past and
writes them down.

WHAT THIS IS NOT. It is not a supervising process. The predecessor wrapped the
CLI in a PTY to scrape this pair off the wire; here Claude Code hands it over
through a documented interface it already calls on its own schedule, and the
cost is one small file write.

FIVE THINGS THE DOCS SAY THAT THE CODE HAS TO RESPECT:

  Pro/Max only        `rate_limits` is absent entirely on other plans. Absent
                      is UNMEASURED, never zero.
  after first reply   it appears only once the session has had an API
                      response, so an early statusline call carries nothing.
  independently gone  each window may be missing on its own.
  dropped on reset    Claude Code REMOVES a window once its resets_at passes.
                      So a missing five_hour means "no longer tracked", which
                      is emphatically not "0% used".
  spend_limit         a third window, v2.1.251+. Read if present; this machine
                      is 2.1.248 and does not send it, which is why the code
                      iterates whatever keys arrive instead of naming two.

AND ONE THING THE PAYLOAD ITSELF SAYS: 57.99999999999999. The percentages are
binary floats, so nothing here compares them for equality or round-trips them
through a formatted string.

THE STATUS BAR IS THE USER'S, NOT OURS. Whatever this prints is what they
stare at all day, and a statusline that errors is an error they cannot get rid
of without editing settings.json. So every path here prints something and
nothing raises -- and `--wrap` exists so installing dimissory does not cost
somebody the statusline they already had.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# Where the reading is left for the hook to find. Account-wide rather than
# per-session, because that is what a plan window is: two sessions on one
# account share it, and the freshest observation is the true one.
DEFAULT_CACHE = "~/.dimissory/window/claude.json"

# How long a wrapped statusline may take before we give up on it. Was 10s,
# which is a very long time to stare at a frozen bar -- and Claude Code
# cancels a statusline that is still running when the next update arrives, so
# a slow wrap is wasted work as well as a visible stall. The recording happens
# BEFORE the wrap, so a hang costs the bar and never the sample.
WRAP_TIMEOUT = 3.0

# Which windows we understand well enough to name in a letter. Anything else
# in `rate_limits` is still recorded -- the shape is theirs to extend, and a
# window we cannot label is not a window we should silently drop.
LABELS = {"five_hour": "claude 5h",
          "seven_day": "claude 7d",
          "spend_limit": "claude spend"}


def cache_path(root=None):
    return os.path.expanduser(root or DEFAULT_CACHE)


def percentage(value):
    """A usable percentage, or None. Rejects what `isinstance` lets through.

    `isinstance(v, (int, float))` accepts `True`, `NaN` and `inf`, and every
    one of those is a live defect in the number that decides when to seal:

        True    bool subclasses int, so float(True) is 1.0
        inf     `inf >= 85` is True -- an immediate seal, forever
        NaN     every comparison is False, so it reads as "plenty of room"
                while also defeating any ordering

    Both R3 reviewers found this independently. The bound is generous at the
    top because a spend limit is documented as able to exceed 100.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pct != pct or pct in (float("inf"), float("-inf")):
        return None                       # NaN, +inf, -inf
    if pct < 0.0 or pct > 1000.0:
        return None                       # not a percentage anybody measured
    return pct


def reset_time(value):
    """A plausible reset epoch, or None. Same reasoning as `percentage`.

    A NaN reset defeats the expiry check specifically: `NaN <= now` is False,
    so it never looks expired and the window it belongs to never retires.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        at = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if at != at or at in (float("inf"), float("-inf")):
        return None
    # Anything outside a couple of decades either side is not a reset time.
    if not (946_684_800 < at < 4_102_444_800):
        return None
    return at


def extract(payload):
    """The windows in a statusline payload, as a list of plain dicts.

    Empty when there is nothing to read -- which is a normal state, not an
    error: the field is absent on plans without it and before the session's
    first API response.
    """
    limits = (payload or {}).get("rate_limits")
    if not isinstance(limits, dict):
        return []
    out = []
    for key, value in limits.items():
        if not isinstance(value, dict):
            continue
        pct = percentage(value.get("used_percentage"))
        if pct is None:
            continue                  # no percentage is not a percentage of 0
        if not isinstance(key, str) or not key or len(key) > 64:
            continue                  # a key we cannot label is not a window
        out.append({"kind": key,
                    "label": LABELS.get(key, f"claude {key}"),
                    "used_percent": pct,
                    "resets_at": reset_time(value.get("resets_at"))})
    # Nearest full first, so a reader taking [0] gets the binding window --
    # the same rule the Codex path uses, for the same reason: the cap that is
    # closest to stopping you is the one that matters.
    out.sort(key=lambda w: w["used_percent"], reverse=True)
    return out


def record(payload, root=None):
    """Write the windows down for the hook to read. Returns them, or [].

    MERGED BY WINDOW KIND, not written as a whole list. Each window can be
    absent on its own, and two sessions on one account do not always report
    the same pair -- so replacing the file wholesale let one session erase
    another's reading. Measured by review: session A records five_hour 95% and
    seven_day 40%, session B reports only seven_day 41%, and the 95% cap that
    was about to stop the work is simply gone.

    Within one kind the rule is `_codex`'s rule, for the same reason: usage
    inside a window cannot go down, so a lower reading for the SAME resets_at
    is a stale or late-arriving sample and does not overwrite a higher one. A
    changed resets_at is a real rollover and always wins.

    Never raises. A statusline that fails is a failure the user sees on every
    render, so a lost observation costs one sample and nothing else.
    """
    windows = extract(payload)
    if not windows:
        # Deliberately does NOT clear an existing reading. A payload with no
        # rate_limits (too early in the session, or a plan without them) is
        # silence, and silence is not evidence that the last real reading was
        # wrong. The staleness gate in window.py is what retires it.
        return []
    path = cache_path(root)
    now = time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        merged = {}
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
            for w in (old.get("windows") or []):
                if isinstance(w, dict) and w.get("kind"):
                    merged[w["kind"]] = w
        except (OSError, ValueError, AttributeError):
            pass
        for w in windows:
            held = merged.get(w["kind"])
            if held and held.get("resets_at") == w.get("resets_at") \
                    and isinstance(held.get("used_percent"), (int, float)) \
                    and held["used_percent"] > w["used_percent"]:
                continue          # same window, lower number: a late sample
            merged[w["kind"]] = dict(w, seen_at=now)
        # Drop windows whose reset has passed rather than carrying them
        # forever; Claude Code stops sending them, so nothing would.
        live = [w for w in merged.values()
                if not (isinstance(w.get("resets_at"), (int, float))
                        and w["resets_at"] <= now)]
        live.sort(key=lambda w: w["used_percent"], reverse=True)
        blob = {"observed_at": now,
                "source": "claude",
                "session": (payload or {}).get("session_id"),
                "version": (payload or {}).get("version"),
                "windows": live}
        tmp = f"{path}.{os.getpid()}.tmp"
        # 0o600 like the letters and the journal. This file becomes OBSERVED
        # in a document that says it was established by dimissory rather than
        # by the agent, so it should not be readable by every account on the
        # host just because the umask was loose.
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        os.replace(tmp, path)          # atomic: a reader never sees a partial
    except OSError:
        pass
    return windows


def _fmt_reset(epoch):
    if not isinstance(epoch, (int, float)):
        return ""
    try:
        return time.strftime("%H:%M", time.localtime(epoch))
    except (ValueError, OSError, OverflowError):
        return ""


def describe(payload, windows):
    """The line we print when we are the whole statusline.

    Short, because the bar is narrow, and about the plan window, because that
    is what this tool is for. The model name earns its place: it is the thing
    people most often want the bar to tell them.
    """
    bits = []
    model = ((payload or {}).get("model") or {}).get("display_name")
    if isinstance(model, str) and model:
        bits.append(model)
    for w in windows[:2]:
        short = {"claude 5h": "5h", "claude 7d": "7d",
                 "claude spend": "spend"}.get(w["label"], w["kind"])
        bits.append(f"{short} {w['used_percent']:.0f}%")
    if windows and windows[0]["resets_at"]:
        when = _fmt_reset(windows[0]["resets_at"])
        if when:
            bits.append(f"resets {when}")
    ctx = ((payload or {}).get("context_window") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)):
        bits.append(f"ctx {ctx:.0f}%")
    return "  ".join(bits) if bits else "dimissory"


def main(argv=None, stdin=None):
    """`dim statusline [--wrap CMD]` -- record the window, then print a bar.

    Exits 0 on every path. A non-zero statusline is noise the user cannot
    silence from inside Claude Code, and a crashed one can replace their bar
    with a stack trace.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    wrap = None
    if "--wrap" in argv:
        i = argv.index("--wrap")
        wrap = argv[i + 1] if i + 1 < len(argv) else None
    root = None
    if "--cache" in argv:
        i = argv.index("--cache")
        root = argv[i + 1] if i + 1 < len(argv) else None

    raw = ""
    try:
        raw = (stdin or sys.stdin).read()
    except (OSError, UnicodeDecodeError):
        pass
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        windows = record(payload, root)
    except Exception:                                        # noqa: BLE001
        windows = []

    if wrap:
        # Somebody else's statusline. Theirs is what shows; we only listened.
        # Installing a tool must not cost a person the bar they already had.
        #
        # RUN THROUGH A SHELL, because that is how Claude Code runs
        # `statusLine.command`. The first version used shlex.split with no
        # shell, which silently changed the meaning of every command with a
        # pipe, a redirect or an `&&` in it -- and the example in Claude
        # Code's own statusline documentation is a `jq` pipeline. Measured by
        # review: `echo hello && echo world` printed "hello && echo world".
        #
        # Passing an existing command back to the same interpreter that ran
        # it before is not an added risk: it is already in the user's
        # settings file and Claude Code already executes it this way.
        try:
            done = subprocess.run(wrap, shell=True, input=raw,
                                  capture_output=True, text=True,
                                  timeout=WRAP_TIMEOUT)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Their command is broken or hung. Say which, once, in the bar --
            # silence here would look like dimissory ate their statusline.
            sys.stdout.write("dimissory: wrapped statusline failed")
            return 0
        sys.stdout.write(done.stdout)
        if done.returncode != 0 and not done.stdout.strip():
            # A non-zero exit that printed nothing used to vanish entirely,
            # leaving an empty bar and no clue why. Their stderr is not echoed
            # into the bar (it would corrupt the line), so the exit code is
            # the only thing left to report.
            sys.stdout.write(f"dimissory: wrapped statusline exited "
                             f"{done.returncode}")
        return 0

    try:
        sys.stdout.write(describe(payload, windows))
    except Exception:                                        # noqa: BLE001
        sys.stdout.write("dimissory")
    return 0
