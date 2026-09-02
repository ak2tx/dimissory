"""dimissory -- issue a letter of transfer for an agent session.

    dim write     issue a letter now
    dim show      print the most recent letter
    dim resume    run a letter's Verify block and report whether it still holds
    dim status    how much of the plan window is gone

`dim` and `dimissory` are the same command.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

from . import __version__
from .brief import Brief, Declared
from .config import Config
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
    o = observe(cwd=cwd, transcript=args.transcript)
    from .observe import _exclude_pathspec, _git
    _d = _letters_dir(args)
    _j = os.path.expanduser(args.journal or "~/.dimissory/journal")
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
    os.makedirs(d, exist_ok=True)
    import time
    path = os.path.join(d, f"{brief.session}-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(brief))
    print(path)
    if brief.is_degraded:
        print("  DEGRADED: no agent-written half. Machine-derived facts only.",
              file=sys.stderr)
    return 0


def _latest(d):
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d)
                        if f.endswith(".md")), key=os.path.getmtime)
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
                pairs.append((pending, t.split("expected:", 1)[1].strip()))
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
    if not wrote:
        print("dim declare: nothing to record. Pass at least one of "
              "--task/--next/--decided/--ruled-out/--constraint/--revoke",
              file=sys.stderr)
        return 1
    print(f"recorded {len(wrote)}: {', '.join(sorted(set(wrote)))}")
    return 0


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


def cmd_status(args):
    # The plan-window meter is the piece that makes writing a letter BEFORE
    # lockout possible at all, and it is the next thing to port.
    print("plan-window meter not wired up yet -- see docs/contract.md",
          file=sys.stderr)
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="dim", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"dimissory {__version__}")
    p.add_argument("--dir", help=f"where letters live (default: {DEFAULT_DIR})")
    p.add_argument("-c", "--config", help="config file (default: ~/.dimissory/config.toml)")
    p.add_argument("--journal", help="journal directory (default: ~/.dimissory/journal)")
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser("write", help="issue a letter now")
    w.add_argument("--session"); w.add_argument("--cwd")
    w.add_argument("--transcript", help="path to the agent transcript")
    w.set_defaults(fn=cmd_write)

    s = sub.add_parser("show", help="print the most recent letter")
    s.add_argument("path", nargs="?"); s.set_defaults(fn=cmd_show)

    r = sub.add_parser("resume", help="verify a letter still holds")
    r.add_argument("path", nargs="?"); r.set_defaults(fn=cmd_resume)

    sub.add_parser("status", help="plan-window usage").set_defaults(fn=cmd_status)

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
