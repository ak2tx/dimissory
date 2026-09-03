"""dimissory -- issue a letter of transfer for an agent session.

    dim write     issue a letter now
    dim show      print the most recent letter
    dim resume    run a letter's Verify block and report whether it still holds
    dim status    how much of the plan window is gone

`dim` and `dimissory` are the same command.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

from . import __version__
from .brief import Brief, Declared
from .config import Config, write_at
from .observe import checks_for, observe
from .render import render

DEFAULT_DIR = os.path.expanduser("~/.dimissory/letters")


def _read_letter(path):
    """A letter's text, or (None, why). Never raises on someone else's bytes.

    A letter is a portable document that arrives from another machine. It may
    have been re-saved by an editor with a different default encoding, mailed
    through something lossy, or truncated. `open(p, encoding="utf-8").read()`
    turned all of that into a traceback -- on Windows, where a cp1252 em-dash
    is byte 0x97, which is not valid UTF-8.

    UTF-8 first because that is what dimissory writes. The fallback decodes
    with replacement and SAYS SO, because silently substituting characters in a
    document whose verify block is compared byte-for-byte would turn a
    corrupted letter into a stale one, and those are different findings.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return None, f"cannot be read: {e}"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as e:
        return (raw.decode("utf-8", "replace"),
                f"not valid UTF-8 ({e.reason} at byte {e.start}); decoded with "
                f"replacement, so any check may disagree for that reason alone")


def _letters_dir(args):
    """--dir, then the config, then the default. In that order, and it is worth
    saying out loud: the predecessor had a hook that ignored `-c` entirely and
    wrote to the default directory while the operator watched the configured
    one -- a wrong location reported as success."""
    if args.dir:
        return os.path.abspath(args.dir)
    return os.path.abspath(Config.load(getattr(args, "config", None)).letters_dir)


def cmd_write(args):
    cwd = args.cwd or os.getcwd()
    _d0 = _letters_dir(args)
    _j0 = os.path.expanduser(args.journal or "~/.dimissory/journal")
    # The meter, which `dim write` never recorded. A hand-issued letter had
    # no window line at all while a hook-sealed one did, so the two documents
    # disagreed about what had been observed for no reason a reader could see.
    from . import window as _W
    _win = _W.read(transcript=args.transcript)
    o = observe(cwd=cwd, transcript=args.transcript, our_dirs=(_d0, _j0),
                window=_win.as_dict() if _win else None)
    from .observe import _exclude_pathspec, _git
    _d, _j = _d0, _j0
    _ours = (_d, _j)
    _spec = _exclude_pathspec(cwd, *_ours)
    # The recorded output must come from the SAME command the letter asks the
    # reader to run, exclusions included, or the two can never agree.
    _rels = []
    for _o in _ours:
        try:
            _r = os.path.relpath(os.path.realpath(_o), os.path.realpath(cwd))
        except ValueError:
            continue
        if not _r.startswith(os.pardir) and not os.path.isabs(_r):
            _rels.append(f":(exclude){_r.replace(os.sep, '/')}")
    _porcelain = _git(cwd, "status", "--porcelain",
                      *(["--", *_rels] if _rels else []))
    session = args.session or os.path.basename(os.getcwd()) or "session"
    # SEAL the journal rather than ask a question now. Both round-25 reviews
    # made this correction: at the moment a letter is needed the agent has the
    # least capacity to write one, so it declares as it works and the trigger
    # only fixes what is already there.
    from . import journal as _J
    declared, ages, damaged = _J.to_declared(session, root=args.journal)
    if damaged:
        print(f"  journal: {damaged} unreadable entr(ies) skipped", file=sys.stderr)
    brief = Brief(
        session=session,
        observed=o,
        declared=declared,
        ages=ages,
        checks=checks_for(o, cwd=cwd, our_dirs=_ours,
                          porcelain=_porcelain if isinstance(_porcelain, str)
                          else None),
    )
    d = _letters_dir(args)
    from . import letters as _L
    path = _L.write(d, brief.session, render(brief))
    if path is None:
        print(f"could not write a letter into {d}", file=sys.stderr)
        return 1
    print(path)
    if brief.is_degraded:
        print("  DEGRADED: no agent-written half. Machine-derived facts only.",
              file=sys.stderr)
    return 0


