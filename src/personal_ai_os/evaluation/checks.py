"""Checks -- the assertions a case can make about one run.

Every check reads from something that already exists: the ``AgentResult``, the
trace events, or the database. Nothing here asks a model for an opinion
(ADR-020).

The set is deliberately closed and small. It grew from what the first two cases
actually needed, not from imagining a query language. Add a check when a case
needs one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import TaskStore
from personal_ai_os.observability.trace import Events, TraceEvent


@dataclass
class RunContext:
    """Everything a check may inspect about one completed run."""

    result: AgentResult
    events: list[TraceEvent] = field(default_factory=list)
    store: Store | None = None

    # --- trace helpers, shared by several checks ---

    def of_type(self, event_type: str) -> list[TraceEvent]:
        return [e for e in self.events if e.type == event_type]

    def tools_requested(self) -> list[str]:
        """Tool names in the order the model asked for them."""
        return [
            str(e.data.get("tool"))
            for e in self.of_type(Events.TOOL_REQUESTED)
            if e.data.get("tool")
        ]

    def failed_tool_results(self) -> list[TraceEvent]:
        return [e for e in self.of_type(Events.TOOL_RESULT) if not e.data.get("ok")]

    def invalid_argument_count(self) -> int:
        """Tool calls rejected by input validation before ever running."""
        return sum(
            1
            for e in self.failed_tool_results()
            if "invalid arguments" in str(e.data.get("error", "")).lower()
        )

    def permission_denials(self) -> list[TraceEvent]:
        return [
            e
            for e in self.of_type(Events.PERMISSION_DECISION)
            if not e.data.get("granted")
        ]

    def tasks(self) -> TaskStore | None:
        return TaskStore(self.store) if self.store is not None else None


class CheckOutcome(BaseModel):
    """One check's verdict, with enough detail to explain a failure."""

    name: str
    passed: bool
    detail: str = ""
    #: Name plus parameters, e.g. ``task_matching({'title': 'oat milk', ...})``.
    #: Set by the runner. A case can use the same check twice with different
    #: parameters, and without this they collapse into one indistinguishable
    #: row in the report -- which hides which of the two actually failed.
    label: str = ""

    @property
    def key(self) -> str:
        return self.label or self.name


CheckFn = Callable[[RunContext, dict[str, Any]], CheckOutcome]

#: name -> implementation. Populated by the @check decorator below.
CHECKS: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return register


def _outcome(name: str, passed: bool, detail: str = "") -> CheckOutcome:
    return CheckOutcome(name=name, passed=passed, detail=detail)


def known_checks() -> list[str]:
    return sorted(CHECKS)


def run_check(name: str, ctx: RunContext, params: dict[str, Any]) -> CheckOutcome:
    fn = CHECKS.get(name)
    if fn is None:
        return _outcome(name, False, f"unknown check {name!r}")
    try:
        return fn(ctx, params)
    except Exception as exc:  # a broken check must not abort a whole suite
        return _outcome(name, False, f"check raised {type(exc).__name__}: {exc}")


# --- result-level checks ---------------------------------------------------


