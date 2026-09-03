#!/usr/bin/env python3
"""The trust contract, asserted.

Every check here corresponds to a defect that actually shipped in the
predecessor project, not to a hypothetical. The reason they are tests rather
than documentation is that the predecessor documented all of this correctly and
shipped the opposite anyway -- twice in the same release.

Run: python3 tests/test_contract.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from dimissory.brief import (                                    # noqa: E402
    Brief, Check, Declared, Observed, UNMEASURED, Unmeasured)
from dimissory.render import render                              # noqa: E402

RAN = 0
FAILED: list = []


def check(name, cond, detail=""):
    global RAN
    RAN += 1
    if not cond:
        FAILED.append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f" -- {detail}" if detail and not cond else ""))


def _full():
    return Brief(
        session="proj",
        observed=Observed(head="7f3a91c", head_subject="wip: lock around refresh",
                          dirty=("src/auth/session.py",), last_command="pytest -x",
                          last_exit=1, written_at="2026-09-02 15:51"),
        declared=Declared(task="Fix the token-refresh race.",
                          decided=("Lock around refresh, not the request path.",),
                          ruled_out=("Optimistic versioning: needs a migration.",),
                          next_action="Move the release below the assignment."),
        checks=(Check("git rev-parse --short HEAD", "7f3a91c"),),
    )


def test_an_unmeasured_value_is_never_a_number():
    """`ran for: 0s` -- the defect this whole module exists to prevent.

    The predecessor printed a zero under a heading promising a record of what
    the session was observed doing. The zero did not mean "no time passed", it
    meant "nobody looked". Those are different claims and only one was true.
    """
    check("UNMEASURED is falsey, so `if value:` guards work",
          not UNMEASURED)
    for fn, label in ((int, "int()"), (float, "float()")):
        try:
            fn(UNMEASURED)
            check(f"{label} on an unmeasured value raises", False, "returned")
        except TypeError as e:
            check(f"{label} on an unmeasured value raises", True)
            check(f"{label} explains what to do instead",
                  "Omit the line" in str(e), str(e)[:60])
    check("and it is a singleton, so `is UNMEASURED` is reliable",
          Unmeasured() is UNMEASURED)


def test_unmeasured_fields_are_omitted_not_zero_filled():
    b = Brief(session="s", observed=Observed(head="abc1234"),
              declared=Declared(task="t"), checks=())
    out = render(b)
    check("a measured field appears", "abc1234" in out)
    # Scoped to the observed BLOCK, not the whole document. The first version
    # searched the full string for "window" and failed on the prose phrase
    # "before the window closed" -- a test breaking on a re-flowed paragraph,
    # which wastes the time of whoever is next and proves nothing.
    i = out.find("## Observed")
    block = out[i:out.find("```", out.find("```", i) + 3)] if i != -1 else ""
    for bad in ("last cmd", "window", "calls", "dirty"):
        check(f"the unmeasured `{bad}` line is absent from the observed block",
              bad not in block, block)
    check("and no zero is invented anywhere",
          " 0%" not in out and "exit 0" not in out, out)


def test_declared_content_is_always_attributed():
    """A summary and a finding must not look the same on the page.

    The attribution sits on the heading line specifically so that reformatting,
    truncation, or a reader skimming headings cannot separate the claim from
    whose claim it is.
    """
    out = render(_full())
    for section in ("Task", "Decided", "Ruled out", "Next action"):
        idx = out.find(f"## {section}")
        check(f"`{section}` is attributed on its own heading line",
              idx != -1 and "the agent's own words" in
              out[idx:out.find("\n", idx)], out[idx:idx + 60] if idx != -1 else "")
    o_idx = out.find("## Observed")
    check("and the observed block says it was NOT the agent",
          o_idx != -1 and "not by the agent" in out[o_idx:out.find("\n", o_idx)])


def test_a_brief_without_the_agents_half_looks_incomplete():
    """It must not merely be short. It must be visibly degraded.

    Both real handoffs the predecessor ever wrote were missing this half, and
    both looked like finished documents.
    """
    b = Brief(session="s", observed=Observed(head="abc1234"),
              declared=Declared(), checks=(Check("true", "ok"),))
    check("the brief knows it is degraded", b.is_degraded)
    out = render(b)
    check("and says so before anything else",
          "DEGRADED" in out and out.index("DEGRADED") < out.index("## "), out[:200])
    check("naming what is missing, not just that something is",
          "no stated next action" in out)

    check("a complete brief is NOT flagged", not _full().is_degraded)
    check("and carries no banner", "DEGRADED" not in render(_full()))


def test_a_brief_with_no_checks_declares_itself_unverifiable():
    b = Brief(session="s", observed=Observed(head="abc1234"),
              declared=Declared(task="t"), checks=())
    check("it knows", b.is_unverifiable)
    out = render(b)
    check("and says so at the top", "UNVERIFIABLE" in out)
    check("in the terms that matter: a claim, not a finding",
          "not a finding" in out)
    check("a checked brief is not flagged",
          not _full().is_unverifiable and "UNVERIFIABLE" not in render(_full()))


def test_the_verify_block_comes_before_anything_actionable():
    """Order is load-bearing. A reader who acts before verifying has taken the
    brief on faith, which is the failure mode of every competing tool."""
    out = render(_full())
    v, n = out.find("## Verify first"), out.find("## Next action")
    check("Verify precedes Next action", v != -1 and n != -1 and v < n, (v, n))
    check("and it says what a disagreement means", "STALE" in out)


def test_an_unusable_check_is_refused_at_write_time():
    """A malformed check must fail on the machine that can still fix it."""
    for bad in ("", "   ", '"unclosed'):
        try:
            Check(bad, "x")
            check(f"Check({bad!r}) is refused", False, "accepted")
        except ValueError:
            check(f"Check({bad!r}) is refused", True)
    check("a real one is accepted", Check("git status", "clean").command == "git status")


def test_a_grok_session_directory_is_read_as_a_transcript():
    """Found by running the product on a real Grok box, not by reviewing it.

    Grok hands the hook a session DIRECTORY, not a file. A real end-to-end run
    sealed a letter whose Observed block read `calls 0 observed in the
    transcript tail` -- which is a MEASUREMENT, "this session made no tool
    calls", about a session that had just made six. Reporting zero where the
    answer was six is precisely the defect this project exists to prevent, and
    four rounds of source review never saw it.
    """
    import tempfile, json as _json
    from dimissory.transcript import recent_calls, resolve

    d = tempfile.mkdtemp(prefix="dim-grokt-")
    session = os.path.join(d, "01a0-session-id")
    os.makedirs(session)
    # The real shape, copied from a live Grok chat_history.jsonl: `tool_calls`
    # on an assistant record, with `arguments` as a JSON STRING.
    with open(os.path.join(session, "chat_history.jsonl"), "w",
              encoding="utf-8") as fh:
        fh.write(_json.dumps({"type": "system", "content": "..."}) + "\n")
        fh.write(_json.dumps({"type": "assistant", "content": "", "tool_calls": [
            {"id": "call-0", "name": "read_file",
             "arguments": '{"target_file":"/tmp/e2e/work/calc.py"}'},
            {"id": "call-1", "name": "run_terminal_command",
             "arguments": '{"command":"git commit","description":"commit it"}'},
        ]}) + "\n")
        fh.write(_json.dumps({"type": "tool_result", "content": "ok"}) + "\n")

    # The shape the hook is ACTUALLY handed: updates.jsonl, measured from a
    # live payload dump after the directory theory failed to fix anything.
    upd = os.path.join(session, "updates.jsonl")
    with open(upd, "w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"params": {"update": {
            "sessionUpdate": "tool_call", "title": "read_file",
            "rawInput": {"target_file": "calc.py"},
            "_meta": {"x.ai/tool": {"name": "read_file"}}}}}) + "\n")
        fh.write(_json.dumps({"params": {"update": {
            "sessionUpdate": "tool_call_update", "title": "Read `calc.py`"}}}) + "\n")
    got = recent_calls(upd)
    check("the updates.jsonl stream yields its tool calls",
          got is not None and len(got) == 1, got)
    check("named from the x.ai metadata, not the display title",
          got and got[0][0] == "read_file", got)
    check("a tool_call_update is not counted as a second call",
          got and len(got) == 1, got)
    check("and its arguments are hashed",
          got and len(got[0][2]) == 16 and "calc.py" not in got[0][2], got)

    check("a directory resolves to the history file",
          os.path.basename(resolve(session)) == "chat_history.jsonl",
          resolve(session))
    calls = recent_calls(session)
    check("and its tool calls are found", calls is not None and len(calls) == 2,
          calls)
    check("with their real names",
          [c[0] for c in calls] == ["read_file", "run_terminal_command"], calls)
    check("a human-readable step comes from target_file or description",
          calls[0][1].endswith("calc.py") and calls[1][1] == "commit it", calls)
    check("and the arguments are HASHED, never copied",
          all(len(c[2]) == 16 and "calc.py" not in c[2] for c in calls), calls)

    # A directory with nothing readable in it is None, not zero calls.
    empty = os.path.join(d, "empty-session")
    os.makedirs(empty)
    check("an unreadable session directory is None, not 0 calls",
          recent_calls(empty) is None, recent_calls(empty))

    # And the Claude/Codex shape must still work -- one reader, two vendors.
    other = os.path.join(d, "claude.jsonl")
    with open(other, "w", encoding="utf-8") as fh:
        fh.write(_json.dumps({"message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "ls", "description": "list"}}]}}) + "\n")
    got = recent_calls(other)
    check("the anthropic shape still reads",
          got and got[0][0] == "Bash" and got[0][1] == "list", got)


def main():
    print("=" * 64)
    print(" the letter says which parts you may believe, and on whose word")
    print("=" * 64)
    for t in (test_a_grok_session_directory_is_read_as_a_transcript,
              test_an_unmeasured_value_is_never_a_number,
              test_unmeasured_fields_are_omitted_not_zero_filled,
              test_declared_content_is_always_attributed,
              test_a_brief_without_the_agents_half_looks_incomplete,
              test_a_brief_with_no_checks_declares_itself_unverifiable,
              test_the_verify_block_comes_before_anything_actionable,
              test_an_unusable_check_is_refused_at_write_time):
        t()
    print("\n" + "=" * 64)
    print(f" {'PASS' if not FAILED else 'FAIL'} {RAN - len(FAILED)}/{RAN}"
          + (f"   failed: {FAILED}" if FAILED else ""))
    print("=" * 64)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
