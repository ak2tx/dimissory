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

> **Status: early, `0.0.10`.** The brief format, the trust contract, the verify
> mechanism, the agent hooks, the plan-window meter and the pickup on the
> receiving side all work and are tested — green from a bare checkout with
> nothing installed. Two vendors need a second step before anything is
> automatic, and `dim status` exits non-zero until they have it: Claude needs
> `dim statusline --install`, and Codex needs you to approve its hooks once.
> Both are listed under [Known gaps](#known-gaps-stated-plainly).

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
dim setup                 # once: hooks for every agent CLI found, config, a proof letter
dim meter                 # how much of every plan window is gone
dim write                 # issue a letter now
dim show                  # print the most recent one
dim resume                # run its Verify block; exit 2 if stale
dim pickup                # print AND verify the letter for this directory
dim status                # is anything going to seal before a window closes?
```

`dim` and `dimissory` are the same command. Letters land in
`~/.dimissory/letters` unless you pass `--dir`.

`dim resume` has exactly two meaningful outcomes. **Exit 0**: every check
agreed, the letter may be acted on. **Exit 2**: it is stale. There is
deliberately no exit code meaning "probably fine".

## Handing off to another model

This is the loop the tool exists for, end to end, with nothing typed twice:

1. **You work in Claude Code**, in `~/proj`. The hook asks the agent to
   declare as it goes: what the task is, what it has done, what it decided,
   what it ruled out, what it learned about the codebase, and the exact next
   action. Each is one `dim declare` call, and the agent runs them itself.
   After each commit the hook asks once for `--done`, and `dim declare`
   refuses a pasted placeholder, a one-word next action, or a next action
   that just restates the task.
2. **The five-hour cap approaches.** At 85% of the window (`dim meter` shows
   it; `write_at` sets it) the hook seals a letter into `~/.dimissory/letters`.
   It records the directory, the branch, the commit, the dirty paths, which
   guide files the project has (`CLAUDE.md`, `AGENTS.md`, `README.md`), and
   everything the agent declared — plus a Verify block that can prove the
   letter is still true.
3. **You open Codex, or Claude on another account, in the same `~/proj`.**
   Its SessionStart hook finds the newest letter for that directory, runs the
   Verify block, and hands the new session the letter with the verdict:

   ```
   dimissory: a handoff letter for this directory was sealed 4m ago by a
   claude session (abc123). Its Verify block holds (3 check(s) agreed), so
   the world has not moved since it was written. Continue from it instead of
   reconstructing: take its Next action as your starting point, read the
   project guides it lists under Observed before touching code, and do not
   redo what it lists under Done. ...
   ----- begin letter -----
   # Dimissory letter: abc123
   ...
   ```

   The new model continues from *Next action* without being told what the
   project is. If the world moved — you committed, you switched branch — the
   verdict says `STALE` and names the check that disagreed, and the letter is
   still delivered because *Decided* and *Ruled out* are still worth having.
4. **It is delivered once per session per letter.** A session restarting
   after `/compact` is handed the letter PreCompact just sealed; a session is
   never handed its own letter back on every prompt. `pickup.max_age` (a
   week) is the only age limit; the Verify block, not the clock, decides
   whether a letter is still true.

Two things this does not do. It does not carry the letter to **another
machine** — for that, `dim show` and paste; the `Resume` section at the bottom
of every letter is the prompt. And on **Grok**, which ignores SessionStart
context, it is `dim pickup` by hand: the same letter and the same verdict,
printed for you to paste.

```bash
dim pickup                # in the project directory: the letter, then its verdict
dim pickup path/to.md     # a specific one, wherever it came from
```

## What is built

| Piece | State |
|---|---|
| Brief model and trust contract | working, 32 checks |
| Markdown renderer | working |
| Observed block — git, branch, dirty paths, diff size, directory, guide files | working |
| Observed block — last shell command and whether the host flagged it failed | working, Claude Code transcripts |
| Declared block — task, done, decided, ruled out, learned, next, constraints | working |
| Quality floor on `dim declare` — placeholders, bare words, next = task refused | working |
| Nudge to record `--done` after a commit | working, Claude Code payloads; others untested |
| Verify block — derive, render, compare, fail | working |
| Transcript reading — bounded tail, hashed args | working |
| Agent hooks — install, ask, gate | working, all three CLIs |
| Plan-window meter — Codex, Grok | working, both caps |
| Plan-window meter — Claude | working, via `dim statusline` |
| Seal before the wall, on the tool-call heartbeat | working |
| **Pickup — the next session in the directory is handed the letter, verified** | working, Claude and Codex; Grok by hand |
| `dim pickup`, `dim resume` scoped to the directory you are in | working |
| `dim status` | working |
| Exit codes | not claimed: no transcript measured carries one |
| One letter per session unless something changed | working |
| Pruning old letters (`letters.keep`) | working; 0 or less keeps everything |
| `agents.<name> = false` silences that agent's installed hook | working |
| Codex hooks | installed, but inert until Codex trusts them |
| Delivery to another machine | by hand: `dim show`, paste the letter |

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

**Codex will not run a hook it has not trusted, and says nothing when it
declines to.** Measured on a live box, same file and same task:

```
codex exec ...                              ->  0 hooks fired
codex exec --dangerously-bypass-hook-trust  ->  2 hooks fired
```

So installing Codex hooks is necessary and not sufficient. Start `codex` once
and approve them when it asks. `dim hook --install` now says `NOT ARMED YET`
rather than reporting a bare success, and `dim status` shows Codex as
`untrusted` instead of `yes`. dimissory does **not** write a trust record on
your behalf — that is a control Codex built deliberately, and the flag which
skips it is called `--dangerously-bypass-hook-trust`.

**Non-interactive Claude has no meter.** `claude -p`, the SDK, and
`--safe-mode` have no status bar, so nothing records Claude's window there:
the interface that carries the number only exists in the interactive TUI.

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

**Pickup is also a request.** The letter arrives as context at session
start, the same channel as the ask, and the same measurement applies: Claude
Code and Codex acted on it every time it was tried; Grok ignores it. A model
can still read the letter and reconstruct anyway. What pickup removes is the
step where a person has to remember the letter exists.

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
python3 tests/test_pickup.py            # the next session is handed the letter
python3 tests/test_handoff_quality.py   # what goes into the letter, and honest settings
```

No dependencies and no test runner. Requires Python 3.11+ (`tomllib`).

MIT © Ak2tx LLC
