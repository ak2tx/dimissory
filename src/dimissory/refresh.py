"""Making a stale meter current, by asking the vendor to fetch it.

Everything else in this project READS what a vendor happened to leave on disk.
This is the one place that makes something happen, and it exists because
reading was not enough:

    Grok, measured on a live account
      cache says   35.0%   -- 61.3 hours old, while the file's mtime looked
                              fresh, because the session kept appending other
                              lines to the same log
      truth        80.0%

Forty-five points, hidden for two and a half days. Grok writes its billing row
only when the interactive pager starts; `grok -p` never writes it, no matter
how much work the agent does. So an agent-driven box can run the account to
zero while dimissory reports a third of it used.

WHAT THIS DOES NOT DO is invent the number, ask an undocumented endpoint, or
parse a screen. It starts the vendor's own CLI the way a person would, lets it
do the billing fetch it always does at startup, and quits. The vendor writes
its own file; we only caused it to.

COST, measured rather than assumed: an EMPTY startup with no prompt runs no
model turn and spends nothing. 22 seconds, and the reading moved 35 -> 80.
Sending a prompt would also refresh, and would spend the window it is
reporting on, so nothing here ever sends one.

Only Grok needs this. Codex writes its window into the rollout as it works,
and Claude hands its window to the statusline on every render -- both refresh
themselves during ordinary use. Grok is the one vendor whose number goes stale
precisely while you are using it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

# Long enough for auth plus the billing fetch, which was measured at about
# 100ms after startup; short enough that a wedged CLI cannot hold up a command
# somebody is watching. The process is expected to be killed by this: it is an
# interactive TUI with nothing to do, so a non-zero exit is the normal path,
# not a failure.
TIMEOUT = 25


def grok_binary(root=None):
    """Where Grok actually is. PATH is not enough -- it usually is not there.

    Grok's installer puts the binary under ~/.grok, so `which grok` fails on a
    box that runs Grok all day. Detecting only PATH detects the operator's
    shell, not the agent.
    """
    for name in ("grok",):
        found = shutil.which(name)
        if found:
            return found
    base = os.path.expanduser(root or "~/.grok")
    for rel in ("bin/grok", "downloads/grok-linux-x86_64",
                "downloads/grok-linux-arm64", "downloads/grok-darwin-arm64"):
        p = os.path.join(base, rel)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def can_refresh(provider):
    """Whether we know a way to make this provider's reading current."""
    return provider == "grok" and grok_binary() is not None


def grok(root=None, timeout=TIMEOUT):
    """Start Grok's TUI briefly so it writes a fresh billing row.

    Returns True when the log gained a newer reading than it had. Never
    raises: a meter that cannot refresh should report a stale number, not
    fail.

    `script` is required, not incidental. Grok fetches billing on PAGER
    startup, and the pager only starts on a terminal -- headless `grok -p`
    was measured writing `startup complete` with no billing line at all. A
    pseudo-terminal is what makes it behave like the interactive case.
    """
    binary = grok_binary(root)
    if binary is None or not shutil.which("script"):
        return False
    log = os.path.join(os.path.expanduser(root or "~/.grok"),
                       "logs", "unified.jsonl")
    before = _last_fetch_at(log)
    try:
        subprocess.run(["script", "-qefc", binary, "/dev/null"],
                       stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass                 # expected: an idle TUI has no reason to exit
    except (OSError, ValueError):
        return False
    after = _last_fetch_at(log)
    if after is None:
        return False
    return before is None or after > before


def _last_fetch_at(log):
    """When the newest billing row was FETCHED, from its own `ts`.

    Not the file's mtime. The session appends other lines constantly, so mtime
    said "seconds ago" while the billing row was 61 hours old -- which is
    exactly how a 45-point error stayed invisible.
    """
    from .window import _epoch
    import json
    latest = None
    try:
        size = os.path.getsize(log)
        with open(log, "rb") as fh:
            if size > 2_000_000:
                fh.seek(size - 2_000_000)
                fh.readline()
            for raw in fh:
                if b"creditUsagePercent" in raw:
                    latest = raw
    except OSError:
        return None
    if not latest:
        return None
    try:
        return _epoch(json.loads(latest).get("ts"))
    except ValueError:
        return None


def refresh(provider, **kw):
    """Refresh one provider if we know how. Returns True if it moved."""
    if provider == "grok":
        return grok(**kw)
    # Codex refreshes itself into the rollout as it works, and Claude's
    # statusline refreshes on every render once installed. Neither needs a
    # nudge, and pretending otherwise would spawn a CLI for nothing.
    return False


def stale_providers(readings):
    """Which of {provider: Window|None} could be made current by a refresh."""
    out = []
    for provider, win in readings.items():
        if not can_refresh(provider):
            continue
        if win is None or win.is_stale:
            out.append(provider)
    return out
