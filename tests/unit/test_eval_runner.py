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
from personal_ai_os.evaluation.runner import EvalRunner, detect_code_version
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


class TestRuntimeVersionProvenance:
    """ADR-041: a result must say which inference runtime produced it.

    The gap this closes: mapping a committed result to an Ollama build was
    commit-date detective work, and the naive "latest result per suite" selector
    once picked up runs made under *different application code*, which would
    have credited their effects to a runtime bump.
    """

    def _suite(self) -> EvalSuite:
        return EvalSuite(
            suite="prov",
            agent="task_agent",
            repeat=1,
            cases=[make_case(checks=["answered"])],
        )

    def test_the_result_records_a_runtime_version_field(self):
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        result = runner.run_suite(self._suite())
        # ScriptedModel reports no version, which is the correct answer for it.
        assert result.runtime_version == ""
        assert "runtime_version" in result.model_dump()

    def test_it_comes_from_the_runtime_provided_registry(self):
        """The architectural requirement, asserted rather than assumed.

        `evaluation/` must not import or construct a provider -- the model seam
        is the project's central rule. The version has to arrive through the
        registry the injected `runtime_builder` supplied, so a test that hands
        over a registry reporting a known version must see exactly that value.
        """

        class VersionedModel(ScriptedModel):
            def health(self):
                h = super().health()
                return h.model_copy(update={"runtime_version": "9.9.9-test"})

        def build(settings: Settings, store: Store, broker: RecordingBroker) -> Runtime:
            registry = ModelRegistry(
                settings.models,
                factory=lambda name, tier, s: VersionedModel(
                    add_task_then_answer()(), name=name
                ),
            )
            return Runtime.build(
                settings=settings, store=store, broker=broker, models=registry,
                configure_logging=False,
            )

        result = EvalRunner(repo_root=REPO_ROOT, runtime_builder=build).run_suite(
            self._suite()
        )
        assert result.runtime_version == "9.9.9-test"

    def test_it_builds_no_second_provider_and_makes_no_network_call(self, monkeypatch):
        """Offline-safety, made structural rather than incidental.

        `pytest -q` must pass with Ollama stopped. If `run_suite` constructed its
        own registry it would build a real `OllamaModel` under
        `default_factory`, and the socket block in `tests/unit/conftest.py` would
        fire. Poisoning the default factory proves the runner never reaches for
        it -- sockets are blocked here anyway, so this asserts the *reason* the
        suite stays offline, not merely that it does.
        """
        import personal_ai_os.models.registry as registry_module

        def explode(*a, **k):  # pragma: no cover - must never be called
            raise AssertionError(
                "run_suite constructed a provider via default_factory; the "
                "runtime's own registry must be used instead (ADR-041)"
            )

        monkeypatch.setattr(registry_module, "default_factory", explode)

        calls: list[str] = []
        original = ScriptedModel.health

        def counting_health(self):
            calls.append(self.name)
            return original(self)

        monkeypatch.setattr(ScriptedModel, "health", counting_health)

        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        suite = EvalSuite(
            suite="prov", agent="task_agent", repeat=3,
            cases=[make_case(checks=["answered"])],
        )
        result = runner.run_suite(suite)
        assert result.runtime_version == ""
        # Observed once at suite initialization, not once per repetition.
        assert len(calls) == 1, f"health() called {len(calls)} times, expected 1"

    def test_it_is_re_observed_for_each_suite(self):
        """A long session can span a server restart; a stale version is worse
        than none, so the cache is per suite rather than per runner."""
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        runner.run_suite(self._suite())
        runner._runtime_version = "stale"
        result = runner.run_suite(self._suite())
        assert result.runtime_version == ""


