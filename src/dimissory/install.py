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
TARGETS = {
    "claude": {
        "label": "Claude Code",
        "path": "~/.claude/settings.json",
        "events": ("SessionStart", "Stop", "PreCompact", "SessionEnd"),
        "root_key": "hooks",
        "detect": ("claude",),
    },
    "codex": {
        "label": "Codex CLI",
        "path": "~/.codex/hooks.json",
        "events": ("SessionStart", "PreCompact", "SessionEnd"),
        "root_key": "hooks",
        "detect": ("codex",),
    },
    "grok": {
        "label": "Grok CLI",
        "path": "~/.grok/hooks/dimissory.json",
        "events": ("SessionStart", "Stop", "PreCompact", "SessionEnd"),
        "root_key": "hooks",
        "detect": ("grok",),
    },
}

# How we recognise our own entry on a re-install. This is a TUPLE because the
# command is resolved per environment, and the forms it takes do not share one
# substring: an absolute console script ends in `.../dim hook`, while a source
# checkout with nothing installed gets `"<python>" -m dimissory.cli hook`. A
# marker matching only the first form would fail to spot our own entry in the
# second, and every re-install would append another copy of a hook that was
# already there -- silently, since duplicates still fire.
MARKERS = ("dim hook", "dimissory hook", "dimissory.cli hook")
MARKER = MARKERS[0]                          # kept: callers and tests use it


def is_ours(entry):
    """Whether a hook entry is one we wrote, in any of its resolved forms."""
    blob = json.dumps(entry)
    return any(m in blob for m in MARKERS)


class InstallRefused(Exception):
    """The file was not in a state we are willing to rewrite."""


def detect():
    """Which agent CLIs are actually on PATH, keyed as TARGETS is keyed.

    Keyed identically on purpose. The predecessor's setup detected into a dict
    keyed by CLI name and then asked `if "anthropic" in found`, which was never
    true, so the one step that mattered silently never ran while setup reported
    success every time.
    """
    return {k: next((shutil.which(c) for c in v["detect"] if shutil.which(c)),
                    None)
            for k, v in TARGETS.items()}


def _block(events, command):
    return {e: [{"hooks": [{"type": "command", "command": command}]}]
            for e in events}


def _backup(path):
    """Keep whatever was there. Never overwrite a previous backup.

    The predecessor's second install copied its own generated file over the
    first backup, so the operator's original -- the only thing worth keeping --
    was gone after two installs.
    """
    if not os.path.exists(path):
        return None
    first = path + ".dim-backup"
    dest = first if not os.path.exists(first) else \
        f"{path}.dim-backup.{time.strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(path, dest)
    return dest


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


def install(target, command=None, path=None, assume_yes=False,
            out=print, ask=None):
    """Install our hooks into one host. Returns (path, added) or (None, []).

    `command` defaults to the ABSOLUTE invocation for this environment, not to
    a bare `dim hook`. The host runs its hooks through a shell carrying its own
    PATH; ours is not it.
    """
    if command is None:
        from .hook import hook_command
        command = hook_command()
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

    if not assume_yes:
        answer = (ask or input)(f"    proceed? [y/N] ")
        if not str(answer).strip().lower().startswith("y"):
            out("    declined; nothing was changed")
            return None, []

    backup = _backup(p)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".dim-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    out(f"    wrote {p}" + (f" (previous kept at {backup})" if backup else ""))
    return p, added
