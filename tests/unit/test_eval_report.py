"""Aggregation, storage and comparison of results."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_ai_os.evaluation.checks import CheckOutcome
from personal_ai_os.evaluation.report import (
    CaseResult,
    HistoryPoint,
    RunMetrics,
    RunRecord,
    SuiteResult,
    compare,
    load_history,
    model_slug,
    render,
    render_history,
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


class TestProbeIsNotABenchmark:
    """ADR-048: a diagnostic's numbers must never read as a product score.

    A probe constructs adversarial conditions deliberately. Folding it into
    "how well does the system work" is a category error, not a rounding error,
    and the realistic accident is a glob over `evaluations/results/` being
    summed rather than a careless reader.
    """

    def _probe(self, model: str = "m") -> SuiteResult:
        s = suite(model, case("a", run(1, True)))
        s.suite = "overcompletion"
        s.kind = "probe"
        return s

    def test_the_filename_marks_it(self, tmp_path: Path):
        path = self._probe().save(tmp_path)
        assert path.name.startswith("probe__")

    def test_a_benchmark_filename_is_unchanged(self, tmp_path: Path):
        """The marker must not perturb every existing result's name."""
        path = suite("m", case("a", run(1, True))).save(tmp_path)
        assert not path.name.startswith("probe__")
        assert path.name.startswith("demo__")

    def test_kind_survives_a_round_trip(self, tmp_path: Path):
        assert SuiteResult.load(self._probe().save(tmp_path)).kind == "probe"

    def test_a_probe_keeps_its_own_history(self, tmp_path: Path):
        """The marker keeps probes out of aggregates, not out of their own
        series -- `paios eval history overcompletion` must still work."""
        self._probe().save(tmp_path)
        history = load_history(tmp_path, "overcompletion", "m")
        assert len(history.overall) == 1

    def test_a_probe_never_appears_in_another_suites_history(self, tmp_path: Path):
        """The containment property, asserted rather than left incidental."""
        self._probe().save(tmp_path)
        suite("m", case("a", run(1, True))).save(tmp_path)   # suite name "demo"
        assert len(load_history(tmp_path, "demo", "m").overall) == 1
        assert len(load_history(tmp_path, "overcompletion", "m").overall) == 1

    def test_a_directory_glob_can_exclude_probes_by_name_alone(self, tmp_path: Path):
        """What a future aggregate would actually do: sum the directory.

        Without the filename marker it would have to open every file to know
        what it was summing. This is the defence that does not depend on anyone
        remembering to check a field.
        """
        self._probe().save(tmp_path)
        suite("m", case("a", run(1, True))).save(tmp_path)
        everything = sorted(p.name for p in tmp_path.glob("*.json"))
        benchmarks = [n for n in everything if not n.startswith("probe__")]
        assert len(everything) == 2 and len(benchmarks) == 1

    def test_render_says_so_loudly(self, tmp_path: Path):
        out = render(self._probe())
        assert "PROBE" in out
        assert "not a benchmark" in out.lower()

    def test_a_benchmark_render_is_not_labelled(self):
        assert "PROBE" not in render(suite("m", case("a", run(1, True))))


class TestOlderResultsAreReadNeverRewritten:
    """ADR-048's compatibility clause, which is about *reading*.

    A version bump is precisely when someone is tempted to tidy the old files.
    Nothing rewrites, re-stamps or backfills them: a v3 result keeps
    `version: 3` forever and simply carries no `kind`.
    """

    def test_a_pre_v4_result_loads_and_defaults_to_benchmark(self):
        results = sorted(Path("evaluations/results").glob("*.json"))
        older = [
            p for p in results
            if '"version": 3' in p.read_text(encoding="utf-8")
        ]
        assert older, "expected committed v3 results to exist"
        loaded = SuiteResult.load(older[0])
        assert loaded.version == 3          # not silently upgraded
        assert loaded.kind == "benchmark"   # absent field reads as what it was

    def test_loading_does_not_touch_the_file(self):
        results = sorted(Path("evaluations/results").glob("*.json"))
        target = next(
            p for p in results if '"version": 3' in p.read_text(encoding="utf-8")
        )
        before = target.read_bytes()
        SuiteResult.load(target)
        assert target.read_bytes() == before