class TestTraceCapture:
    """ADR-045: the harness can record traces, so a mechanism can be counted.

    Judging a fix by *which failure disappeared* is this project's central
    method, and the harness could not support it: `_run_once` hardcoded
    `RunTrace.disabled`, and each repetition's fixture is deleted on teardown.
    Both prior diagnoses edited the runner by hand -- so every diagnostic run
    was taken on a dirty tree, which ADR-044 says is never a reference point --
    and both times the alternative of rebuilding the scenario outside the
    harness diverged from it (ADR-040, ADR-042).
    """

    def _suite(self, *, repeat: int = 2) -> EvalSuite:
        return EvalSuite(
            suite="traced",
            agent="task_agent",
            repeat=repeat,
            cases=[make_case("a_case", checks=["answered"])],
        )

    def _runner(self, trace_dir=None, **kw) -> EvalRunner:
        return EvalRunner(
            repo_root=REPO_ROOT,
            trace_dir=trace_dir,
            runtime_builder=scripted_builder(add_task_then_answer()),
            **kw,
        )

    # --- 1. the default is untouched --------------------------------------

    def test_no_trace_dir_writes_nothing(self, tmp_path: Path):
        """The shipped path must be byte-for-byte what it was."""
        self._runner().run_suite(self._suite())
        assert list(tmp_path.rglob("*")) == []

    def test_no_trace_dir_still_records_events_in_memory(self):
        """Checks score from `trace.events`, which exists either way."""
        runner = self._runner()
        record = runner._run_once(make_case(checks=["answered"]), 1)
        assert record.metrics.tool_calls == 1

    # --- 2. tracing is write-only -----------------------------------------

    def test_tracing_changes_neither_the_transcript_nor_any_verdict(
        self, tmp_path: Path
    ):
        """The invariant that makes a traced number usable.

        If switching the instrument on could move a check, every measurement
        taken with it would be suspect -- so this is asserted, not assumed.
        """
        checks = [
            "answered",
            {"first_tool_is": "add_task"},
            "no_invalid_arguments",
            {"task_count": 1},
            {"task_field_absent": "due_date"},
        ]
        untraced = self._runner().run_case(make_case(checks=checks, repeat=2))
        traced = self._runner(trace_dir=tmp_path / "t").run_case(
            make_case(checks=checks, repeat=2), suite="traced"
        )

        def verdicts(result):
            return [
                [(o.label, o.passed) for o in run.checks] for run in result.runs
            ]

        assert verdicts(traced) == verdicts(untraced)
        assert [r.passed for r in traced.runs] == [r.passed for r in untraced.runs]
        assert [r.output_preview for r in traced.runs] == [
            r.output_preview for r in untraced.runs
        ]
        assert [r.stop_reason for r in traced.runs] == [
            r.stop_reason for r in untraced.runs
        ]

    # --- 3. outside the fixture, and invisible to the agent ---------------

    def test_the_trace_is_not_inside_the_evaluated_workspace(self, tmp_path: Path):
        """Two failures in one, which is why it is one test.

        The fixture is a TemporaryDirectory deleted after each repetition, and
        `_settings_for` pins `paths.allowed_roots` to it. A trace written
        inside would be destroyed on teardown *and* readable by the agent under
        test. An instrument must not become an input.
        """
        trace_dir = tmp_path / "traces"
        runner = self._runner(trace_dir=trace_dir)
        runner.run_suite(self._suite(repeat=1))

        written = list(trace_dir.rglob("*.jsonl"))
        assert written, "expected a trace file"

        fixture_settings = runner._settings_for(tmp_path / "fixture")
        for root in fixture_settings.resolved_allowed_roots():
            for path in written:
                assert not path.is_relative_to(root)

    def test_the_path_jail_refuses_the_trace_directory(self, tmp_path: Path):
        """Asserted through the jail itself, not by reasoning about paths."""
        from personal_ai_os.core.errors import PathNotAllowedError
        from personal_ai_os.tools.base import ToolContext
        from personal_ai_os.tools.builtin.list_dir import ListDirTool

        trace_dir = tmp_path / "traces"
        runner = self._runner(trace_dir=trace_dir)
        runner.run_suite(self._suite(repeat=1))

        fixture = tmp_path / "fixture"
        fixture.mkdir()
        settings = runner._settings_for(fixture)
        ctx = ToolContext(
            allowed_roots=settings.resolved_allowed_roots(),
            workspace_root=fixture,
        )
        tool = ListDirTool()
        with pytest.raises(PathNotAllowedError, match="outside every allowed root"):
            tool.run(tool.validate_input({"path": str(trace_dir)}), ctx)

    def test_no_trace_content_reaches_the_transcript(self, tmp_path: Path):
        trace_dir = tmp_path / "traces"
        runner = self._runner(trace_dir=trace_dir)
        runner.run_suite(self._suite(repeat=1))
        preview_source = str(trace_dir)
        record = runner._run_once(make_case(checks=["answered"]), 1, "traced")
        assert preview_source not in record.output_preview

    # --- 4 & 5. durable, attributable, replayable -------------------------

    def test_one_file_per_repetition_named_for_suite_case_and_index(
        self, tmp_path: Path
    ):
        trace_dir = tmp_path / "traces"
        self._runner(trace_dir=trace_dir).run_suite(self._suite(repeat=3))

        case_dir = trace_dir / "traced" / "a_case"
        files = sorted(p.name for p in case_dir.glob("*.jsonl"))
        assert len(files) == 3
        assert files[0].startswith("run-01_") and files[0].endswith(".jsonl")
        assert [f[:6] for f in files] == ["run-01", "run-02", "run-03"]

    def test_the_files_survive_fixture_teardown(self, tmp_path: Path):
        """The reason the directory cannot simply be the fixture's runs/."""
        trace_dir = tmp_path / "traces"
        self._runner(trace_dir=trace_dir).run_suite(self._suite(repeat=2))
        assert all(p.stat().st_size > 0 for p in trace_dir.rglob("*.jsonl"))

    def test_a_trace_replays_with_the_tool_call_and_its_payload(self, tmp_path: Path):
        """What mechanism counting actually reads."""
        from personal_ai_os.observability.trace import Events, read_trace

        trace_dir = tmp_path / "traces"
        self._runner(trace_dir=trace_dir).run_suite(self._suite(repeat=1))
        path = next(trace_dir.rglob("*.jsonl"))

        events = list(read_trace(path))
        requested = [e for e in events if e.type == Events.TOOL_REQUESTED]
        results = [e for e in events if e.type == Events.TOOL_RESULT]
        assert [e.data["tool"] for e in requested] == ["add_task"]
        assert requested[0].data["arguments"]["title"] == "Buy oat milk"
        assert results and results[0].data["ok"] is True
        assert "Buy oat milk" in results[0].data["result"]

    def test_the_filename_stays_readable_by_find_trace(self, tmp_path: Path):
        """`paios trace <run_id>` must keep working on these files."""
        from personal_ai_os.observability.trace import find_trace

        trace_dir = tmp_path / "traces"
        self._runner(trace_dir=trace_dir).run_suite(self._suite(repeat=1))
        path = next(trace_dir.rglob("*.jsonl"))
        run_id = path.stem.split("_")[-1]
        assert find_trace(path.parent, run_id) == path

    def test_suite_and_case_names_are_slugged_into_safe_components(
        self, tmp_path: Path
    ):
        """Case names are data. One carrying a separator must not escape."""
        from personal_ai_os.evaluation.runner import _slug

        assert _slug("robustness/../etc") == "robustness_.._etc"
        assert _slug("a::b") == "a_b"
        assert _slug("...") == "unnamed"

        trace_dir = tmp_path / "traces"
        runner = self._runner(trace_dir=trace_dir)
        runner.run_case(make_case("odd/name", checks=["answered"], repeat=1), suite="s")
        assert (trace_dir / "s" / "odd_name").is_dir()

    def test_each_repetition_gets_its_own_run_id(self, tmp_path: Path):
        trace_dir = tmp_path / "traces"
        self._runner(trace_dir=trace_dir).run_suite(self._suite(repeat=3))
        ids = {p.stem.split("_")[-1] for p in trace_dir.rglob("*.jsonl")}
        assert len(ids) == 3


