#!/usr/bin/env python3
"""Packaging: the failures that only ever happen to somebody else.

Everything here passes trivially on the machine that built the package. Each
check exists because the same thing is false once dimissory is installed from
a wheel, on a different PATH, or on Windows -- which is every user who is not
the author.

The one that mattered most was measured, not guessed. `dim hook` was written
into settings.json as a BARE command, and a hook host runs its command through
a shell carrying its own PATH:

    $ env -i /bin/sh -c 'dim hook'
    /bin/sh: 1: dim: not found          exit 127

A hook that exits 127 is indistinguishable from a hook with nothing to say, so
this failed silently for anyone whose dimissory lived in a venv, a pipx
install, or ~/.local/bin -- which is the normal case, not the edge one.

Run: python3 tests/test_packaging.py
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import dimissory                                            # noqa: E402
from dimissory import hook as H                             # noqa: E402
from dimissory import install as I                          # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _pyproject():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        import tomllib
        return tomllib.load(fh)


def test_the_installed_command_is_absolute_not_a_bare_name():
    """The measured failure. The host's PATH is not ours."""
    cmd = H.hook_command()
    argv = shlex.split(cmd) if os.name != "nt" else cmd.split()
    check("the hook command ends in the hook subcommand",
          argv[-1] == "hook", cmd)
    exe = argv[0]
    check("and does not start with a bare name",
          os.path.isabs(exe) or exe.startswith('"'), cmd)
    check("the resolved program actually exists",
          os.path.exists(exe.strip('"')), exe)


def test_a_command_that_cannot_run_is_never_installed():
    """This test used to assert that the emitted command runs under `env -i`,
    and it passed for two days because the AUTHOR had dimissory installed.
    Uninstalling it turned the suite red -- a check measuring the developer's
    machine rather than the code, which is this tool's own defect class.

    The honest property is not "it always runs" (it cannot, from a source
    checkout where the `<python> -m dimissory.cli` fallback has no way to find
    its package once a host strips the environment). It is: WE NEVER WRITE A
    COMMAND THAT DOES NOT RUN. A hook that cannot run is indistinguishable
    from a hook with nothing to say.
    """
    ok, why = H.command_works()
    check("command_works answers with a reason, not just a bool",
          isinstance(ok, bool) and isinstance(why, str), (ok, why))
    if ok:
        check("this environment can run the emitted command", True)
    else:
        check(f"this environment cannot ({why[:60]}) -- and that is reported",
              bool(why))

    # Whatever the answer, install must AGREE with it.
    d = tempfile.mkdtemp(prefix="dim-verify-")
    p = os.path.join(d, "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark"}, fh)
    try:
        I.install("claude", path=p, assume_yes=True, out=lambda *_a: None)
        installed = True
    except I.InstallRefused as e:
        installed = False
        check("refusing says the hook would do nothing",
              "silently does nothing" in str(e), str(e)[:90])
    check("install proceeds exactly when the command works", installed == ok,
          (installed, ok))

    # And a command that definitely cannot run is always refused.
    try:
        I.install("claude", command="/nonexistent/dim hook", path=p,
                  assume_yes=True, out=lambda *_a: None)
        check("a broken command is refused", False, "it was written anyway")
    except I.InstallRefused:
        check("a broken command is refused", True)
    check("and the file was not touched",
          json.load(open(p, encoding="utf-8")) == {"theme": "dark"}
          or installed, "file changed by a refused install")

    # The negative control: the thing we replaced must genuinely fail here,
    # otherwise this test is passing for a reason that has nothing to do with
    # the fix.
    bare = subprocess.run(["env", "-i", "/bin/sh", "-c", "dim hook"],
                          input="{}", capture_output=True, text=True, timeout=60)
    check("and the bare command it replaced does NOT (127)",
          bare.returncode == 127, f"rc={bare.returncode}")


def test_a_path_with_a_space_survives_the_shell():
    """The default Windows install is under 'C:\\Program Files'. Unquoted, the
    command line splits into a first word that is not an interpreter."""
    quoted = H._quote("/c/Program Files/Py/python.exe")
    if os.name == "nt":
        check("quoted with double quotes for cmd.exe",
              quoted.startswith('"') and quoted.endswith('"'), quoted)
    else:
        check("one argument survives splitting",
              shlex.split(f"{quoted} -m dimissory.cli")[0]
              == "/c/Program Files/Py/python.exe", quoted)
    check("a path with no space is left alone",
          H._quote("/usr/bin/dim") == "/usr/bin/dim", H._quote("/usr/bin/dim"))


