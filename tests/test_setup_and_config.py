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

from dimissory import cli, setup as setup_mod                    # noqa: E402
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
    with open(path, "w") as fh:
        fh.write(f'[letters]\ndir = "{letters}"\n')

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


def test_setup_is_safe_to_rerun_and_never_blocks():
    """It must run to completion with no terminal and change nothing twice."""
    import io, contextlib
    d = tempfile.mkdtemp(prefix="dim-setup-")
    p = os.path.join(d, "c.toml")
    lines1, lines2 = [], []
    with contextlib.redirect_stdout(io.StringIO()):
        rc1 = setup_mod.run(config_path=p, assume_yes=True, out=lines1.append)
        rc2 = setup_mod.run(config_path=p, assume_yes=True, out=lines2.append)
    check("first run succeeds", rc1 == 0, rc1)
    check("second run succeeds", rc2 == 0, rc2)
    joined1, joined2 = "\n".join(lines1), "\n".join(lines2)
    check("the first run created the config", "created" in joined1)
    check("the second left it alone", "already existed, unchanged" in joined2,
          joined2[-300:])
    check("and the summary is built from real outcomes, not assumptions",
          "summary" in joined2 and "detect" in joined2)


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
              test_setup_is_safe_to_rerun_and_never_blocks):
        t()
    print("\n" + "=" * 64)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 64)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
