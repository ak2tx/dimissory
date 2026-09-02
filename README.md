# dimissory

**Don't lose the run when the window closes.**

A *dimissory letter* transfers a cleric's standing from one bishop to another,
so he can continue under a new authority without being examined again.

This does the same for an agent session. When your plan window is about to run
out — the five-hour cap, the weekly cap, a session about to be compacted —
`dimissory` writes a portable letter of transfer, so the next session continues
the work instead of reconstructing it. On another account, or another model.

> **Status: early. `0.0.1` is a skeleton.** The brief format, the trust
> contract and the verify mechanism work and are tested. The plan-window meter
> and the agent-side hooks are being ported from the predecessor project. There
> is no release on PyPI yet.

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
| Verify block — derive, render, run | working |
| Transcript reading — bounded tail, hashed args | working |
| Observed block — last command and exit code | not yet |
| Plan-window meter | porting |
| Agent hooks — ask for the declared half | not yet |
| Cross-account delivery | not yet |

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
```

No dependencies and no test runner. Requires Python 3.11+ (`tomllib`).

MIT © Ak2tx LLC
