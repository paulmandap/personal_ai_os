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
from personal_ai_os.evaluation.taxonomy import Failure, by_severity

#: Result-file schema version. Bumped to 2 when `runtime_version` was added
#: (ADR-041). The distinction it preserves is real: **v1 means the field did not
#: exist, so the runtime is unknown; v2 with an empty string means the field
#: existed and the runtime declined to say.** Without the bump those two are the
#: same empty value -- the exact ambiguity ADR-041 exists to remove.
RESULT_VERSION = 2
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

    def critical_failures(self) -> list[CheckOutcome]:
        """Failed checks the taxonomy calls defects rather than scores."""
        return [
            c
            for c in self.failed_checks()
            if c.failure is not None and c.failure.severity == "critical"
        ]

    @property
    def defect_free(self) -> bool:
        """Did this run avoid every *critical* failure?

        Weaker than `passed`, deliberately (ADR-037). `passed` is the conjunction
        of every check, so it folds together three different properties: the
        system was compromised, the model was persuaded, and the run stumbled.
        This isolates the first. A suite reporting 92% passed and 100%
        defect-free is saying something precise -- the model was talked into
        proposing writes that never landed -- and one number cannot.
        """
        return not self.critical_failures()


class CaseResult(BaseModel):
    """All repetitions of one case.

    The headline number is a **pass rate**, not a boolean (ADR-021).
    """

    case: str
    description: str = ""
    agent: str = ""
    #: What competence this case measures, e.g. "grounding", "routing".
    category: str = ""
    #: train | validation | holdout -- recorded so a result can never be
    #: mistaken for a holdout measurement when it was not one.
    split: str = "train"
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

    @property
    def defect_free(self) -> int:
        return sum(1 for r in self.runs if r.defect_free)

    @property
    def defect_free_rate(self) -> float:
        return self.defect_free / self.total if self.total else 0.0

    @property
    def denials(self) -> int:
        """Permission refusals across the repetitions -- the gate's activity.

        Collected since Phase 4 and never displayed. ADR-036 made it worth
        showing: it is the difference between a model that was never tempted and
        one that was refused twenty-seven times.
        """
        return sum(r.metrics.permission_denials for r in self.runs)

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
    #: The inference server's version, **observed once at suite initialization**
    #: -- not a per-run guarantee. A server restarted or upgraded mid-suite would
    #: not be reflected here (ADR-041).
    #:
    #: Empty for results written before this existed (`version: 1`), and for
    #: providers that report no version, such as the scripted model used by the
    #: offline tests.
    runtime_version: str = ""
    started_at: str
    finished_at: str = ""
    #: Which split was run. A result that does not say this cannot be trusted
    #: as a holdout measurement later.
    split: str = "train+validation"
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

    @property
    def defect_free_runs(self) -> int:
        return sum(c.defect_free for c in self.cases)

    @property
    def defect_free_rate(self) -> float:
        """Runs with no critical failure. Never lower than `pass_rate`."""
        return self.defect_free_runs / self.total_runs if self.total_runs else 0.0

    @property
    def denials(self) -> int:
        return sum(c.denials for c in self.cases)

    def case(self, name: str) -> CaseResult | None:
        return next((c for c in self.cases if c.case == name), None)

    # --- diagnosis ---------------------------------------------------------

    def failures(self) -> dict[Failure, int]:
        """How many times each *kind* of failure occurred.

        This is the number that says what to fix. A pass rate says how often
        something went wrong; this says what went wrong, and those need
        different responses.
        """
        counts: dict[Failure, int] = {}
        for case in self.cases:
            for run in case.runs:
                for outcome in run.failed_checks():
                    if outcome.failure is not None:
                        counts[outcome.failure] = counts.get(outcome.failure, 0) + 1
        return counts

    def critical_failures(self) -> dict[Failure, int]:
        """Failures that are defects rather than scores. Must be empty."""
        return {f: n for f, n in self.failures().items() if f.severity == "critical"}

    def by_category(self) -> dict[str, tuple[int, int]]:
        """category -> (passed, total), so weakness is locatable."""
        totals: dict[str, tuple[int, int]] = {}
        for case in self.cases:
            key = case.category or "uncategorised"
            passed, total = totals.get(key, (0, 0))
            totals[key] = (passed + case.passed, total + case.total)
        return dict(sorted(totals.items()))

    # --- storage -----------------------------------------------------------

    def filename(self) -> str:
        stamp = self.started_at.replace(":", "").replace("-", "").split(".")[0]
        # The split is in the filename as well as the body: a holdout result
        # must be recognisable without opening it.
        tag = "__holdout" if self.split == "holdout" else ""
        return f"{self.suite}{tag}__{model_slug(self.model)}__{stamp}.json"

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
        f"runtime: {result.runtime_version or 'not recorded'}",
        f"split  : {result.split}",
        f"run at : {result.started_at}",
        "",
        # 52, not 32: the longest case name in the suites is 50 characters, and
        # a name that overflows shunts every column right and makes the table
        # unreadable -- which it had been since the DENY column arrived.
        f"  {'CASE':<52} {'PASS':<9} {'RATE':<12} ITER  TOK/S  DENY",
    ]
    for case in result.cases:
        mean_iter = case.metric("iterations")
        mean_tps = case.metric("tokens_per_second")
        lines.append(
            f"  {case.case:<52} {case.passed}/{case.total:<7} "
            f"{_bar(case.pass_rate)} {case.pass_rate:>5.0%}  "
            f"{(mean_iter[0] if mean_iter else 0):>4.1f}  "
            f"{(mean_tps[0] if mean_tps else 0):>5.1f}  "
            f"{case.denials:>4}"
        )
        for name, rate in case.check_rates().items():
            if rate < 1.0:
                lines.append(f"      {name:<50} {rate:>5.0%}")

    categories = result.by_category()
    if len(categories) > 1:
        lines.append("")
        lines.append("  by category:")
        for name, (passed, total) in categories.items():
            rate = passed / total if total else 0.0
            lines.append(f"    {name:<28} {passed}/{total:<5} {rate:>5.0%}")

    failures = result.failures()
    if failures:
        lines.append("")
        lines.append("  failures by kind (most serious first):")
        for failure, count in by_severity(failures):
            marker = "  <-- defect" if failure.severity == "critical" else ""
            lines.append(
                f"    {failure.value}  {failure.label:<26} {count:>3}"
                f"  [{failure.severity}]{marker}"
            )

    # Two verdicts, not one (ADR-037). `overall` is every check; `defect free`
    # is the critical ones alone. Where they diverge, the run went wrong in a way
    # that left the stored state correct -- on the safety suite that is exactly
    # the difference between a persuaded model and a breached system.
    lines += [
        "",
        f"  overall     : {result.passed_runs}/{result.total_runs} runs passed "
        f"({result.pass_rate:.0%})",
        f"  defect free : {result.defect_free_runs}/{result.total_runs} "
        f"({result.defect_free_rate:.0%})   <- critical checks only",
        f"  denials     : {result.denials} across {result.total_runs} runs",
    ]
    if result.defect_free_runs > result.passed_runs:
        gap = result.defect_free_runs - result.passed_runs
        lines.append(
            f"  read both: {gap} run(s) failed only on non-critical checks -- "
            "no hallucination, no unsupported claim, no safety violation, and "
            "the stored state is as the case expects."
        )
    if result.pass_rate == 1.0 and result.total_runs:
        lines.append(
            "  note: 100% means this suite has stopped measuring anything. "
            "Write harder cases."
        )
    return "\n".join(lines)


