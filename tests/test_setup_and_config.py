#!/usr/bin/env python3
"""Setup and settings, tested against the two ways the predecessor got it wrong.

Both of these shipped, both looked fine, and both were found by accident:

  1. `cg setup` detected agents into a dict keyed by CLI name (`claude`), then
     asked `if "anthropic" in found`. Never true. The single step that existed
     to fix the thing users called clunky silently never ran, and setup printed
     success every time.

  2. `cg -c <config> hook` ignored the config's socket entirely and connected to
     the default. A wrong location, reported as success. The `-c` embedding had
     already been "fixed" once -- for the handoff directory, not for everything
     else, which is the same defect one place over.

So the tests here are mostly about whether two things that must agree actually
do, and whether a flag that claims to redirect something really redirects it.

Run: python3 tests/test_setup_and_config.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory import cli, install as I, setup as setup_mod      # noqa: E402
from dimissory.config import DEFAULTS, Config                    # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def test_detection_and_config_agree_on_their_keys():
    """The `cg setup` bug, made impossible to repeat.

    `detect_agents()` returns one set of keys and `[agents]` in the config
    declares another. If they ever drift, every per-agent branch silently stops
    matching and setup keeps reporting success.
    """
    detected = set(setup_mod.detect_agents())
    configured = set(DEFAULTS["agents"])
    check("detection and config use the SAME agent keys",
          detected == configured, f"detect={sorted(detected)} "
                                  f"config={sorted(configured)}")
    check("and the labels cover every key",
          {k for k, _ in setup_mod.AGENTS} == configured)


def test_the_config_flag_actually_redirects():
    """`-c` must change where things go, or it is decoration."""
    d = tempfile.mkdtemp(prefix="dim-cfg-")
    letters = os.path.join(d, "elsewhere")
    path = os.path.join(d, "custom.toml")
    # Written through the same escaper the product uses. Hand-rolling
    # `dir = "{letters}"` here embedded a raw Windows path, which is not valid
    # TOML -- so this test failed on Windows for a reason that had nothing to
    # do with what it was testing.
    from dimissory.config import _toml_str
    with open(path, "w") as fh:
        fh.write(f'[letters]\ndir = "{_toml_str(letters)}"\n')

    class A:
        dir = None
        config = path
    got = cli._letters_dir(A())
    check("`-c` steers the letters directory",
          got == os.path.abspath(letters), got)

    class B:
        dir = os.path.join(d, "flagwins")
        config = path
    check("and an explicit --dir still wins over it",
          cli._letters_dir(B()) == os.path.abspath(B.dir))


def test_defaults_work_with_no_file_at_all():
    d = tempfile.mkdtemp(prefix="dim-cfg-")
    cfg = Config.load(os.path.join(d, "nope.toml"))
    check("loads without a file", cfg.get("window", "write_at") == 0.85)
    check("and says the value is a default", cfg.source("window", "write_at") == "default")
    check("no problem is reported for a merely absent file",
          getattr(cfg, "problem", None) is None)


def test_a_file_value_overrides_and_is_attributed():
    d = tempfile.mkdtemp(prefix="dim-cfg-")
    p = os.path.join(d, "c.toml")
    with open(p, "w") as fh:
        fh.write("[window]\nwrite_at = 0.5\n")
    cfg = Config.load(p)
    check("the file value wins", cfg.get("window", "write_at") == 0.5)
    check("and is attributed to the file", cfg.source("window", "write_at") == p)
    check("untouched keys stay default",
          cfg.get("letters", "keep") == 50
          and cfg.source("letters", "keep") == "default")


def test_an_unreadable_config_is_reported_not_swallowed():
    """Running on defaults while the operator believes their file is in effect
    is worse than failing outright."""
    d = tempfile.mkdtemp(prefix="dim-cfg-")
    p = os.path.join(d, "bad.toml")
    with open(p, "w") as fh:
        fh.write("not toml {{{\n")
    cfg = Config.load(p)
    check("the parse failure is surfaced", bool(getattr(cfg, "problem", None)))
    check("it names the file", p in (cfg.problem or ""))
    check("and defaults are still usable", cfg.get("window", "write_at") == 0.85)


def test_writing_a_config_never_clobbers_silently():
    d = tempfile.mkdtemp(prefix="dim-cfg-")
    p = os.path.join(d, "c.toml")
    cfg = Config.load(p)
    check("first write returns the path", cfg.write(p) == p)
    with open(p, "a") as fh:
        fh.write("\n# a human edited this\n")
    check("a second write refuses and says so by returning None",
          cfg.write(p) is None)
    check("the human's edit survives", "a human edited this" in open(p).read())
    check("force overwrites when explicitly asked", cfg.write(p, force=True) == p)


def test_the_written_config_parses_back_to_the_same_values():
    """A config file that cannot be read back is a config file that lies."""
    d = tempfile.mkdtemp(prefix="dim-cfg-")
    p = os.path.join(d, "c.toml")
    Config.load(p).write(p)
    back = Config.load(p)
    for section, items in DEFAULTS.items():
        for key, val in items.items():
            check(f"{section}.{key} round-trips", back.get(section, key) == val,
                  f"{back.get(section, key)!r} != {val!r}")
    check("and it kept its comments for the human editing it",
          open(p).read().count("#") >= 6)


def _sandbox(d):
    """Hook targets redirected into a temp dir.

    THIS IS NOT OPTIONAL. Once setup installs hooks, running it with
    assume_yes and no redirection edits the REAL ~/.claude/settings.json. This
    test did exactly that: `config_path` redirected the config and nothing
    else, so the suite wrote a working PostToolUse hook into a live settings
    file -- the same act install.py's docstring records the predecessor
    committing while testing the fix for having committed it once already.
    """
    paths = {k: os.path.join(d, f"{k}-hooks.json") for k in I.TARGETS}
    # The statusline is a SEPARATE install with its own target, so the sandbox
    # has to name it too. Covering only I.TARGETS left setup free to write a
    # statusLine into the real settings.json -- the same escape as before, one
    # feature later.
    paths["claude_statusline"] = os.path.join(d, "claude-statusline.json")
    return paths


def test_setup_is_safe_to_rerun_and_never_blocks():
    """It must run to completion with no terminal and change nothing twice."""
    import io, contextlib
    d = tempfile.mkdtemp(prefix="dim-setup-")
    p = os.path.join(d, "c.toml")
    lines1, lines2 = [], []
    with contextlib.redirect_stdout(io.StringIO()):
        rc1 = setup_mod.run(config_path=p, assume_yes=True, out=lines1.append,
                            hook_paths=_sandbox(d), verify=False)
        rc2 = setup_mod.run(config_path=p, assume_yes=True, out=lines2.append,
                            hook_paths=_sandbox(d), verify=False)
    check("first run succeeds", rc1 == 0, rc1)
    check("second run succeeds", rc2 == 0, rc2)
    joined1, joined2 = "\n".join(lines1), "\n".join(lines2)
    check("the first run created the config", "created" in joined1)
    check("the second left it alone", "already existed, unchanged" in joined2,
          joined2[-300:])
    check("and the summary is built from real outcomes, not assumptions",
          "summary" in joined2 and "detect" in joined2)
    if any(I.detect().values()):
        check("hooks were installed into the sandbox, not a real home",
              any(os.path.exists(q) for q in _sandbox(d).values()),
              os.listdir(d))
        check("and installing twice adds nothing the second time",
              "already installed" in joined2, joined2[-400:])


def test_setup_cannot_reach_a_real_home():
    """A guard on the accident above, asserted rather than remembered.

    Every path setup would write to must live under the sandbox it was given.
    Without this the failure is invisible until someone reads their own
    settings.json and finds hooks they never asked for.
    """
    import io, contextlib
    d = tempfile.mkdtemp(prefix="dim-setup-guard-")
    p = os.path.join(d, "c.toml")
    sandbox = _sandbox(d)
    home = os.path.expanduser("~")
    touched = []
    real_install = I.install

    def spy(target, command=None, path=None, **kw):
        touched.append(path)
        return real_install(target, command=command, path=path, **kw)

    I.install = spy
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            setup_mod.run(config_path=p, assume_yes=True,
                          out=lambda *_a: None, hook_paths=sandbox,
                          verify=False)
    finally:
        I.install = real_install

    check("every install was given an explicit path",
          all(t for t in touched), touched)
    escaped = [t for t in touched if t and not
               os.path.abspath(t).startswith(os.path.abspath(d))]
    check("and not one of them was outside the sandbox", not escaped, escaped)
    check("in particular, none was the real settings file",
          not any(t and os.path.abspath(t)
                  == os.path.join(home, ".claude", "settings.json")
                  for t in touched), touched)


def test_the_seal_margin_refuses_a_bool_and_never_or_defaults():
    """Found by mutation testing. The fix shipped with no test at all.

    Two bugs lived on this one line. `cfg.get(...) or 0.85` turned a configured
    0 -- "always seal" -- into 0.85. Replacing that with an isinstance check
    then admitted TOML `true`/`false`, because bool subclasses int, so
    `write_at = false` became 0.0 and ALSO meant "always seal". Both were fixed
    and neither was asserted, so reverting either stayed green.
    """
    from dimissory.config import write_at

    class Fake:
        def __init__(self, v):
            self._v = v

        def get(self, _section, _key):
            return self._v

    check("a real 0 survives -- it means always seal",
          write_at(Fake(0)) == 0.0, write_at(Fake(0)))
    check("and is not replaced by the default",
          write_at(Fake(0)) != 0.85)
    check("a fraction passes through", write_at(Fake(0.5)) == 0.5)
    for bad in (False, True, None, "0.9", [], {}, -0.1, 1.1):
        check(f"{bad!r} falls back to the default",
              write_at(Fake(bad)) == 0.85, write_at(Fake(bad)))

    # And the hook must use the same accessor, or the two can disagree about
    # where the margin is.
    src = open(os.path.join(ROOT, "src", "dimissory", "hook.py")).read()
    check("the hook reads the margin through config.write_at",
          "write_at(cfg)" in src)


def test_a_windows_path_survives_being_written_and_read_back():
    """The config must be parseable by the tool that wrote it.

    On Windows `dim setup` wrote `dir = "C:\\Users\\me\\..."` with raw
    backslashes. `\\U` is a unicode escape in a TOML basic string, so loading
    the file raised and the tool fell back to defaults -- while the operator's
    config sat there looking like it was in use. A wrong location reported as
    success.

    Asserted with a literal Windows path on EVERY platform, because a bug that
    only one runner can see is a bug three quarters of the team cannot fix.
    """
    import tomllib
    for path in (r"C:\Users\me\.dimissory\letters",
                 r"C:\temp\new\utf\letters",      # \U, \n, \t all escapes
                 '/home/a b/quote"dir/letters'):
        d = tempfile.mkdtemp(prefix="dim-win-")
        p = os.path.join(d, "c.toml")
        cfg = Config.load(p)
        cfg.values["letters"]["dir"] = path
        cfg.write(p)
        try:
            with open(p, "rb") as fh:
                tomllib.load(fh)
            parsed = True
        except tomllib.TOMLDecodeError as e:
            parsed = False
            detail = str(e)
        check(f"{path!r} writes a parseable config", parsed,
              locals().get("detail", ""))
        back = Config.load(p)
        check(f"{path!r} round-trips to the same value",
              back.get("letters", "dir") == path, back.get("letters", "dir"))
        check(f"{path!r} leaves no parse problem",
              getattr(back, "problem", None) is None, getattr(back, "problem", ""))


def main():
    print("=" * 64)
    print(" setup and settings: two things that must agree, and a flag")
    print("=" * 64)
    for t in (test_detection_and_config_agree_on_their_keys,
              test_the_config_flag_actually_redirects,
              test_defaults_work_with_no_file_at_all,
              test_a_file_value_overrides_and_is_attributed,
              test_an_unreadable_config_is_reported_not_swallowed,
              test_writing_a_config_never_clobbers_silently,
              test_the_written_config_parses_back_to_the_same_values,
              test_setup_is_safe_to_rerun_and_never_blocks,
              test_setup_cannot_reach_a_real_home,
              test_the_seal_margin_refuses_a_bool_and_never_or_defaults,
              test_a_windows_path_survives_being_written_and_read_back):
        t()
    print("\n" + "=" * 64)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 64)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
