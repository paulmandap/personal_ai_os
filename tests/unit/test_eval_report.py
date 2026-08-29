"""Aggregation, storage and comparison of results."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_ai_os.evaluation.checks import CheckOutcome
from personal_ai_os.evaluation.report import (
    CaseResult,
    RunMetrics,
    RunRecord,
    SuiteResult,
    compare,
    model_slug,
    render,
)
from personal_ai_os.evaluation.taxonomy import Failure


def run(index: int, passed: bool, **checks: bool) -> RunRecord:
    return RunRecord(
        index=index,
        passed=passed,
        stop_reason="answered",
        checks=[CheckOutcome(name=n, passed=p) for n, p in checks.items()],
        metrics=RunMetrics(iterations=2, tool_calls=1, tokens_per_second=30.0),
    )


def case(name: str, *runs: RunRecord) -> CaseResult:
    return CaseResult(case=name, agent="task_agent", runs=list(runs))


def suite(model: str, *cases: CaseResult) -> SuiteResult:
    return SuiteResult(
        suite="demo", model=model, started_at="2026-08-27T13:00:00Z", cases=list(cases)
    )


class TestPassRate:
    def test_a_case_result_is_a_rate_not_a_boolean(self):
        """The central design decision (ADR-021)."""
        c = case("x", run(1, True), run(2, False), run(3, True))
        assert c.passed == 2
        assert c.total == 3
        assert c.pass_rate == pytest.approx(2 / 3)

    def test_all_failing_is_zero_not_an_error(self):
        c = case("x", run(1, False), run(2, False))
        assert c.pass_rate == 0.0

    def test_empty_case_does_not_divide_by_zero(self):
        assert case("x").pass_rate == 0.0

    def test_suite_rate_counts_runs_not_cases(self):
        """A 4-run case failing should weigh more than a 1-run case failing."""
        s = suite(
            "m",
            case("a", run(1, True), run(2, True), run(3, True), run(4, True)),
            case("b", run(1, False)),
        )
        assert s.total_runs == 5
        assert s.passed_runs == 4
        assert s.pass_rate == pytest.approx(0.8)


class TestCheckRates:
    def test_per_check_rate_identifies_the_flaky_assertion(self):
        c = case(
            "x",
            run(1, False, answered=True, task_count=False),
            run(2, True, answered=True, task_count=True),
        )
        rates = c.check_rates()
        assert rates["answered"] == 1.0
        assert rates["task_count"] == 0.5


class TestMetrics:
    def test_metric_returns_mean_min_max(self):
        c = CaseResult(
            case="x",
            runs=[
                RunRecord(index=1, passed=True, metrics=RunMetrics(iterations=2)),
                RunRecord(index=2, passed=True, metrics=RunMetrics(iterations=6)),
            ],
        )
        mean, low, high = c.metric("iterations")
        assert (mean, low, high) == (4.0, 2, 6)

    def test_missing_metric_returns_none(self):
        c = CaseResult(case="x", runs=[RunRecord(index=1, passed=True)])
        assert c.metric("tokens_per_second") is None


class TestStorage:
    def test_round_trip(self, tmp_path: Path):
        original = suite("qwen2.5:7b-instruct", case("a", run(1, True)))
        path = original.save(tmp_path)
        loaded = SuiteResult.load(path)
        assert loaded.suite == original.suite
        assert loaded.model == original.model
        assert loaded.cases[0].pass_rate == original.cases[0].pass_rate

    def test_filename_is_filesystem_safe(self):
        name = suite("qwen2.5:7b-instruct", case("a", run(1, True))).filename()
        assert ":" not in name
        assert name.endswith(".json")

    def test_model_slug_replaces_separators(self):
        assert model_slug("qwen2.5:7b-instruct") == "qwen2.5-7b-instruct"

    def test_results_carry_no_transcript(self, tmp_path: Path):
        """Results are committed, so they must stay small and non-sensitive."""
        s = suite("m", case("a", run(1, True)))
        text = s.save(tmp_path).read_text(encoding="utf-8")
        assert "transcript" not in text


class TestTwoVerdicts:
    """ADR-037: `passed` is every check; `defect_free` is the critical ones."""

    @staticmethod
    def _run(index: int, **checks: Failure | None) -> RunRecord:
        """A run whose named checks failed, each with the given failure kind."""
        return RunRecord(
            index=index,
            passed=not checks,
            stop_reason="answered",
            checks=[
                CheckOutcome(name=n, passed=False, failure=f) for n, f in checks.items()
            ],
            metrics=RunMetrics(permission_denials=2),
        )

    def test_a_critical_failure_moves_both_numbers(self):
        c = case("x", self._run(1, tool_did_not_run=Failure.SAFETY_VIOLATION))
        assert c.pass_rate == 0.0
        assert c.defect_free_rate == 0.0

    def test_a_non_critical_failure_moves_only_the_pass_rate(self):
        """The safety suite's actual shape: persuaded model, intact state."""
        c = case("x", self._run(1, did_not_call_tool=Failure.WRONG_TOOL))
        assert c.pass_rate == 0.0
        assert c.defect_free_rate == 1.0

    def test_defect_free_is_never_below_the_pass_rate(self):
        s = suite(
            "m",
            case(
                "a",
                run(1, True),
                self._run(2, did_not_call_tool=Failure.WRONG_TOOL),
                self._run(3, task_count=Failure.STATE_MANAGEMENT),
            ),
        )
        assert s.pass_rate == pytest.approx(1 / 3)
        assert s.defect_free_rate == pytest.approx(2 / 3)
        assert s.defect_free_rate >= s.pass_rate

    def test_an_unclassified_check_is_not_treated_as_a_defect(self):
        """`failure` is None only for hand-built outcomes; do not guess."""
        c = case("x", self._run(1, mystery=None))
        assert c.defect_free_rate == 1.0

    def test_denials_are_summed_for_display(self):
        c = case(
            "x", self._run(1, a=Failure.WRONG_TOOL), self._run(2, b=Failure.WRONG_TOOL)
        )
        assert c.denials == 4

    def test_render_reports_both_and_says_they_differ(self):
        s = suite("m", case("a", run(1, True), self._run(2, dnc=Failure.WRONG_TOOL)))
        text = render(s)
        assert "overall" in text and "defect free" in text
        assert "read both" in text

    def test_compare_exposes_a_critical_regression_behind_a_flat_rate(self):
        """Same pass rate, different meaning -- the row exists for this."""
        a = suite("7b", case("a", self._run(1, dnc=Failure.WRONG_TOOL)))
        b = suite("3b", case("a", self._run(1, tdr=Failure.SAFETY_VIOLATION)))
        text = compare(a, b)
        assert a.pass_rate == b.pass_rate
        assert "defect free  A 100%   B 0%" in text


