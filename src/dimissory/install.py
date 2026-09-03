"""Writing hook config into files this project does not own.

The predecessor destroyed a real `~/.claude/settings.json` doing exactly this:
it coerced a file it could not parse to `{}` and wrote that back, taking the
user's theme, notification preference and permission rules with it. Then it did
the same thing again to a live settings file while testing the fix.

So the rules here are the ones that would have prevented it, and each is
asserted in tests/test_install.py:

    refuse       a file that will not parse, or is the wrong shape, is never
                 rewritten -- not repaired, not replaced, not "fixed"
    back up      whatever was there survives, under a name that is never reused
    ask          the operator sees the diff and can decline; declining is not
                 reported as success
    merge        existing hooks and every unrelated key are preserved
    idempotent   installing twice adds nothing the second time

Three targets, one JSON shape, because all three CLIs implement the contract
Claude Code defined:

    ~/.claude/settings.json     Claude Code   (Grok also reads this one)
    ~/.codex/hooks.json         Codex
    ~/.grok/hooks/dimissory.json  Grok
"""

from __future__ import annotations

import json
import os
import shutil
import time

# Which events each host actually honours. Measured per CLI -- see hook.py.
# Codex has no bare turn-end Stop in its event registry, so asking for one
# there would install a hook that never fires and quietly does nothing.
#
# PostToolUse IS THE ONE THAT MATTERS and it was missing from all three. The
# window check -- the whole "seal before the wall" claim -- lives on that event
# in hook.py, and with no target registering it the check was unreachable in
# every real installation. It measured green only because the test handed
# `handle()` a PostToolUse payload directly, which bypasses the installer, and
# the installer is the only thing that decides whether the event ever arrives.
# Found by external review, not by this suite. A tool call is also the only
# regular heartbeat a hook gets, so there is nowhere else for it to live.
TARGETS = {
    "claude": {
        "label": "Claude Code",
        "path": "~/.claude/settings.json",
        "events": ("SessionStart", "PostToolUse", "Stop", "PreCompact",
                   "SessionEnd"),
        "root_key": "hooks",
        "detect": ("claude",),
    },
    "codex": {
        "label": "Codex CLI",
        "path": "~/.codex/hooks.json",
        "events": ("SessionStart", "PostToolUse", "PreCompact", "SessionEnd"),
        "root_key": "hooks",
        "detect": ("codex",),
    },
    "grok": {
        "label": "Grok CLI",
        "path": "~/.grok/hooks/dimissory.json",
        "events": ("SessionStart", "PostToolUse", "Stop", "PreCompact",
                   "SessionEnd"),
        "root_key": "hooks",
        "detect": ("grok",),
    },
}

# The event the window check lives on. Named here so a test can assert every
# target registers it, rather than trusting three tuples to stay in agreement
# with a branch in another file.
WINDOW_EVENT = "PostToolUse"

# How we recognise our own entry on a re-install. This is a TUPLE because the
# command is resolved per environment, and the forms it takes do not share one
# substring: an absolute console script ends in `.../dim hook`, while a source
# checkout with nothing installed gets `"<python>" -m dimissory.cli hook`. A
# marker matching only the first form would fail to spot our own entry in the
# second, and every re-install would append another copy of a hook that was
# already there -- silently, since duplicates still fire.
MARKERS = ("dim hook", "dimissory hook", "dimissory.cli hook")

# How we recognise our own statusline entry.
#
# This was the bare substring "statusline", and both R3 reviewers found it
# independently. Measured: it matches `~/.claude/statusline.sh` -- THE PATH IN
# CLAUDE CODE'S OWN DOCUMENTATION -- and `claude-code-statusline`, and
# `powerline-statusline`, and anything else with the word in it. Install then
# reported "already installed" and did nothing, so the meter was never
# recorded, `dim status` told the user to run `--install`, and running it
# again did nothing again.
#
# That is this lineage's signature defect -- a no-op reported as success --
# landing on the single step the whole feature depends on. Same shape as the
# hook MARKERS now: match OUR command and OUR subcommand, not a word.
STATUSLINE_MARKERS = ("dim statusline", "dimissory statusline",
                      "dimissory.cli statusline")
