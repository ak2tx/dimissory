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

import json

from .brief import Brief, Unmeasured

_ATTRIB = "the agent's own words"

# How old a declaration may be before it stops being presented as current.
# PER FIELD, because they decay differently -- review was explicit about this:
#
#   next        a stale next action is the dangerous one. It reads as "do this
#               now" while describing a world two hours gone.
#   task        the same shape, but a task changes far less often.
#   decided     age alone does not invalidate a decision. It is a historical
#   ruled_out   assertion and stays true until revoked.
#   constraint  effective until revoked.
#
# So only the CURRENT-state fields have a threshold at all. Wall-clock seconds
# rather than "a fraction of the session", because session duration is unstable
# and often unknowable -- a fraction of an unknown is not a threshold.
STALE_AFTER = {"next": 45 * 60, "task": 4 * 60 * 60}


def _age_phrase(seconds):
    if seconds is None:
        return ""
    if seconds < 90:
        return "declared just now"
    if seconds < 3600:
        return f"declared {int(seconds // 60)}m before sealing"
    return f"declared {seconds / 3600:.1f}h before sealing"


def is_stale(field, seconds):
    """Whether a declared field is too old to present as current."""
    limit = STALE_AFTER.get(field)
    return limit is not None and seconds is not None and seconds > limit


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
    if brief.has_stale_current_state:
        parts += [
            "> **STALE DECLARED STATE -- the plan below may not be current.**",
            "> The agent declared a task or next action and then did not "
            "update it. Those",
            "> are under 'Stale declared state' rather than presented as "
            "instructions.",
            "",
        ]
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
            # A multi-line expectation breaks the one-line `#   expected:`
            # form: it spilled across the block, and `resume` -- which reads
            # the single line after each command -- compared against only its
            # first line. Encoded as JSON so it is exactly one line, always
            # parseable, and unambiguous about trailing whitespace.
            parts.append(f"#   expected: {json.dumps(c.expect)}")
        parts += ["```", ""]

    if not d.is_empty():
        ages = brief.ages or {}
        stale = []

        def head(label, field):
            phrase = _age_phrase(ages.get(field))
            return f"## {label} -- {_ATTRIB}" + (f", {phrase}" if phrase else "")

        if d.task:
            if is_stale("task", ages.get("task")):
                stale.append(("Task", d.task, ages.get("task")))
            else:
                parts += [head("Task", "task"), "", d.task, ""]
        if d.decided:
            parts += [head("Decided", "decided"), ""]
            parts += [f"- {x}" for x in d.decided] + [""]
        if d.ruled_out:
            parts += [head("Ruled out", "ruled_out"), ""]
            parts += [f"- {x}" for x in d.ruled_out] + [""]
        if d.next_action:
            if is_stale("next", ages.get("next")):
                stale.append(("Next action", d.next_action, ages.get("next")))
            else:
                parts += [head("Next action", "next"), "", d.next_action, ""]
        if d.constraints:
            parts += [head("Constraints", "constraint"), ""]
            parts += [f"- {x}" for x in d.constraints] + [""]

        # Kept, but NOT under a heading that reads as an instruction. A stale
        # next action presented as "Next action" is the failure mode: it says
        # "do this now" about a world that has moved on.
        if stale:
            parts += ["## Stale declared state -- NOT current", "",
                      "The agent declared these and then did not update them. "
                      "They are recorded\nbecause they are evidence, and "
                      "withheld from the sections above because\nacting on "
                      "them directly would be acting on a stale plan.", ""]
            for label, value, secs in stale:
                parts += [f"- **{label}** ({_age_phrase(secs)}): {value}"]
            parts += [""]

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
