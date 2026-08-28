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