def _latest(d):
    try:
        # mtime first, then name as a tiebreak. Two letters can share an mtime
        # on a coarse filesystem, and "whichever the directory happened to
        # yield first" is not an answer -- letter names now sort
        # chronologically, so the name settles it the same way time would.
        files = sorted((os.path.join(d, f) for f in os.listdir(d)
                        if f.endswith(".md")),
                       key=lambda p: (os.path.getmtime(p), os.path.basename(p)))
    except OSError:
        return None
    return files[-1] if files else None


def cmd_show(args):
    p = args.path or _latest(_letters_dir(args))
    if not p or not os.path.exists(p):
        print("no letter found", file=sys.stderr)
        return 1
    text, why = _read_letter(p)
    if text is None:
        print(f"{p}: {why}", file=sys.stderr)
        return 1
    if why:
        print(f"{p}: {why}", file=sys.stderr)
    sys.stdout.write(text)
    return 0


def cmd_resume(args):
    """Run the Verify block and say plainly whether the letter still holds.

    Exit 0 means every check agreed and the letter may be acted on. Exit 2
    means it is stale. There is deliberately no exit code that means "probably
    fine" -- the point of a verify block is that it answers.
    """
    p = args.path or _latest(_letters_dir(args))
    if not p or not os.path.exists(p):
        print("no letter found", file=sys.stderr)
        return 1
    text, why = _read_letter(p)
    if text is None:
        print(f"{p}: {why}", file=sys.stderr)
        return 2
    if why:
        print(f"{p}: WARNING -- {why}", file=sys.stderr)
    if "## Verify first" not in text:
        print(f"{p}: UNVERIFIABLE -- this letter carries no checks.",
              file=sys.stderr)
        return 2
    block = text.split("## Verify first", 1)[1].split("```")[1]

    # Parse `command` followed by its `#   expected:` line. The expectation is
    # load-bearing: the first version compared nothing and asked only whether
    # the command exited 0. `git rev-parse --short HEAD` exits 0 in ANY
    # repository, so a letter written at one commit reported "still holds" at
    # another -- the verify block, the entire differentiator, could not fail
    # for the reason it exists. Found in review, reproduced in seconds.
    pairs, pending = [], None
    for ln in block.splitlines():
        t = ln.strip()
        if not t:
            continue
        if t.startswith("#   expected:"):
            if pending is not None:
                raw = t.split("expected:", 1)[1].strip()
                # JSON since the format changed to survive multi-line values.
                # Older letters carry a bare string, so both are accepted --
                # a letter written by yesterday's version must still verify.
                try:
                    want = json.loads(raw)
                    if not isinstance(want, str):
                        want = raw
                except ValueError:
                    want = raw
                pairs.append((pending, want))
                pending = None
        elif t.startswith("#"):
            continue
        else:
            pending = t
    if pending is not None:
        pairs.append((pending, None))

    if not pairs:
        print(f"{p}: no checks could be parsed from the Verify block.",
              file=sys.stderr)
        return 2

    stale = 0
    for cmd, expect in pairs:
        try:
            # NO SHELL. Two reasons, and both were live.
            #
            # Portability: the tree check carries a git pathspec,
            # `-- ':(exclude)letters'`. cmd.exe does not strip single quotes,
            # so on Windows git received the quotes literally and the exclusion
            # silently did nothing. shlex.split gives the same argv everywhere.
            #
            # Safety: a letter is a portable document that arrives from another
            # machine, and `resume` executes what is written in it. Review's
            # words: keep executable verify content machine-generated. Running
            # it through a shell adds redirection, chaining and expansion to
            # anything that ever reaches this block.
            argv = shlex.split(cmd)
            if not argv:
                print(f"  FAIL  {cmd}\n          unparseable command")
                stale += 1
                continue
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=30)
            got = (r.stdout or "").strip()
            ran = r.returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            got, ran = str(e), False
        if not ran:
            ok, why = False, "command failed"
        elif expect is None:
            # No recorded expectation means nothing to compare against. That is
            # NOT a pass -- it is a check that cannot fail, and saying so is the
            # whole point of this tool.
            ok, why = False, "no recorded expectation to compare against"
        else:
            ok = got == expect.strip()
            why = "" if ok else f"expected {expect.strip()!r}, got {got!r}"
        print(f"  {'ok  ' if ok else 'FAIL'}  {cmd}"
              + (f"\n          {why}" if why else ""))
        stale += 0 if ok else 1

    if stale:
        print(f"\n{p} is STALE: {stale} of {len(pairs)} check(s) disagree. "
              f"Re-derive before continuing.", file=sys.stderr)
        return 2
    print(f"\n{p} still holds ({len(pairs)} check(s) agreed).")
    return 0


