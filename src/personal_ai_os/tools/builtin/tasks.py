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
            if not matches:
                open_titles = [t.title for t in tasks.list(limit=20)]
                raise ToolExecutionError(
                    f"no open task matches {args.find!r}. Open tasks: "
                    f"{open_titles or 'none'}"
                )
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
        if not matches:
            open_titles = [t.title for t in tasks.list(limit=20)]
            raise ToolExecutionError(
                f"no open task matches {args.title!r}. Open tasks: "
                f"{open_titles or 'none'}"
            )
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
