"""`dim setup` -- one guided command instead of five things you had to know.

Written against a specific failure in the predecessor's setup command. Its
agent-seeding branch tested `"anthropic" in found`, where `found` was keyed by
CLI name -- `claude`, `codex`, `grok`. The condition was never true, so the one
step that existed to fix the thing people called clunky silently never ran, and
setup printed success every time. It was found only by running it on a machine
that actually had the agent installed.

So the rule here: every step returns what it actually did, and the summary is
built from those return values rather than from the fact that the function was
called. A step that did nothing says it did nothing.
"""

from __future__ import annotations

import os
import shutil
import sys

from .config import Config

AGENTS = (
    ("claude", "Claude Code"),
    ("codex", "Codex CLI"),
    ("grok", "Grok CLI"),
)


def detect_agents():
    """Which agent CLIs are on PATH. Keyed by the SAME name the config uses.

    The key is `claude`/`codex`/`grok` in both places, deliberately and with a
    test, because the predecessor's setup was keyed one way and queried the
    other and nobody noticed for weeks.
    """
    return {key: shutil.which(key) for key, _label in AGENTS}


def _ask(prompt, default=True, assume=None):
    """Ask, unless there is nobody to ask. Never blocks a non-interactive run."""
    if assume is not None:
        return assume
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return default if not answer else answer.startswith("y")


