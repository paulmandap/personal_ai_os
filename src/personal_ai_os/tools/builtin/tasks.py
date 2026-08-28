"""Task tools -- the first tools that persist something.

These are also the first tools at `write` level, so they are the first real
exercise of the permission system's `ask` path.

Design note: `complete_task` exists as its own tool rather than folding into
`update_task(id, status="done")`. Completing is the most common mutation, and a
one-argument call is markedly easier for a small model to get right than a
two-argument call whose second argument must match an enum exactly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

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
from personal_ai_os.tools.base import Tool, ToolContext


def _tasks(ctx: ToolContext) -> TaskStore:
    if ctx.store is None:
        raise ToolExecutionError(
            "task storage is not available in this context; the runtime did "
            "not provide a database"
        )
    return TaskStore(ctx.store)


# --- add -------------------------------------------------------------------


class AddTaskInput(BaseModel):
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
        return _tasks(ctx).add(
            args.title,
            notes=args.notes,
            priority=args.priority,
            due_date=args.due_date,
        )


# --- list ------------------------------------------------------------------


class ListTasksInput(BaseModel):
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
    description = (
        "List the user's tasks, most important first. Use this before "
        "answering any question about what the user has to do."
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


class UpdateTaskInput(BaseModel):
    id: int = Field(description="The task's numeric id, from list_tasks.")
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


class UpdateTaskTool(Tool):
    name = "update_task"
    description = (
        "Change an existing task. Only the fields you provide are modified. "
        "To mark a task finished, prefer complete_task."
    )
    Input = UpdateTaskInput
    Output = Task
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: UpdateTaskInput) -> str:  # type: ignore[override]
        return f"task #{args.id}"

    def run(self, args: UpdateTaskInput, ctx: ToolContext) -> Task:  # type: ignore[override]
        try:
            return _tasks(ctx).update(
                args.id,
                title=args.title,
                notes=args.notes,
                status=args.status,
                priority=args.priority,
                due_date=args.due_date,
            )
        except TaskNotFoundError as exc:
            raise ToolExecutionError(f"{exc}. Use list_tasks to see valid ids.") from exc


# --- complete --------------------------------------------------------------


class CompleteTaskInput(BaseModel):
    id: int = Field(description="The task's numeric id, from list_tasks.")


class CompleteTaskTool(Tool):
    name = "complete_task"
    description = "Mark a task as done. Use this when the user says they finished something."
    Input = CompleteTaskInput
    Output = Task
    permission = PermissionLevel.WRITE
    timeout_s = 10.0

    def describe_resource(self, args: CompleteTaskInput) -> str:  # type: ignore[override]
        return f"task #{args.id}"

    def run(self, args: CompleteTaskInput, ctx: ToolContext) -> Task:  # type: ignore[override]
        try:
            return _tasks(ctx).complete(args.id)
        except TaskNotFoundError as exc:
            raise ToolExecutionError(f"{exc}. Use list_tasks to see valid ids.") from exc