STATUSLINE_MARKER = STATUSLINE_MARKERS[0]        # kept for callers and tests


def is_our_statusline(entry):
    """Whether a statusLine value is one we wrote, in any resolved form."""
    blob = entry if isinstance(entry, str) else json.dumps(entry)
    return any(m in blob for m in STATUSLINE_MARKERS)
MARKER = MARKERS[0]                          # kept: callers and tests use it


def is_ours(entry):
    """Whether a hook entry is one we wrote, in any of its resolved forms."""
    blob = json.dumps(entry)
    return any(m in blob for m in MARKERS)


class InstallRefused(Exception):
    """The file was not in a state we are willing to rewrite."""


# Where these CLIs live when they are NOT on PATH. Grok's own installer puts
# it in ~/.grok/bin, and a box that runs Grok all day can still fail
# `which grok` -- measured. `dim status` then printed `grok  no` and setup
# never offered to install its hooks, on the machine Grok was running on.
# Detecting only what is on PATH is detecting the operator's shell, not the
# agent.
EXTRA_PLACES = {
    "grok": ("~/.grok/bin/grok", "~/.grok/downloads/grok-linux-x86_64"),
    "claude": ("~/.local/bin/claude", "~/.claude/local/claude"),
    "codex": ("~/.npm-global/bin/codex", "~/.local/bin/codex"),
}


def detect():
    """Where each agent CLI actually is, keyed as TARGETS is keyed.

    Keyed identically on purpose. The predecessor's setup detected into a dict
    keyed by CLI name and then asked `if "anthropic" in found`, which was never
    true, so the one step that mattered silently never ran while setup reported
    success every time.
    """
    found = {}
    for key, spec in TARGETS.items():
        hit = next((shutil.which(c) for c in spec["detect"] if shutil.which(c)),
                   None)
        if hit is None:
            for candidate in EXTRA_PLACES.get(key, ()):
                p = os.path.expanduser(candidate)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    hit = p
                    break
        found[key] = hit
    return found


def _fingerprint(path):
    """Enough to tell whether the file changed under us. None when absent.

    Content, not mtime: mtime has one-second resolution on some filesystems
    and an edit inside the same second would be invisible.
    """
    try:
        with open(path, "rb") as fh:
            import hashlib
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _block(events, command):
    return {e: [{"hooks": [{"type": "command", "command": command}]}]
            for e in events}


def _backup(path):
    """Keep whatever was there. Never overwrite a previous backup.

    The predecessor's second install copied its own generated file over the
    first backup, so the operator's original -- the only thing worth keeping --
    was gone after two installs.

    The first version of THIS function reintroduced that, one step further
    along. It reserved `<path>.dim-backup`, and when that name was already
    taken it fell back to a name stamped to the SECOND with no existence check
    at all. Measured: with a leftover `.dim-backup` present and two installs
    inside one second, the timestamped copy was overwritten by the
    already-merged file, and no backup on disk was pre-install any more --
    `recoverable pristine original: NONE`. The docstring promised "a name that
    is never reused" and the code chose one that could be.

    So the name is now claimed with O_CREAT|O_EXCL, which cannot lose a race
    with another process or with an earlier copy in the same second, and the
    counter walks until it finds a name nobody holds.
    """
    if not os.path.exists(path):
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    candidates = [path + ".dim-backup", f"{path}.dim-backup.{stamp}"]
    candidates += [f"{path}.dim-backup.{stamp}.{n}" for n in range(1, 1000)]
    for dest in candidates:
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError:
            return None
        os.close(fd)
        # copyfile, not copy2: the destination already exists (we just claimed
        # it), and copystat is applied afterwards so the mode we opened with
        # does not outlive the copy.
        shutil.copyfile(path, dest)
        try:
            shutil.copystat(path, dest)
        except OSError:
            pass
        return dest
    return None