def cmd_declare(args):
    """Record what the agent knows, while it still has budget to know it."""
    from . import journal as _J
    session = args.session or os.path.basename(os.getcwd()) or "session"
    wrote = []
    for field, values in (("task", [args.task] if args.task else []),
                          ("next", [args.next] if args.next else []),
                          ("decided", args.decided or []),
                          ("ruled_out", args.ruled_out or []),
                          ("constraint", args.constraint or []),
                          (_J.REVOKE, args.revoke or [])):
        for v in values:
            try:
                _J.declare(session, field, v, root=args.journal)
                wrote.append(field)
            except _J.JournalError as e:
                print(f"dim declare: {e}", file=sys.stderr)
                return 1
            except OSError as e:
                # THE AGENT IS READING THIS. Measured on Codex: it complied
                # with the ask on its very first tool call, ran `dim declare`
                # with a real task and next action, and got back
                #
                #   OSError: [Errno 30] Read-only file system:
                #   '/home/ak2tx/.dimissory'
                #
                # as a raw Python traceback -- because Codex runs agent tools
                # in a sandbox where $HOME is read-only, while the HOOK runs
                # outside it and writes there fine. The agent did everything
                # right and got a stack trace. The letter came out DEGRADED
                # and nothing anywhere said why.
                #
                # A traceback is not an error message: it describes our call
                # stack instead of the reader's problem, and here the reader
                # is a machine that will act on what it is told.
                root = os.path.expanduser(args.journal or _J.default_root())
                print(f"dim declare: cannot write the journal at {root}\n"
                      f"  {e.strerror or e}\n"
                      f"  Inside a sandbox $HOME is often read-only. Retry "
                      f"with a path you can write:\n"
                      f"    dim --journal ./.dimissory/journal declare "
                      f"--session {session} ...\n"
                      f"  or set DIMISSORY_HOME to a writable directory.",
                      file=sys.stderr)
                return 1
    if not wrote:
        print("dim declare: nothing to record. Pass at least one of "
              "--task/--next/--decided/--ruled-out/--constraint/--revoke",
              file=sys.stderr)
        return 1
    print(f"recorded {len(wrote)}: {', '.join(sorted(set(wrote)))}")
    return 0


def cmd_hook(args):
    """`dim hook` -- the hook handler, and its installer."""
    from . import hook as H
    from . import install as I
    if args.install:
        detected = I.detect()
        have = [k for k, v in detected.items() if v]
        if not args.target and not have:
            print("no agent CLIs found on PATH (claude, codex, grok). "
                  "Install one, or pass --target to force.", file=sys.stderr)
            return 1
        targets = [args.target] if args.target else have
        rc, done = 0, []
        for t in targets:
            if t not in I.TARGETS:
                print(f"unknown target {t!r}; expected one of "
                      f"{', '.join(I.TARGETS)}", file=sys.stderr)
                return 1
            try:
                path, added = I.install(t, command=args.command,
                                        assume_yes=args.yes)
            except I.InstallRefused as e:
                print(f"  {I.TARGETS[t]['label']}: {e}", file=sys.stderr)
                rc = 1
                continue
            if path and added:
                done.append(t)
        if done:
            print(f"\ninstalled for: {', '.join(done)}")
            print("restart the agent for it to read the new configuration")
        elif rc == 0:
            # Declining is not success, and neither is a run that changed
            # nothing while printing nothing.
            print("nothing was installed", file=sys.stderr)
            rc = 1
        return rc
    if args.print_config:
        t = args.target or "claude"
        _e, merged, _a = I.plan(t, args.command or H.hook_command(),
                                path=os.devnull)
        print(json.dumps(merged, indent=2))
        return 0
    return H.main()


