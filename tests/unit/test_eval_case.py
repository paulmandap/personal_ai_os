"""Evaluation case loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from personal_ai_os.evaluation.case import (
    CheckSpec,
    EvalCaseError,
    EvalSuite,
    load_suite,
    load_suites,
)

MINIMAL = {
    "suite": "demo",
    "agent": "task_agent",
    "cases": [
        {"name": "one", "objective": "do a thing", "checks": ["answered"]},
    ],
}


def write_suite(tmp_path: Path, data: dict, name: str = "s.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestCheckSpec:
    def test_bare_name(self):
        spec = CheckSpec.parse("answered")
        assert spec.name == "answered" and spec.params == {}

    def test_scalar_parameter_becomes_value(self):
        spec = CheckSpec.parse({"first_tool_is": "add_task"})
        assert spec.name == "first_tool_is"
        assert spec.params == {"value": "add_task"}

    def test_mapping_parameter_is_kept_whole(self):
        spec = CheckSpec.parse({"task_field_is": {"field": "priority", "value": "high"}})
        assert spec.params == {"field": "priority", "value": "high"}

    def test_unknown_check_is_rejected(self):
        """A typo must fail at load, not silently drop an assertion."""
        with pytest.raises(ValueError, match="unknown check"):
            CheckSpec.parse("no_such_check_exists")

    def test_multi_key_mapping_is_rejected(self):
        with pytest.raises(EvalCaseError, match="single-key"):
            CheckSpec.parse({"answered": 1, "called_tool": "x"})

    def test_describe_is_readable(self):
        assert CheckSpec.parse("answered").describe() == "answered"
        assert CheckSpec.parse({"called_tool": "x"}).describe() == "called_tool=x"


class TestSuiteLoading:
    def test_loads_a_minimal_suite(self, tmp_path: Path):
        suite = load_suite(write_suite(tmp_path, MINIMAL))
        assert suite.suite == "demo"
        assert suite.cases[0].agent == "task_agent"

    def test_suite_agent_cascades_to_cases(self, tmp_path: Path):
        suite = load_suite(write_suite(tmp_path, MINIMAL))
        assert all(c.agent == "task_agent" for c in suite.cases)

    def test_case_can_override_the_suite_agent(self, tmp_path: Path):
        data = {**MINIMAL}
        data["cases"] = [
            {"name": "one", "objective": "x", "checks": ["answered"], "agent": "ping"}
        ]
        assert load_suite(write_suite(tmp_path, data)).cases[0].agent == "ping"

    def test_suite_repeat_cascades(self, tmp_path: Path):
        data = {**MINIMAL, "repeat": 7}
        assert load_suite(write_suite(tmp_path, data)).cases[0].repeat == 7

    def test_case_repeat_wins_over_suite_repeat(self, tmp_path: Path):
        data = {**MINIMAL, "repeat": 7}
        data["cases"] = [
            {"name": "one", "objective": "x", "checks": ["answered"], "repeat": 2}
        ]
        assert load_suite(write_suite(tmp_path, data)).cases[0].repeat == 2

    def test_case_without_checks_is_rejected(self, tmp_path: Path):
        """A case asserting nothing always passes and measures nothing."""
        data = {**MINIMAL, "cases": [{"name": "empty", "objective": "x", "checks": []}]}
        with pytest.raises(EvalCaseError, match="no checks"):
            load_suite(write_suite(tmp_path, data))

    def test_case_without_an_agent_anywhere_is_rejected(self, tmp_path: Path):
        data = {"suite": "d", "cases": [{"name": "x", "objective": "y", "checks": ["answered"]}]}
        with pytest.raises(EvalCaseError, match="no agent"):
            load_suite(write_suite(tmp_path, data))

    def test_duplicate_case_names_are_rejected(self, tmp_path: Path):
        data = {**MINIMAL}
        data["cases"] = [
            {"name": "dup", "objective": "a", "checks": ["answered"]},
            {"name": "dup", "objective": "b", "checks": ["answered"]},
        ]
        with pytest.raises(EvalCaseError, match="duplicate"):
            load_suite(write_suite(tmp_path, data))

    def test_unknown_key_is_rejected(self, tmp_path: Path):
        data = {**MINIMAL, "reppeat": 3}
        with pytest.raises(EvalCaseError):
            load_suite(write_suite(tmp_path, data))

    def test_malformed_yaml_names_the_file(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text("suite: [oops\n", encoding="utf-8")
        with pytest.raises(EvalCaseError, match="bad.yaml"):
            load_suite(tmp_path / "bad.yaml")

    def test_total_runs_sums_repeats(self, tmp_path: Path):
        data = {**MINIMAL, "repeat": 3}
        data["cases"] = [
            {"name": "a", "objective": "x", "checks": ["answered"]},
            {"name": "b", "objective": "y", "checks": ["answered"], "repeat": 5},
        ]
        assert load_suite(write_suite(tmp_path, data)).total_runs() == 8

    def test_missing_directory_yields_nothing(self, tmp_path: Path):
        assert load_suites(tmp_path / "nope") == []


class TestSetup:
    def test_seed_tasks_parse(self, tmp_path: Path):
        data = {**MINIMAL}
        data["cases"] = [
            {
                "name": "seeded",
                "objective": "x",
                "checks": ["answered"],
                "setup": {"tasks": [{"title": "existing", "priority": "high"}]},
            }
        ]
        case = load_suite(write_suite(tmp_path, data)).cases[0]
        assert case.setup.tasks[0].title == "existing"
        assert case.setup.tasks[0].priority.value == "high"


class TestShippedSuites:
    """Guards the suites that ship with the repo."""

    @pytest.fixture
    def suites(self) -> list[EvalSuite]:
        cases_dir = Path(__file__).resolve().parents[2] / "evaluations" / "cases"
        return load_suites(cases_dir)

    def test_every_shipped_suite_loads(self, suites):
        """Presence, not an exact set -- adding a suite should not fail a test
        whose job is to prove the shipped suites parse."""
        names = {s.suite for s in suites}
        assert {"embellishment", "hallucination", "tool_calling", "finance"} <= names
        assert all(s.cases for s in suites)

    def test_embellishment_tests_both_directions(self, suites):
        """It must also check that stated values ARE recorded, not only that
        invented ones are absent -- otherwise the fix could overcorrect."""
        suite = next(s for s in suites if s.suite == "embellishment")
        names = {c.name for c in suite.cases}
        assert "no_invented_due_date" in names
        assert "honours_a_stated_due_date" in names

    def test_every_case_names_a_real_agent(self, suites, tools):
        from personal_ai_os.agents.registry import AgentRegistry

        agents_dir = Path(__file__).resolve().parents[2] / "agents"
        registry = AgentRegistry.from_dir(agents_dir, tools=tools)
        for suite in suites:
            for case in suite.cases:
                assert registry.has(case.agent), f"{case.name} -> {case.agent}"
