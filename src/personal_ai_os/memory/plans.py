"""Plans -- what a top-level run actually did, so it survives a restart.

**This records what the runtime OBSERVED, never what a model DECLARED**
(ADR-065). Every step is written from a real `AgentResult`, so the record cannot
disagree with what happened. The alternative -- a `record_plan` tool the Master
calls -- was rejected: it would change the Master's tool surface, and it would
store narration. `docs/security.md` is explicit that *agent narration is not an
audit trail; the store is the record*, and ADR-038 measured an agent reporting a
completion it never performed.

THE LOAD-BEARING INVARIANT (INV-1)
----------------------------------
::

    BEGIN -> INSERT pending -> COMMIT -> invoke sub-agent
          -> receive AgentResult -> UPDATE pending->done/failed -> COMMIT

`begin_step` commits the intent **before** the sub-agent can run; `finish_step`
records the outcome **after** it returns. Everything resume infers rests on that
ordering, because *"no step row means the delegation never started"* is only
true if the intent was committed first. Were the insert still open when the
sub-agent ran, a crash would leave no row for a delegation that did execute, and
resume would silently **under**-report -- the dangerous direction, since
over-reporting is safe under at-least-once and under-reporting is not.

`Runtime` must therefore never invoke a sub-agent inside a `store.write()`
block. Pinned by a test that reads the pending row from an independent
connection *while the sub-agent is running*.

WHAT THE STATES MEAN
--------------------
=================  ===========================================================
no step row        the delegation was never started
``pending``        it was started; **whether its effects landed is unknown**
``done``/``failed``  it ran and the outcome was observed
=================  ===========================================================

**Execution is at-least-once.** A `pending` step may or may not have taken
effect, and nothing in the store distinguishes "crashed before returning" from
"succeeded but crashed before persistence". Resume treats `pending` as *may have
happened* and says so; exactly-once is not claimed and cannot be.
"""

from __future__ import annotations

import sqlite3
from enum import Enum

from pydantic import BaseModel

from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.core.ids import utc_iso
from personal_ai_os.memory.store import Store


class PlanNotFoundError(PersonalAIOSError):
    """No plan with that id."""


class PlanNotResumableError(PersonalAIOSError):
    """The plan reached a terminal state; it cannot be resumed.

    Terminal is terminal: re-attempting a failed objective is a NEW run and a
    new plan, which keeps the failure record intact instead of overwriting it.
    """


class PlanStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


class StepStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


#: Terminal plan states. Only a `running` plan can be resumed or abandoned.
TERMINAL: frozenset[PlanStatus] = frozenset(
    {PlanStatus.DONE, PlanStatus.FAILED, PlanStatus.ABANDONED}
)


class PlanStep(BaseModel):
    id: int | None = None
    plan_id: int
    seq: int
    run_id: str
    agent: str
    objective: str
    status: StepStatus = StepStatus.PENDING
    output: str = ""
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PlanStep:
        return cls(**dict(row))

    def summary(self) -> str:
        marker = {"pending": "?", "done": "x", "failed": "!"}[self.status.value]
        return f"[{marker}] {self.seq}. {self.agent}: {self.objective}"


class Plan(BaseModel):
    id: int | None = None
    run_id: str
    agent: str
    objective: str
    status: PlanStatus = PlanStatus.RUNNING
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Plan:
        return cls(**dict(row))

    @property
    def resumable(self) -> bool:
        return self.status is PlanStatus.RUNNING

    def summary(self) -> str:
        return f"#{self.id} [{self.status.value}] {self.agent}: {self.objective}"


