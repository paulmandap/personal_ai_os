"""The runner, driven end to end by a ScriptedModel.

No Ollama, no network — the harness has to be provably correct offline, or its
numbers cannot be trusted. It does read the real `config/default.yaml` and the
real `agents/*.yaml`, deliberately: what gets measured should be the manifests
that actually ship.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from personal_ai_os.config.schema import Settings
from personal_ai_os.evaluation.case import EvalCase, EvalSuite
from personal_ai_os.evaluation.runner import EvalRunner
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import TaskStore
from personal_ai_os.models.fake import ScriptedModel, text_response, tool_call_response
from personal_ai_os.models.registry import ModelRegistry
from personal_ai_os.permissions.broker import RecordingBroker
from personal_ai_os.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[2]


def scripted_builder(make_responses: Callable[[], list]):
    """A runtime whose every tier resolves to a fresh ScriptedModel."""

    def build(settings: Settings, store: Store, broker: RecordingBroker) -> Runtime:
        registry = ModelRegistry(
            settings.models,
            factory=lambda name, tier, s: ScriptedModel(make_responses(), name=name),
        )
        return Runtime.build(
            settings=settings,
            store=store,
            broker=broker,
            models=registry,
            configure_logging=False,
        )

    return build


def make_case(name: str = "c", *, checks: list, objective: str = "Add a task to buy oat milk.", **kw) -> EvalCase:
    return EvalCase(
        name=name, agent="task_agent", objective=objective, checks=checks, **kw
    )


def add_task_then_answer(**args):
    def make():
        return [
            tool_call_response("add_task", {"title": "Buy oat milk", **args}),
            text_response("Added it."),
        ]

    return make


class TestScoring:
    def test_a_clean_run_passes_every_check(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        case = make_case(
            checks=[
                "answered",
                {"first_tool_is": "add_task"},
                "no_invalid_arguments",
                {"task_count": 1},
                {"task_field_absent": "due_date"},
            ]
        )
        result = runner.run_case(case)
        assert result.pass_rate == 1.0
        assert all(o.passed for r in result.runs for o in r.checks)

    def test_an_invented_due_date_is_caught(self):
        """The exact failure this suite exists to detect."""
        runner = EvalRunner(
            repo_root=REPO_ROOT,
            runtime_builder=scripted_builder(add_task_then_answer(due_date="2026-09-07")),
        )
        case = make_case(checks=["answered", {"task_field_absent": "due_date"}])
        result = runner.run_case(case)

        assert result.pass_rate == 0.0
        rates = result.check_rates()
        assert rates["answered"] == 1.0                    # the run itself was fine
        assert rates["task_field_absent=due_date"] == 0.0  # the data was not

    def test_repeated_checks_are_reported_separately(self):
        """A case may use one check twice with different parameters. Keying on
        the bare name would collapse them and hide which one failed."""
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        case = make_case(
            checks=[
                {"task_title_contains": "oat milk"},   # true
                {"task_title_contains": "passport"},   # false
            ]
        )
        rates = runner.run_case(case).check_rates()
        assert rates["task_title_contains=oat milk"] == 1.0
        assert rates["task_title_contains=passport"] == 0.0

    def test_partial_failure_is_reported_per_check(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        case = make_case(checks=["answered", {"first_tool_is": "list_tasks"}])
        result = runner.run_case(case)
        assert result.pass_rate == 0.0
        assert result.check_rates()["answered"] == 1.0


class TestRepetition:
    def test_repeat_produces_that_many_runs(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        result = runner.run_case(make_case(checks=["answered"], repeat=4))
        assert result.total == 4

    def test_runner_repeat_overrides_the_case(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT,
            repeat=2,
            runtime_builder=scripted_builder(add_task_then_answer()),
        )
        assert runner.run_case(make_case(checks=["answered"], repeat=9)).total == 2


class TestIsolation:
    def test_each_repetition_starts_from_an_empty_database(self):
        """Otherwise task_count would climb and later runs would fail."""
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        result = runner.run_case(make_case(checks=[{"task_count": 1}], repeat=3))
        assert result.pass_rate == 1.0

    def test_seed_tasks_exist_before_the_run(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        case = make_case(
            checks=[{"task_count": 3}],
            setup={"tasks": [{"title": "one"}, {"title": "two"}]},
        )
        assert runner.run_case(case).pass_rate == 1.0

    def test_the_real_database_is_never_touched(self):
        """The whole point of the fixture workspace."""
        real_db = REPO_ROOT / "data" / "paios.db"
        before = real_db.stat().st_mtime if real_db.exists() else None

        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        runner.run_case(make_case(checks=["answered"], repeat=2))

        after = real_db.stat().st_mtime if real_db.exists() else None
        assert before == after

    def test_fixture_paths_point_away_from_the_repo(self, tmp_path: Path):
        runner = EvalRunner(repo_root=REPO_ROOT)
        settings = runner._settings_for(tmp_path)
        assert settings.resolved_path(settings.paths.db_path).is_relative_to(tmp_path)
        assert settings.resolved_allowed_roots() == [tmp_path.resolve()]
        # ...but the agents directory is the real one.
        assert settings.resolved_path(settings.paths.agents_dir) == REPO_ROOT / "agents"


class TestModelPinning:
    def test_pinning_overrides_every_mapped_tier(self):
        runner = EvalRunner(repo_root=REPO_ROOT, model="pinned-model")
        settings = runner._settings_for(Path("."))
        mapped = [t.model for t in settings.models.tiers.values() if t.model]
        assert mapped and set(mapped) == {"pinned-model"}

    def test_unmapped_tiers_stay_unmapped(self):
        """Pinning must not accidentally give the large tier a model."""
        runner = EvalRunner(repo_root=REPO_ROOT, model="pinned-model")
        settings = runner._settings_for(Path("."))
        assert settings.models.tiers["large"].model is None

    def test_the_pinned_model_is_recorded_in_the_result(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT,
            model="pinned-model",
            runtime_builder=scripted_builder(add_task_then_answer()),
        )
        suite = EvalSuite(
            suite="s", agent="task_agent", cases=[make_case(checks=["answered"], repeat=1)]
        )
        assert runner.run_suite(suite).model == "pinned-model"


class TestPermissions:
    def test_evaluation_never_blocks_on_a_prompt(self):
        """write is auto so a run completes, but the broker is still consulted."""
        runner = EvalRunner(repo_root=REPO_ROOT)
        settings = runner._settings_for(Path("."))
        assert settings.permissions.interactive is False
        assert settings.permissions.policy["write"] == "auto"

    def test_dangerous_levels_stay_denied_during_evaluation(self):
        runner = EvalRunner(repo_root=REPO_ROOT)
        policy = runner._settings_for(Path(".")).permissions.policy
        assert policy["destructive"] == "deny"
        assert policy["spend_money"] == "deny"


class TestHarnessErrors:
    def test_an_unknown_agent_is_recorded_not_raised(self):
        """An infrastructure failure must not look like a 0% score with no reason."""
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        case = EvalCase(
            name="broken", agent="no_such_agent", objective="x", checks=["answered"], repeat=1
        )
        result = runner.run_case(case)
        assert result.pass_rate == 0.0
        assert result.runs[0].stop_reason == "harness_error"
        assert "no_such_agent" in (result.runs[0].error or "")


class TestSuiteRun:
    def test_a_suite_aggregates_its_cases(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        suite = EvalSuite(
            suite="demo",
            agent="task_agent",
            cases=[
                make_case("good", checks=["answered"], repeat=2),
                make_case("bad", checks=[{"first_tool_is": "list_tasks"}], repeat=2),
            ],
        )
        result = runner.run_suite(suite)
        assert result.total_runs == 4
        assert result.passed_runs == 2
        assert result.pass_rate == pytest.approx(0.5)
        assert result.case("good").pass_rate == 1.0
        assert result.case("bad").pass_rate == 0.0