def cmd_setup(args):
    from .setup import run
    return run(config_path=args.config, assume_yes=True if args.yes else None)


def cmd_config(args):
    """Show every setting, its value, and where that value came from.

    The third column is the point. "Is my config actually being used?" is a
    question a config command should be able to answer, and the predecessor
    shipped a hook that read the default config whatever `-c` you passed.
    """
    cfg = Config.load(args.config)
    if getattr(cfg, "problem", None):
        print(f"config UNREADABLE: {cfg.problem}", file=sys.stderr)
        print("running on defaults -- your file is NOT in effect",
              file=sys.stderr)
    if args.edit:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        if not os.path.exists(cfg.path):
            written = cfg.write()
            print(f"created {written}")
        return subprocess.call([editor, cfg.path])
    if args.path:
        print(cfg.path)
        return 0
    print(f"file: {cfg.path}"
          f"{'' if os.path.exists(cfg.path) else '  (does not exist yet)'}\n")
    width = max(len(f"{sec}.{k}") for sec, it in cfg.values.items() for k in it)
    for section in sorted(cfg.values):
        for key in sorted(cfg.values[section]):
            src = cfg.source(section, key)
            where = "default" if src == "default" else "config"
            print(f"  {section + '.' + key:<{width}}  "
                  f"{str(cfg.get(section, key)):<24}  {where}")
    print("\n  dim config --edit    open it in $EDITOR")
    return 0


def _statusline_installed(path=None):
    """Whether Claude Code is configured to run OUR statusline.

    Needed so `dim status` can tell "you never installed the meter" from
    "the meter is installed and every window has simply reset". Telling
    somebody to install what they already installed is how a tool loses their
    trust in everything else it reports.
    """
    from . import install as I
    try:
        p = os.path.expanduser(path or I.TARGETS["claude"]["path"])
        with open(p, encoding="utf-8") as fh:
            current = json.load(fh).get("statusLine")
    except (OSError, ValueError, AttributeError, KeyError):
        return False
    return current is not None and I.is_our_statusline(current)


def _when(epoch):
    """A reset time a person can act on.

    "%H:%M" alone said `resets 02:00` for a weekly window three days out,
    which reads as two o'clock tonight. A time of day is only unambiguous
    within today, so the day is named whenever it is not today -- the same
    reasoning render.py already records about printing a bare epoch: correct
    and useless is still useless.
    """
    import time as _t
    try:
        when = _t.localtime(float(epoch))
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown"
    today = _t.localtime()
    if when[:3] == today[:3]:
        return _t.strftime("%H:%M", when)
    days = (_t.mktime(when[:3] + (0, 0, 0, 0, 0, -1))
            - _t.mktime(today[:3] + (0, 0, 0, 0, 0, -1))) / 86400.0
    if 0 < days < 2:
        return _t.strftime("tomorrow %H:%M", when)
    if 0 < days < 7:
        return _t.strftime("%a %H:%M", when)
    return _t.strftime("%d %b %H:%M", when)


def cmd_statusline_cmd(args):
    """`dim statusline` and `dim statusline --install`.

    Two very different jobs behind one name, because the name is the thing a
    person has to type into their settings and it should match the docs they
    are reading. Without --install this is run BY Claude Code, on stdin.
    """
    if args.install:
        from . import install as I
        try:
            path, note = I.install_statusline(assume_yes=args.yes)
        except I.InstallRefused as e:
            print(f"  {e}", file=sys.stderr)
            return 1
        if not path:
            print("nothing was installed", file=sys.stderr)
            return 1
        print("\nrestart Claude Code for it to read the new configuration")
        print("then check it with: dim status")
        return 0
    return cmd_statusline(args)


def cmd_statusline(args):
    """`dim statusline` -- Claude Code's meter, recorded as it goes past."""
    from . import statusline
    argv = []
    if args.wrap:
        argv += ["--wrap", args.wrap]
    if getattr(args, "cache", None):
        argv += ["--cache", args.cache]
    return statusline.main(argv)


