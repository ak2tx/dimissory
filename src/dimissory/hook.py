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
        the turn end and feeds the reason back. Grok has it. Claude Code has
        it. Codex's event registry has no bare turn-end Stop, so Codex gets
        the ask alone.

The gate is STRONGER THAN THE ASK AND STILL NOT A GUARANTEE, and this used to
claim otherwise -- "the only thing here that does not depend on the model
choosing to cooperate", which is false. It blocks ONCE: the continuation
carries `stopHookActive`, and blocking again from there is how a gate becomes
a trap the user has to kill. So an agent that ignores the block finishes
anyway, and the letter is sealed DEGRADED rather than not sealed at all.

That is deliberate -- never trapping a user's session is the higher duty --
but it means the gate raises the cost of not declaring rather than removing
the option, and the docstring should say which.

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


def _seal_state(sid, journal_root):
    """What we already know about sealing for this session, or None.

    Keyed on the window's reset time by the caller, so a genuinely NEW window
    seals again rather than being suppressed by a marker left over from the
    old one. That distinction is why this is not just a timestamp.
    """
    try:
        with open(_seal_state_path(sid, journal_root), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _record_seal(sid, journal_root, win, degraded, first_crossing):
    path = _seal_state_path(sid, journal_root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"resets_at": win.resets_at if win else None,
                       "at": time.time(), "degraded": bool(degraded),
                       "first_crossing": first_crossing}, fh)
        os.replace(tmp, path)
    except OSError:
        pass          # a lost marker costs a duplicate letter, not the session


def _number(value, fallback):
    return value if isinstance(value, (int, float)) else fallback


