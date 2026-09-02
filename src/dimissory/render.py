"""Brief -> Markdown.

One file, portable, no tool-specific format, because the destination is
explicitly another account or another vendor. It has to survive being pasted
into a product nobody here has seen.

The renderer is where the trust contract becomes visible, so it is the one
place that must never be clever. Three rules, each of which was a real defect
in the predecessor before it was a rule:

  1. An unmeasured field is OMITTED. Never `0`, never `unknown`, never `-`.
     All three read as findings to someone scanning a document.
  2. Declared content is printed under an attribution that cannot be lost by
     reformatting -- it is on the same line as the heading.
  3. A degraded or unverifiable brief says so at the top, where a reader
     decides how much to trust the rest, not in a footnote.
"""

from __future__ import annotations

from .brief import Brief, Unmeasured

_ATTRIB = "the agent's own words, written before the window closed"


def _lines(observed) -> list:
    """The observed block, containing only what was actually measured."""
    out = []
    k = observed.known()
    if "head" in k:
        subj = k.get("head_subject")
        out.append(f"HEAD        {k['head']}" + (f"  {subj!r}" if subj else ""))
    if "dirty" in k:
        paths = k["dirty"]
        out.append(f"dirty       {', '.join(paths) if paths else '(clean)'}")
    if "last_command" in k:
        exit_ = k.get("last_exit")
        tail = f"  -> exit {exit_}" if exit_ is not None else ""
        out.append(f"last cmd    {k['last_command']}{tail}")
    if "calls" in k:
        calls = k["calls"]
        out.append(f"calls       {len(calls)} observed in the transcript tail")
    if "window_used_percent" in k:
        resets = k.get("window_resets_at")
        tail = f", resets {resets}" if resets else ""
        out.append(f"window      {k['window_used_percent']:.0f}% used{tail}")
    return out


def render(brief: Brief) -> str:
    """The whole letter, as Markdown."""
    o, d = brief.observed, brief.declared
    k = o.known()
    parts = [f"# Dimissory letter: {brief.session}", ""]

    written = k.get("written_at")
    parts.append(
        "Issued by dimissory" + (f" at {written}" if written else "") + "."
    )
    parts.append(
        "It transfers what this session had reached, so the next one continues "
        "instead of\nreconstructing. Read the layers as what they are."
    )
    parts.append("")

    # The banners go first, because they change how everything below is read.
    if brief.is_degraded:
        parts += [
            "> **DEGRADED -- the agent did not write its half.**",
            "> Everything below is machine-derived. There is no task, no "
            "decision record and",
            "> no stated next action, because none was supplied before the "
            "window closed.",
            "",
        ]
    if brief.is_unverifiable:
        parts += [
            "> **UNVERIFIABLE -- this letter carries no checks.**",
            "> Nothing here can be confirmed against the current state of the "
            "world. Treat it",
            "> as a claim, not a finding.",
            "",
        ]

    if brief.checks:
        parts += ["## Verify first", "",
                  "Run these before acting. If any disagrees, the world moved "
                  "after this letter was\nwritten and it is STALE -- re-derive "
                  "rather than continue.", "", "```"]
        for c in brief.checks:
            if c.why:
                parts.append(f"# {c.why}")
            parts.append(c.command)
            parts.append(f"#   expected: {c.expect}")
        parts += ["```", ""]

    if not d.is_empty():
        if d.task:
            parts += [f"## Task -- {_ATTRIB}", "", d.task, ""]
        if d.decided:
            parts += [f"## Decided -- {_ATTRIB}", ""]
            parts += [f"- {x}" for x in d.decided] + [""]
        if d.ruled_out:
            parts += [f"## Ruled out -- {_ATTRIB}", ""]
            parts += [f"- {x}" for x in d.ruled_out] + [""]
        if d.next_action:
            parts += [f"## Next action -- {_ATTRIB}", "", d.next_action, ""]
        if d.constraints:
            parts += [f"## Constraints -- {_ATTRIB}", ""]
            parts += [f"- {x}" for x in d.constraints] + [""]

    body = _lines(o)
    if body:
        parts += ["## Observed -- established by dimissory, not by the agent", "",
                  "```"] + body + ["```", ""]

    parts += [
        "## Resume", "",
        "Paste into any agent CLI:", "",
        "```",
        f'Read the dimissory letter for "{brief.session}", run its Verify '
        f"block, then",
        "continue from Next action. If a check disagrees, stop and say so.",
        "```",
        "",
    ]
    return "\n".join(parts).rstrip() + "\n"