def cmd_meter(args):
    """`dim meter` -- how much of every plan window is gone. That is all.

    `dim status` answers "is this tool set up correctly". This answers "how
    much have I got left", which is a different question and the one people
    actually have. It works whether or not any hook is installed.

    It also REFRESHES what it can, which is the difference between a meter and
    a cache. Grok writes its billing row only when its interactive pager
    starts, so on an agent-driven box the row goes stale precisely while you
    are working: measured at 35% while the account was at 80%, hidden for 61
    hours because the file's mtime kept looking fresh. Starting the vendor's
    own CLI briefly costs no tokens and corrected it in 22 seconds.
    """
    from . import refresh as R
    from . import window as W

    readings = {
        "claude": W._claude(getattr(args, "window_cache", None)),
        "codex": W._codex(getattr(args, "transcript", None)),
        "grok": W._grok(),
    }

    if not getattr(args, "no_refresh", False):
        for provider in R.stale_providers(readings):
            print(f"  {provider}: reading is stale, asking {provider} to "
                  f"refresh it...", file=sys.stderr)
            if R.refresh(provider):
                readings[provider] = W._grok() if provider == "grok" else None
            else:
                print(f"  {provider}: could not refresh", file=sys.stderr)

    rows, worst = [], None
    for provider, win in readings.items():
        if win is None:
            rows.append((provider, "-", "no reading", ""))
            continue
        age = "?" if win.age is None else _age(win.age)
        note = ""
        if win.is_stale:
            note = "STALE -- not safe to act on"
        elif worst is None or win.used_percent > worst.used_percent:
            worst = win
        when = _when(win.resets_at) if win.resets_at else ""
        rows.append((provider, f"{win.used_percent:.0f}%",
                     f"{win.label()}{', resets ' + when if when else ''}",
                     f"{age} old{'  ' + note if note else ''}"))
        for other in win.also:
            rows.append(("", f"{other.used_percent:.0f}%", other.label(), ""))

    width = max((len(r[2]) for r in rows), default=10)
    for provider, pct, what, note in rows:
        print(f"{provider:8} {pct:>5}  {what:<{width}}  {note}")

    if worst is None:
        print("\nno usable reading anywhere -- nothing here can tell you how "
              "much is left.", file=sys.stderr)
        return 1
    print()
    at = write_at(Config.load(getattr(args, "config", None)))
    if W.should_seal(worst, at):
        print(f"past the {at * 100:.0f}% margin on {worst.label()} -- a letter "
              f"is due.")
    else:
        print(f"under the {at * 100:.0f}% margin. Nearest is {worst.label()} "
              f"at {worst.used_percent:.0f}%.")
    return 0


