# The trust contract

A dimissory letter transfers standing between authorities. Its value is that
the receiving authority knows exactly how much of it to believe, and on whose
word. That is the design, and it is enforced by types rather than by convention.

## Three layers

| Layer | Source | Trust |
|---|---|---|
| **Observed** | `observe.py` — git, the filesystem, the transcript tail | Machine-derived. No model wrote it. |
| **Declared** | The agent, asked while it still had budget | Its own words. Always attributed. |
| **Verify** | `checks_for()` — derived from what was measured | Executable. Run first. Can fail. |

The third layer is the differentiator. Every competing tool writes a summary and
hopes; a letter that can fail its own check is one you can act on without
re-reading the transcript.

## The rules

Each is enforced in code and asserted in `tests/test_contract.py`, with a
negative control proving the assertion can fail.

**A number nobody measured is omitted, never zero.** `UNMEASURED` is a falsey
singleton that raises on `int()`. You cannot accidentally render it as `0`. The
predecessor printed `ran for: 0s` and `steps completed (seq): 0` under a heading
promising a record of what the session was observed doing — an empty page
dressed as evidence, in the feature built to fix the evidence problem.

**Declared is never promoted to observed.** Attribution sits on the heading
line, where reformatting cannot separate a claim from whose claim it is.

**A letter missing the agent's half is visibly degraded.** Not merely short.
Both real handoffs the predecessor ever wrote were missing that half and both
looked finished.

**A letter with no checks says it is unverifiable.** A claim, not a finding.

**A check is only emitted for a measured fact.** Fabricating one against an
unmeasured value produces a verification step that always passes, which is
worse than no verification at all.

**No secrets.** Arguments are hashed, never copied. The letter is designed to be
pasted into another vendor's product.

## Why the trigger has to be a meter

Firing at a 429 needs no meter and buys nothing: by then the agent has no budget
to write the valuable half. Firing at 85% of the plan window requires knowing
what the window is, which means provider adapters and a staleness gate that
refuses to report a reading it cannot date.

That is the part a competitor cannot skip, and it is the next thing to port.

## Delivery is part of the contract

A letter nobody reads transfers nothing. The receiving side is `hook._pickup`:
at session start, the newest letter written in the current directory is found
(`letters.latest_for_cwd`, matching on the `cwd` the Observed block records),
its Verify block is run (`verify.run`), and letter plus verdict are handed to
the session as context. The verdict is delivered alongside, never used to
withhold: a stale letter's Decided and Ruled out are still the previous
session's words, and the reader is told which check moved.

Three rules hold here too:

**A letter is matched to a directory it recorded, or not at all.** Handing a
session the newest letter from anywhere means handing it another project's
plan. A letter with no `cwd` line is never picked up automatically.

**Once per session per letter.** A newer letter -- sealed at PreCompact -- is
delivered again after the restart. A session's own letter is never read back
to it on a prompt.

**The pointer, not the copy.** The letter names the project's guide files
(`CLAUDE.md`, `AGENTS.md`, `README.md`) and tells the reader to open them. It
does not copy them: each CLI reads only its own, so the letter's job is to say
the others exist.

## The test that settles whether this is a product

Ten real interrupted tasks producing letters, and five resumed successfully from
the letter alone — no re-explaining, on a different account or model. It is
cheap and it can fail. Until it passes, this is a feature with a good argument.