class TestDetectCodeVersion:
    """ADR-044: provenance instrumentation that can never fail a suite.

    Git is monkeypatched throughout so nothing here depends on the real tree
    state -- a test asserting "dirty" against the live repository would pass or
    fail depending on whether someone had edits open.
    """

    def _fake_git(self, monkeypatch, head=("abc1234\n", 0), status=("", 0), raises=None):
        import subprocess as sp
        from personal_ai_os.evaluation import runner as runner_mod

        def fake_run(cmd, **kwargs):
            if raises is not None:
                raise raises
            payload, code = head if "rev-parse" in cmd else status
            return sp.CompletedProcess(cmd, code, stdout=payload, stderr="")

        monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    def test_a_clean_tree_is_a_bare_sha(self, monkeypatch):
        self._fake_git(monkeypatch)
        assert detect_code_version(REPO_ROOT) == "abc1234"

    def test_unstaged_changes_mark_it_dirty(self, monkeypatch):
        self._fake_git(monkeypatch, status=(" M src/x.py\n", 0))
        assert detect_code_version(REPO_ROOT) == "abc1234-dirty"

    def test_staged_changes_mark_it_dirty(self, monkeypatch):
        self._fake_git(monkeypatch, status=("M  src/x.py\n", 0))
        assert detect_code_version(REPO_ROOT) == "abc1234-dirty"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"head": ("", 128)},                    # not a repository
            {"head": ("not-a-sha\n", 0)},           # malformed output
            {"head": ("\n", 0)},                    # empty output
            {"status": ("", 128)},                  # status failed
            {"raises": FileNotFoundError("git")},   # git not installed
            {"raises": OSError("boom")},            # anything unexpected
        ],
    )
    def test_every_failure_path_returns_empty_and_never_raises(
        self, monkeypatch, kwargs
    ):
        """Provenance is optional; a suite must not die because git will not
        answer. Empty claims nothing, which is the honest result."""
        self._fake_git(monkeypatch, **kwargs)
        assert detect_code_version(REPO_ROOT) == ""

    def test_a_timeout_returns_empty(self, monkeypatch):
        import subprocess as sp
        self._fake_git(monkeypatch, raises=sp.TimeoutExpired("git", 10))
        assert detect_code_version(REPO_ROOT) == ""

    def test_untracked_files_alone_do_not_mark_it_dirty(self, monkeypatch):
        """`--untracked-files=no` is deliberate: a scratch file does not change
        what the code does."""
        self._fake_git(monkeypatch, status=("", 0))
        assert detect_code_version(REPO_ROOT) == "abc1234"

    def test_it_is_observed_once_per_suite_not_per_case(self, monkeypatch):
        """Every case in one result must share one provenance observation."""
        calls: list = []
        import subprocess as sp
        from personal_ai_os.evaluation import runner as runner_mod

        def counting(cmd, **kwargs):
            calls.append(cmd)
            payload = "abc1234\n" if "rev-parse" in cmd else ""
            return sp.CompletedProcess(cmd, 0, stdout=payload, stderr="")

        monkeypatch.setattr(runner_mod.subprocess, "run", counting)
        runner = EvalRunner(
            repo_root=REPO_ROOT, runtime_builder=scripted_builder(add_task_then_answer())
        )
        suite = EvalSuite(
            suite="prov", agent="task_agent", repeat=3,
            cases=[make_case(checks=["answered"]), make_case("c2", checks=["answered"])],
        )
        result = runner.run_suite(suite)
        assert result.code_version == "abc1234"
        # One rev-parse + one status for the whole suite, not per repetition.
        assert len(calls) == 2, f"git invoked {len(calls)} times, expected 2"
