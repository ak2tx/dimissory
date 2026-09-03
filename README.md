# dimissory

**Don't lose the run when the window closes.**

A *dimissory letter* transfers a cleric's standing from one bishop to another,
so he can continue under a new authority without being examined again.

This does the same for an agent session. When your plan window is about to run
out — the five-hour cap, the weekly cap, a session about to be compacted —
`dimissory` writes a portable letter of transfer, so the next session continues
the work instead of reconstructing it. On another account, or another model.

```bash
pip install dimissory
```

> **Status: early, `0.0.2`.** The brief format, the trust contract, the verify
> mechanism, the agent hooks and the plan-window meter all work and are tested
> — 540 checks across 10 files. What is *not* done is listed under
> [What is built](#what-is-built), including the gap that matters most:
> non-interactive runs have no meter on either vendor. Claude needs
> `dim statusline --install` as a second step, and `dim status` will tell you
> if you skipped it — it exits non-zero when nothing can seal.

---

## The problem this is actually solving

Writing a handoff before a session dies is not a new idea. Lifeline captures a
session when it sees a usage-limit message; `rate-limit-handoff` maintains a
living `handoff.md`; Context Passport extracts decisions and a resume prompt; a
dozen skills write `HANDOFF.md` when you remember to ask.

They share two properties:

1. **They fire after the wall.** A 429, a "usage limit reached", a context
   window that already filled.
2. **They write a summary**, which the receiving agent has to take on faith.

Context compaction and `--resume` already solve the *context* problem, and the
vendors will keep improving them. What none of them solve is the **plan window
ending**, on **an account you no longer have**, in a way the next agent can
**check**.

That is the whole of it: earlier, and checkable.

## The trust contract

A letter is made of three layers, and they are never blended, because the
receiving agent has to know how much of it to believe and on whose word.

| Layer | What it is | How much to trust it |
|---|---|---|
| **Observed** | Git HEAD and dirty paths, the last command and its exit code, the tool-call tail with hashed arguments, window state. | Machine-derived. No model wrote any of it. |
| **Declared** | The task, decisions taken, what was ruled out, the next action, standing constraints. | The agent's own words, always attributed. The valuable half and the soft half. |
| **Verify** | A short executable block. | Run it **first**. If it fails, the world moved and the letter is stale. |

The third layer is the differentiator, and it isn't a formatting choice. Every
competing tool writes a summary and hopes. **A letter that can fail its own
check is one you can act on without re-reading the transcript** — which is the
promise: *continue, don't reconstruct*.

## The rules, and why they are types rather than documentation

The predecessor project shipped a handoff that printed this under a heading
promising "a record of what the session was observed doing":

```
- ran for: 0s
- steps completed (seq): 0
```

Two of those were written on a real machine, and both were worthless. The zero
did not mean "no time passed", it meant "nobody looked". So:

- **A number nobody measured is omitted, never zero.** `UNMEASURED` is a
  singleton that is falsey and *raises* on `int()` — you cannot accidentally
  format it as `0`.
- **Declared is never rendered as observed.** Attribution sits on the heading
  line, where reformatting cannot separate a claim from whose claim it is.
- **A letter missing the agent's half is visibly degraded**, not merely short.
- **A letter with no checks says it is unverifiable** — a claim, not a finding.
- **No secrets, ever.** Arguments are hashed, not copied. The letter is
  designed to be pasted into another vendor's product.

Each of those is asserted in `tests/test_contract.py`, because the predecessor
documented all of them correctly and shipped the opposite anyway.

## Use

```bash
dim write                 # issue a letter now
dim show                  # print the most recent one
dim resume                # run its Verify block; exit 2 if stale
dim status                # how much of the plan window is gone
```

`dim` and `dimissory` are the same command. Letters land in
`~/.dimissory/letters` unless you pass `--dir`.

`dim resume` has exactly two meaningful outcomes. **Exit 0**: every check
agreed, the letter may be acted on. **Exit 2**: it is stale. There is
deliberately no exit code meaning "probably fine".

## What is built

| Piece | State |
|---|---|
| Brief model and trust contract | working, 32 checks |
| Markdown renderer | working |
| Observed block — git, dirty paths | working |
| Verify block — derive, render, compare, fail | working |
| Transcript reading — bounded tail, hashed args | working |
| Agent hooks — install, ask, gate | working, all three CLIs |
| Plan-window meter — Codex, Grok | working, both caps |
| Plan-window meter — Claude | working, via `dim statusline` |
| Seal before the wall, on the tool-call heartbeat | working |
| `dim status` | working |
| Observed block — last command and exit code | not yet |
| Pruning old letters (`letters.keep`) | not yet, setting is inert |
| `codex exec` (non-interactive) | no seal path, see below |
| Cross-account delivery | not yet |

### Claude needs one extra step, and it is not optional

Codex and Grok write their plan window to disk, so dimissory just reads it.
**Claude Code writes it nowhere** — but it hands the numbers to your
statusline command on stdin every turn:

```json
"rate_limits": {
  "five_hour": {"used_percentage": 100, "resets_at": 1788416400},
  "seven_day": {"used_percentage": 58,  "resets_at": 1788764400}
}
```

So `dim statusline` records them, and the hook reads them back:

```
dim statusline --install     # wraps any statusline you already have
dim status                   # says what the meter can see, per agent
```

Without it, Claude has no percentage to seal on and you only get a letter
*at* the wall — dimissory reads the `quotaLimits` tombstone from the
transcript for that, which carries the reset time but no percentage
(measured: 81 records on a live machine, every one `status: "rejected"`).
`dim status` exits non-zero when no meter is live, precisely so this cannot
be mistaken for readiness.

This is **not** a supervising process. Nothing wraps the `claude` binary and
nothing parses its output; Claude Code calls `dim statusline` itself, through
an interface it already invokes on its own schedule.

### Known gaps, stated plainly

**Non-interactive runs have no meter, on either vendor.** `codex exec` fires
`UserPromptSubmit` but not `PostToolUse`, so it has no seal path at all.
`claude -p`, the SDK, and `--safe-mode` have no status bar, so nothing records
Claude's window there either. Both are the same shape of gap: the interface
that carries the number only exists in the interactive TUI.

**The sample clock and the seal clock are different clocks.** The seal fires
on tool calls; the statusline does *not* — Claude Code re-renders on session
start, each new assistant message, `/compact`, and a `refreshInterval` timer.
A long single tool call or a long reasoning stretch leaves the reading frozen
in between, so install sets `refreshInterval` to keep it moving.

That gap is handled by arithmetic rather than by trusting the sample.
Staleness here is **one-directional**: usage inside a window only grows, so an
old reading always *understates*. It can never cause a false seal — it causes
**no seal at all**, which is the only failure that matters. And the growth has
a ceiling: inside a window of length L, usage cannot exceed 100% over L, so in
`age` seconds it can have risen by at most `(age / L) × 100` points.

So the decision asks whether the *ceiling* has crossed the margin, not whether
the last sample did. An 84% reading taken an hour ago seals; the same reading
taken a minute ago does not; 58% of a weekly cap barely moves in an hour and
correctly doesn't. The ceiling drives the decision only — a letter always
reports the figure that was actually measured.

### The gate is a request, not a guarantee

The `Stop` hook blocks a turn that declared nothing and feeds back what to
run. It blocks **once**: the continuation carries `stop_hook_active`, and
blocking again is how a gate becomes a trap that a user has to kill. So an
agent that ignores the block finishes anyway, and the letter is sealed
DEGRADED rather than not sealed at all.

Never trapping your session is the higher duty, so this is deliberate — but
it means nothing here forces the agent to write its half. The journal
narrows the problem by collecting declarations as work happens instead of
asking for everything at the end. It does not close it.

## How this loses

Stated here rather than in a postmortem, because the predecessor's credibility
came from publishing the unflattering number:

- **The declared half is only as good as a tired agent.** An agent at 85% of
  its window, writing about its own reasoning, is the weakest link. If those
  sections read as vague, this is a call log with extra steps.
- **Vendors close the gap for free.** Session Memory, `--resume` and
  resume-from-summary already exist. Cross-account and cross-vendor is the part
  they have no incentive to build — but "no incentive" is not "never".
- **Verify is cheap to copy.** A week of work, once someone sees it. The
  defensible part is the meter that lets you fire before the wall, not the
  block itself.
- **The test that settles it has not been run.** Ten real interrupted tasks
  producing letters, five resumed successfully from the letter alone, on a
  different account or model. Until that passes, this is a feature with a good
  argument, not a product.

## Development

```bash
python3 tests/test_contract.py          # the trust contract
python3 tests/test_setup_and_config.py  # setup, settings, and the -c flag
python3 tests/test_declared_floor.py    # the Python version we claim to support
python3 tests/test_verify_can_fail.py   # the verify block detects a moved world
```

No dependencies and no test runner. Requires Python 3.11+ (`tomllib`).

MIT © Ak2tx LLC
