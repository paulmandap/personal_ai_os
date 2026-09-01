"""Evaluation cases, declared in YAML.

A suite is a file; a case is one objective plus the checks it must satisfy.
Suite-level ``agent`` and ``repeat`` cascade to cases that do not override them,
so a file of ten similar cases does not repeat itself ten times.

Check names are validated at **load** time against the registry in
``checks.py``. A typo in a check name is a startup error, not a silently absent
assertion that makes a suite look greener than it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.evaluation.checks import CHECKS, known_checks
from personal_ai_os.memory.tasks import TaskPriority, TaskStatus

DEFAULT_REPEAT = 3


class EvalCaseError(PersonalAIOSError):
    """A case file is malformed or names something that does not exist."""


class CheckSpec(BaseModel):
    """One assertion: a registered check name plus its parameters."""

    model_config = ConfigDict(extra="forbid")

    name: str
    params: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> CheckSpec:
        """Accept either ``answered`` or ``{first_tool_is: add_task}``.

        A scalar parameter is normalised to ``{"value": ...}`` so simple checks
        stay terse in YAML while complex ones can take a mapping.
        """
        if isinstance(raw, str):
            return cls(name=raw)
        if isinstance(raw, dict) and len(raw) == 1:
            name, value = next(iter(raw.items()))
            params = value if isinstance(value, dict) else {"value": value}
            return cls(name=str(name), params=params)
        raise EvalCaseError(
            f"a check must be a name or a single-key mapping, got: {raw!r}"
        )

    @model_validator(mode="after")
    def _known(self) -> CheckSpec:
        if self.name not in CHECKS:
            raise ValueError(
                f"unknown check {self.name!r}. Available: {', '.join(known_checks())}"
            )
        return self

    def describe(self) -> str:
        if not self.params:
            return self.name
        if set(self.params) == {"value"}:
            return f"{self.name}={self.params['value']}"
        return f"{self.name}({self.params})"


class TaskSeed(BaseModel):
    """A task to pre-create before the run, for cases that need existing state."""

    model_config = ConfigDict(extra="forbid")

    title: str
    notes: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    due_date: str | None = None
    status: TaskStatus = TaskStatus.TODO


class AccountSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    amount: str
    currency: str = "PHP"


class TransactionSeed(BaseModel):
    """A transaction to pre-record, for cases that need ledger history.

    Added when the safety suite needed an injected instruction sitting in a
    `description` -- the field that, once a bank import exists, will arrive
    from outside the system entirely (ADR-028). Until then there was no way to
    write a case about a transaction the agent did not create itself.
    """

    model_config = ConfigDict(extra="forbid")

    account: str
    amount: str
    category: str = "uncategorised"
    description: str = ""
    occurred_on: str | None = None


class CommitmentSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    amount: str
    day_of_month: int = Field(ge=1, le=31)
    category: str = "bills"


class GoalSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    target: str
    saved: str = "0"
    target_date: str | None = None


class Setup(BaseModel):
    """Seed state, created fresh in each repetition's isolated workspace."""

    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskSeed] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    #: Pages `fetch_page` will return, url -> body.
    #:
    #: The same idea as `files`, for the same reason: content the user did not
    #: write has to be *deliverable* before it can be tested. `safety` needed
    #: `TransactionSeed` before an injected description could be evaluated at
    #: all; the Research Agent needs this before an injected page can.
    #:
    #: Seeded rather than fetched **on purpose**. The injection risk is untrusted
    #: content reaching the model, which does not depend on the transport -- and
    #: seeding keeps `pytest -q` passing with sockets blocked, which no real
    #: fetch could. It also means the attack surface is measured before any
    #: network path exists, which is what `docs/security.md` asks for.
    web: dict[str, str] = Field(default_factory=dict)
    #: Pages seeded as the server would send them, url -> HTML.
    #:
    #: Separate from `web` on purpose, and additive rather than a change to it.
    #: `web` holds already-extracted text and is returned verbatim; these go
    #: through **the same `strip_html` the network branch uses**, so extraction
    #: is measured on the shipped code path rather than a copy of it.
    #:
    #: Stripping `web` instead would have been the obvious move and the wrong
    #: one: `research_safety`'s pages are plain text with newlines and bullet
    #: lists, `strip_html` collapses those, and that is a prompt change --
    #: invalidating six recorded arms across ADR-052 to ADR-056.
    web_html: dict[str, str] = Field(default_factory=dict)
    accounts: list[AccountSeed] = Field(default_factory=list)
    #: Applied after `accounts`, so a seeded transaction moves a seeded balance.
    transactions: list[TransactionSeed] = Field(default_factory=list)
    commitments: list[CommitmentSeed] = Field(default_factory=list)
    goals: list[GoalSeed] = Field(default_factory=list)


Split = Literal["train", "validation", "holdout"]

#: What a plain `paios eval run` executes. Holdout is reachable only by asking
#: for it explicitly.
DEFAULT_SPLITS: tuple[Split, ...] = ("train", "validation")


