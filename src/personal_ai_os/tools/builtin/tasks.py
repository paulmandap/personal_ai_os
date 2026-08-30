"""Task tools -- the first tools that persist something.

These are also the first tools at `write` level, so they are the first real
exercise of the permission system's `ask` path.

Design note: `complete_task` exists as its own tool rather than folding into
`update_task(id, status="done")`. Completing is the most common mutation, and a
one-argument call is markedly easier for a small model to get right than a
two-argument call whose second argument must match an enum exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from personal_ai_os.core.errors import ToolExecutionError
from personal_ai_os.memory.tasks import (
    Task,
    TaskNotFoundError,
    TaskPriority,
    TaskStatus,
    TaskStore,
    validate_iso_date,
)
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext, ToolInput


def _tasks(ctx: ToolContext) -> TaskStore:
    if ctx.store is None:
        raise ToolExecutionError(
            "task storage is not available in this context; the runtime did "
            "not provide a database"
        )
    return TaskStore(ctx.store)


# --- when an open-task lookup misses (ADR-046) -----------------------------
#
# `find_by_title` searches open tasks, which is right for deciding what to act
# on and wrong for explaining a miss. "no open task matches 'oat milk'" is true
# and misleading in the same breath: the task exists, it was just completed, and
# nothing in that sentence lets the model tell "never existed" from "already
# done". Measured on the 3B, that false refusal directly precedes a wrong write
# in 4 runs of 15.
#
# ADR-042's rule still binds -- a failed lookup must not hand the model a menu --
# so these answer the query and offer nothing beyond it.


def _closed_matches(tasks: TaskStore, needle: str) -> list[Task]:
    """Non-open tasks the lookup would have found had it been looking.

    Filtering to non-open is belt and braces: the open search already returned
    nothing, so nothing open cleared the threshold. It is kept so the caller's
    branches stay true by construction rather than by that argument holding.
    """
    return [
        task
        for task in tasks.find_by_title(needle, include_done=True)
        if not task.status.is_open
    ]


def _too_many_closed(needle: str, count: int) -> str:
    """Several closed matches: refuse, and name none of them.

    Naming one would rebuild ADR-042's menu on the closed side, and picking one
    would be the guess `find_by_title` exists to avoid. The count is the honest
    thing to report -- the phrase is too broad, and that is all the caller
    needs to know.
    """
    return (
        f"no open task matches {needle!r}. {count} closed tasks do; use a "
        f"more specific title."
    )


# --- add -------------------------------------------------------------------


class AddTaskInput(ToolInput):
    title: str = Field(
        min_length=1,
        max_length=500,
        description="Short description of the task, e.g. 'Review the Phase 2 code'.",
    )
    notes: str = Field(default="", description="Optional longer detail.")
    priority: TaskPriority = Field(
        default=TaskPriority.NORMAL, description="One of: low, normal, high."
    )
    due_date: str | None = Field(
        default=None, description="Optional ISO date, e.g. '2026-09-07'."
    )
    confirm_duplicate: bool = Field(
        default=False,
        description=(
            "Leave this false. Set it true ONLY after add_task has already "
            "refused this title as a duplicate and the user genuinely wants a "
            "second, separate task."
        ),
    )

    # Validate at the *input* boundary, not just inside the store. Otherwise a
    # malformed date reaches the model as a generic execution failure instead
    # of the message written to help it retry correctly.
    _check_due = field_validator("due_date")(lambda v: validate_iso_date(v))


class AddTaskTool(Tool):
    name = "add_task"
    description = (
        "Create a new task in the user's task list. Use this when the user "
        "asks to remember, track, or add something to do."
    )
    Input = AddTaskInput
    Output = Task
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: AddTaskInput) -> str:  # type: ignore[override]
        return f'"{args.title}"'

    def run(self, args: AddTaskInput, ctx: ToolContext) -> Task:  # type: ignore[override]
        tasks = _tasks(ctx)

        # The referent check the other mutating tools already have. Measured:
        # told to leave a "Renew passport" task alone, qwen2.5:7b created
        # "Apply for passport renewal" instead -- reaching for the one tool
        # that never asks whether what the user named already exists
        # (ADR-030). Refusing here names the real task and its status, which
        # is exactly the fact the agent needs to answer correctly.
        if not args.confirm_duplicate:
            clashes = tasks.colliding(args.title)
            if clashes:
                existing = ", ".join(
                    f"{t.title!r} (status {t.status.value})" for t in clashes
                )
                raise ToolExecutionError(
                    f"nothing was created: {args.title!r} looks like a task the "
                    f"user already has -- {existing}. If they meant that task, "
                    f"use update_task or complete_task with `find`. If they "
                    f"genuinely want a second separate task, call add_task "
                    f"again with confirm_duplicate=true."
                )

        return tasks.add(
            args.title,
            notes=args.notes,
            priority=args.priority,
            due_date=args.due_date,
        )


# --- list ------------------------------------------------------------------


class ListTasksInput(ToolInput):
    status: TaskStatus | None = Field(
        default=None,
        description=(
            "Filter to one status: todo, doing, done, cancelled. "
            "Omit to see all open tasks."
        ),
    )
    include_done: bool = Field(
        default=False, description="Include completed and cancelled tasks."
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum tasks to return.")


class ListTasksOutput(BaseModel):
    count: int
    tasks: list[Task]
    #: A rendered text block alongside the structured list. Small models read
    #: a flat list far more reliably than they read nested JSON, and the cost
    #: of including both is a few dozen tokens.
    summary: str


class ListTasksTool(Tool):
    name = "list_tasks"
    # The scope here matters as much as the wording. It used to say "any
    # question about what the user has to do", and qwen2.5:3b obeyed that
    # exactly: asked "what do I need for the passport appointment?" -- with the
    # answer sitting in that task's notes -- it called nothing, 5 of 5, while
    # calling list_tasks 3 of 3 for "what is on my task list?". The rule was
    # wrong, not the model. Saying that notes come back is the other half: a
    # model cannot know the answer is in there unless the description says so.
    description = (
        "List the user's tasks with their notes, due dates, priority and "
        "status, most important first. Use this before answering any question "
        "the task list could answer -- including details stored inside a task, "
        "not only what is outstanding."
    )
    Input = ListTasksInput
    Output = ListTasksOutput
    permission = PermissionLevel.READ
    timeout_s = 10.0

    def describe_resource(self, args: ListTasksInput) -> str:  # type: ignore[override]
        return args.status.value if args.status else "open tasks"

    def run(self, args: ListTasksInput, ctx: ToolContext) -> ListTasksOutput:  # type: ignore[override]
        found = _tasks(ctx).list(
            status=args.status, include_done=args.include_done, limit=args.limit
        )
        rendered = "\n".join(t.summary() for t in found) or "(no tasks)"
        return ListTasksOutput(count=len(found), tasks=found, summary=rendered)


# --- update ----------------------------------------------------------------


class UpdateTaskInput(ToolInput):
    """Identify the task by `find` or by `id` -- exactly one.

    The selector is called `find`, not `title`, because `title` already means
    *the new title* on this tool. Naming both the same would make
    `update_task(title=...)` ambiguous in a way a model would resolve wrongly
    about half the time.
    """

    find: str | None = Field(
        default=None,
        description=(
            "Words from the existing task's title, e.g. 'oat milk'. Prefer "
            "this - use it whenever the user names the task rather than a number."
        ),
    )
    id: int | None = Field(
        default=None,
        description="The task's numeric id. Only use an id you saw in list_tasks output.",
    )
    title: str | None = Field(default=None, description="New title.")
    notes: str | None = Field(default=None, description="New notes.")
    status: TaskStatus | None = Field(
        default=None, description="New status: todo, doing, done, cancelled."
    )
    priority: TaskPriority | None = Field(
        default=None, description="New priority: low, normal, high."
    )
    due_date: str | None = Field(
        default=None, description="New ISO due date, e.g. '2026-09-07'."
    )

    _check_due = field_validator("due_date")(lambda v: validate_iso_date(v))

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> UpdateTaskInput:
        if (self.find is None) == (self.id is None):
            raise ValueError(
                "give exactly one of 'find' or 'id' to choose the task -- "
                "'find' is usually what you want"
            )
        return self


def _update_miss(tasks: TaskStore, needle: str) -> str:
    """Why `update_task` found nothing open under this phrase.

    Deliberately **not** the same sentence `complete_task` uses. They share the
    lookup, not the constraint: there, a closed match means the user's request
    is already satisfied (or can never be); here it means the request is not
    satisfied and this selector cannot reach the task. One string for both would
    have to be wrong about one of them.

    It also stops short of "use its id from list_tasks", which would be *true* --
    the id path does reach a closed task -- and would advertise the exact
    `list_tasks` -> id pathway ADR-042 created and this work is trying to shrink.
    State the constraint; do not hand out a route.
    """
    closed = _closed_matches(tasks, needle)
    if not closed:
        return f"no open task matches {needle!r}."          # ADR-042, verbatim
    if len(closed) > 1:
        return _too_many_closed(needle, len(closed))
    task = closed[0]
    return (
        f"{task.title!r} is {task.status.value}, not open, and update_task "
        f"matches open tasks by title."
    )


class UpdateTaskTool(Tool):
    name = "update_task"
    description = (
        "Change an existing task. Identify it with `find` (words from its "
        "title) unless you already know its id. Only the fields you provide "
        "are modified. To mark a task finished, prefer complete_task."
    )
    Input = UpdateTaskInput
    Output = Task
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: UpdateTaskInput) -> str:  # type: ignore[override]
        return f"task #{args.id}" if args.id is not None else f'"{args.find}"'

    def run(self, args: UpdateTaskInput, ctx: ToolContext) -> Task:  # type: ignore[override]
        tasks = _tasks(ctx)
        task_id = args.id

        if task_id is None:
            # Same resolution as complete_task: requiring an id the user never
            # gave is what pushes a model into inventing one (ADR-022).
            matches = tasks.find_by_title(str(args.find))
            # ADR-042: a zero-match refusal must NOT enumerate the other open
            # tasks. It used to, and the list read as a menu: qwen2.5:3b
            # completed a task nobody asked about in 4 of 15 traced runs, always
            # by naming a title this refusal had just listed. The decisive one
            # is the *second* refusal in a run -- once the requested task is
            # done it is no longer "open", the retry fails, and the list has
            # narrowed to a single entry that reads as the answer.
            #
            # The ambiguity branch below keeps its list, deliberately: there the
            # candidates *are* the answer, and naming them is what stops the
            # tool completing whichever sorted first.
            if not matches:
                raise ToolExecutionError(_update_miss(tasks, str(args.find)))
            if len(matches) > 1:
                raise ToolExecutionError(
                    f"{len(matches)} open tasks match {args.find!r}: "
                    f"{[t.title for t in matches]}. Use a more specific phrase, "
                    f"or an id from list_tasks."
                )
            task_id = matches[0].id

        try:
            return tasks.update(
                int(task_id),
                title=args.title,
                notes=args.notes,
                status=args.status,
                priority=args.priority,
                due_date=args.due_date,
            )
        except TaskNotFoundError as exc:
            raise ToolExecutionError(
                f"{exc}. Use `find` with words from the title instead, or "
                f"list_tasks to see valid ids."
            ) from exc


# --- complete --------------------------------------------------------------


class CompleteTaskInput(ToolInput):
    """Identify the task by title or by id -- exactly one.

    ``title`` exists because it is the handle the user actually gives ("I
    finished the oat milk one"). Measured behaviour: when only ``id`` was
    accepted, both qwen2.5:3b and 7b guessed an id rather than calling
    list_tasks first, and completed the wrong task. Removing the need to know
    an id removes the failure, which a prompt instruction did not.

    **Accepting both selectors when they agree was tried and reverted**
    (ADR-047). qwen2.5:3b supplies both in 12 runs of 15, so the rejection is
    common -- but removing it took invalid-argument refusals from 27 to 0 and
    left the defect it was aimed at completely unmoved (4/15 either way). The
    loop it removes is a delay, not a cause.
    """

    title: str | None = Field(
        default=None,
        description=(
            "Words from the task's title, e.g. 'oat milk'. Prefer this - "
            "use it whenever the user names the task rather than a number."
        ),
    )
    id: int | None = Field(
        default=None,
        description="The task's numeric id. Only use an id you saw in list_tasks output.",
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> CompleteTaskInput:
        if (self.title is None) == (self.id is None):
            raise ValueError(
                "give exactly one of 'title' or 'id' -- title is usually what you want"
            )
        return self


def _complete_miss(tasks: TaskStore, needle: str) -> str:
    """Why `complete_task` found nothing open under this phrase.

    Two closed outcomes, and they are different requests rather than two
    wordings of one, so they get different sentences:

    - **done** -- what the user asked for is already true. "Nothing to change"
      is the fact, and it is the fact that stops a retry turning into a wrong
      write.
    - **cancelled** -- it can never be true, because a cancelled task is not
      waiting to be finished.

    The status is read from the row. Assuming "done" would be right most of the
    time and would report a cancelled task as completed -- a false claim about
    stored state, from the tool, which is the class of defect this whole area
    exists to prevent.

    **Still a refusal, not an idempotent success.** Returning the already-done
    task would arguably be more correct, and is not done here: it would make
    `complete_task` *succeed* where it previously refused, which moves
    `tool_succeeded` and `tool_did_not_run` -- the safety suite's two oracles
    (ADR-037). A security-relevant surface does not get changed as a side effect
    of fixing a message. The fact the model needs is delivered either way.
    """
    closed = _closed_matches(tasks, needle)
    if not closed:
        return f"no open task matches {needle!r}."          # ADR-042, verbatim
    if len(closed) > 1:
        return _too_many_closed(needle, len(closed))
    task = closed[0]
    if task.status is TaskStatus.DONE:
        return f"nothing to change: {task.title!r} is already marked done."
    return (
        f"{task.title!r} is {task.status.value}, not open, so it cannot be "
        f"completed."
    )


class CompleteTaskTool(Tool):
    name = "complete_task"
    description = (
        "Mark a task as done. Use this when the user says they finished "
        "something. Identify the task by its title unless you already know its id."
    )
    Input = CompleteTaskInput
    Output = Task
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: CompleteTaskInput) -> str:  # type: ignore[override]
        return f"task #{args.id}" if args.id is not None else f'"{args.title}"'

    def run(self, args: CompleteTaskInput, ctx: ToolContext) -> Task:  # type: ignore[override]
        tasks = _tasks(ctx)

        if args.id is not None:
            try:
                return tasks.complete(args.id)
            except TaskNotFoundError as exc:
                raise ToolExecutionError(
                    f"{exc}. Call complete_task with a title instead, or "
                    f"list_tasks to see valid ids."
                ) from exc

        matches = tasks.find_by_title(str(args.title))
        # ADR-042: a zero-match refusal must NOT enumerate the other open
        # tasks. It used to, and the list read as a menu: qwen2.5:3b
        # completed a task nobody asked about in 4 of 15 traced runs, always
        # by naming a title this refusal had just listed. The decisive one
        # is the *second* refusal in a run -- once the requested task is
        # done it is no longer "open", the retry fails, and the list has
        # narrowed to a single entry that reads as the answer.
        #
        # The ambiguity branch below keeps its list, deliberately: there the
        # candidates *are* the answer, and naming them is what stops the
        # tool completing whichever sorted first.
        if not matches:
            raise ToolExecutionError(_complete_miss(tasks, str(args.title)))
        if len(matches) > 1:
            # Ambiguity is recoverable: name the candidates and let the model
            # pick, rather than silently completing whichever sorted first.
            raise ToolExecutionError(
                f"{len(matches)} open tasks match {args.title!r}: "
                f"{[t.title for t in matches]}. Use a more specific title, or "
                f"an id from list_tasks."
            )

        assert matches[0].id is not None
        return tasks.complete(matches[0].id)
