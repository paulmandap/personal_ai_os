"""Live tests against a real Ollama server.

Run with ``pytest -m integration``. Skipped automatically when the server is
unreachable or the model is not pulled, so a fresh clone never fails here for
environmental reasons.

These are the tests that would have caught a wire-format change. Everything
else in the suite runs against a translation layer that could, in principle,
be translating to a dialect nobody speaks any more.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from personal_ai_os.agents.base import AgentResult, BaseAgent, StopReason
from personal_ai_os.agents.builtin.master import MasterAgent
from personal_ai_os.agents.builtin.task_agent import TaskAgent
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.core.errors import ToolExecutionError
from personal_ai_os.core.types import FinishReason, Message, ToolSchema
from personal_ai_os.memory.tasks import TaskStore
from personal_ai_os.models.ollama import OllamaModel
from personal_ai_os.observability.trace import RunTrace
from personal_ai_os.permissions.broker import PolicyBroker, RecordingBroker
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.delegate import DelegateTool
from personal_ai_os.tools.registry import default_registry

pytestmark = pytest.mark.integration

BASE_URL = "http://localhost:11434"
SMALL_MODEL = "qwen2.5:3b-instruct"
MEDIUM_MODEL = "qwen2.5:7b-instruct"

WEATHER_TOOL = ToolSchema(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
)


def _installed_models() -> set[str]:
    try:
        reply = httpx.get(f"{BASE_URL}/api/tags", timeout=5.0)
        reply.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL):
        return set()
    return {m.get("model") for m in reply.json().get("models", [])}


INSTALLED = _installed_models()

requires_small = pytest.mark.skipif(
    SMALL_MODEL not in INSTALLED, reason=f"{SMALL_MODEL} not installed"
)
requires_medium = pytest.mark.skipif(
    MEDIUM_MODEL not in INSTALLED, reason=f"{MEDIUM_MODEL} not installed"
)


def unload(model: str) -> None:
    """Evict a model from VRAM (``keep_alive: 0``).

    Not hygiene -- necessity. On an 8 GB card the 3B and 7B do not co-reside,
    and letting Ollama swap them under load made its runner die mid-request
    with `wsarecv: connection forcibly closed`. Evicting deliberately between
    model sizes turns an intermittent crash into a predictable pause.
    """
    try:
        httpx.post(
            f"{BASE_URL}/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=30.0,
        )
    except httpx.HTTPError:  # pragma: no cover - best effort
        pass


@pytest.fixture(scope="class")
def fresh_vram():
    """Free the small model before a class that needs the larger one."""
    unload(SMALL_MODEL)
    yield
    unload(MEDIUM_MODEL)


@requires_small
class TestBasicGeneration:
    def test_generates_text(self):
        model = OllamaModel(SMALL_MODEL, base_url=BASE_URL, temperature=0.0)
        response = model.generate(
            [Message.user("Reply with the single word: ready")], max_tokens=10
        )
        assert "ready" in response.message.content.lower()
        assert response.finish_reason is FinishReason.STOP
        assert response.provider == "ollama"

    def test_usage_is_populated(self):
        model = OllamaModel(SMALL_MODEL, base_url=BASE_URL, temperature=0.0)
        usage = model.generate([Message.user("Say hi")], max_tokens=10).usage
        assert usage is not None
        assert usage.prompt_tokens and usage.prompt_tokens > 0
        assert usage.completion_tokens and usage.completion_tokens > 0

    def test_health_sees_an_installed_model(self):
        health = OllamaModel(SMALL_MODEL, base_url=BASE_URL).health()
        assert health.ok

    def test_health_sees_a_missing_model_without_raising(self):
        health = OllamaModel("definitely-not-a-real-model", base_url=BASE_URL).health()
        assert health.server_reachable
        assert not health.model_available


@requires_medium
@pytest.mark.usefixtures("fresh_vram")
class TestToolCalling:
    """Validates the wire format that all the mocked tests assume."""

    def test_model_requests_a_tool(self):
        model = OllamaModel(MEDIUM_MODEL, base_url=BASE_URL, temperature=0.0)
        response = model.generate(
            [Message.user("What is the weather in Manila? Use the tool.")],
            tools=[WEATHER_TOOL],
        )
        assert response.has_tool_calls
        call = response.message.tool_calls[0]
        assert call.name == "get_weather"
        assert isinstance(call.arguments, dict)
        assert "manila" in str(call.arguments).lower()
        assert call.id

    def test_full_round_trip_with_a_tool_result(self):
        model = OllamaModel(MEDIUM_MODEL, base_url=BASE_URL, temperature=0.0)
        first = model.generate(
            [Message.user("What is the weather in Manila? Use the tool.")],
            tools=[WEATHER_TOOL],
        )
        call = first.message.tool_calls[0]

        final = model.generate(
            [
                Message.user("What is the weather in Manila? Use the tool."),
                first.message,
                Message.tool(
                    '{"city": "Manila", "temp_c": 31}',
                    name=call.name,
                    tool_call_id=call.id,
                ),
            ],
            tools=[WEATHER_TOOL],
        )
        assert "31" in final.message.content


@requires_medium
@pytest.mark.usefixtures("fresh_vram")
class TestAgentEndToEnd:
    def test_agent_reads_a_real_file_through_the_permission_gate(self, tool_context):
        """The whole vertical slice against a real local model."""
        spec = AgentSpec(
            name="live_reader",
            description="Reads workspace files.",
            tools=["read_file", "list_dir"],
            permissions=[PermissionLevel.READ],
            max_iterations=6,
        )
        broker = RecordingBroker(
            PolicyBroker({PermissionLevel.READ: "auto"}, interactive=False)
        )
        agent = BaseAgent(
            spec,
            model=OllamaModel(MEDIUM_MODEL, base_url=BASE_URL, temperature=0.0),
            tools=default_registry(),
            broker=broker,
            context=tool_context,
            system_prompt=(
                "You inspect files in a workspace. Use read_file to read them. "
                f"The workspace root is {tool_context.workspace_root}. "
                "Answer using only what the tools return."
            ),
        )

        result = agent.run("What is the value of VALUE in src/app.py?")

        assert result.ok, f"run failed: {result.error}"
        assert result.stop_reason is StopReason.ANSWERED
        assert "42" in result.output
        # The gate was actually consulted, not bypassed.
        assert broker.requests
        assert all(d.granted for d in broker.decisions)

    def test_denied_permission_is_survivable_against_a_real_model(self, tool_context):
        """A refusal must not crash the run, even with a real model driving."""
        from personal_ai_os.permissions.broker import DenyAllBroker

        spec = AgentSpec(
            name="blocked_reader",
            description="Tries to read files.",
            tools=["read_file"],
            permissions=[PermissionLevel.READ],
            max_iterations=4,
        )
        agent = BaseAgent(
            spec,
            model=OllamaModel(MEDIUM_MODEL, base_url=BASE_URL, temperature=0.0),
            tools=default_registry(),
            broker=DenyAllBroker("not allowed in this test"),
            context=tool_context,
            system_prompt=(
                "You inspect files. If a tool is denied, say plainly that you "
                "could not access it. Do not invent file contents."
            ),
        )

        result = agent.run("Read README.md and tell me the first heading.")
        # It may answer or exhaust its turns; it must not raise, and it must
        # never have executed the denied tool.
        assert result.stop_reason in {StopReason.ANSWERED, StopReason.MAX_ITERATIONS}


@requires_medium
@pytest.mark.usefixtures("fresh_vram")
class TestDelegationEndToEnd:
    """The Phase 2 vertical slice against a real local model.

    Both agents sit on the medium tier deliberately: they share one resident
    model, so delegation costs no VRAM swap on an 8 GB card.
    """

    @staticmethod
    def _task_agent(tool_context, broker) -> BaseAgent:
        spec = AgentSpec(
            name="task_agent",
            description="Manages the user's tasks.",
            tools=["list_tasks", "add_task", "update_task", "complete_task"],
            permissions=[PermissionLevel.READ, PermissionLevel.WRITE],
            max_iterations=8,
        )
        return TaskAgent(
            spec,
            model=OllamaModel(MEDIUM_MODEL, base_url=BASE_URL, temperature=0.0),
            tools=default_registry(),
            broker=broker,
            context=tool_context,
        )

    def test_task_agent_really_persists_a_task(self, tool_context, store):
        broker = PolicyBroker(
            {PermissionLevel.READ: "auto", PermissionLevel.WRITE: "auto"},
            interactive=False,
        )
        result = self._task_agent(tool_context, broker).run(
            "Add a task titled 'Buy oat milk'. No due date, normal priority."
        )

        assert result.ok, f"run failed: {result.error}"
        # Verified against the database, not against what the model claimed.
        titles = [t.title for t in TaskStore(store).list()]
        assert any("oat milk" in t.lower() for t in titles), titles

    def test_master_delegates_and_the_work_actually_happens(
        self, tool_context, store
    ):
        broker = PolicyBroker(
            {PermissionLevel.READ: "auto", PermissionLevel.WRITE: "auto"},
            interactive=False,
        )
        trace = RunTrace.disabled(agent="master")
        delegated: list[str] = []

        def delegate(agent_name: str, objective: str) -> AgentResult:
            delegated.append(agent_name)
            sub_ctx = replace(
                tool_context, agent=agent_name, depth=1, call_stack=("master", agent_name)
            )
            return self._task_agent(sub_ctx, broker).run(objective)

        master_spec = AgentSpec(
            name="master",
            description="Coordinates work.",
            tools=["delegate"],
            permissions=[PermissionLevel.READ],
            max_iterations=6,
        )
        master = MasterAgent(
            master_spec,
            model=OllamaModel(MEDIUM_MODEL, base_url=BASE_URL, temperature=0.0),
            tools=default_registry(),
            broker=broker,
            context=replace(
                tool_context,
                agent="master",
                delegate=delegate,
                call_stack=("master",),
                agent_roster={
                    "master": "Coordinates work.",
                    "task_agent": "Manages the user's tasks: add, list, complete.",
                },
            ),
            trace=trace,
        )

        result = master.run("Please add a task to renew my passport.")

        assert result.ok, f"master failed: {result.error}"
        assert delegated == ["task_agent"], f"delegated to: {delegated}"
        titles = [t.title.lower() for t in TaskStore(store).list()]
        assert any("passport" in t for t in titles), titles

    def test_depth_limit_holds_against_a_real_model(self, tool_context):
        """A model cannot talk its way past the guard, whatever it decides."""
        exhausted = replace(
            tool_context, depth=2, max_delegation_depth=2, call_stack=("master",),
            delegate=lambda a, o: pytest.fail("sub-agent must not run"),
        )
        tool = DelegateTool()
        with pytest.raises(ToolExecutionError, match="depth limit"):
            tool.run(
                tool.validate_input({"agent": "task_agent", "objective": "go"}),
                exhausted,
            )