def _no_meter(sid, payload, journal_root, letters_dir, cfg):
    """What to do on a host with no percentage: watch for the wall itself.

    Claude publishes no utilization figure anywhere on disk, so there is no
    85% to seal at. It DOES write a `quotaLimits` tombstone once a limit has
    actually been hit -- status "rejected", the window kind, and when it
    reopens. That is after the wall rather than before it, and saying so is
    the point: it is the difference between this project's claim and what it
    can deliver on Claude today.

    Sealing here is still worth it. A letter written the moment the wall is
    hit beats nothing, and the reset time answers the first question the next
    session has. It is labelled as an at-the-wall letter so nobody mistakes it
    for the before-the-wall one.

    This is also where `_claude_wall` stopped being dead code -- review found
    it added, tested, and wired to nothing.
    """
    from . import window as _W
    from .config import seconds

    wall = _W._claude_wall(field(payload, "transcript"))
    if not wall or wall.get("status") != "rejected":
        return ""
    at = wall.get("observed_at")
    if not isinstance(at, (int, float)) or (time.time() - at) > _W.MAX_AGE:
        return ""              # an old tombstone is not this session's wall

    reseal = seconds(cfg.get("window", "reseal_after"), 600.0)
    state = _seal_state(sid, journal_root)
    if state is not None and state.get("resets_at") == wall.get("resets_at") \
            and (time.time() - _number(state.get("at"), 0.0)) < reseal:
        return ""

    path = seal(sid, payload, journal_root, letters_dir)
    if not path:
        return ""
    declared = _declared_anything(sid, journal_root)
    _record_seal(sid, journal_root,
                 _W.Window(0.0, resets_at=wall.get("resets_at"),
                           source="claude", observed_at=time.time()),
                 degraded=not declared, first_crossing=time.time())
    when = ""
    if isinstance(wall.get("resets_at"), (int, float)):
        when = (" It reopens at "
                + time.strftime("%H:%M", time.localtime(wall["resets_at"]))
                + ".")
    tail = ("" if declared else
            f" It is marked DEGRADED because nothing was declared. Run "
            f"`{dim_command()} declare --session {sid} --task \"...\" --next "
            f"\"...\"` and it will be rewritten.")
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext":
            f"dimissory: your {wall.get('kind') or 'plan'} limit has been hit, "
            f"so a handoff letter was sealed at {path}.{when}{tail}"}})


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
        # `write_at` read WITHOUT `or`. `cfg.get(...) or 0.85` turned a
        # configured 0 into 0.85, so the one setting that means "always seal"
        # silently could not.
        at = cfg.get("window", "write_at")
        at = float(at) if isinstance(at, (int, float)) else 0.85
        due = _W.should_seal(win, at)
        # None and False are different answers and this used to collapse them.
        # should_seal goes out of its way to distinguish "no meter at all" from
        # "plenty of room left"; discarding that one line later made a Claude
        # session -- which has no meter -- indistinguishable from a session
        # with budget to spare, which is the exact conflation this project's
        # UNMEASURED singleton exists to prevent.
        if due is None:
            return _no_meter(sid, payload, journal_root, letters_dir, cfg)
        if due is False:
            return ""

        # Seal once per window, then refresh at an interval. A letter written
        # at 85% and never touched again is describing a session that has
        # since run to 99%.
        reseal = seconds(cfg.get("window", "reseal_after"), 600.0)
        grace = seconds(cfg.get("window", "grace"), 300.0)
        declared = _declared_anything(sid, journal_root)
        now = time.time()

        state = _seal_state(sid, journal_root)
        same_window = (state is not None and state.get("resets_at")
                       == (win.resets_at if win else None))
        first_crossing = (_number(state.get("first_crossing"), now)
                          if same_window else now)

        upgrading = False
        if same_window:
            # GRACE. The setting used to promise "wait this long for the
            # agent's half before writing without it", which a hook cannot
            # do: blocking a PostToolUse hook for five minutes freezes the
            # user's session, and if the session then dies mid-wait there is
            # no letter at all -- strictly worse than a degraded one.
            #
            # So the letter goes out IMMEDIATELY, and grace is the window
            # during which a letter that went out without the agent's half is
            # UPGRADED the moment that half arrives. Same intent, better
            # guarantee: there is always a letter on disk, and it improves.
            upgrading = (bool(state.get("degraded")) and declared
                         and (now - first_crossing) < grace)
            if not upgrading and (now - _number(state.get("at"), 0.0)) < reseal:
                return ""

        path = seal(sid, payload, journal_root, letters_dir)
        if not path:
            return ""
        _record_seal(sid, journal_root, win, degraded=not declared,
                     first_crossing=first_crossing)

        head = (f"dimissory: {win.used_percent:.0f}% of your {win.label()} "
                f"window is gone, so a handoff letter was sealed at {path}.")
        if declared:
            tail = (f" If your next action has changed, record it now with "
                    f"`{dim_command()} declare --session {sid} --next \"...\"`"
                    f" -- there may not be a later chance.")
            if upgrading:
                head = (f"dimissory: the handoff letter at {path} has been "
                        f"rewritten to include what you declared.")
                tail = ""
        else:
            # The letter just written is missing the half only the agent can
            # supply, and it is labelled DEGRADED. Saying so is the point:
            # this is the last reliable moment to fix it.
            mins = max(1, int(grace // 60))
            tail = (f" It is marked DEGRADED because you have declared "
                    f"nothing, so it carries no task and no next action. Run "
                    f"this now and the letter will be rewritten with it:\n"
                    f"  {dim_command()} declare --session {sid} --task "
                    f"\"<what this session is for>\" --next \"<the exact next "
                    f"action>\"\nAfter about {mins} minute(s) the degraded "
                    f"letter stands as final.")
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": head + tail}})

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
    # The CONFIGURED directory, not a hardcoded one. `dim show` and `dim
    # resume` look in `letters.dir` from the config; this sealed to
    # ~/.dimissory/letters regardless. Set `letters.dir` and the hook wrote
    # letters where nothing would ever look for them, reporting success both
    # times -- the predecessor's "wrong location reported as success", which
    # install.py keeps a whole docstring about.
    if letters_dir is None:
        from .config import Config
        letters_dir = Config.load(None).letters_dir
    letters = os.path.expanduser(letters_dir)
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
    # THE NAME IS CLAIMED, NOT COMPOSED AND HOPED FOR.
    #
    # This used to be one `open(path, "w")` on a name with one-second
    # resolution. Registering PostToolUse put that line on the one event hosts
    # run CONCURRENTLY, and review measured the result: two seals in the same
    # second opened the same path and the last writer won -- losing an UPGRADED
    # letter to the degraded one it was meant to replace, while the seal marker
    # recorded the upgrade as done. The letter is the artifact this package
    # exists to produce, so that is the worst possible place for a race.
    #
    # A pid and a microsecond stamp would make a collision unlikely. O_EXCL
    # makes it impossible, across processes and within one, which is the same
    # reasoning install._backup now uses for the same class of bug.
    #
    # It also invalidated a test of mine: the grace test slept 1.05s "because
    # letter names are second-resolution", working around this rather than
    # catching it. Sleeping past a race does not remove it.
    # The counter is ALWAYS present and zero-padded so that sorting by name
    # gives the same order as sorting by time. An unpadded, sometimes-absent
    # suffix does not: "-1.md" sorts BEFORE ".md", because '-' is 0x2D and '.'
    # is 0x2E, so the newest letter of a second stopped being the last one by
    # name. `_latest` uses mtime and was unaffected, but leaving the two
    # orderings disagreeing is a trap for the next reader -- and it caught a
    # test in this suite within minutes of the change.
    base = f"{sid[:60]}-{time.strftime('%Y%m%dT%H%M%S')}"
    for n in range(1000):
        path = os.path.join(letters, f"{base}-{n:03d}.md")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render(brief))
        return path
    return None


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