class PlanStore:
    """CRUD over `plans` and `plan_steps`."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # --- plans -------------------------------------------------------------

    def open(self, *, run_id: str, agent: str, objective: str) -> Plan:
        """Record that a top-level run has started. Always `running`."""
        now = utc_iso()
        with self._store.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO plans (run_id, agent, objective, status,
                                   created_at, updated_at)
                VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (run_id, agent, objective, now, now),
            )
            plan_id = int(cursor.lastrowid or 0)
        return self.get(plan_id)

    def close(self, plan_id: int, *, ok: bool) -> Plan:
        """Record a positively observed outcome: `done` or `failed`.

        Never called for a crash -- a `finally` block cannot run after SIGKILL
        or power loss, so an interrupted plan simply stays `running`. That is
        the crash signature, and it is the only way a stale `running` row can
        exist.
        """
        return self._terminate(plan_id, PlanStatus.DONE if ok else PlanStatus.FAILED)

    def abandon(self, plan_id: int) -> Plan:
        """The only route to `abandoned`, and only by explicit human act."""
        return self._terminate(plan_id, PlanStatus.ABANDONED)

    def _terminate(self, plan_id: int, status: PlanStatus) -> Plan:
        plan = self.get(plan_id)
        if plan.status in TERMINAL:
            raise PlanNotResumableError(
                f"plan {plan_id} is already {plan.status.value}; terminal states "
                f"do not change"
            )
        now = utc_iso()
        with self._store.write() as conn:
            conn.execute(
                "UPDATE plans SET status = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ?",
                (status.value, now, now, plan_id),
            )
        return self.get(plan_id)

    def get(self, plan_id: int) -> Plan:
        row = self._store.query_one("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if row is None:
            raise PlanNotFoundError(f"no plan with id {plan_id}")
        return Plan.from_row(row)

    def list(self, *, status: PlanStatus | None = None, limit: int = 50) -> list[Plan]:
        """Newest first."""
        sql = "SELECT * FROM plans"
        params: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status.value,)
        sql += " ORDER BY id DESC LIMIT ?"
        return [Plan.from_row(r) for r in self._store.query(sql, (*params, limit))]

    # --- steps -------------------------------------------------------------

    def begin_step(
        self, plan_id: int, *, run_id: str, agent: str, objective: str
    ) -> PlanStep:
        """Commit the INTENT to delegate. **INV-1: call this before invoking.**

        The returned step is `pending`. Its row is committed when this returns,
        which is what makes "no row" mean "never started".
        """
        now = utc_iso()
        with self._store.write() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM plan_steps "
                "WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            seq = int(row["next"])
            cursor = conn.execute(
                """
                INSERT INTO plan_steps (plan_id, seq, run_id, agent, objective,
                                        status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (plan_id, seq, run_id, agent, objective, now, now),
            )
            step_id = int(cursor.lastrowid or 0)
        return self.get_step(step_id)

    def finish_step(
        self, step_id: int, *, ok: bool, output: str = "", error: str | None = None
    ) -> PlanStep:
        """Record the OUTCOME. **INV-1: call this only after the result returns.**"""
        with self._store.write() as conn:
            conn.execute(
                "UPDATE plan_steps SET status = ?, output = ?, error = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    StepStatus.DONE.value if ok else StepStatus.FAILED.value,
                    output,
                    error,
                    utc_iso(),
                    step_id,
                ),
            )
        return self.get_step(step_id)

    def get_step(self, step_id: int) -> PlanStep:
        row = self._store.query_one("SELECT * FROM plan_steps WHERE id = ?", (step_id,))
        if row is None:
            raise PlanNotFoundError(f"no plan step with id {step_id}")
        return PlanStep.from_row(row)

    def steps(self, plan_id: int) -> list[PlanStep]:
        return [
            PlanStep.from_row(r)
            for r in self._store.query(
                "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY seq", (plan_id,)
            )
        ]

    # --- resume ------------------------------------------------------------

    def resume_objective(self, plan_id: int) -> str:
        """Compose the objective for a resumed run.

        **This is the one model-facing part of plan persistence**, and only on
        the resume path. Normal runs send the model nothing from this module.

        A `pending` step is reported as *may or may not have completed*, because
        the store genuinely cannot tell -- "crashed before returning" and
        "succeeded but crashed before persistence" leave identical rows. Saying
        it plainly is the difference between honest context and a false claim
        about the world.

        Completed steps are reported as **history, not a guarantee**: they say
        what the runtime observed then, not that it is still true now.
        """
        plan = self.get(plan_id)
        if not plan.resumable:
            raise PlanNotResumableError(
                f"plan {plan_id} is {plan.status.value}; only a running plan can "
                f"be resumed"
            )

        done: list[str] = []
        uncertain: list[str] = []
        for step in self.steps(plan_id):
            line = f"- {step.agent}: {step.objective}"
            if step.status is StepStatus.PENDING:
                uncertain.append(line)
            elif step.status is StepStatus.DONE:
                done.append(f"{line} -> {step.output.strip()[:200]}")
            else:
                done.append(f"{line} -> FAILED: {step.error or 'unknown error'}")

        if not done and not uncertain:
            return plan.objective

        parts = [plan.objective, "", "This work was started in an earlier session."]
        if done:
            parts += ["", "Already carried out (as recorded then):", *done]
        if uncertain:
            parts += [
                "",
                "Started but the outcome was never recorded -- these MAY or MAY "
                "NOT have taken effect:",
                *uncertain,
            ]
        parts += [
            "",
            "Check the current state before repeating any of it, and do not "
            "claim work you have not verified.",
        ]
        return "\n".join(parts)