def run(config_path=None, assume_yes=None, out=print, hook_paths=None):
    """Guided setup. Safe to re-run; nothing here overwrites without asking.

    Returns an exit code. Every line printed is something that was checked,
    not something that was assumed.

    `hook_paths` maps a target key to the file its hooks should be written to.
    It exists because it has to: once this function installs hooks, calling it
    with `assume_yes=True` and no redirection EDITS THE CALLER'S REAL
    ~/.claude/settings.json. `config_path` redirected the config and nothing
    else, so the suite's own idempotence test silently installed a working
    PostToolUse hook into a live settings file -- which is, word for word, the
    thing install.py's docstring says the predecessor did while testing the fix
    for having done it once already. Third time in this lineage. A test that
    can reach a real home is not a test.
    """
    steps = []                       # (label, what actually happened)

    out("")
    out("  dimissory setup")
    out("  ---------------")
    out("")

    # 1. What is actually installed.
    found = detect_agents()
    have = [k for k, path in found.items() if path]
    out("  agent CLIs on PATH:")
    for key, label in AGENTS:
        path = found[key]
        out(f"    {'yes' if path else ' no'}  {label:<12} "
            f"{path or '(not installed)'}")
    steps.append(("detect", f"{len(have)} of {len(AGENTS)} found"))
    if not have:
        out("")
        out("  None found. dimissory can still write letters by hand"
            " (`dim write`),")
        out("  but there is nothing to write them FOR until an agent CLI is"
            " installed.")

    # 2. Config.
    cfg = Config.load(config_path)
    out("")
    if getattr(cfg, "problem", None):
        out(f"  config UNREADABLE: {cfg.problem}")
        out("  Running on defaults. Fix or delete the file -- it is NOT in"
            " effect.")
        steps.append(("config", "unreadable, defaults in use"))
    elif os.path.exists(cfg.path):
        out(f"  config already at {cfg.path} -- left alone")
        steps.append(("config", "already existed, unchanged"))
    else:
        if _ask(f"  write a config at {cfg.path}?", True, assume_yes):
            written = cfg.write()
            if written:
                out(f"  wrote {written}")
                steps.append(("config", f"created {written}"))
            else:
                out("  config not written")
                steps.append(("config", "write refused"))
        else:
            out("  skipped; defaults are in effect")
            steps.append(("config", "declined, defaults in use"))

    # 3. Where letters will go.
    d = cfg.letters_dir
    existed = os.path.isdir(d)
    try:
        os.makedirs(d, exist_ok=True)
        steps.append(("letters", "already there" if existed else f"created {d}"))
        out(f"  letters -> {d}" + ("" if existed else "  (created)"))
    except OSError as e:
        out(f"  could NOT create {d}: {e}")
        steps.append(("letters", f"failed: {e}"))

    # 4. Install the hooks. WITHOUT THIS STEP NOTHING ELSE HERE MATTERS.
    #
    # Guided setup used to end at the previous line: it detected the CLIs,
    # wrote a config, made a directory, wrote a proof letter and printed a
    # summary of successes -- while never installing a single hook. So the
    # agent was never asked to declare, no gate was ever armed, and no letter
    # was ever sealed at the margin. Every automatic behaviour the tool exists
    # for was off, after a setup that reported success on all four steps.
    #
    # That is the failure this file's docstring is about, committed again in
    # the same file. Found in review round 1, not here.
    out("")
    if have:
        from . import install as I
        from .hook import hook_command
        command = hook_command()
        out(f"  hooks: this is what makes any of it automatic.")
        out(f"    command to be installed: {command}")
        installed, declined, refused = [], [], []
        for key in have:
            if not cfg.values.get("agents", {}).get(key, True):
                out(f"    {I.TARGETS[key]['label']}: disabled in config,"
                    f" skipped")
                continue
            try:
                path, added = I.install(key, command=command,
                                        path=(hook_paths or {}).get(key),
                                        assume_yes=assume_yes, out=out)
            except I.InstallRefused as e:
                out(f"    {I.TARGETS[key]['label']}: {e}")
                refused.append(key)
                continue
            if path and added:
                installed.append(key)
            elif path:
                installed.append(key)          # already present, still armed
            else:
                declined.append(key)
        # Reported in order of severity, and a partial install says so rather
        # than letting the first success speak for the whole step. Review:
        # "mixed installed + refused: first branch wins, summary is 'armed for
        # X', refused is only a loop print, exit 0."
        if refused:
            steps.append(("hooks", "failed: refused for "
                                   + ", ".join(refused)
                                   + (f" (armed for {', '.join(installed)})"
                                      if installed else "")))
        elif installed:
            steps.append(("hooks", f"armed for {', '.join(installed)}"))
        else:
            # Declining is not success, and it must not be summarised as one
            # OR exit as one -- `dim hook --install` already returns 1 for
            # "nothing was installed", and `dim setup --yes` returning 0 with
            # every automatic behaviour off makes a CI check meaningless.
            steps.append(("hooks", "failed: NOT installed -- nothing is "
                                   "automatic"))
    else:
        steps.append(("hooks", "no agent CLIs found, nothing to install"))

    # 4b. Claude's meter, which is a SEPARATE install from the hooks.
    #
    # Hooks alone give Claude nothing to seal on: Claude Code writes no
    # utilization percentage to disk anywhere, so `should_seal` has no number
    # and the letter only ever arrives at the wall. The statusline is where
    # that percentage is handed over, so on Claude this step is the difference
    # between the product's actual claim and a consolation prize -- and
    # guided setup would be repeating its own documented sin to leave it out.
    if found.get("claude") and cfg.values.get("agents", {}).get("claude", True):
        out("")
        try:
            sl_path, note = I.install_statusline(
                path=(hook_paths or {}).get("claude_statusline"),
                assume_yes=assume_yes, out=out)
        except I.InstallRefused as e:
            out(f"    {e}")
            sl_path, note = None, "refused"
        if sl_path and note != "declined":
            steps.append(("meter", f"claude: {note}"))
        else:
            # "failed:" is load-bearing -- it is what the exit code below
            # matches on. Review measured this exact step reporting
            # "NOT installed" and still exiting 0, so `dim setup --yes` was
            # green on a box where Claude could never seal before the wall.
            # The comment above says leaving the step out would repeat this
            # file's documented sin; the exit code did it anyway.
            steps.append(("meter", "failed: claude NOT metered -- Claude will "
                                   "only get a letter AT the wall, not before"))

    # 5. Prove it works, rather than asserting it does.
    out("")
    if _ask("  write one letter now, to prove the whole path works?",
            True, assume_yes):
        from .brief import Brief, Declared
        from .observe import checks_for, observe
        from .render import render
        import time
        o = observe(cwd=os.getcwd())
        b = Brief(session="setup-check", observed=o, declared=Declared(),
                  checks=checks_for(o))
        from . import letters as _L
        try:
            path = _L.write(d, "setup-check", render(b))
            if path is None:
                raise OSError(f"could not claim a letter name in {d}")
            measured = sorted(o.known())
            out(f"  wrote {path}")
            out(f"  it measured: {', '.join(measured) or 'nothing'}")
            if b.is_degraded:
                out("  and it is marked DEGRADED, correctly -- no agent wrote"
                    " a half for it.")
            steps.append(("letter", f"wrote one, measured {len(measured)} field(s)"))
        except OSError as e:
            out(f"  could NOT write a letter: {e}")
            steps.append(("letter", f"failed: {e}"))
    else:
        steps.append(("letter", "skipped"))

    # 6. What actually happened -- assembled from the results above.
    out("")
    out("  summary")
    for label, what in steps:
        out(f"    {label:<8} {what}")
    out("")
    if any(l == "hooks" and w.startswith("armed") for l, w in steps):
        out("  restart your agent CLI so it reads the new configuration.")
    else:
        out("  NOTE: no hooks are installed, so nothing happens"
            " automatically yet.")
        out("        run `dim hook --install` when you want that.")
    out("")
    out("  next:  dim write     issue a letter")
    out("         dim config    see every setting and where it came from")
    out("")
    failed = [l for l, w in steps if "failed" in w or "UNREAD" in w.upper()]
    return 1 if failed else 0