class TestRendering:
    def test_render_shows_rate_and_names_failing_checks(self):
        s = suite(
            "m",
            case("a", run(1, False, answered=True, task_count=False), run(2, True, answered=True, task_count=True)),
        )
        text = render(s)
        assert "1/2" in text
        assert "task_count" in text

    def test_compare_marks_a_regression(self):
        good = suite("7b", case("a", run(1, True), run(2, True)))
        bad = suite("3b", case("a", run(1, True), run(2, False)))
        text = compare(good, bad)
        assert "REGRESSION" in text

    def test_compare_marks_an_improvement(self):
        bad = suite("3b", case("a", run(1, False), run(2, False)))
        good = suite("7b", case("a", run(1, True), run(2, True)))
        assert "better" in compare(bad, good)

    def test_compare_shows_both_columns_not_one_score(self):
        """A faster model that fails more is a regression; one number hides it."""
        a = suite("3b", case("a", run(1, True)))
        b = suite("7b", case("a", run(1, True)))
        text = compare(a, b)
        assert "3b" in text and "7b" in text
        assert "tokens_per_second" in text

    def test_compare_tolerates_a_case_missing_from_one_side(self):
        a = suite("x", case("only_in_a", run(1, True)))
        b = suite("y", case("only_in_b", run(1, True)))
        text = compare(a, b)
        assert "only_in_a" in text and "only_in_b" in text


class TestRuntimeVersionProvenance:
    """ADR-041: which inference runtime produced this result?"""

    def test_it_round_trips(self, tmp_path: Path):
        s = suite("m", case("a", run(1, True)))
        s.runtime_version = "0.33.2"
        loaded = SuiteResult.load(s.save(tmp_path))
        assert loaded.runtime_version == "0.33.2"
        assert loaded.version == 2

    def test_a_v1_result_with_no_such_field_still_loads(self):
        """Backward compatibility, asserted against a real committed result.

        Every result written before ADR-041 lacks the field. They must keep
        loading, and must be distinguishable from a v2 result whose server would
        not name itself -- v1 means "unknown", v2 with "" means "asked, no
        answer". That distinction is the whole reason RESULT_VERSION moved.
        """
        results = sorted(Path("evaluations/results").glob("*.json"))
        older = [p for p in results if '"version": 1' in p.read_text(encoding="utf-8")]
        assert older, "expected committed v1 results to exist"
        loaded = SuiteResult.load(older[0])
        assert loaded.version == 1
        assert loaded.runtime_version == ""
        assert loaded.total_runs > 0

    def test_render_shows_it_and_says_so_when_absent(self):
        s = suite("m", case("a", run(1, True)))
        assert "not recorded" in render(s)
        s.runtime_version = "0.33.2"
        assert "0.33.2" in render(s)

    def test_compare_warns_when_the_runtimes_differ(self):
        """The case that nearly caused a false attribution during ADR-010's
        re-confirmation: two results produced by different servers."""
        a = suite("7b", case("a", run(1, True)))
        b = suite("7b", case("a", run(1, False)))
        a.runtime_version, b.runtime_version = "0.33.1", "0.33.2"
        text = compare(a, b)
        assert "different inference runtimes" in text
        assert "0.33.1" in text and "0.33.2" in text

    def test_compare_stays_quiet_when_they_match(self):
        a = suite("7b", case("a", run(1, True)))
        b = suite("3b", case("a", run(1, True)))
        a.runtime_version = b.runtime_version = "0.33.2"
        assert "different inference runtimes" not in compare(a, b)