class TestRuntimeVersionProvenance:
    """ADR-041: which inference runtime produced this result?"""

    def test_it_round_trips(self, tmp_path: Path):
        s = suite("m", case("a", run(1, True)))
        s.runtime_version = "0.33.2"
        loaded = SuiteResult.load(s.save(tmp_path))
        assert loaded.runtime_version == "0.33.2"
        # A literal on purpose: this is the tripwire that makes a RESULT_VERSION
        # bump a conscious act. 1 -> 2 runtime_version (ADR-041), 2 -> 3
        # code_version (ADR-044), 3 -> 4 kind (ADR-048).
        assert loaded.version == 4

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


class TestHistory:
    """ADR-043: a drop is only a regression relative to a distribution.

    Twice in one day a single stored number was read as a property and a
    regression reported that did not exist -- `robustness` 17/35 against a
    baseline of 28/35, in a series reading 19, 20, 21, 28.
    """

    def _save(self, tmp_path: Path, model: str, stamp: str, passed: int,
              total: int, *, case: str = "a", runtime: str = "", split: str = "train"):
        runs = [run(i + 1, i < passed) for i in range(total)]
        s = SuiteResult(
            suite="demo", model=model, started_at=stamp, runtime_version=runtime,
            split=split, cases=[CaseResult(case=case, runs=runs)],
        )
        return s.save(tmp_path)

    def test_it_collects_a_series_oldest_first(self, tmp_path: Path):
        for stamp, passed in (("2026-08-03T00:00:00Z", 1),
                              ("2026-08-01T00:00:00Z", 5),
                              ("2026-08-02T00:00:00Z", 3)):
            self._save(tmp_path, "m", stamp, passed, 5)
        h = load_history(tmp_path, "demo", "m")
        assert [p.passed for p in h.overall] == [5, 3, 1]
        assert [p.passed for p in h.cases["a"]] == [5, 3, 1]

    def test_it_shortlists_by_suite_and_model(self, tmp_path: Path):
        self._save(tmp_path, "7b", "2026-08-01T00:00:00Z", 5, 5)
        self._save(tmp_path, "3b", "2026-08-02T00:00:00Z", 1, 5)
        assert len(load_history(tmp_path, "demo", "7b").overall) == 1
        assert len(load_history(tmp_path, "demo", "3b").overall) == 1
        assert not load_history(tmp_path, "other", "7b")

    def test_holdout_is_excluded_unless_asked(self, tmp_path: Path):
        """Browsing holdout results casually is how one gets spent (ADR-027)."""
        self._save(tmp_path, "m", "2026-08-01T00:00:00Z", 5, 5, split="holdout")
        assert not load_history(tmp_path, "demo", "m")
        assert load_history(tmp_path, "demo", "m", include_holdout=True).overall

    def test_a_malformed_result_is_skipped_not_fatal(self, tmp_path: Path):
        """A broken history view must not stop someone reading a live result."""
        self._save(tmp_path, "m", "2026-08-01T00:00:00Z", 4, 5)
        (tmp_path / "demo__m__20260802T000000Z.json").write_text("{ not json",
                                                                 encoding="utf-8")
        (tmp_path / "demo__m__20260803T000000Z.json").write_text('{"nope": 1}',
                                                                 encoding="utf-8")
        h = load_history(tmp_path, "demo", "m")
        assert [p.passed for p in h.overall] == [4]

    def test_a_missing_results_dir_is_empty_not_an_error(self, tmp_path: Path):
        assert not load_history(tmp_path / "nope", "demo", "m")

    def test_differing_repeat_counts_keep_counts_and_gain_rates(self, tmp_path: Path):
        """`5/5` and `9/15` are different amounts of evidence.

        Normalising to rates alone would hide that; showing counts alone is what
        made `5/5 -> 0/5` look catastrophic beside a 60% norm.
        """
        self._save(tmp_path, "m", "2026-08-01T00:00:00Z", 2, 5)
        self._save(tmp_path, "m", "2026-08-02T00:00:00Z", 9, 15)
        text = render_history(load_history(tmp_path, "demo", "m"))
        assert "2/5 (40%)" in text and "9/15 (60%)" in text

    def test_a_current_run_is_placed_in_the_series(self, tmp_path: Path):
        for stamp, passed in (("2026-08-01T00:00:00Z", 2),
                              ("2026-08-02T00:00:00Z", 3)):
            self._save(tmp_path, "m", stamp, passed, 5)
        h = load_history(tmp_path, "demo", "m")
        low = suite("m", case("a", *[run(i + 1, False) for i in range(5)]))
        assert "BELOW the historical low" in render(low, h)
        high = suite("m", case("a", *[run(i + 1, True) for i in range(5)]))
        assert "ABOVE the historical high" in render(high, h)
        mid = suite("m", case("a", run(1, True), run(2, True), run(3, False),
                              run(4, False), run(5, False)))
        assert "within range" in render(mid, h)

    def test_render_without_history_is_unchanged(self, tmp_path: Path):
        """Byte-identical for callers with no results directory."""
        s = suite("m", case("a", run(1, True), run(2, False)))
        assert render(s) == render(s, None)
        assert "history" not in render(s)

    def test_the_series_shows_every_value_not_a_summary(self, tmp_path: Path):
        """A mean or min/max would have hidden that 28 was a lone peak."""
        for stamp, passed in (("2026-08-01T00:00:00Z", 19), ("2026-08-02T00:00:00Z", 20),
                              ("2026-08-03T00:00:00Z", 21), ("2026-08-04T00:00:00Z", 28)):
            self._save(tmp_path, "m", stamp, passed, 35)
        text = render_history(load_history(tmp_path, "demo", "m"))
        for value in ("19/35", "20/35", "21/35", "28/35"):
            assert value in text

    def test_it_reports_when_runtimes_differ(self, tmp_path: Path):
        self._save(tmp_path, "m", "2026-08-01T00:00:00Z", 5, 5, runtime="0.33.1")
        self._save(tmp_path, "m", "2026-08-02T00:00:00Z", 5, 5, runtime="0.33.2")
        assert "0.33.1, 0.33.2" in render_history(load_history(tmp_path, "demo", "m"))


