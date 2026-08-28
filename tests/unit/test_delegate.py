"""Delegation: the Master Agent mechanism, and the guards that bound it.

The guards matter more than the happy path. A model that can spawn agents can
spawn them in a loop, and the thing standing between that and a wedged machine
is the depth limit and the cycle check -- not a permission prompt (ADR-014).
"""

from __future__ import annotations

import pytest

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.core.errors import AgentNotFoundError, ToolExecutionError
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.delegate import DelegateTool


def ok_result(agent: str = "task_agent", output: str = "done") -> AgentResult:
    return AgentResult(
        ok=True,
        agent=agent,
        run_id="r",
        stop_reason=StopReason.ANSWERED,
        output=output,
        iterations=2,
        tool_calls=1,
    )


def failed_result(agent: str = "task_agent", error: str = "it broke") -> AgentResult:
    return AgentResult(
        ok=False,
        agent=agent,
        run_id="r",
        stop_reason=StopReason.MAX_ITERATIONS,
        output="",
        error=error,
        iterations=6,
    )


def ctx_with(delegate=None, *, depth=0, stack=("master",), max_depth=2) -> ToolContext:
    return ToolContext(
        agent=stack[-1] if stack else "master",
        run_id="r",
        delegate=delegate,
        depth=depth,
        call_stack=stack,
        max_delegation_depth=max_depth,
    )


def run(tool: DelegateTool, args: dict, ctx: ToolContext):
    return tool.run(tool.validate_input(args), ctx)


class TestHappyPath:
    def test_returns_the_sub_agents_result(self):
        calls: list[tuple[str, str]] = []

        def delegate(agent: str, objective: str) -> AgentResult:
            calls.append((agent, objective))
            return ok_result(output="task added")

        result = run(
            DelegateTool(),
            {"agent": "task_agent", "objective": "Add a task"},
            ctx_with(delegate),
        )
        assert calls == [("task_agent", "Add a task")]
        assert result.ok
        assert result.output == "task added"
        assert result.iterations == 2

    def test_delegation_is_read_level(self):
        """Consequences are gated inside the sub-agent, where they happen."""
        assert DelegateTool().permission is PermissionLevel.READ

    def test_approval_prompt_names_the_target_agent(self):
        tool = DelegateTool()
        args = tool.validate_input({"agent": "finance", "objective": "x"})
        assert tool.describe_resource(args) == "finance"


class TestFailureIsAnObservation:
    def test_a_failed_sub_agent_returns_ok_false_rather_than_raising(self):
        """The caller decides what to do about it -- that is the whole point."""
        result = run(
            DelegateTool(),
            {"agent": "task_agent", "objective": "x"},
            ctx_with(lambda a, o: failed_result()),
        )
        assert result.ok is False
        assert result.error == "it broke"

    def test_unknown_agent_surfaces_the_valid_names(self):
        def delegate(agent: str, objective: str) -> AgentResult:
            raise AgentNotFoundError(
                "no agent named 'nope'. Registered agents: master, ping, task_agent"
            )

        with pytest.raises(ToolExecutionError, match="task_agent"):
            run(
                DelegateTool(),
                {"agent": "nope", "objective": "x"},
                ctx_with(delegate),
            )


class TestGuards:
    def test_depth_limit_is_enforced(self):
        spy: list[str] = []
        ctx = ctx_with(lambda a, o: spy.append(a) or ok_result(), depth=2, max_depth=2)
        with pytest.raises(ToolExecutionError, match="depth limit"):
            run(DelegateTool(), {"agent": "task_agent", "objective": "x"}, ctx)
        assert spy == [], "the sub-agent must not run once the limit is hit"

    def test_one_below_the_limit_still_works(self):
        ctx = ctx_with(lambda a, o: ok_result(), depth=1, max_depth=2)
        assert run(DelegateTool(), {"agent": "task_agent", "objective": "x"}, ctx).ok

    def test_self_delegation_is_refused(self):
        spy: list[str] = []
        ctx = ctx_with(lambda a, o: spy.append(a) or ok_result(), stack=("master",))
        with pytest.raises(ToolExecutionError, match="already running"):
            run(DelegateTool(), {"agent": "master", "objective": "x"}, ctx)
        assert spy == []

    def test_a_longer_cycle_is_refused(self):
        """master -> task_agent -> master must not be possible."""
        ctx = ctx_with(
            lambda a, o: ok_result(), depth=1, stack=("master", "task_agent")
        )
        with pytest.raises(ToolExecutionError, match="already running"):
            run(DelegateTool(), {"agent": "master", "objective": "x"}, ctx)

    def test_the_refusal_shows_the_chain(self):
        ctx = ctx_with(
            lambda a, o: ok_result(), depth=1, stack=("master", "task_agent")
        )
        with pytest.raises(ToolExecutionError, match="master -> task_agent"):
            run(DelegateTool(), {"agent": "master", "objective": "x"}, ctx)

    def test_a_sibling_agent_not_on_the_stack_is_allowed(self):
        ctx = ctx_with(lambda a, o: ok_result(), depth=1, stack=("master", "ping"))
        assert run(DelegateTool(), {"agent": "task_agent", "objective": "x"}, ctx).ok


class TestMissingCapability:
    def test_no_runner_reports_clearly(self):
        with pytest.raises(ToolExecutionError, match="not available"):
            run(
                DelegateTool(),
                {"agent": "task_agent", "objective": "x"},
                ctx_with(None),
            )


class TestSchema:
    def test_objective_field_warns_that_context_is_not_shared(self):
        """The most common delegation failure is a one-word objective."""
        props = DelegateTool().schema().parameters["properties"]
        assert "cannot see" in props["objective"]["description"]

    def test_empty_objective_is_rejected(self):
        from personal_ai_os.core.errors import ToolInputError

        with pytest.raises(ToolInputError, match="objective"):
            DelegateTool().validate_input({"agent": "x", "objective": ""})
