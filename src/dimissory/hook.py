"""The hook: how declaring stops depending on the agent remembering.

Measured, not assumed. All three CLIs implement the same hook contract -- the
one Claude Code defined -- and each was driven with a real task whose prompt
never mentioned declaring:

    Claude Code   ~/.claude/settings.json      SessionStart + additionalContext
                  complied on claude-haiku-4-5 and claude-sonnet-5
    Codex         ~/.codex/hooks.json          SessionStart + additionalContext
                  complied on 4 of 4 runs
    Grok          ~/.grok/hooks/*.json         Stop hook, decision=block
                  complied; SessionStart additionalContext was IGNORED there

Grok's docs list `~/.claude/settings.json` as a hook source under "Claude Code
compatibility, always trusted", and Codex's binary carries CLAUDE_PLUGIN_ROOT
and disableAllHooks. One JSON shape reaches all three.

TWO MECHANISMS, because they are not equally strong:

  ASK   SessionStart returns hookSpecificOutput.additionalContext, which is
        injected into the model's context. It is a request. It worked every
        time it was measured on Claude Code and Codex, and not at all on Grok.

  GATE  Stop returns {"decision": "block", "reason": ...}, which refuses to let
        the turn end and feeds the reason back. It is not a request, and it is
        the only thing here that does not depend on the model choosing to
        cooperate. Grok has it. Claude Code has it. Codex's event registry has
        no bare turn-end Stop, so Codex gets the ask alone.

The gate must never trap an agent in a loop, so it fires once: the payload
carries `stopHookActive` (already continuing because of a stop hook) and it
yields immediately when that is set.

WHAT THIS STILL DOES NOT DO: make the CONTENT good. A gate can require that
`dim declare` was called; it cannot require that what the agent wrote is worth
reading. Both external reviews said the journal mitigates the compliance
problem rather than fixing it, and a gate narrows it further without closing
it.
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import journal
from .brief import Brief
from .observe import checks_for, observe
from .render import render

# The payload key conventions differ. Claude Code and Codex use snake_case;
# Grok uses camelCase and supplies no transcript path at all. Reading only one
# convention would silently produce a hook that fires and learns nothing --
# which is this project's recurring defect wearing a new hat.
_ALIASES = {
    "event": ("hook_event_name", "hookEventName"),
    "session": ("session_id", "sessionId"),
    "transcript": ("transcript_path", "transcriptPath"),
    "cwd": ("cwd", "workspaceRoot"),
    "stop_active": ("stop_hook_active", "stopHookActive"),
}


def field(payload, name, default=None):
    for key in _ALIASES[name]:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


def normalise_event(raw):
    """`session-start`, `session_start` and `SessionStart` are one event."""
    return "".join(str(raw or "").replace("-", "_").split("_")).lower()


def _quote(part):
    """Shell-safe on the platform that will actually run it.

    `shlex.quote` wraps in POSIX single quotes, which cmd.exe does not
    understand -- it would pass the quotes through as part of the path. The
    Windows convention is double quotes, and only when they are needed.
    """
    if os.name == "nt":
        return f'"{part}"' if (" " in part or "\t" in part) else part
    import shlex
    return shlex.quote(part)


def dim_command():
    """The exact invocation to run dimissory: absolute, and shell-safe.

    Emitting a bare `dim` assumes it is on the reader's PATH, which is a
    different PATH from ours and frequently does not contain it. Measured
    twice, both silent:

      the agent   the hook fired, injected its instruction, and the agent did
                  nothing, because `dim` was not resolvable for it. An
                  instruction the reader cannot execute is indistinguishable
                  from no instruction at all.
      the host    a hook host runs its command through a shell whose PATH is
                  its own. With dimissory in a venv or ~/.local/bin, `dim hook`
                  exits 127 and the hook never fires -- and a hook that never
                  fires looks exactly like a hook with nothing to say.

    Resolution order matters. The console script belonging to THIS interpreter
    wins over anything on PATH, because a venv or a source checkout can easily
    find a DIFFERENT, older `dim` first and write that into a config that then
    points at the wrong install for good.
    """
    import shutil
    names = ("dim.exe", "dimissory.exe") if os.name == "nt" else ("dim", "dimissory")
    bindir = os.path.dirname(os.path.abspath(sys.executable))
    for name in names:
        cand = os.path.join(bindir, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return _quote(cand)
    for name in ("dim", "dimissory"):
        found = shutil.which(name)
        if found:
            return _quote(found)
    # Always correct, even from a source checkout with nothing installed --
    # and quoted, because the default Windows install lives under a path with
    # a space in it ("C:\Program Files\..."), where an unquoted command line
    # splits into a first word that is not an interpreter.
    return f"{_quote(sys.executable)} -m dimissory.cli"


def hook_command():
    """What a hook host should be configured to run."""
    return dim_command() + " hook"


# WORDING IS PART OF THE MECHANISM, and this was measured the hard way.
#
# The first version explained the product, offered four command variants and
# ended with advice. The hook fired, the command it emitted was correct and
# resolvable, and the agent did the task and stopped without declaring -- twice.
#
# What had worked in the probe that established the mechanism was short,
# imperative, and ONE command: "IMPORTANT: before you finish, you MUST run the
# command: ...". A menu invites a choice, and the choice an agent makes on a
# small task is to skip it.
#
# So: one command, one imperative, and the detail moved AFTER it where it
# cannot dilute the instruction.
ASK = (
    "IMPORTANT: before you finish, you MUST run this command:\n"
    "  {dim} declare --session {sid} --task \"<one line: what this session is "
    "for>\" --next \"<one line: the exact next action>\"\n"
    "It records a handoff so work survives this session's plan window closing. "
    "Run it now, and run it again with an updated --next whenever the next "
    "action changes. You may also add --decided \"...\" or --ruled-out "
    "\"...\" as you go."
)

GATE = (
    "This session has recorded nothing for its handoff letter, so if the "
    "window closes now the letter will be empty of everything that matters. "
    "Before finishing, run:\n"
    "  {dim} declare --session {sid} --task \"<what this session was for>\" "
    "--next \"<the exact next action for whoever continues>\"\n"
    "Then finish normally."
)


def handle(payload, journal_root=None, letters_dir=None):
    """Act on one hook payload. Returns the JSON string to print, or "".

    Never raises. A hook that fails must not break the user's session: the
    cost of a dropped observation is one sample, and the cost of a broken tool
    call is the user's work.
    """
    try:
        return _handle(payload, journal_root, letters_dir)
    except Exception:                                    # noqa: BLE001
        return ""


def _declared_anything(sid, root):
    values, _ages, _dmg = journal.read(sid, root)
    return bool(values)


def _seal_state_path(sid, journal_root):
    root = os.path.expanduser(journal_root or "~/.dimissory/journal")
    return os.path.join(root, ".sealed", f"{sid[:60]}.json")


def _sealed_recently(sid, journal_root, win, reseal_after):
    """Whether a letter for THIS window was already sealed, recently enough.

    Without this the trigger is not a trigger, it is a loop. Crossing the
    margin is not an event that happens once: the window stays past it for the
    rest of the session, so every following tool call would seal another
    letter -- each one shelling out to git to do it. One letter per tool call,
    for hours.

    Keyed on the window's reset time, so a genuinely NEW window seals again
    rather than being suppressed by a marker left over from the old one. That
    distinction is the reason this is not just a timestamp.
    """
    try:
        with open(_seal_state_path(sid, journal_root), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    if state.get("resets_at") != (win.resets_at if win else None):
        return False                  # different window; the old letter is not about it
    at = state.get("at")
    if not isinstance(at, (int, float)):
        return False                  # no usable stamp is not "sealed just now"
    return 0 <= (time.time() - at) < reseal_after


def _record_seal(sid, journal_root, win):
    path = _seal_state_path(sid, journal_root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"resets_at": win.resets_at if win else None,
                       "at": time.time()}, fh)
        os.replace(tmp, path)
    except OSError:
        pass          # a lost marker costs a duplicate letter, not the session


def _handle(payload, journal_root, letters_dir):
    event = normalise_event(field(payload, "event"))
    sid = field(payload, "session")
    if not sid:
        return ""                    # nothing to key a journal on; stay silent

    if event in ("sessionstart", "userpromptsubmit"):
        if _declared_anything(sid, journal_root):
            return ""                # already declaring; do not nag every turn
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ASK.format(sid=sid, dim=dim_command())}})

    if event in ("stop", "subagentstop"):
        # Fire ONCE. `stopHookActive` is true when the agent is already
        # continuing because of a stop hook, and blocking again from there is
        # how a gate becomes an infinite loop.
        if field(payload, "stop_active"):
            return ""
        if _declared_anything(sid, journal_root):
            return ""
        return json.dumps({"decision": "block",
                           "reason": GATE.format(sid=sid, dim=dim_command())})

    # THE TRIGGER. A tool call is the only regular heartbeat a hook gets, so
    # this is where the window is checked. Sealing here means the letter is
    # written while the agent still has budget -- which is the entire claim,
    # and the difference from every tool that reacts to a 429.
    if event in ("posttooluse", "posttoolusefailure"):
        from . import window as _W
        from .config import Config, seconds
        cfg = Config.load(None)
        win = _W.read(transcript=field(payload, "transcript"))
        due = _W.should_seal(win, float(cfg.get("window", "write_at") or 0.85))
        if due:
            # Seal once per window, then refresh at an interval. A letter
            # written at 85% and never touched again is describing a session
            # that has since run to 99%.
            reseal = seconds(cfg.get("window", "reseal_after"), 600.0)
            if _sealed_recently(sid, journal_root, win, reseal):
                return ""
            path = seal(sid, payload, journal_root, letters_dir)
            if path:
                _record_seal(sid, journal_root, win)
                return json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext":
                        f"dimissory: {win.used_percent:.0f}% of your "
                        f"{win.label()} window is gone, so a handoff letter was "
                        f"sealed at {path}. If your next action has changed, "
                        f"record it now with `dim declare --session {sid} "
                        f"--next \"...\"` -- there may not be a later chance."}})
        return ""

    if event in ("precompact", "sessionend"):
        path = seal(sid, payload, journal_root, letters_dir)
        if path:
            return json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreCompact",
                "additionalContext":
                    f"dimissory wrote a handoff letter to {path}. If you are "
                    f"about to lose context, read it back rather than "
                    f"reconstructing."}})
        return ""

    return ""


def seal(sid, payload, journal_root=None, letters_dir=None):
    """Fold the journal and the world into a letter. Returns its path or None."""
    import time
    cwd = field(payload, "cwd") or os.getcwd()
    letters = os.path.expanduser(letters_dir or "~/.dimissory/letters")
    jroot = os.path.expanduser(journal_root or "~/.dimissory/journal")
    ours = (letters, jroot)

    # The meter. This is what makes a letter possible BEFORE the wall rather
    # than after a 429, and it costs one read of the transcript the hook
    # already handed us. Absent or stale reads as None, and the renderer omits
    # the line rather than inventing a percentage.
    from . import window as _W
    win = _W.read(transcript=field(payload, "transcript"))
    observed = observe(cwd=cwd, transcript=field(payload, "transcript"),
                       our_dirs=ours,
                       window=win.as_dict() if win else None)
    declared, ages, _damaged = journal.to_declared(sid, root=journal_root)
    # The expectation must be the command's REAL OUTPUT. Without this the check
    # fell back to a derived list of paths, which does not look like
    # `git status --porcelain` output and so could never match it -- a check
    # that always disagrees is as useless as one that always passes. Review
    # caught this once already, in cli.py; the rule had not reached here.
    from .observe import _exclude_pathspec, _git
    porcelain = None
    spec = _exclude_pathspec(cwd, *ours)
    rels = []
    for d in ours:
        try:
            r = os.path.relpath(os.path.realpath(d), os.path.realpath(cwd))
        except ValueError:
            continue
        if not r.startswith(os.pardir) and not os.path.isabs(r):
            rels.append(f":(exclude){r.replace(os.sep, '/')}")
    got = _git(cwd, "status", "--porcelain", *(["--", *rels] if rels else []))
    if isinstance(got, str):
        porcelain = got
    brief = Brief(session=sid, observed=observed, declared=declared,
                  ages=ages, checks=checks_for(observed, cwd=cwd,
                                               our_dirs=ours,
                                               porcelain=porcelain))
    os.makedirs(letters, exist_ok=True)
    name = f"{sid[:60]}-{time.strftime('%Y%m%dT%H%M%S')}.md"
    path = os.path.join(letters, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(brief))
    return path


def main(argv=None, stdin=None):
    """`dim hook` -- read one payload on stdin, print any response on stdout.

    Always exits 0 except where a gate deliberately blocks, because an
    unexpected non-zero from a hook is indistinguishable from a deny on some of
    these hosts and would start denying the user's tool calls.
    """
    raw = (stdin or sys.stdin).read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    out = handle(payload)
    if out:
        sys.stdout.write(out)
    return 0