def compare(a: SuiteResult, b: SuiteResult) -> str:
    """Side-by-side, deliberately not collapsed to one number."""
    lines = [
        f"suite: {a.suite}",
        "",
        f"  A = {a.model}   ({a.started_at})   runtime "
        f"{a.runtime_version or 'not recorded'}",
        f"  B = {b.model}   ({b.started_at})   runtime "
        f"{b.runtime_version or 'not recorded'}",
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
        f"  overall      A {a.pass_rate:.0%}   B {b.pass_rate:.0%}",
        # A change that adds critical failures while the pass rate holds steady
        # is the regression this row exists to expose (ADR-037).
        f"  defect free  A {a.defect_free_rate:.0%}   B {b.defect_free_rate:.0%}",
        f"  denials      A {a.denials}   B {b.denials}",
        "",
        "  Read both columns. A faster model that fails more checks is a",
        "  regression, however good its throughput looks.",
    ]
    if a.runtime_version != b.runtime_version:
        lines += [
            "",
            f"  NOTE: different inference runtimes ({a.runtime_version or '?'} vs "
            f"{b.runtime_version or '?'}). Any difference below may be the",
            "  runtime rather than the change under test -- check before attributing.",
        ]
    return "\n".join(lines)


def _suite_metric(result: SuiteResult, name: str) -> float:
    values: list[float] = []
    for case in result.cases:
        got = case.metric(name)
        if got:
            values.append(got[0])
    return statistics.fmean(values) if values else 0.0
