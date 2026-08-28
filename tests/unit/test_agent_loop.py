"""The agent loop.

Everything here runs against :class:`ScriptedModel` -- no GPU, no network, no
Ollama. That is the point: if these tests need a model server, the
architecture does not really own the workflow.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from personal_ai_os.agents.base import BaseAgent, StopReason
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.core.errors import ModelUnavailableError
from personal_ai_os.core.types import Message, Role
from personal_ai_os.models.fake import ScriptedModel, text_response, tool_call_response
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import (
    AllowAllBroker,
    DenyAllBroker,
    PolicyBroker,
    RecordingBroker,
)
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext
from personal_ai_os.tools.registry import ToolRegistry, default_registry


class SpyInput(BaseModel):
    value: str = "x"


class SpyTool(Tool):
    """Records whether it was ever actually executed."""

    name = "spy"
    description = "Records invocations."
    Input = SpyInput
    Output = SpyInput
    permission = PermissionLevel.DELETE  # deliberately consequential

    def __init__(self) -> None:
        self.invocations: list[SpyInput] = []

    def run(self, args: SpyInput, ctx: ToolContext) -> SpyInput:  # type: ignore[override]
        self.invocations.append(args)
        return args


def make_agent(
    responses,
    *,
    tools: ToolRegistry | None = None,
    tool_names: list[str] | None = None,
    permissions: list[PermissionLevel] | None = None,
    broker=None,
    context: ToolContext,
    trace: RunTrace | None = None,
    max_iterations: int = 4,
) -> BaseAgent:
    spec = AgentSpec(
        name="test_agent",
        description="Agent under test.",
        tools=tool_names if tool_names is not None else ["read_file", "list_dir"],
        permissions=permissions or [PermissionLevel.READ],
        max_iterations=max_iterations,
    )
    return BaseAgent(
        spec,
        model=ScriptedModel(responses),
        tools=tools or default_registry(),
        broker=broker or AllowAllBroker(),
        context=context,
        trace=trace or RunTrace.disabled(agent="test_agent"),
    )


class TestPlainAnswer:
    def test_answers_without_calling_tools(self, tool_context):
        agent = make_agent([text_response("42")], context=tool_context)
        result = agent.run("What is the answer?")
        assert result.ok
        assert result.output == "42"
        assert result.stop_reason is StopReason.ANSWERED
        assert result.iterations == 1
        assert result.tool_calls == 0

    def test_transcript_starts_with_system_then_user(self, tool_context):
        agent = make_agent([text_response("hi")], context=tool_context)
        result = agent.run("hello")
        assert result.transcript[0].role is Role.SYSTEM
        assert result.transcript[1].role is Role.USER
        assert result.transcript[1].content == "hello"

    def test_history_is_inserted_between_system_and_objective(self, tool_context):
        agent = make_agent([text_response("ok")], context=tool_context)
        result = agent.run("now", history=[Message.user("before")])
        contents = [m.content for m in result.transcript]
        assert contents.index("before") < contents.index("now")


class TestToolUse:
    def test_calls_a_tool_then_answers(self, tool_context):
        agent = make_agent(
            [
                tool_call_response("read_file", {"path": "README.md"}),
                text_response("It is a test workspace."),
            ],
            context=tool_context,
        )
        result = agent.run("What does the README say?")
        assert result.ok
        assert result.tool_calls == 1
        assert result.iterations == 2

    def test_tool_result_is_fed_back_as_a_tool_message(self, tool_context):
        agent = make_agent(
            [
                tool_call_response("read_file", {"path": "README.md"}),
                text_response("done"),
            ],
            context=tool_context,
        )
        result = agent.run("read it")
        tool_messages = [m for m in result.transcript if m.role is Role.TOOL]
        assert len(tool_messages) == 1
        assert "Test Workspace" in tool_messages[0].content
        assert tool_messages[0].name == "read_file"

    def test_only_declared_tools_are_advertised(self, tool_context):
        model = ScriptedModel([text_response("ok")])
        spec = AgentSpec(
            name="a",
            description="d",
            tools=["read_file"],
            permissions=[PermissionLevel.READ],
        )
        BaseAgent(
            spec,
            model=model,
            tools=default_registry(),
            broker=AllowAllBroker(),
            context=tool_context,
        ).run("go")
        assert model.calls[0]["tool_names"] == ["read_file"]

    def test_an_agent_with_no_tools_is_offered_none(self, tool_context):
        model = ScriptedModel([text_response("ok")])
        spec = AgentSpec(name="a", description="d", tools=[])
        BaseAgent(
            spec,
            model=model,
            tools=default_registry(),
            broker=AllowAllBroker(),
            context=tool_context,
        ).run("go")
        assert model.calls[0]["tools"] == []


class TestRecoverableFailures:
    """Bad arguments and refusals come back as observations, so the model can
    correct itself. A 3B model will make these mistakes; ending the run on the
    first one would be the wrong behaviour."""

    def test_invalid_arguments_are_reported_back_and_recovered_from(self, tool_context):
        agent = make_agent(
            [
                tool_call_response("read_file", {"path": "x", "max_bytes": 10_000_000}),
                tool_call_response("read_file", {"path": "README.md"}),
                text_response("recovered"),
            ],
            context=tool_context,
        )
        result = agent.run("read something")
        assert result.ok
        assert result.output == "recovered"
        first_observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert first_observation.content.startswith("ERROR:")
        assert "max_bytes" in first_observation.content

    def test_calling_an_undeclared_tool_is_reported_back(self, tool_context):
        agent = make_agent(
            [tool_call_response("list_dir", {}), text_response("ok")],
            tool_names=["read_file"],
            context=tool_context,
        )
        result = agent.run("go")
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert "not available to this agent" in observation.content
        assert result.ok

    def test_tool_execution_error_is_reported_back(self, tool_context):
        agent = make_agent(
            [
                tool_call_response("read_file", {"path": "does_not_exist.txt"}),
                text_response("the file is missing"),
            ],
            context=tool_context,
        )
        result = agent.run("read it")
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert observation.content.startswith("ERROR:")
        assert result.ok

    def test_path_escape_is_reported_back_not_raised(self, tool_context):
        agent = make_agent(
            [
                tool_call_response("read_file", {"path": "../../../etc/passwd"}),
                text_response("cannot reach that"),
            ],
            context=tool_context,
        )
        result = agent.run("read it")
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert "outside every allowed root" in observation.content
        assert result.ok


class TestPermissionGate:
    def test_a_denied_tool_is_never_executed(self, tool_context):
        """The core safety property of the whole system."""
        spy = SpyTool()
        agent = make_agent(
            [tool_call_response("spy", {"value": "a"}), text_response("understood")],
            tools=ToolRegistry([spy]),
            tool_names=["spy"],
            permissions=[PermissionLevel.DELETE],
            broker=DenyAllBroker(),
            context=tool_context,
        )
        result = agent.run("do the thing")
        assert spy.invocations == []
        assert result.ok

    def test_denial_is_explained_to_the_model(self, tool_context):
        agent = make_agent(
            [tool_call_response("read_file", {"path": "README.md"}), text_response("ok")],
            broker=DenyAllBroker("policy says no"),
            context=tool_context,
        )
        result = agent.run("read it")
        observation = [m for m in result.transcript if m.role is Role.TOOL][0]
        assert observation.content.startswith("DENIED:")
        assert "policy says no" in observation.content

    def test_the_broker_is_consulted_before_execution(self, tool_context):
        spy = SpyTool()
        broker = RecordingBroker(AllowAllBroker())
        agent = make_agent(
            [tool_call_response("spy", {"value": "a"}), text_response("ok")],
            tools=ToolRegistry([spy]),
            tool_names=["spy"],
            permissions=[PermissionLevel.DELETE],
            broker=broker,
            context=tool_context,
        )
        agent.run("go")
        assert len(broker.requests) == 1
        assert broker.requests[0].level is PermissionLevel.DELETE
        assert broker.requests[0].action == "spy"
        assert spy.invocations  # granted, so it ran

    def test_invalid_arguments_are_rejected_before_the_broker_is_asked(
        self, tool_context
    ):
        """No point prompting a human about a call that cannot run anyway."""
        broker = RecordingBroker(AllowAllBroker())
        agent = make_agent(
            [
                tool_call_response("read_file", {"wrong_field": 1}),
                text_response("ok"),
            ],
            broker=broker,
            context=tool_context,
        )
        agent.run("go")
        assert broker.requests == []

    def test_agent_level_approval_flag_forces_a_prompt(self, tool_context):
        prompts: list = []
        broker = PolicyBroker(
            {PermissionLevel.READ: "auto"},
            interactive=True,
            prompt=lambda r: bool(prompts.append(r)) or True,
        )
        spec = AgentSpec(
            name="careful",
            description="d",
            tools=["read_file"],
            permissions=[PermissionLevel.READ],
            requires_human_approval=True,
        )
        BaseAgent(
            spec,
            model=ScriptedModel(
                [
                    tool_call_response("read_file", {"path": "README.md"}),
                    text_response("ok"),
                ]
            ),
            tools=default_registry(),
            broker=broker,
            context=tool_context,
        ).run("read it")
        assert len(prompts) == 1


class TestEmptyResponse:
    """An empty turn is not an answer.

    Found by the delegation suite: qwen2.5:3b driving the Master returned no
    content and no tool call, and the harness scored it `answered` -- a run
    that produced literally nothing counted as a success.
    """

    def test_empty_content_with_no_tool_calls_is_a_failure(self, tool_context):
        agent = make_agent([text_response("")], context=tool_context)
        result = agent.run("do something")
        assert not result.ok
        assert result.stop_reason is StopReason.EMPTY_RESPONSE
        assert "nothing was produced" in (result.error or "")

    def test_whitespace_only_is_also_empty(self, tool_context):
        agent = make_agent([text_response("   \n  ")], context=tool_context)
        assert agent.run("go").stop_reason is StopReason.EMPTY_RESPONSE

    def test_a_real_answer_is_unaffected(self, tool_context):
        agent = make_agent([text_response("42")], context=tool_context)
        result = agent.run("what is the answer?")
        assert result.ok and result.stop_reason is StopReason.ANSWERED


class TestFatalFailures:
    def test_model_unavailable_ends_the_run(self, tool_context):
        """The model cannot be told that the model is down."""

        class DeadModel(ScriptedModel):
            def generate(self, *a, **kw):
                raise ModelUnavailableError("ollama is not running")

        spec = AgentSpec(name="a", description="d")
        result = BaseAgent(
            spec,
            model=DeadModel(),
            tools=default_registry(),
            broker=AllowAllBroker(),
            context=tool_context,
        ).run("go")
        assert not result.ok
        assert result.stop_reason is StopReason.MODEL_ERROR
        assert "ollama is not running" in (result.error or "")

    def test_max_iterations_is_reported_honestly(self, tool_context):
        agent = make_agent(
            [tool_call_response("read_file", {"path": "README.md"}) for _ in range(3)],
            context=tool_context,
            max_iterations=3,
        )
        result = agent.run("loop forever")
        assert not result.ok
        assert result.stop_reason is StopReason.MAX_ITERATIONS
        assert result.iterations == 3
        assert "3 iterations" in (result.error or "")


class TestTracing:
    def test_a_full_run_is_recorded_in_order(self, tool_context):
        trace = RunTrace.disabled(agent="test_agent")
        agent = make_agent(
            [
                tool_call_response("read_file", {"path": "README.md"}),
                text_response("done"),
            ],
            context=tool_context,
            trace=trace,
        )
        agent.run("read it")
        types = [e.type for e in trace.events]
        assert types == [
            Events.MODEL_REQUEST,
            Events.MODEL_RESPONSE,
            Events.TOOL_REQUESTED,
            Events.PERMISSION_DECISION,
            Events.TOOL_RESULT,
            Events.MODEL_REQUEST,
            Events.MODEL_RESPONSE,
        ]

    def test_permission_decisions_are_always_recorded(self, tool_context):
        trace = RunTrace.disabled(agent="test_agent")
        agent = make_agent(
            [tool_call_response("read_file", {"path": "README.md"}), text_response("ok")],
            broker=DenyAllBroker(),
            context=tool_context,
            trace=trace,
        )
        agent.run("go")
        decisions = [e for e in trace.events if e.type == Events.PERMISSION_DECISION]
        assert len(decisions) == 1
        assert decisions[0].data["granted"] is False


class TestScriptedModelItself:
    def test_running_out_of_script_says_so_clearly(self, tool_context):
        agent = make_agent(
            [tool_call_response("read_file", {"path": "README.md"})],
            context=tool_context,
        )
        with pytest.raises(RuntimeError, match="ran out of responses"):
            agent.run("go")
