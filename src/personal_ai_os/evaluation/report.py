"""Results: aggregation, storage, and comparison.

Results are **summary-only** -- scores, metrics and check outcomes, never
transcripts. That keeps them small enough to commit, which is the point: git is
already the regression history, so tracking results over time needs no new
machinery.

Nothing here collapses a result to a single number. A model that halves latency
and doubles the tool-error rate is a regression, and one score would hide that.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from personal_ai_os.evaluation.checks import CheckOutcome

RESULT_VERSION = 1
PREVIEW_CHARS = 200


class RunMetrics(BaseModel):
    """Per-run numbers, collected whether or not the checks passed."""

    iterations: int = 0
    tool_calls: int = 0
    invalid_arguments: int = 0
    permission_denials: int = 0
    wall_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_second: float | None = None


class RunRecord(BaseModel):
    """One repetition of one case."""

    index: int
    passed: bool
    stop_reason: str = ""
    error: str | None = None
    checks: list[CheckOutcome] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    #: A short excerpt for debugging a failure. Never the full transcript.
    output_preview: str = ""

    def failed_checks(self) -> list[CheckOutcome]:
        return [c for c in self.checks if not c.passed]


class CaseResult(BaseModel):
    """All repetitions of one case.

    The headline number is a **pass rate**, not a boolean (ADR-021).
    """

    case: str
    description: str = ""
    agent: str = ""
    runs: list[RunRecord] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def check_rates(self) -> dict[str, float]:
        """Pass rate per individual check, across repetitions.

        More useful than the case rate when diagnosing: it says *which*
        assertion is flaky rather than that something was.
        """
        totals: dict[str, list[bool]] = {}
        for run in self.runs:
            for outcome in run.checks:
                # Keyed on the label so a case using one check twice with
                # different parameters reports two rows, not one.
                totals.setdefault(outcome.key, []).append(outcome.passed)
        return {
            name: sum(results) / len(results) for name, results in sorted(totals.items())
        }

    def metric(self, name: str) -> tuple[float, float, float] | None:
        """(mean, min, max) for one metric across runs -- the spread matters."""
        values = [
            v
            for v in (getattr(r.metrics, name, None) for r in self.runs)
            if isinstance(v, (int, float))
        ]
        if not values:
            return None
        return statistics.fmean(values), min(values), max(values)


class SuiteResult(BaseModel):
    """One suite run against one model."""

    model_config = ConfigDict(protected_namespaces=())

    version: int = RESULT_VERSION
    suite: str
    model: str
    started_at: str
    finished_at: str = ""
    cases: list[CaseResult] = Field(default_factory=list)

    @property
    def total_runs(self) -> int:
        return sum(c.total for c in self.cases)

    @property
    def passed_runs(self) -> int:
        return sum(c.passed for c in self.cases)

    @property
    def pass_rate(self) -> float:
        return self.passed_runs / self.total_runs if self.total_runs else 0.0

    def case(self, name: str) -> CaseResult | None:
        return next((c for c in self.cases if c.case == name), None)

    # --- storage -----------------------------------------------------------

    def filename(self) -> str:
        stamp = self.started_at.replace(":", "").replace("-", "").split(".")[0]
        return f"{self.suite}__{model_slug(self.model)}__{stamp}.json"

    def save(self, results_dir: Path) -> Path:
        results_dir.mkdir(parents=True, exist_ok=True)
        path = results_dir / self.filename()
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> SuiteResult:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))


def model_slug(model: str) -> str:
    """`qwen2.5:7b-instruct` -> `qwen2.5-7b-instruct`, safe for filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")


def utc_stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- rendering -------------------------------------------------------------


def _bar(rate: float, width: int = 10) -> str:
    filled = round(rate * width)
    return "#" * filled + "." * (width - filled)


def render(result: SuiteResult) -> str:
    lines = [
        f"suite  : {result.suite}",
        f"model  : {result.model}",
        f"run at : {result.started_at}",
        "",
        f"  {'CASE':<28} {'PASS':<9} {'RATE':<12} ITER  TOK/S",
    ]
    for case in result.cases:
        mean_iter = case.metric("iterations")
        mean_tps = case.metric("tokens_per_second")
        lines.append(
            f"  {case.case:<28} {case.passed}/{case.total:<7} "
            f"{_bar(case.pass_rate)} {case.pass_rate:>5.0%}  "
            f"{(mean_iter[0] if mean_iter else 0):>4.1f}  "
            f"{(mean_tps[0] if mean_tps else 0):>5.1f}"
        )
        for name, rate in case.check_rates().items():
            if rate < 1.0:
                lines.append(f"      {name:<26} {rate:>5.0%}")

    lines += [
        "",
        f"  overall: {result.passed_runs}/{result.total_runs} runs passed "
        f"({result.pass_rate:.0%})",
    ]
    return "\n".join(lines)


def compare(a: SuiteResult, b: SuiteResult) -> str:
    """Side-by-side, deliberately not collapsed to one number."""
    lines = [
        f"suite: {a.suite}",
        "",
        f"  A = {a.model}   ({a.started_at})",
        f"  B = {b.model}   ({b.started_at})",
        "",
        f"  {'CASE':<28} {'A':>8} {'B':>8}   {'DELTA':>8}",
    ]

    names = [c.case for c in a.cases] + [
        c.case for c in b.cases if not a.case(c.case)
    ]
    for name in names:
        ca, cb = a.case(name), b.case(name)
        ra = ca.pass_rate if ca else float("nan")
        rb = cb.pass_rate if cb else float("nan")
        delta = rb - ra if ca and cb else float("nan")
        marker = ""
        if ca and cb:
            marker = "  REGRESSION" if delta < -0.001 else ("  better" if delta > 0.001 else "")
        lines.append(
            f"  {name:<28} {ra:>7.0%} {rb:>7.0%}   {delta:>+7.0%}{marker}"
        )

    lines.append("")
    lines.append(f"  {'METRIC':<28} {'A':>8} {'B':>8}")
    for metric in ("iterations", "tool_calls", "invalid_arguments", "tokens_per_second"):
        va = _suite_metric(a, metric)
        vb = _suite_metric(b, metric)
        lines.append(f"  {metric:<28} {va:>8.2f} {vb:>8.2f}")

    lines += [
        "",
        f"  overall  A {a.pass_rate:.0%}   B {b.pass_rate:.0%}",
        "",
        "  Read both columns. A faster model that fails more checks is a",
        "  regression, however good its throughput looks.",
    ]
    return "\n".join(lines)


def _suite_metric(result: SuiteResult, name: str) -> float:
    values: list[float] = []
    for case in result.cases:
        got = case.metric(name)
        if got:
            values.append(got[0])
    return statistics.fmean(values) if values else 0.0
