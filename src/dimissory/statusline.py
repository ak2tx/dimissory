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

# Which windows we understand well enough to name in a letter. Anything else
# in `rate_limits` is still recorded -- the shape is theirs to extend, and a
# window we cannot label is not a window we should silently drop.
LABELS = {"five_hour": "claude 5h",
          "seven_day": "claude 7d",
          "spend_limit": "claude spend"}


def cache_path(root=None):
    return os.path.expanduser(root or DEFAULT_CACHE)


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
        pct = value.get("used_percentage")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue                  # no percentage is not a percentage of 0
        resets = value.get("resets_at")
        out.append({"kind": key,
                    "label": LABELS.get(key, f"claude {key}"),
                    "used_percent": float(pct),
                    "resets_at": resets if isinstance(resets, (int, float))
                    else None})
    # Nearest full first, so a reader taking [0] gets the binding window --
    # the same rule the Codex path uses, for the same reason: the cap that is
    # closest to stopping you is the one that matters.
    out.sort(key=lambda w: w["used_percent"], reverse=True)
    return out


def record(payload, root=None):
    """Write the windows down for the hook to read. Returns them, or [].

    Never raises. A statusline that fails is a failure the user sees on every
    turn, so a lost observation costs one sample and nothing else.
    """
    windows = extract(payload)
    if not windows:
        # Deliberately does NOT clear an existing reading. A payload with no
        # rate_limits (too early in the session, or a plan without them) is
        # silence, and silence is not evidence that the last real reading was
        # wrong. The staleness gate in window.py is what retires it.
        return []
    path = cache_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        blob = {"observed_at": time.time(),
                "source": "claude",
                "session": (payload or {}).get("session_id"),
                "version": (payload or {}).get("version"),
                "windows": windows}
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
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
        try:
            import shlex
            done = subprocess.run(shlex.split(wrap), input=raw,
                                  capture_output=True, text=True, timeout=10)
            sys.stdout.write(done.stdout)
            return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            # Their command is broken. Say which, once, in the bar -- silence
            # here would look like dimissory ate their statusline.
            sys.stdout.write("dimissory: wrapped statusline failed")
            return 0

    try:
        sys.stdout.write(describe(payload, windows))
    except Exception:                                        # noqa: BLE001
        sys.stdout.write("dimissory")
    return 0
