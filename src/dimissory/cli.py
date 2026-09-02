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
import subprocess
import sys

from . import __version__
from .brief import Brief, Declared
from .config import Config
from .observe import checks_for, observe
from .render import render

DEFAULT_DIR = os.path.expanduser("~/.dimissory/letters")


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
    _spec = _exclude_pathspec(cwd, _d)
    # The recorded output must come from the SAME command the letter asks the
    # reader to run, exclusions included, or the two can never agree.
    _porcelain = _git(cwd, "status", "--porcelain",
                      *(["--", f":(exclude){os.path.relpath(_d, cwd)}"]
                        if _spec else []))
    brief = Brief(
        session=args.session or os.path.basename(os.getcwd()) or "session",
        observed=o,
        # Empty until the agent is asked for its half. The letter renders a
        # DEGRADED banner in this state rather than looking finished, which is
        # the whole reason that banner exists.
        declared=Declared(),
        checks=checks_for(o, cwd=cwd, letters_dir=_d,
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
    sys.stdout.write(open(p, encoding="utf-8").read())
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
    text = open(p, encoding="utf-8").read()
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
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
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
