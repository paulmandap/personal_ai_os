"""The Master Agent.

The point of these tests is that the Master needs no orchestration engine of
its own: it is a `BaseAgent` whose only tool happens to run another agent
(ADR-012). If any of this required new loop machinery, the loop was too
specific.
"""

from __future__ import annotations

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.agents.builtin.master import MasterAgent
from personal_ai_os.agents.builtin.task_agent import TaskAgent
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.core.types import Role
from personal_ai_os.models.fake import ScriptedModel, text_response, tool_call_response
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import AllowAllBroker
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.registry import default_registry

ROSTER = {
    "master": "Coordinates work.",
    "task_agent": "Manages the user's tasks.",
    "ping": "Verifies tool calling.",
}


def master_spec() -> AgentSpec:
    return AgentSpec(
        name="master",
        description="Coordinates work.",
        entrypoint="personal_ai_os.agents.builtin.master:MasterAgent",
        tools=["delegate"],
        permissions=[PermissionLevel.READ],
        max_iterations=6,
    )


def sub_result(ok: bool = True, output: str = "task added", error: str | None = None):
    return AgentResult(
        ok=ok,
        agent="task_agent",
        run_id="r",
        stop_reason=StopReason.ANSWERED if ok else StopReason.MAX_ITERATIONS,
        output=output,
        error=error,
        iterations=2,
        tool_calls=1,
    )


def build_master(responses, *, delegate=None, trace=None) -> MasterAgent:
    return MasterAgent(
        master_spec(),
        model=ScriptedModel(responses),
        tools=default_registry(),
        broker=AllowAllBroker(),
        context=ToolContext(
            agent="master",
            run_id="r",
            delegate=delegate or (lambda a, o: sub_result()),
            agent_roster=ROSTER,
            call_stack=("master",),
            max_delegation_depth=2,
        ),
        trace=trace,
    )


class TestPrompt:
    def test_roster_lists_the_other_agents(self):
        prompt = build_master([text_response("hi")]).system_prompt()
        assert "task_agent: Manages the user's tasks." in prompt
        assert "ping: Verifies tool calling." in prompt

    def test_master_excludes_itself_from_its_own_roster(self):
        """Otherwise the model eventually tries it and burns an iteration."""
        prompt = build_master([text_response("hi")]).system_prompt()
        assert "- master:" not in prompt

    def test_empty_roster_says_so(self):
        agent = build_master([text_response("hi")])
        agent.context.agent_roster = {"master": "me"}
        assert "none available" in agent.system_prompt()


class TestDelegation:
    def test_delegates_then_answers(self):
        agent = build_master(
            [
                tool_call_response(
                    "delegate", {"agent": "task_agent", "objective": "Add a task"}
                ),
                text_response("I added the task."),
            ]
        )
        result = agent.run("Add a task to review the code")
        assert result.ok
        assert result.output == "I added the task."
        assert result.tool_calls == 1

    def test_the_sub_agents_output_reaches_the_master(self):
        agent = build_master(
            [
                tool_call_response(
                    "delegate", {"agent": "task_agent", "objective": "x"}
                ),
                text_response("done"),
            ],
            delegate=lambda a, o: sub_result(output="3 tasks on your list"),
        )
        result = agent.run("what is on my list?")
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert "3 tasks on your list" in observation.content

    def test_the_objective_is_passed_through_verbatim(self):
        seen: list[str] = []
        agent = build_master(
            [
                tool_call_response(
                    "delegate",
                    {"agent": "task_agent", "objective": "Add 'buy milk' due Friday"},
                ),
                text_response("ok"),
            ],
            delegate=lambda a, o: seen.append(o) or sub_result(),
        )
        agent.run("remember to buy milk")
        assert seen == ["Add 'buy milk' due Friday"]

    def test_a_failed_delegation_does_not_kill_the_run(self):
        agent = build_master(
            [
                tool_call_response(
                    "delegate", {"agent": "task_agent", "objective": "x"}
                ),
                text_response("I could not complete that."),
            ],
            delegate=lambda a, o: sub_result(ok=False, output="", error="store offline"),
        )
        result = agent.run("do something")
        assert result.ok
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert "store offline" in observation.content

    def test_master_can_delegate_more_than_once(self):
        agent = build_master(
            [
                tool_call_response(
                    "delegate", {"agent": "task_agent", "objective": "add"},
                    call_id="c1",
                ),
                tool_call_response(
                    "delegate", {"agent": "task_agent", "objective": "list"},
                    call_id="c2",
                ),
                text_response("both done"),
            ]
        )
        result = agent.run("add a task then list them")
        assert result.ok
        assert result.tool_calls == 2


class TestOnlyDelegateIsAvailable:
    def test_master_is_offered_no_other_tools(self):
        """Delegation discipline is enforced by reach, not by prompt wording."""
        model = ScriptedModel([text_response("hi")])
        agent = build_master([])
        agent.model = model
        agent.run("hello")
        assert model.calls[0]["tool_names"] == ["delegate"]

    def test_calling_an_undeclared_tool_is_refused(self):
        agent = build_master(
            [
                tool_call_response("add_task", {"title": "sneaky"}),
                text_response("ok"),
            ]
        )
        result = agent.run("add a task directly")
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert "not available to this agent" in observation.content


class TestTracing:
    def test_events_are_stamped_with_agent_and_depth(self):
        """Sub-agents share the parent's trace, so every event must say whose
        it is -- otherwise a nested run reads as one flat sequence."""
        trace = RunTrace.disabled(agent="master")
        agent = build_master([text_response("hi")], trace=trace)
        agent.run("hello")
        for event in trace.events:
            assert event.data["agent"] == "master"
            assert event.data["depth"] == 0


class TestTaskAgentPrompt:
    def test_todays_date_is_injected(self):
        """Without it, a model asked for 'due Friday' invents a date."""
        from datetime import UTC, datetime

        agent = TaskAgent(
            AgentSpec(name="task_agent", description="d"),
            model=ScriptedModel([]),
            tools=default_registry(),
            broker=AllowAllBroker(),
            context=ToolContext(),
        )
        assert datetime.now(UTC).strftime("%Y-%m-%d") in agent.system_prompt()

    def test_prompt_forbids_inventing_attributes(self):
        """Observed: the 7B invented a due date and priority nobody asked for."""
        agent = TaskAgent(
            AgentSpec(name="task_agent", description="d"),
            model=ScriptedModel([]),
            tools=default_registry(),
            broker=AllowAllBroker(),
            context=ToolContext(),
        )
        assert "Do not invent" in agent.system_prompt()
