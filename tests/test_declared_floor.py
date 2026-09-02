#!/usr/bin/env python3
"""The Python version we CLAIM to support must be one we actually support.

Written because the first push declared `requires-python = ">=3.9"` and
imported `tomllib`, which is 3.11+. CI caught it on three platforms at once,
but CI is not the point: pip would have installed it happily on 3.9, because
the metadata said it was fine, and the user would have got an ImportError the
first time they ran the tool. A declaration nothing checks is a claim, and this
project's whole argument is that claims get checked.

Two things must agree, and neither is allowed to drift:

    the floor in pyproject.toml   >=  the newest stdlib module we import
    the floor in pyproject.toml   ==  the lowest Python in the CI matrix

The second matters as much as the first. Raising the floor without raising the
matrix leaves CI testing a version nobody claims to support; raising the matrix
without the floor leaves the claim wrong. Either way the two numbers stop
meaning the same thing, which is how this happened.

Run: python3 tests/test_declared_floor.py
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "dimissory")

# Stdlib modules that did not always exist. Only what could plausibly be
# imported here -- an exhaustive table would rot, and a short one that is
# actually maintained is worth more than a long one that is not.
ADDED_IN = {
    "tomllib": (3, 11),
    "graphlib": (3, 9),
    "zoneinfo": (3, 9),
    "asyncio.taskgroups": (3, 11),
    "importlib.metadata": (3, 8),
    "dataclasses": (3, 7),
    "contextvars": (3, 7),
}

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def declared_floor():
    text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    m = re.search(r'requires-python\s*=\s*"[><=~^]*\s*(\d+)\.(\d+)', text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def imported_modules():
    """Top-level module names imported anywhere in the package."""
    names = set()
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(SRC, fn), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module)
    return names


def ci_matrix_versions():
    p = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    if not os.path.exists(p):
        return []
    m = re.search(r'python:\s*\[([^\]]+)\]', open(p, encoding="utf-8").read())
    if not m:
        return []
    out = []
    for part in m.group(1).split(","):
        got = re.search(r'(\d+)\.(\d+)', part)
        if got:
            out.append((int(got.group(1)), int(got.group(2))))
    return sorted(out)


def test_the_floor_is_declared_at_all():
    floor = declared_floor()
    check("pyproject declares requires-python", floor is not None, floor)
    check("and it parses to a version", floor is None or len(floor) == 2, floor)


def test_no_import_needs_a_newer_python_than_we_claim():
    """The bug that shipped: tomllib (3.11) under a >=3.9 claim."""
    floor = declared_floor()
    if floor is None:
        check("cannot check imports without a declared floor", False)
        return
    offenders = []
    for mod in sorted(imported_modules()):
        need = ADDED_IN.get(mod)
        if need and need > floor:
            offenders.append(f"{mod} needs {need[0]}.{need[1]}")
    check(f"every import works on the declared floor "
          f"{floor[0]}.{floor[1]}", not offenders, "; ".join(offenders))


def test_ci_tests_the_version_we_claim():
    """A floor CI never runs is a floor nobody has verified."""
    floor, matrix = declared_floor(), ci_matrix_versions()
    check("the CI matrix names some Python versions", bool(matrix), matrix)
    if not matrix or floor is None:
        return
    check("CI's lowest version IS the declared floor",
          matrix[0] == floor,
          f"floor {floor[0]}.{floor[1]}, CI lowest {matrix[0][0]}.{matrix[0][1]}")
    check("and CI also tests something newer than the floor",
          matrix[-1] > floor, matrix)


def test_the_table_itself_is_honest():
    """A lookup table that never matches anything cannot fail.

    If `tomllib` stopped being imported the check above would pass for a reason
    unrelated to the code being correct, so assert the table is actually load
    bearing -- at least one entry has to match something we really import.
    """
    used = imported_modules() & set(ADDED_IN)
    check("at least one version-gated module is actually imported",
          bool(used), sorted(imported_modules()))
    check("and tomllib is still the one that matters here",
          "tomllib" in used, sorted(used))


def main():
    print("=" * 64)
    print(" the Python we claim to support is one we actually support")
    print("=" * 64)
    for t in (test_the_floor_is_declared_at_all,
              test_no_import_needs_a_newer_python_than_we_claim,
              test_ci_tests_the_version_we_claim,
              test_the_table_itself_is_honest):
        t()
    print("\n" + "=" * 64)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 64)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