def plan(target, command, path=None):
    """What installing would do, without doing it.

    Returns (existing_json, merged_json, added_events). Raises InstallRefused
    for anything we will not rewrite.
    """
    spec = TARGETS[target]
    p = os.path.expanduser(path or spec["path"])
    existing = {}
    if os.path.exists(p):
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
            existing = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise InstallRefused(
                f"{p} could not be read as JSON ({e}). Refusing to touch it -- "
                f"fix or move it and run this again. Nothing was changed.")
        if not isinstance(existing, dict):
            raise InstallRefused(
                f"{p} is a {type(existing).__name__}, not an object. Refusing "
                f"to rewrite it. Nothing was changed.")

    hooks = existing.get(spec["root_key"], {})
    if hooks is not None and not isinstance(hooks, dict):
        raise InstallRefused(
            f"{p} has a '{spec['root_key']}' key that is a "
            f"{type(hooks).__name__}, not an object. Refusing to rewrite it.")

    merged = dict(existing)
    new_hooks = {k: list(v) if isinstance(v, list) else v
                 for k, v in (hooks or {}).items()}
    added = []
    for event, entries in _block(spec["events"], command).items():
        current = new_hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise InstallRefused(
                f"{p}: hooks.{event} is a {type(current).__name__}, not a "
                f"list. Refusing to rewrite it.")
        if any(is_ours(x) for x in current):
            continue                      # already ours; installing twice is a no-op
        current.extend(entries)
        added.append(event)
    merged[spec["root_key"]] = new_hooks
    return existing, merged, added


def plan_statusline(command, path=None):
    """What installing the statusline would do. Returns (existing, merged, note).

    Claude Code's `statusLine` is a single command, not a list, so unlike the
    hooks there is no merging: installing here REPLACES whatever is set. That
    would silently cost somebody the status bar they built, so an existing
    command is WRAPPED -- ours records the window, then runs theirs and prints
    its output verbatim.

    Note the asymmetry with hooks and why it is not an oversight: a host that
    supports many hooks per event lets us add one alongside. A single-valued
    setting does not, so the only honest options are to wrap or to refuse.
    """
    p = os.path.expanduser(path or TARGETS["claude"]["path"])
    existing = {}
    if os.path.exists(p):
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
            existing = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (OSError, ValueError, UnicodeDecodeError) as e:
            raise InstallRefused(
                f"{p} could not be read as JSON ({e}). Refusing to touch it. "
                f"Nothing was changed.")
        if not isinstance(existing, dict):
            raise InstallRefused(
                f"{p} is a {type(existing).__name__}, not an object. Refusing "
                f"to rewrite it. Nothing was changed.")

    current = existing.get("statusLine")
    if current is not None and is_our_statusline(current):
        return existing, dict(existing), "already installed"
    theirs = None
    keep = {}
    if isinstance(current, dict):
        theirs = current.get("command")
        # `padding` and `refreshInterval` are Claude Code's own statusLine
        # settings and they were being DROPPED on install. refreshInterval
        # matters most: it is the only knob that re-samples while the session
        # is idle, which is exactly the gap this meter has.
        keep = {k: v for k, v in current.items()
                if k not in ("type", "command")}
    elif isinstance(current, str) and current:
        theirs = current                      # the older string form

    if theirs:
        # Quoted for the platform that will actually split it. `shlex.quote`
        # emits POSIX single quotes, which cmd.exe passes through as literal
        # characters -- hook._quote exists for precisely this and was not
        # being used here, so the same bug class had a new call site.
        from .hook import _quote
        full = f"{command} --wrap {_quote(str(theirs))}"
        note = f"wrapping the existing statusline: {theirs}"
    else:
        full = command
        note = "no statusline was set"

    merged = dict(existing)
    block = {"type": "command", "command": full}
    block.update(keep)                    # their padding/refreshInterval survive
    # Without a refresh, the sample only updates when Claude Code re-renders --
    # which is NOT on tool calls. A long tool or reasoning stretch freezes the
    # reading while the seal heartbeat keeps firing, so the two clocks drift.
    block.setdefault("refreshInterval", 60000)
    merged["statusLine"] = block
    return existing, merged, note


