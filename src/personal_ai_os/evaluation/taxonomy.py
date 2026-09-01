"""Failure taxonomy.

A pass rate says *how often* an agent failed. It does not say *what kind* of
failure it was, and those need different fixes: a wrong tool is a schema or
description problem, a hallucination is a grounding problem, a planning failure
is a loop-shape problem.

Every check declares the failure it detects, so a run can be summarised as
"31 missing-tool, 18 incorrect-reasoning" rather than "55%" -- which is what
makes the next action obvious instead of a guess.

Codes are stable identifiers. Never renumber one: results are committed, and a
code that changes meaning silently invalidates every stored comparison.
"""

from __future__ import annotations

from enum import Enum


class Failure(str, Enum):
    """What went wrong, not how much."""

    INCORRECT_REASONING = "F001"
    WRONG_TOOL = "F002"
    MISSING_TOOL = "F003"
    WRONG_TOOL_ORDER = "F004"
    HALLUCINATION = "F005"
    INCOMPLETE_ANSWER = "F006"
    INSTRUCTION_VIOLATION = "F007"
    SAFETY_VIOLATION = "F008"
    MEMORY_FAILURE = "F009"
    CONTEXT_FAILURE = "F010"
    PLANNING_FAILURE = "F011"
    STATE_MANAGEMENT = "F012"
    FORMATTING = "F013"
    UNSUPPORTED_CLAIM = "F014"
    UNNECESSARY_ESCALATION = "F015"
    #: The case did not deliver what it claims to test. Not an agent
    #: failure at all -- a fact about the instrument, and the worst kind
    #: of green. Twice this project nearly shipped a conclusion on one:
    #: ADR-038's three cases at 100% that never delivered their attack,
    #: and ADR-052's `did_not_call_tool` naming a tool that does not
    #: exist.
    VACUOUS_CASE = "F016"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def severity(self) -> str:
        """How much a failure of this kind matters.

        `critical` failures are defects rather than scores: a safety violation
        or an invented claim is not "a lower number", it is something that must
        be zero. Ranking them alongside a formatting slip would let a good
        average hide the one result that matters.
        """
        return _SEVERITY[self]

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.value} {self.label}"


_LABELS: dict[Failure, str] = {
    Failure.INCORRECT_REASONING: "incorrect reasoning",
    Failure.WRONG_TOOL: "wrong tool",
    Failure.MISSING_TOOL: "missing tool",
    Failure.WRONG_TOOL_ORDER: "wrong tool order",
    Failure.HALLUCINATION: "hallucination",
    Failure.INCOMPLETE_ANSWER: "incomplete answer",
    Failure.INSTRUCTION_VIOLATION: "instruction violation",
    Failure.SAFETY_VIOLATION: "safety violation",
    Failure.MEMORY_FAILURE: "memory failure",
    Failure.CONTEXT_FAILURE: "context failure",
    Failure.PLANNING_FAILURE: "planning failure",
    Failure.STATE_MANAGEMENT: "state-management failure",
    Failure.FORMATTING: "formatting failure",
    Failure.UNSUPPORTED_CLAIM: "unsupported claim",
    Failure.UNNECESSARY_ESCALATION: "unnecessary escalation",
    Failure.VACUOUS_CASE: "case tested nothing",
}

_SEVERITY: dict[Failure, str] = {
    # Defects, not scores. Any non-zero count here is a bug to fix.
    Failure.HALLUCINATION: "critical",
    Failure.SAFETY_VIOLATION: "critical",
    Failure.UNSUPPORTED_CLAIM: "critical",
    Failure.STATE_MANAGEMENT: "critical",  # the stored data ended up wrong
    Failure.VACUOUS_CASE: "critical",  # a green that measured nothing
    # Real problems, but the agent was honest about what it did.
    Failure.INCORRECT_REASONING: "major",
    Failure.WRONG_TOOL: "major",
    Failure.MISSING_TOOL: "major",
    Failure.PLANNING_FAILURE: "major",
    Failure.INSTRUCTION_VIOLATION: "major",
    Failure.MEMORY_FAILURE: "major",
    Failure.CONTEXT_FAILURE: "major",
    # Costs an iteration; the loop is built to absorb these (ADR-008).
    Failure.WRONG_TOOL_ORDER: "minor",
    Failure.INCOMPLETE_ANSWER: "minor",
    Failure.FORMATTING: "minor",
    Failure.UNNECESSARY_ESCALATION: "minor",
}

SEVERITY_ORDER = ("critical", "major", "minor")


def by_severity(failures: dict[Failure, int]) -> list[tuple[Failure, int]]:
    """Most serious first, then most frequent -- the order to fix them in."""
    return sorted(
        failures.items(),
        key=lambda item: (SEVERITY_ORDER.index(item[0].severity), -item[1]),
    )
