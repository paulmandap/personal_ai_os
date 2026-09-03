"""Plan persistence, and the ordering invariant everything else rests on.

`PlanStore` records what the runtime OBSERVED, so these tests mostly assert that
what the store says matches what actually happened. The load-bearing one is
`TestInvariantCommitBeforeInvocation`: if that ordering breaks, resume starts
*under*-reporting delegations that really ran, which is the dangerous direction.

All offline. No Ollama, no GPU, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.memory.plans import (
    PlanNotFoundError,
    PlanNotResumableError,
    PlanStatus,
    PlanStore,
    StepStatus,
)
from personal_ai_os.memory.store import Store
from personal_ai_os.observability.trace import RunTrace
from personal_ai_os.runtime import Runtime


@pytest.fixture
def plans() -> PlanStore:
    return PlanStore(Store.in_memory())


def _result(ok: bool = True, output: str = "done", error: str | None = None):
    return AgentResult(
        ok=ok,
        agent="task_agent",
        run_id="r-sub",
        stop_reason=StopReason.ANSWERED if ok else StopReason.ERROR,
        output=output,
        error=error,
    )


class TestPlanStateMachine:
    def test_a_new_plan_is_running(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="do the thing")
        assert plan.status is PlanStatus.RUNNING
        assert plan.resumable

    def test_observed_success_closes_it_done(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        assert plans.close(plan.id, ok=True).status is PlanStatus.DONE

    def test_observed_failure_closes_it_failed(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        assert plans.close(plan.id, ok=False).status is PlanStatus.FAILED

    def test_abandon_is_the_only_route_to_abandoned(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        assert plans.abandon(plan.id).status is PlanStatus.ABANDONED

    @pytest.mark.parametrize("terminal", [True, False])
    def test_terminal_is_terminal(self, plans: PlanStore, terminal: bool):
        """Re-attempting a failed objective is a NEW plan.

        Overwriting the old one would destroy the record of the failure, which
        is the only evidence that it happened.
        """
        plan = plans.open(run_id="r1", agent="master", objective="o")
        plans.close(plan.id, ok=terminal)
        with pytest.raises(PlanNotResumableError):
            plans.close(plan.id, ok=True)
        with pytest.raises(PlanNotResumableError):
            plans.abandon(plan.id)

    def test_an_unknown_plan_raises(self, plans: PlanStore):
        with pytest.raises(PlanNotFoundError):
            plans.get(999)

    def test_one_plan_per_originating_run(self, plans: PlanStore):
        """`run_id` is UNIQUE, so a retry cannot silently attach to a plan."""
        plans.open(run_id="r1", agent="master", objective="o")
        with pytest.raises(Exception):
            plans.open(run_id="r1", agent="master", objective="other")


class TestSteps:
    def test_a_begun_step_is_pending(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        step = plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="x")
        assert step.status is StepStatus.PENDING
        assert step.seq == 1

    def test_sequence_numbers_increment_per_plan(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        a = plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="1")
        b = plans.begin_step(plan.id, run_id="r1", agent="finance", objective="2")
        assert (a.seq, b.seq) == (1, 2)

    def test_finishing_records_the_observed_outcome(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        step = plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="x")
        done = plans.finish_step(step.id, ok=True, output="added")
        assert done.status is StepStatus.DONE
        assert done.output == "added"

    def test_a_failed_step_keeps_its_error(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        step = plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="x")
        failed = plans.finish_step(step.id, ok=False, error="no such tool")
        assert failed.status is StepStatus.FAILED
        assert failed.error == "no such tool"

    def test_steps_cascade_when_a_plan_is_deleted(self, plans: PlanStore):
        """`PRAGMA foreign_keys = ON` is set in `Store.connect`, so this fires."""
        store = plans._store
        plan = plans.open(run_id="r1", agent="master", objective="o")
        plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="x")
        with store.write() as conn:
            conn.execute("DELETE FROM plans WHERE id = ?", (plan.id,))
        assert store.query("SELECT * FROM plan_steps") == []


class TestResumeObjective:
    def test_a_plan_with_no_steps_resumes_the_bare_objective(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="do the thing")
        assert plans.resume_objective(plan.id) == "do the thing"

    def test_completed_steps_are_offered_as_history(self, plans: PlanStore):
        plan = plans.open(run_id="r1", agent="master", objective="do the thing")
        step = plans.begin_step(
            plan.id, run_id="r1", agent="task_agent", objective="add passport task"
        )
        plans.finish_step(step.id, ok=True, output="created #1")
        text = plans.resume_objective(plan.id)
        assert "add passport task" in text
        assert "created #1" in text
        assert "do the thing" in text

    def test_a_pending_step_is_reported_as_uncertain(self, plans: PlanStore):
        """The honest phrasing. The store cannot tell whether it took effect."""
        plan = plans.open(run_id="r1", agent="master", objective="o")
        plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="risky")
        text = plans.resume_objective(plan.id)
        assert "MAY or MAY NOT" in text
        assert "risky" in text

    def test_resume_never_asserts_prior_effects_still_hold(self, plans: PlanStore):
        """A step is history, not a guarantee about the world right now."""
        plan = plans.open(run_id="r1", agent="master", objective="o")
        step = plans.begin_step(plan.id, run_id="r1", agent="task_agent", objective="x")
        plans.finish_step(step.id, ok=True, output="ok")
        text = plans.resume_objective(plan.id)
        assert "as recorded then" in text
        assert "Check the current state" in text

    @pytest.mark.parametrize("closer", ["done", "failed", "abandoned"])
    def test_a_terminal_plan_cannot_be_resumed(self, plans: PlanStore, closer: str):
        plan = plans.open(run_id="r1", agent="master", objective="o")
        if closer == "abandoned":
            plans.abandon(plan.id)
        else:
            plans.close(plan.id, ok=closer == "done")
        with pytest.raises(PlanNotResumableError):
            plans.resume_objective(plan.id)


class TestInvariantCommitBeforeInvocation:
    """INV-1, proved mechanically rather than by review.

    `no step row` may only be read as "this runtime never started the
    delegation" if the pending row was durably committed BEFORE the sub-agent
    could run. If the insert were still inside an open transaction, a crash
    would leave no row for a delegation that DID execute -- and resume would
    under-report, which is worse than repeating work.
    """

    @staticmethod
    def _runtime(workspace: Path, db: Path) -> Runtime:
        return Runtime.build(workspace_root=workspace, store=Store(db))

    def test_the_pending_row_is_visible_to_an_independent_connection(
        self, workspace: Path, tmp_path: Path
    ):
        """The failure injection: read the row from a SEPARATE Store, mid-run.

        A file-backed database is required -- a second connection to `:memory:`
        is a different database entirely, so the test would prove nothing.
        """
        db = tmp_path / "plans.db"
        runtime = self._runtime(workspace, db)
        plan = runtime.plans.open(run_id="r1", agent="master", objective="o")
        observed: dict[str, object] = {}

        class Stub:
            def run(self, objective: str) -> AgentResult:
                # Runs *during* the delegation. If the insert had not committed,
                # this independent connection would see nothing.
                with Store(db) as other:
                    rows = other.query("SELECT * FROM plan_steps")
                    observed["rows"] = [dict(r) for r in rows]
                return _result()

        runtime.create_agent = lambda *a, **k: Stub()  # type: ignore[assignment]
        runtime.run_sub_agent(
            "task_agent",
            "add a task",
            trace=RunTrace.disabled(agent="master"),
            depth=1,
            call_stack=("master",),
            plan_id=plan.id,
        )

        rows = observed["rows"]
        assert len(rows) == 1, "the pending row was not committed before invocation"
        assert rows[0]["status"] == "pending", "the outcome was written too early"
        assert rows[0]["objective"] == "add a task"
        runtime.close()

    def test_the_outcome_is_recorded_after_the_result_returns(
        self, workspace: Path, tmp_path: Path
    ):
        db = tmp_path / "plans.db"
        runtime = self._runtime(workspace, db)
        plan = runtime.plans.open(run_id="r1", agent="master", objective="o")

        class Stub:
            def run(self, objective: str) -> AgentResult:
                return _result(output="created #1")

        runtime.create_agent = lambda *a, **k: Stub()  # type: ignore[assignment]
        runtime.run_sub_agent(
            "task_agent",
            "add a task",
            trace=RunTrace.disabled(agent="master"),
            depth=1,
            call_stack=("master",),
            plan_id=plan.id,
        )

        steps = runtime.plans.steps(plan.id)
        assert [s.status for s in steps] == [StepStatus.DONE]
        assert steps[0].output == "created #1"
        runtime.close()

    def test_a_crash_during_the_delegation_leaves_the_step_pending(
        self, workspace: Path, tmp_path: Path
    ):
        db = tmp_path / "plans.db"
        runtime = self._runtime(workspace, db)
        plan = runtime.plans.open(run_id="r1", agent="master", objective="o")

        class Boom:
            def run(self, objective: str) -> AgentResult:
                raise RuntimeError("power loss")

        runtime.create_agent = lambda *a, **k: Boom()  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            runtime.run_sub_agent(
                "task_agent",
                "add a task",
                trace=RunTrace.disabled(agent="master"),
                depth=1,
                call_stack=("master",),
                plan_id=plan.id,
            )

        # The crash signature: step pending, plan still running. No cleanup code
        # ran, and none is relied upon -- a `finally` cannot survive SIGKILL.
        assert [s.status for s in runtime.plans.steps(plan.id)] == [StepStatus.PENDING]
        assert runtime.plans.get(plan.id).status is PlanStatus.RUNNING
        runtime.close()

    def test_the_two_crash_windows_are_indistinguishable(
        self, workspace: Path, tmp_path: Path
    ):
        """The limitation, asserted rather than described.

        "Crashed before returning" and "succeeded but crashed before the outcome
        was written" leave identical rows. This is why execution is at-least-once
        and resume reports a pending step as *may or may not* have happened.
        """
        def crash_before(objective: str) -> AgentResult:
            """Died while the sub-agent was still working."""
            raise RuntimeError("crash")

        def crash_after(objective: str) -> AgentResult:
            """Succeeded, then died before the runtime could record it."""
            _result()  # the work completed; its effects landed
            raise RuntimeError("crash")

        rows: list[dict] = []
        for label, stub_run in (("before", crash_before), ("after", crash_after)):
            db = tmp_path / f"{label}.db"
            runtime = self._runtime(workspace, db)
            plan = runtime.plans.open(run_id="r1", agent="master", objective="o")

            class Stub:
                run = staticmethod(stub_run)

            runtime.create_agent = lambda *a, **k: Stub()  # type: ignore[assignment]
            with pytest.raises(RuntimeError):
                runtime.run_sub_agent(
                    "task_agent",
                    "add a task",
                    trace=RunTrace.disabled(agent="master"),
                    depth=1,
                    call_stack=("master",),
                    plan_id=plan.id,
                )
            step = runtime.plans.steps(plan.id)[0]
            rows.append(
                {"status": step.status, "output": step.output, "error": step.error}
            )
            runtime.close()

        assert rows[0] == rows[1], "the store must not appear to distinguish these"
        assert rows[0]["status"] is StepStatus.PENDING


class TestRecordingIsModelFacingNeutral:
    """Structural proof, not the benchmark (ADR-065).

    The `master` suite is a canary for accidental leakage. What makes the claim
    true is that nothing from the plan store reaches the model on a normal run.
    """

    def test_no_plan_text_reaches_the_delegated_objective(
        self, workspace: Path, tmp_path: Path
    ):
        db = tmp_path / "plans.db"
        runtime = Runtime.build(workspace_root=workspace, store=Store(db))
        plan = runtime.plans.open(
            run_id="r1", agent="master", objective="MARKER_THAT_MUST_NOT_LEAK"
        )
        seen: dict[str, str] = {}

        class Stub:
            def run(self, objective: str) -> AgentResult:
                seen["objective"] = objective
                return _result()

        runtime.create_agent = lambda *a, **k: Stub()  # type: ignore[assignment]
        runtime.run_sub_agent(
            "task_agent",
            "add a task",
            trace=RunTrace.disabled(agent="master"),
            depth=1,
            call_stack=("master",),
            plan_id=plan.id,
        )

        assert seen["objective"] == "add a task"
        assert "MARKER" not in seen["objective"]
        runtime.close()


class TestMigrationIsAdditive:
    def test_existing_task_rows_survive_the_v3_migration(self, tmp_path: Path):
        """The migration runs against a live database. It must add, never move."""
        from personal_ai_os.memory.tasks import TaskStore

        db = tmp_path / "live.db"
        with Store(db) as store:
            TaskStore(store).add("Renew passport", notes="keep me")

        with Store(db) as reopened:
            assert reopened.version >= 3
            tasks = TaskStore(reopened).list()
            assert [t.title for t in tasks] == ["Renew passport"]
            assert tasks[0].notes == "keep me"
            assert reopened.query("SELECT * FROM plans") == []