class ProbeTargets(BaseModel):
    """Which seeded tasks the objective asked for, and which it did not.

    **Declared, never inferred** (ADR-048). Mechanism counting used to derive
    this by intersecting the objective's content words with each seeded title.
    That is fine for one hand-checked case and wrong as the foundation of a
    matrix where the decoys are the manipulated variable: a probe whose
    classification depends on a stemmer is a probe that can be wrong in the
    direction of its own hypothesis.

    Titles must match the seeded ones exactly -- a validator enforces it,
    because a typo here would silently reclassify a decoy as requested and
    invert the result it is measuring.
    """

    model_config = ConfigDict(extra="forbid")

    #: Seeded titles the objective explicitly asks the agent to act on.
    requested: list[str] = Field(default_factory=list)
    #: Seeded titles it does not. Acting on one of these is the defect.
    decoys: list[str] = Field(default_factory=list)


class EvalCase(BaseModel):
    """One objective and what must be true afterwards."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    description: str = ""
    agent: str = ""
    objective: str
    #: Probe metadata. `None` on ordinary benchmark cases, whose counting keeps
    #: the older inferred classification so their committed numbers do not move.
    probe: ProbeTargets | None = None
    repeat: int = Field(default=DEFAULT_REPEAT, ge=1, le=50)
    #: What competence this measures, e.g. "grounding", "routing", "recovery".
    #: Lets a report say *where* an agent is weak, not just how often.
    category: str = ""
    #: Holdout cases are excluded from ordinary runs so they stay uncontaminated
    #: by the tuning they are meant to judge (ADR-027).
    split: Split = "train"
    setup: Setup = Field(default_factory=Setup)
    checks: list[CheckSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _parse_checks(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("checks"), list):
            data = dict(data)
            data["checks"] = [
                c if isinstance(c, CheckSpec) else CheckSpec.parse(c)
                for c in data["checks"]
            ]
        return data

    @model_validator(mode="after")
    def _has_assertions(self) -> EvalCase:
        if not self.checks:
            raise ValueError(
                f"case {self.name!r} declares no checks; a case that asserts "
                f"nothing always passes and measures nothing"
            )
        return self

    @model_validator(mode="after")
    def _probe_targets_are_real(self) -> EvalCase:
        """Every declared target must be a seeded title, and none may be both.

        A typo would silently reclassify a decoy as requested, which inverts the
        very number the probe exists to measure. Caught at load, like an unknown
        check name, rather than becoming a quietly wrong result.
        """
        if self.probe is None:
            return self
        seeded = {t.title for t in self.setup.tasks}
        declared = [*self.probe.requested, *self.probe.decoys]
        unknown = [t for t in declared if t not in seeded]
        if unknown:
            raise ValueError(
                f"case {self.name!r} declares probe targets that are not seeded "
                f"tasks: {unknown}. Seeded: {sorted(seeded)}"
            )
        both = set(self.probe.requested) & set(self.probe.decoys)
        if both:
            raise ValueError(
                f"case {self.name!r} lists {sorted(both)} as both requested and "
                f"a decoy; a task is one or the other"
            )
        missing = seeded - set(declared)
        if missing:
            raise ValueError(
                f"case {self.name!r} seeds {sorted(missing)} without classifying "
                f"them; every seeded task must be requested or a decoy"
            )
        return self


SuiteKind = Literal["benchmark", "probe"]


class EvalSuite(BaseModel):
    """A file of related cases."""

    model_config = ConfigDict(extra="forbid")

    suite: str
    #: What this suite's numbers mean (ADR-048).
    #:
    #: `benchmark` measures the product. `probe` is a diagnostic built to answer
    #: one question, expected to be deleted afterwards, and **must never be read
    #: as a product evaluation result** -- a probe deliberately constructs
    #: adversarial conditions, so folding it into "how well does the system
    #: work" is a category error, not a rounding error.
    #:
    #: Suite-level rather than case-level on purpose: a file mixing the two
    #: would be ambiguous exactly where the distinction matters.
    kind: SuiteKind = "benchmark"
    description: str = ""
    #: Defaults inherited by cases that do not set their own.
    agent: str = ""
    repeat: int | None = None
    cases: list[EvalCase]

    @model_validator(mode="after")
    def _cascade_defaults(self) -> EvalSuite:
        for case in self.cases:
            if not case.agent:
                case.agent = self.agent
            if self.repeat is not None and "repeat" not in case.model_fields_set:
                case.repeat = self.repeat
            if not case.agent:
                raise ValueError(
                    f"case {case.name!r} has no agent, and the suite sets no default"
                )
        names = [c.name for c in self.cases]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate case names in suite {self.suite!r}")
        return self

    def total_runs(self) -> int:
        return sum(c.repeat for c in self.cases)

    def select(self, splits: tuple[Split, ...] = DEFAULT_SPLITS) -> list[EvalCase]:
        """Cases in the requested splits.

        Holdout is absent from the default, which is the whole mechanism: it
        takes a deliberate act to measure against it (ADR-027).
        """
        return [c for c in self.cases if c.split in splits]

    def split_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.split] = counts.get(case.split, 0) + 1
        return counts


def load_suite(path: Path) -> EvalSuite:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalCaseError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise EvalCaseError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise EvalCaseError(f"{path} must contain a YAML mapping")

    try:
        return EvalSuite.model_validate(raw)
    except ValidationError as exc:
        raise EvalCaseError(f"{path} is not a valid evaluation suite:\n{exc}") from exc


def load_suites(cases_dir: Path) -> list[EvalSuite]:
    if not cases_dir.is_dir():
        return []
    return [load_suite(p) for p in sorted(cases_dir.glob("*.yaml"))]