def install_statusline(command=None, path=None, assume_yes=False,
                       out=print, ask=None, verify=True):
    """Install `dim statusline` as Claude Code's statusLine. (path, note)."""
    if command is None:
        from .hook import dim_command
        command = f"{dim_command()} statusline"
    p = os.path.expanduser(path or TARGETS["claude"]["path"])
    existing, merged, note = plan_statusline(command, p)
    if note == "already installed":
        out(f"  Claude Code statusline: already installed at {p}")
        return p, note

    out(f"  Claude Code statusline: about to edit {p}")
    out(f"    this is what gives Claude a plan-window meter at all;")
    out(f"    Claude Code writes the percentage nowhere else.")
    out(f"    command:  {merged['statusLine']['command']}")
    out(f"    {note}")
    kept = sorted(k for k in existing if k != "statusLine")
    out(f"    keys left as-is: {', '.join(kept) if kept else '(none)'}")

    before = _fingerprint(p)
    if not assume_yes:
        answer = (ask or input)("    proceed? [y/N] ")
        if not str(answer).strip().lower().startswith("y"):
            out("    declined; nothing was changed")
            return None, "declined"
    if _fingerprint(p) != before:
        raise InstallRefused(
            f"{p} changed while waiting for an answer. Nothing was written -- "
            f"run this again to plan against the current file.")

    backup = _backup(p)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".dim-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    out(f"    wrote {p}" + (f" (previous kept at {backup})" if backup else ""))
    return p, note


def install(target, command=None, path=None, assume_yes=False,
            out=print, ask=None, verify=True):
    """Install our hooks into one host. Returns (path, added) or (None, []).

    `command` defaults to the ABSOLUTE invocation for this environment, not to
    a bare `dim hook`. The host runs its hooks through a shell carrying its own
    PATH; ours is not it.
    """
    if command is None:
        from .hook import hook_command
        command = hook_command()
    # VERIFIED BEFORE IT IS WRITTEN. A hook that cannot run is indistinguishable
    # from a hook with nothing to say -- the failure this project keeps
    # rediscovering -- and the command can legitimately resolve to something
    # unrunnable: from a source checkout with nothing installed, the fallback
    # `<python> -m dimissory.cli` cannot find its own package once the host
    # strips the environment. Refusing to install beats installing silence.
    if verify:
        from .hook import command_works
        ok, why = command_works(command)
        if not ok:
            raise InstallRefused(
                f"the hook command does not work, so installing it would add a "
                f"hook that silently does nothing.\n    command: {command}\n"
                f"    {why}\n    Install dimissory (pip install dimissory) so "
                f"`dim` resolves, then run this again. Nothing was changed.")
    spec = TARGETS[target]
    p = os.path.expanduser(path or spec["path"])
    existing, merged, added = plan(target, command, p)
    if not added:
        out(f"  {spec['label']}: already installed at {p} -- nothing to do")
        return p, []

    out(f"  {spec['label']}: about to edit {p}")
    out(f"    add hooks for: {', '.join(added)}")
    out(f"    command:       {command}")
    kept = sorted(k for k in existing if k != spec["root_key"])
    out(f"    keys left as-is: {', '.join(kept) if kept else '(none)'}")
    if os.path.exists(p):
        out(f"    a copy of the current file is kept alongside it")

    before = _fingerprint(p)
    if not assume_yes:
        answer = (ask or input)(f"    proceed? [y/N] ")
        if not str(answer).strip().lower().startswith("y"):
            out("    declined; nothing was changed")
            return None, []

    # The file may have changed while the operator was reading the prompt --
    # an agent editing permissions, the CLI itself writing a preference, the
    # user in another window. `merged` was computed BEFORE the question was
    # asked, so writing it now would silently revert whatever landed in
    # between, and the only copy of it would be the backup nobody knows to
    # look in. Refusing costs one re-run; the alternative costs the edit.
    if _fingerprint(p) != before:
        raise InstallRefused(
            f"{p} changed while waiting for an answer. Nothing was written -- "
            f"run this again to plan against the current file.")

    backup = _backup(p)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".dim-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    out(f"    wrote {p}" + (f" (previous kept at {backup})" if backup else ""))
    return p, added
