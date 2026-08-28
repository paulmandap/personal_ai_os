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
    accounts: list[AccountSeed] = Field(default_factory=list)
    commitments: list[CommitmentSeed] = Field(default_factory=list)
    goals: list[GoalSeed] = Field(default_factory=list)


Split = Literal["train", "validation", "holdout"]

#: What a plain `paios eval run` executes. Holdout is reachable only by asking
#: for it explicitly.
DEFAULT_SPLITS: tuple[Split, ...] = ("train", "validation")


class EvalCase(BaseModel):
    """One objective and what must be true afterwards."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    description: str = ""
    agent: str = ""
    objective: str
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


class EvalSuite(BaseModel):
    """A file of related cases."""

    model_config = ConfigDict(extra="forbid")

    suite: str
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