def _age(seconds):
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def cmd_status(args):
    """`dim status` -- what the meter can see, per agent, and what it cannot.

    This printed "plan-window meter not wired up yet" long after the meter
    worked, which made it the most misleading command in the tool: it is the
    first thing a person runs, and it told them the central feature was
    missing. Review called it a lie about the very fixes it was shipped
    alongside.
    """
    from . import install as I
    from . import window as W
    cfg = Config.load(getattr(args, "config", None))
    if getattr(cfg, "problem", None):
        print(f"config UNREADABLE: {cfg.problem}", file=sys.stderr)
        print("running on defaults -- your file is NOT in effect",
              file=sys.stderr)

    detected = I.detect()
    at = write_at(cfg)
    print(f"seal at        {at * 100:.0f}% of a plan window")
    print(f"letters        {cfg.letters_dir}")
    print()

    # READINESS IS PER AGENT, and only counts for agents that are installed.
    # Review measured four separate lies in the previous version: a stray Grok
    # billing log made it exit 0 with no Grok CLI and no hooks anywhere; a
    # working Codex box exited 1 because Codex is per-session and never set
    # the flag; four-of-five hooks printed a bare "no"; and an EXPIRED Claude
    # window printed "run `dim statusline --install`" when the statusline was
    # installed and working, the window had simply reset.
    rows, ready = [], []
    for key in ("claude", "codex", "grok"):
        on_path = bool(detected.get(key))
        # `missing` initialised BEFORE the try. Review reproduced an
        # UnboundLocalError here: an unreadable settings.json raises
        # InstallRefused, `missing` is never bound, and the readiness loop
        # below reads it -- so the diagnostic command crashes in exactly the
        # situation it exists to diagnose.
        installed, missing = "-", []
        try:
            # `plan` returns the events it WOULD add, so an empty list means
            # everything is already there. Reducing that to yes/no threw away
            # the useful half: four-of-five installed printed a bare "no",
            # which reads as "nothing is set up" when the only thing missing
            # might be the one event that matters.
            _existing, _merged, missing = I.plan(key, "dim hook", None)
            total = len(I.TARGETS[key]["events"])
            if not missing:
                installed = "yes"
            elif len(missing) == total:
                installed = "no"
            else:
                installed = f"{total - len(missing)}/{total}"
        except I.InstallRefused:
            installed = "refused"
        except (OSError, KeyError):
            installed = "?"
        hooks_ok = installed == "yes"
        # Codex will not RUN a hook it has not trusted, and says nothing when
        # it declines to. Installed is not armed.
        if key == "codex" and hooks_ok:
            trusted, why = I.codex_hooks_trusted()
            if not trusted:
                hooks_ok = False
                installed = "untrusted"

        win = None
        if key == "claude":
            win = W._claude(getattr(args, "window_cache", None))
        elif key == "grok":
            win = W._grok()

        meter_ok = False
        if win is not None and win.is_stale:
            age = f"{int(win.age)}s old" if win.age is not None else "undateable"
            meter = f"stale ({age})"
        elif win is not None:
            meter_ok = True
            meter = f"{win.used_percent:.0f}% of {win.label()}"
            if win.resets_at:
                meter += f", resets {_when(win.resets_at)}"
        elif key == "codex":
            # Per-session: the reading lives in whichever rollout is current,
            # so there is nothing account-wide to print. That is not a missing
            # meter, and it used to be counted as one.
            meter_ok = hooks_ok
            meter = "per-session (from the current rollout)"
        elif key == "claude":
            # Distinguish "never recorded" from "recorded, window since
            # reset". Telling somebody to install what they already installed
            # is how a tool loses their trust in everything else it says.
            statusline_on = _statusline_installed()
            meter = ("recorded, but every window has reset -- it will refresh"
                     if statusline_on else
                     "none -- run `dim statusline --install`")
        else:
            meter = "none recorded"

        if on_path:
            ready.append((key, hooks_ok, meter_ok, installed, missing))
        rows.append((key, "yes" if on_path else "no", installed, meter))

    print(f"{'agent':8} {'on PATH':8} {'hooks':8} meter")
    for key, path, installed, meter in rows:
        print(f"{key:8} {path:8} {installed:8} {meter}")
    print()
    if not ready:
        print("no agent CLIs found, so there is nothing to seal for.",
              file=sys.stderr)
        return 1
    if any(hooks_ok and meter_ok for _k, hooks_ok, meter_ok, _i, _m in ready):
        return 0

    # NOT READY, and it says which half is missing per agent. "no live meter"
    # was printed even when the meter was live and the hooks were the problem,
    # which sends the reader to fix the wrong thing.
    print("nothing will seal before a window closes:", file=sys.stderr)
    for key, hooks_ok, meter_ok, installed, missing in ready:
        why = []
        if not hooks_ok:
            why.append(f"hooks {installed}"
                       + (f" (missing {', '.join(missing)})" if missing
                          and installed != "no" else ""))
        if not meter_ok:
            why.append("no live meter"
                       + (" -- run `dim statusline --install`"
                          if key == "claude" and not _statusline_installed()
                          else ""))
        print(f"  {key}: {'; '.join(why)}", file=sys.stderr)
    return 1