@check("answered")
def _answered(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    ok = ctx.result.ok and ctx.result.stop_reason is StopReason.ANSWERED
    return _outcome(
        "answered", ok, "" if ok else f"stop_reason={ctx.result.stop_reason.value}"
    )


@check("max_iterations_under")
def _max_iterations_under(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    limit = int(p["value"])
    used = ctx.result.iterations
    return _outcome(
        "max_iterations_under", used <= limit, f"used {used}, limit {limit}"
    )


@check("output_contains")
def _output_contains(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    needle = str(p["value"]).lower()
    found = needle in ctx.result.output.lower()
    return _outcome("output_contains", found, f"looking for {needle!r}")


@check("output_not_contains")
def _output_not_contains(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    needle = str(p["value"]).lower()
    found = needle in ctx.result.output.lower()
    return _outcome("output_not_contains", not found, f"found {needle!r}" if found else "")


# --- trace-level checks ----------------------------------------------------


@check("called_tool")
def _called_tool(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    wanted = str(p["value"])
    called = ctx.tools_requested()
    return _outcome("called_tool", wanted in called, f"called {called}")


@check("did_not_call_tool")
def _did_not_call_tool(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    unwanted = str(p["value"])
    called = ctx.tools_requested()
    return _outcome("did_not_call_tool", unwanted not in called, f"called {called}")


@check("first_tool_is")
def _first_tool_is(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    wanted = str(p["value"])
    called = ctx.tools_requested()
    first = called[0] if called else None
    return _outcome(
        "first_tool_is", first == wanted, f"first was {first!r}, wanted {wanted!r}"
    )


@check("no_invalid_arguments")
def _no_invalid_arguments(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    n = ctx.invalid_argument_count()
    return _outcome("no_invalid_arguments", n == 0, f"{n} rejected call(s)")


@check("no_permission_denials")
def _no_permission_denials(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    denials = ctx.permission_denials()
    return _outcome(
        "no_permission_denials",
        not denials,
        f"{len(denials)} denial(s): "
        + ", ".join(str(e.data.get("tool")) for e in denials),
    )


@check("recovered_after_error")
def _recovered_after_error(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    """Did tool failures derail the run?

    Passes when there were no failures at all, or when there were and the agent
    still reached an answer. This is the property that matters: errors are
    expected from a small model (ADR-008), and what is being measured is whether
    they end the run.
    """
    failures = ctx.failed_tool_results()
    answered = ctx.result.stop_reason is StopReason.ANSWERED
    if not failures:
        return _outcome("recovered_after_error", True, "no tool errors occurred")
    return _outcome(
        "recovered_after_error",
        answered,
        f"{len(failures)} tool error(s), "
        + ("recovered" if answered else "did not recover"),
    )


@check("delegated_to")
def _delegated_to(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    wanted = str(p["value"])
    children = [
        str(e.data.get("child_agent")) for e in ctx.of_type(Events.DELEGATE_START)
    ]
    return _outcome("delegated_to", wanted in children, f"delegated to {children}")


# --- store-level checks ----------------------------------------------------
#
# These matter more than they look. For the embellishment question the issue is
# not what the model *said* it did, but what actually landed in the database.


@check("task_count")
def _task_count(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("task_count", False, "no store available")
    expected = int(p["value"])
    actual = tasks.count()
    return _outcome("task_count", actual == expected, f"{actual}, expected {expected}")


def _only_task(ctx: RunContext, name: str) -> tuple[Any, CheckOutcome | None]:
    tasks = ctx.tasks()
    if tasks is None:
        return None, _outcome(name, False, "no store available")
    found = tasks.list(include_done=True, limit=200)
    if len(found) != 1:
        return None, _outcome(name, False, f"expected exactly 1 task, found {len(found)}")
    return found[0], None


@check("task_field_absent")
def _task_field_absent(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """The created task must NOT have this field set.

    This is the embellishment check: a due date the user never gave is worse
    than no due date at all.
    """
    field_name = str(p["value"])
    task, failure = _only_task(ctx, "task_field_absent")
    if failure:
        return failure
    value = getattr(task, field_name, None)
    return _outcome(
        "task_field_absent",
        value in (None, ""),
        f"{field_name}={value!r}" if value else f"{field_name} correctly unset",
    )


@check("task_field_is")
def _task_field_is(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    field_name = str(p["field"])
    expected = p["value"]
    task, failure = _only_task(ctx, "task_field_is")
    if failure:
        return failure
    actual = getattr(task, field_name, None)
    actual_value = getattr(actual, "value", actual)  # unwrap enums
    return _outcome(
        "task_field_is",
        str(actual_value) == str(expected),
        f"{field_name}={actual_value!r}, expected {expected!r}",
    )


@check("task_matching")
def _task_matching(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Find the task whose title contains `title`, assert one of its fields.

    Needed wherever more than one task exists, which is most realistic cases.
    """
    needle = str(p["title"]).lower()
    field_name = str(p["field"])
    expected = p["value"]

    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("task_matching", False, "no store available")

    found = [t for t in tasks.list(include_done=True, limit=200) if needle in t.title.lower()]
    if len(found) != 1:
        return _outcome(
            "task_matching", False, f"{len(found)} task(s) matched title {needle!r}"
        )

    actual = getattr(found[0], field_name, None)
    actual_value = getattr(actual, "value", actual)
    return _outcome(
        "task_matching",
        str(actual_value) == str(expected),
        f"{needle!r}.{field_name}={actual_value!r}, expected {expected!r}",
    )


@check("task_title_contains")
def _task_title_contains(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    needle = str(p["value"]).lower()
    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("task_title_contains", False, "no store available")
    titles = [t.title for t in tasks.list(include_done=True, limit=200)]
    found = any(needle in t.lower() for t in titles)
    return _outcome("task_title_contains", found, f"titles: {titles}")
