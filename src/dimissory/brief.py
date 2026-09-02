"""The brief, and the three layers of trust it is made of.

A dimissory letter transfers a cleric's standing from one bishop to another so
he can continue under a new authority without being examined again. The whole
value of such a letter is that the receiving authority knows exactly how much
of it to believe, and on whose word.

That is the design here, and it is enforced by the types rather than by
remembering it:

    Observed    machine-derived. Nobody's opinion. Omitted when unmeasured.
    Declared    the agent's own words. Always attributed, never promoted.
    Verify      executable. Run first. Fails when the world has moved.

The rules below exist because the predecessor project shipped a handoff that
printed `steps completed (seq): 0` under a heading promising "a record of what
the session was observed doing" -- an empty page dressed as evidence. Two of
those were written on a real machine and both were worthless. A zero that means
"not measured" is the failure this module is built to make impossible.
"""

from __future__ import annotations

import dataclasses
import shlex
from typing import Optional


class Unmeasured:
    """The absence of a measurement, which is not the number zero.

    `ran for: 0s` and `ran for: (not measured)` are different claims and only
    one of them was ever true. A field holding this renders as omitted, and any
    attempt to format it as a number raises rather than quietly producing 0.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return "UNMEASURED"

    def __int__(self):
        raise TypeError(
            "an unmeasured value has no number. Omit the line instead -- this "
            "is the check that stops '0 steps' being printed as evidence."
        )

    __float__ = __int__
    __index__ = __int__


UNMEASURED = Unmeasured()


@dataclasses.dataclass(frozen=True)
class Observed:
    """Facts the tool established for itself. No model wrote any of this.

    Every field is Optional and defaults to UNMEASURED. That default is the
    point: a field nobody filled in renders as absent, not as zero, empty
    string, or "unknown" -- all three of which read as findings.
    """

    head: object = UNMEASURED            # git commit, short
    head_subject: object = UNMEASURED
    dirty: object = UNMEASURED           # tuple[str, ...] of paths
    last_command: object = UNMEASURED
    last_exit: object = UNMEASURED
    calls: object = UNMEASURED           # tuple[Call, ...] from the transcript
    window_used_percent: object = UNMEASURED
    window_resets_at: object = UNMEASURED
    started_at: object = UNMEASURED
    written_at: object = UNMEASURED

    def known(self) -> dict:
        """Only the fields that were actually measured."""
        return {f.name: getattr(self, f.name)
                for f in dataclasses.fields(self)
                if not isinstance(getattr(self, f.name), Unmeasured)}


@dataclasses.dataclass(frozen=True)
class Declared:
    """What the agent said, while it still had budget to think.

    The valuable half and the soft half. It is never rendered without
    attribution, and a brief missing it does not quietly look complete -- see
    `Brief.is_degraded`.
    """

    task: Optional[str] = None
    decided: tuple = ()
    ruled_out: tuple = ()
    next_action: Optional[str] = None
    constraints: tuple = ()

    def is_empty(self) -> bool:
        return not any((self.task, self.decided, self.ruled_out,
                        self.next_action, self.constraints))


@dataclasses.dataclass(frozen=True)
class Check:
    """One executable assertion about the world the brief was written in.

    `expect` is what the command printed when the brief was written. The
    receiving agent runs the command and compares. This is the only part of a
    brief that can fail, which is the only reason the rest can be trusted.
    """

    command: str
    expect: str
    why: str = ""

    def shell(self) -> str:
        return self.command

    def __post_init__(self):
        # A check whose command cannot be parsed is not a check. Catching it
        # here means a malformed brief fails at write time, on the machine that
        # still has the context to fix it, rather than at pickup.
        try:
            if not shlex.split(self.command):
                raise ValueError("empty command")
        except ValueError as e:
            raise ValueError(f"unusable check command {self.command!r}: {e}")


@dataclasses.dataclass(frozen=True)
class Brief:
    """One dimissory letter.

    Assembled by `dimissory.observe` and `dimissory.verify`, rendered by
    `dimissory.render`. Deliberately inert: it holds no file handles, runs
    nothing, and can be constructed in a test without a daemon, a socket or a
    repository.
    """

    session: str
    observed: Observed
    declared: Declared
    checks: tuple = ()
    # How long before sealing each declared field was last written, or None for
    # one never declared. None rather than 0 for the same reason as everywhere
    # else here: zero would read as "declared just now".
    ages: dict = dataclasses.field(default_factory=dict)

    @property
    def is_degraded(self) -> bool:
        """True when the agent never wrote its half.

        A degraded brief is still worth writing -- the observed half stands
        alone and is often enough to resume from. What it must not do is look
        finished. `render` prints a banner for this, and the test suite asserts
        the banner rather than trusting the renderer to remember.
        """
        return self.declared.is_empty()

    @property
    def has_stale_current_state(self) -> bool:
        """True when a CURRENT-state field is too old to present as a plan.

        Only `task` and `next` can be stale. A decision or a constraint is a
        historical assertion that age does not invalidate -- it stands until
        revoked, which is what `journal.REVOKE` exists for. Review was explicit
        that a single global freshness signal is the wrong shape here.
        """
        from .render import is_stale
        return any(is_stale(f, (self.ages or {}).get(f))
                   for f in ("task", "next"))

    @property
    def is_unverifiable(self) -> bool:
        """True when nothing in this brief can be checked on pickup.

        Not fatal, and not hidden. A brief with no checks is a claim; the
        reader is entitled to know which kind of document they are holding.
        """
        return not self.checks