class _Parser(argparse.ArgumentParser):
    """A parser whose usage errors do NOT exit 2.

    `dim resume` documents exit 2 as "this letter is stale", and argparse
    exits 2 for a usage error. So a mistyped flag was indistinguishable from
    a stale letter -- found by mistyping one while demonstrating the tool:
    `dim resume --path X` exited 2 and read exactly like a real verdict.

    That matters more here than in most tools, because exit 2 IS the product:
    it is what a script or the next agent branches on. 64 is the conventional
    EX_USAGE.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def main(argv=None):
    p = _Parser(prog="dim", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"dimissory {__version__}")
    p.add_argument("--dir", help=f"where letters live (default: {DEFAULT_DIR})")
    p.add_argument("-c", "--config", help="config file (default: ~/.dimissory/config.toml)")
    p.add_argument("--journal", help="journal directory (default: ~/.dimissory/journal)")
    # parser_class matters: every SUBparser makes its own usage errors, and
    # without this they are plain ArgumentParsers that still exit 2 -- so
    # `dim resume --typo` would keep reading as "this letter is stale".
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)

    w = sub.add_parser("write", help="issue a letter now")
    w.add_argument("--session"); w.add_argument("--cwd")
    w.add_argument("--transcript", help="path to the agent transcript")
    w.set_defaults(fn=cmd_write)

    s = sub.add_parser("show", help="print the most recent letter")
    s.add_argument("path", nargs="?"); s.set_defaults(fn=cmd_show)

    r = sub.add_parser("resume", help="verify a letter still holds")
    r.add_argument("path", nargs="?"); r.set_defaults(fn=cmd_resume)

    mt = sub.add_parser("meter",
                        help="how much of every plan window is gone")
    mt.add_argument("--no-refresh", action="store_true",
                    help="do not ask a vendor to refresh a stale reading")
    mt.add_argument("--transcript", help="a session transcript, for Codex")
    mt.add_argument("--window-cache", dest="window_cache", default=None,
                    help="where the statusline recorded Claude's window")
    mt.set_defaults(fn=cmd_meter)

    st = sub.add_parser("status",
                        help="what the meter can see, per agent, and what it "
                             "cannot")
    st.add_argument("--window-cache", dest="window_cache", default=None,
                    help="where the statusline recorded Claude's window")
    st.set_defaults(fn=cmd_status)

    dc = sub.add_parser("declare", help="record what you know, as you work")
    dc.add_argument("--session")
    dc.add_argument("--task", help="what this session is for (replaces)")
    dc.add_argument("--next", help="the exact next action (replaces)")
    dc.add_argument("--decided", action="append", help="a decision (accumulates)")
    dc.add_argument("--ruled-out", action="append", dest="ruled_out",
                    help="a dead end and why (accumulates)")
    dc.add_argument("--constraint", action="append",
                    help="a standing constraint (accumulates)")
    dc.add_argument("--revoke", action="append",
                    help="retract an earlier entry, verbatim")
    dc.set_defaults(fn=cmd_declare)

    hk = sub.add_parser("hook", help="the agent hook, and its installer")
    hk.add_argument("--install", action="store_true",
                    help="write hook config for every detected agent CLI")
    hk.add_argument("--target", help="claude | codex | grok (default: all found)")
    hk.add_argument("--command", default=None,
                    help="the command the hook runs (default: this install's "
                         "own dim, resolved to an absolute path)")
    hk.add_argument("--print", dest="print_config", action="store_true",
                    help="print the config that would be installed")
    hk.add_argument("--yes", action="store_true", help="do not ask")
    hk.set_defaults(fn=cmd_hook)

    sl = sub.add_parser("statusline",
                        help="Claude Code's plan-window meter (run BY Claude "
                             "Code, not by you)")
    sl.add_argument("--install", action="store_true",
                    help="add this as Claude Code's statusLine, wrapping any "
                         "command already there")
    sl.add_argument("--wrap", default=None,
                    help="run this command and print ITS output, after "
                         "recording the window")
    sl.add_argument("--cache", default=None,
                    help="where to record the reading")
    sl.add_argument("--yes", action="store_true", help="do not ask")
    sl.set_defaults(fn=cmd_statusline_cmd)

    su = sub.add_parser("setup", help="guided first-time setup")
    su.add_argument("--yes", action="store_true", help="take every default")
    su.set_defaults(fn=cmd_setup)

    c = sub.add_parser("config", help="show or edit settings")
    c.add_argument("--edit", action="store_true", help="open in $EDITOR")
    c.add_argument("--path", action="store_true", help="print the file path only")
    c.set_defaults(fn=cmd_config)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 1
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
