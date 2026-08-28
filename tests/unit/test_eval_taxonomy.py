"""Failure taxonomy, split filtering, and category aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_ai_os.evaluation.case import (
    DEFAULT_SPLITS,
    CheckSpec,
    EvalCase,
    EvalSuite,
    load_suites,
)
from personal_ai_os.evaluation.checks import CHECKS, CheckOutcome, failure_for, run_check
from personal_ai_os.evaluation.report import CaseResult, RunMetrics, RunRecord, SuiteResult
from personal_ai_os.evaluation.taxonomy import SEVERITY_ORDER, Failure, by_severity


class TestTaxonomy:
    def test_every_registered_check_declares_a_failure_kind(self):
        """A check with no code produces a number nobody can act on."""
        for name in CHECKS:
            assert failure_for(name) is not None, name

    def test_codes_are_stable_identifiers(self):
        """Results are committed; renumbering would invalidate stored history."""
        assert Failure.HALLUCINATION.value == "F005"
        assert Failure.STATE_MANAGEMENT.value == "F012"

    def test_every_failure_has_a_label_and_severity(self):
        for failure in Failure:
            assert failure.label
            assert failure.severity in SEVERITY_ORDER

    def test_grounding_failures_are_critical(self):
        """These are defects, not scores."""
        assert Failure.HALLUCINATION.severity == "critical"
        assert Failure.UNSUPPORTED_CLAIM.severity == "critical"
        assert Failure.STATE_MANAGEMENT.severity == "critical"

    def test_recoverable_slips_are_minor(self):
        """The loop is built to absorb these (ADR-008)."""
        assert Failure.FORMATTING.severity == "minor"
        assert Failure.WRONG_TOOL_ORDER.severity == "minor"

    def test_ordering_puts_the_worst_first_then_the_most_frequent(self):
        counts = {
            Failure.FORMATTING: 50,
            Failure.HALLUCINATION: 1,
            Failure.WRONG_TOOL: 9,
            Failure.MISSING_TOOL: 3,
        }
        assert [f for f, _ in by_severity(counts)] == [
            Failure.HALLUCINATION,  # critical, even at count 1
            Failure.WRONG_TOOL,     # major, more frequent
            Failure.MISSING_TOOL,   # major, less frequent
            Failure.FORMATTING,     # minor, even at count 50
        ]

    def test_an_outcome_carries_its_failure_kind(self, store):
        from personal_ai_os.evaluation.checks import RunContext
        from tests.unit.test_eval_checks import result

        got = run_check("answered", RunContext(result=result()), {})
        assert got.failure is Failure.INCOMPLETE_ANSWER


class TestSplits:
    def suite_with(self, *splits: str) -> EvalSuite:
        return EvalSuite(
            suite="s",
            agent="task_agent",
            cases=[
                EvalCase(
                    name=f"case_{i}",
                    agent="task_agent",
                    objective="x",
                    split=split,  # type: ignore[arg-type]
                    checks=[CheckSpec.parse("answered")],
                )
                for i, split in enumerate(splits)
            ],
        )

    def test_cases_default_to_train(self):
        case = EvalCase(
            name="c", agent="a", objective="x", checks=[CheckSpec.parse("answered")]
        )
        assert case.split == "train"

    def test_the_default_run_excludes_holdout(self):
        """The whole mechanism: holdout takes a deliberate act to reach."""
        suite = self.suite_with("train", "validation", "holdout")
        assert {c.split for c in suite.select()} == {"train", "validation"}

    def test_holdout_selects_only_holdout(self):
        suite = self.suite_with("train", "validation", "holdout")
        selected = suite.select(("holdout",))
        assert len(selected) == 1
        assert selected[0].split == "holdout"

    def test_default_splits_does_not_contain_holdout(self):
        assert "holdout" not in DEFAULT_SPLITS

    def test_split_counts_are_reported(self):
        suite = self.suite_with("train", "train", "holdout")
        assert suite.split_counts() == {"train": 2, "holdout": 1}

    def test_an_invalid_split_is_rejected(self):
        with pytest.raises(ValueError):
            EvalCase(
                name="c",
                agent="a",
                objective="x",
                split="test",  # type: ignore[arg-type]
                checks=[CheckSpec.parse("answered")],
            )


class TestAggregation:
    def result_with(self, *cases: CaseResult) -> SuiteResult:
        return SuiteResult(
            suite="s", model="m", started_at="2026-08-28T00:00:00Z", cases=list(cases)
        )

    def failing_run(self, failure: Failure) -> RunRecord:
        return RunRecord(
            index=1,
            passed=False,
            checks=[CheckOutcome(name="x", passed=False, failure=failure)],
            metrics=RunMetrics(),
        )

    def test_failures_are_counted_by_kind(self):
        suite = self.result_with(
            CaseResult(
                case="a",
                runs=[
                    self.failing_run(Failure.HALLUCINATION),
                    self.failing_run(Failure.HALLUCINATION),
                    self.failing_run(Failure.WRONG_TOOL),
                ],
            )
        )
        assert suite.failures() == {
            Failure.HALLUCINATION: 2,
            Failure.WRONG_TOOL: 1,
        }

    def test_critical_failures_are_isolated(self):
        """These need to be visible without reading the whole table."""
        suite = self.result_with(
            CaseResult(
                case="a",
                runs=[
                    self.failing_run(Failure.HALLUCINATION),
                    self.failing_run(Failure.FORMATTING),
                ],
            )
        )
        assert suite.critical_failures() == {Failure.HALLUCINATION: 1}

    def test_passing_runs_contribute_no_failures(self):
        suite = self.result_with(
            CaseResult(
                case="a",
                runs=[RunRecord(index=1, passed=True, checks=[], metrics=RunMetrics())],
            )
        )
        assert suite.failures() == {}

    def test_categories_aggregate_across_cases(self):
        passed = RunRecord(index=1, passed=True, metrics=RunMetrics())
        failed = RunRecord(index=1, passed=False, metrics=RunMetrics())
        suite = self.result_with(
            CaseResult(case="a", category="routing", runs=[passed, passed]),
            CaseResult(case="b", category="routing", runs=[failed]),
            CaseResult(case="c", category="grounding", runs=[passed]),
        )
        assert suite.by_category() == {"routing": (2, 3), "grounding": (1, 1)}

    def test_uncategorised_cases_are_grouped(self):
        suite = self.result_with(
            CaseResult(case="a", runs=[RunRecord(index=1, passed=True)])
        )
        assert "uncategorised" in suite.by_category()

    def test_holdout_results_are_recognisable_from_the_filename(self):
        """A holdout measurement must not be mistaken for an ordinary run."""
        suite = SuiteResult(
            suite="s", model="m", started_at="2026-08-28T00:00:00Z", split="holdout"
        )
        assert "__holdout__" in suite.filename()


class TestShippedSuitesHaveSplitsAndCategories:
    @pytest.fixture
    def suites(self) -> list[EvalSuite]:
        return load_suites(Path(__file__).resolve().parents[2] / "evaluations" / "cases")

    def test_the_harder_suites_reserve_holdout_cases(self):
        cases_dir = Path(__file__).resolve().parents[2] / "evaluations" / "cases"
        for name in ("robustness", "planning", "delegation"):
            suite = next(s for s in load_suites(cases_dir) if s.suite == name)
            assert suite.split_counts().get("holdout", 0) >= 1, name

    def test_every_new_case_declares_a_category(self, suites):
        for suite in suites:
            if suite.suite not in {"robustness", "planning", "delegation"}:
                continue
            for case in suite.cases:
                assert case.category, f"{suite.suite}/{case.name}"