def test_every_form_of_our_command_is_recognised_as_ours():
    """Idempotence depends on spotting our own entry. The forms do not share a
    substring: an absolute console script ends '.../dim hook', a source
    checkout gets '"<python>" -m dimissory.cli hook'. A marker matching only
    the first appends a second copy on every re-install, silently, because
    duplicates still fire."""
    for form in ('dim hook',
                 '/home/u/.local/bin/dim hook',
                 '/opt/venv/bin/dimissory hook',
                 '"/c/Program Files/Py/python.exe" -m dimissory.cli hook'):
        entry = {"hooks": [{"type": "command", "command": form}]}
        check(f"ours: {form[:42]}", I.is_ours(entry))
    check("someone else's hook is NOT ours",
          not I.is_ours({"hooks": [{"type": "command",
                                    "command": "prettier --write"}]}))
    check("a hook merely mentioning dimissory in a path is not a false negative",
          I.is_ours({"hooks": [{"type": "command",
                                "command": "/x/dimissory/bin/dim hook"}]}))


def test_installing_twice_adds_nothing_the_second_time():
    """Run with the form the OLD marker could not see."""
    form = '"/c/Program Files/Py/python.exe" -m dimissory.cli hook'
    d = tempfile.mkdtemp(prefix="dim-pkg-")
    p = os.path.join(d, "settings.json")
    _e, merged, added = I.plan("claude", form, p)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(merged, fh)
    _e, merged2, added2 = I.plan("claude", form, p)
    check("the first install adds the events", bool(added), added)
    check("the second adds nothing", added2 == [], added2)
    check("and there is exactly one SessionStart entry",
          len(merged2["hooks"]["SessionStart"]) == 1,
          merged2["hooks"]["SessionStart"])
    check("the old marker could not have seen this form",
          "dim hook" not in form)


def test_the_version_has_exactly_one_source():
    """Two literals in two files agree until one is bumped alone, and the one
    easy to forget is the one `dim --version` prints."""
    pp = _pyproject()
    check("pyproject declares version dynamic",
          "version" in pp["project"].get("dynamic", []), pp["project"].keys())
    check("and carries no static version to drift from",
          "version" not in pp["project"])
    check("the dynamic source is the package attribute",
          pp["tool"]["setuptools"]["dynamic"]["version"]["attr"]
          == "dimissory.__version__")
    out = subprocess.run([sys.executable, "-m", "dimissory.cli", "--version"],
                         capture_output=True, text=True,
                         cwd=os.path.join(ROOT, "src"), timeout=60).stdout
    check("and `--version` prints that same value",
          dimissory.__version__ in out, f"{out.strip()!r}")


def test_the_type_marker_ships():
    """Annotated throughout; without py.typed PEP 561 says a checker must
    ignore all of it, so every annotation is dead weight to an importer."""
    check("py.typed exists in the source tree",
          os.path.exists(os.path.join(ROOT, "src", "dimissory", "py.typed")))
    pd = _pyproject()["tool"]["setuptools"].get("package-data", {})
    check("and is declared as package data, so it is actually packaged",
          "py.typed" in pd.get("dimissory", []), pd)


def test_the_license_is_declared_the_way_pypi_now_wants_it():
    pp = _pyproject()
    check("license is an SPDX string", pp["project"]["license"] == "MIT",
          pp["project"].get("license"))
    check("the deprecated classifier is gone",
          not any(c.startswith("License ::")
                  for c in pp["project"]["classifiers"]))
    check("and the build backend is new enough to understand that",
          "setuptools>=77" in " ".join(pp["build-system"]["requires"]))


def test_no_module_can_be_added_without_being_packaged():
    """find(where='src') ships a directory only if it is a real package. A new
    subpackage without __init__.py is discovered by nobody and imports fine
    from a checkout -- so it breaks only after install."""
    from setuptools import find_packages
    found = set(find_packages(where=os.path.join(ROOT, "src")))
    base = os.path.join(ROOT, "src", "dimissory")
    missing = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if not any(f.endswith(".py") for f in filenames):
            continue
        rel = os.path.relpath(dirpath, os.path.join(ROOT, "src"))
        if rel.replace(os.sep, ".") not in found:
            missing.append(rel)
    check("every directory holding python is a discovered package",
          not missing, missing)
    check("no exclude rule quietly drops one",
          "exclude" not in _pyproject()["tool"]["setuptools"]["packages"]["find"])


def main():
    print("=" * 66)
    print(" packaging: the failures that only happen to somebody else")
    print("=" * 66)
    for t in (test_the_installed_command_is_absolute_not_a_bare_name,
              test_a_command_that_cannot_run_is_never_installed,
              test_a_path_with_a_space_survives_the_shell,
              test_every_form_of_our_command_is_recognised_as_ours,
              test_installing_twice_adds_nothing_the_second_time,
              test_the_version_has_exactly_one_source,
              test_the_type_marker_ships,
              test_the_license_is_declared_the_way_pypi_now_wants_it,
              test_no_module_can_be_added_without_being_packaged):
        t()
    print("\n" + "=" * 66)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
