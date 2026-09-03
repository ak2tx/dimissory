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


def dim_command():
    """The exact invocation the agent should run, resolved absolutely.

    Emitting a bare `dim` assumes it is on the AGENT's PATH, which is a
    different PATH from the hook's and is frequently not. Measured: the hook
    fired, injected its instruction, and the agent silently did nothing --
    because `dim` was not resolvable for it. An instruction the reader cannot
    execute is indistinguishable from no instruction at all.

    Prefer a real `dim`/`dimissory` on PATH, since that survives the package
    being upgraded. Fall back to running this interpreter against this module,
    which is always correct even from a source checkout.
    """
    import shutil
    for name in ("dim", "dimissory"):
        found = shutil.which(name)
        if found:
            return found
    return f"{sys.executable} -m dimissory.cli"


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

    observed = observe(cwd=cwd, transcript=field(payload, "transcript"),
                       our_dirs=ours)
    declared, ages, _damaged = journal.to_declared(sid, root=journal_root)
    brief = Brief(session=sid, observed=observed, declared=declared,
                  ages=ages, checks=checks_for(observed, cwd=cwd,
                                               our_dirs=ours))
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