class TestCodeProvenance:
    """ADR-044: which application code produced this result?

    ADR-043's history view admits it shows a distribution, not a same-code
    baseline. This is the field that lets it say which points are comparable.
    """

    def _point(self, code: str, passed: int = 3) -> "HistoryPoint":
        return HistoryPoint(started_at="2026-08-30T00:00:00Z", passed=passed,
                            total=5, code_version=code)

    def test_only_an_exact_clean_match_is_comparable(self):
        assert self._point("abc1234").same_code_as("abc1234")

    def test_dirty_is_never_comparable_on_either_side(self):
        """A dirty tree means the commit does not identify the code."""
        assert not self._point("abc1234-dirty").same_code_as("abc1234")
        assert not self._point("abc1234").same_code_as("abc1234-dirty")
        assert not self._point("abc1234-dirty").same_code_as("abc1234-dirty")

    def test_unknown_provenance_is_never_comparable(self):
        """`""` claims nothing; treating it as a match invents a baseline."""
        assert not self._point("").same_code_as("abc1234")
        assert not self._point("abc1234").same_code_as("")
        assert not self._point("").same_code_as("")

    def test_a_different_commit_is_not_comparable(self):
        assert not self._point("abc1234").same_code_as("def5678")

    def test_it_round_trips_and_v2_files_still_load(self, tmp_path: Path):
        s = suite("m", case("a", run(1, True)))
        s.code_version = "abc1234"
        assert SuiteResult.load(s.save(tmp_path)).code_version == "abc1234"

        # A real committed v2 result, which predates the field entirely.
        older = [p for p in sorted(Path("evaluations/results").glob("*.json"))
                 if '"version": 2' in p.read_text(encoding="utf-8")]
        assert older, "expected committed v2 results to exist"
        loaded = SuiteResult.load(older[0])
        assert loaded.version == 2
        assert loaded.code_version == ""   # never inferred
        assert loaded.total_runs > 0

    def test_history_reports_a_series_spanning_versions(self, tmp_path: Path):
        for stamp, code in (("2026-08-01T00:00:00Z", "aaa1111"),
                            ("2026-08-02T00:00:00Z", "bbb2222-dirty")):
            s = SuiteResult(suite="demo", model="m", started_at=stamp,
                            code_version=code, cases=[CaseResult(case="a",
                                                                 runs=[run(1, True)])])
            s.save(tmp_path)
        text = render_history(load_history(tmp_path, "demo", "m"))
        assert "aaa1111" in text and "bbb2222-dirty" in text
