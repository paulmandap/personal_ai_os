"""The agent loop.

This is the smallest complete implementation of the pattern the whole platform
rests on: think, act, observe, repeat -- with a permission gate between
"act" and "observe".

Two design choices carry most of the weight.

**Failures are observations, not exceptions.** A malformed tool argument or a
refused permission is written back into the conversation as a tool result, so
the model sees what went wrong and can correct itself. Raising instead would
end the run at the first mistake, which is exactly the wrong behaviour for a
3B model that will make several. Only failures the model *cannot* act on --
the inference server being down -- end a run.

**One gate, one place.** Every path to executing a tool goes through
:meth:`BaseAgent._execute_tool_call`, and the only way past the broker in that
method is a granted decision. Enforcement is not sprinkled across call sites,
because a check that exists in nine places is a check that is missing from the
tenth.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.core.errors import (
    ModelError,
    ToolError,
    ToolInputError,
    ToolNotFoundError,
)
from personal_ai_os.core.model import AgentModel
from personal_ai_os.core.types import Message, ToolCall, ToolSchema
from personal_ai_os.observability.logging import get_logger
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import PermissionBroker
from personal_ai_os.permissions.types import PermissionRequest
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.registry import ToolRegistry

log = get_logger("agent")

DEFAULT_MAX_ITERATIONS = 6

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant running inside a local personal AI system.\n"
    "When a tool can answer part of the question, call it rather than guessing.\n"
    "Base your answer only on what tools actually returned. If a tool fails or "
    "is denied, say so plainly instead of inventing the result.\n"
    "When you have enough information, reply with the final answer and no "
    "further tool calls."
)


class StopReason(str, Enum):
    ANSWERED = "answered"
    MAX_ITERATIONS = "max_iterations"
    MODEL_ERROR = "model_error"
    AGENT_ERROR = "agent_error"


class AgentResult(BaseModel):
    """What one agent run produced, and enough context to judge it."""

    model_config = ConfigDict(protected_namespaces=())

    ok: bool
    agent: str
    run_id: str
    stop_reason: StopReason
    output: str = ""
    error: str | None = None
    model: str = ""
    iterations: int = 0
    tool_calls: int = 0
    #: Full message history, so a failed run can be inspected after the fact.
    transcript: list[Message] = Field(default_factory=list, repr=False)


class BaseAgent:
    """A model, a set of tools, and a bounded loop between them."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        model: AgentModel,
        tools: ToolRegistry,
        broker: PermissionBroker,
        context: ToolContext,
        trace: RunTrace | None = None,
        system_prompt: str | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self.spec = spec
        self.model = model
        self.tools = tools
        self.broker = broker
        self.context = context
        self.trace = trace or RunTrace.disabled(agent=spec.name)
        self._system_prompt = system_prompt
        self.max_iterations = (
            max_iterations or spec.max_iterations or DEFAULT_MAX_ITERATIONS
        )

    # --- prompt and tools --------------------------------------------------

    def system_prompt(self) -> str:
        return self._system_prompt or self.spec.system_prompt or DEFAULT_SYSTEM_PROMPT

    def tool_schemas(self) -> list[ToolSchema]:
        """Only the tools this agent's manifest declares are advertised.

        An agent cannot call what it was never shown, which keeps the manifest
        an accurate description of what the agent can do.
        """
        return self.tools.schemas(self.spec.tools) if self.spec.tools else []

    # --- the loop ----------------------------------------------------------

    def run(self, objective: str, *, history: list[Message] | None = None) -> AgentResult:
        messages: list[Message] = [Message.system(self.system_prompt())]
        messages.extend(history or [])
        messages.append(Message.user(objective))

        schemas = self.tool_schemas()
        tool_call_count = 0
        iteration = 0

        for iteration in range(1, self.max_iterations + 1):
            self.trace.event(
                Events.MODEL_REQUEST,
                iteration=iteration,
                model=self.model.name,
                message_count=len(messages),
                tools=[s.name for s in schemas],
            )

            try:
                response = self.model.generate(messages, tools=schemas or None)
            except ModelError as exc:
                # The model being unreachable is not something the model can
                # be told about. This is where a run legitimately stops.
                self.trace.error(exc, iteration=iteration)
                log.error("model call failed: %s", exc)
                return self._result(
                    ok=False,
                    stop_reason=StopReason.MODEL_ERROR,
                    error=str(exc),
                    iterations=iteration,
                    tool_calls=tool_call_count,
                    transcript=messages,
                )

            self.trace.event(
                Events.MODEL_RESPONSE,
                iteration=iteration,
                model=response.model,
                finish_reason=response.finish_reason.value,
                content=response.message.content,
                tool_calls=[
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in response.message.tool_calls
                ],
                usage=response.usage.model_dump() if response.usage else None,
            )

            messages.append(response.message)

            if not response.message.tool_calls:
                return self._result(
                    ok=True,
                    stop_reason=StopReason.ANSWERED,
                    output=response.message.content,
                    iterations=iteration,
                    tool_calls=tool_call_count,
                    transcript=messages,
                )

            for call in response.message.tool_calls:
                tool_call_count += 1
                observation = self._execute_tool_call(call)
                messages.append(
                    Message.tool(observation, name=call.name, tool_call_id=call.id)
                )

        # Ran out of turns. Report it honestly rather than passing off the
        # last partial thought as an answer.
        log.warning(
            "%s hit its %d-iteration limit without answering",
            self.spec.name,
            self.max_iterations,
        )
        return self._result(
            ok=False,
            stop_reason=StopReason.MAX_ITERATIONS,
            error=(
                f"stopped after {self.max_iterations} iterations without a final "
                f"answer"
            ),
            output=messages[-1].content if messages else "",
            iterations=iteration,
            tool_calls=tool_call_count,
            transcript=messages,
        )

    # --- the gate ----------------------------------------------------------

    def _execute_tool_call(self, call: ToolCall) -> str:
        """Validate, authorise, then run one tool call.

        Returns the text handed back to the model. Every early return here is
        a recoverable failure: the model is told what went wrong so it can try
        something else.
        """
        self.trace.event(
            Events.TOOL_REQUESTED,
            tool=call.name,
            call_id=call.id,
            arguments=call.arguments,
        )

        # 1. Does the tool exist, and is this agent allowed to see it?
        if call.name not in self.spec.tools:
            available = ", ".join(self.spec.tools) or "<none>"
            return (
                f"ERROR: tool {call.name!r} is not available to this agent. "
                f"Available tools: {available}"
            )
        try:
            tool = self.tools.get(call.name)
        except ToolNotFoundError as exc:
            return f"ERROR: {exc}"

        # 2. Do the arguments type-check?
        try:
            args = tool.validate_input(call.arguments)
        except ToolInputError as exc:
            self.trace.event(
                Events.TOOL_RESULT, tool=call.name, ok=False, error=str(exc)
            )
            return f"ERROR: {exc}. Correct the arguments and call the tool again."

        # 3. The gate. Nothing below this runs without a granted decision.
        request = PermissionRequest(
            level=tool.permission,
            action=tool.name,
            resource=tool.describe_resource(args),
            agent=self.spec.name,
            run_id=self.trace.run_id,
            requires_human_approval=(
                tool.requires_human_approval or self.spec.requires_human_approval
            ),
        )
        decision = self.broker.request(request)
        self.trace.event(
            Events.PERMISSION_DECISION,
            tool=call.name,
            level=tool.permission.value,
            resource=request.resource,
            granted=decision.granted,
            source=decision.source,
            reason=decision.reason,
        )
        if decision.denied:
            return (
                f"DENIED: permission to run {tool.name!r} was refused "
                f"({decision.reason}). Do not retry this call; either continue "
                f"without it or explain what you cannot do."
            )

        # 4. Run it.
        try:
            output = tool.execute(args, self.context)
        except ToolError as exc:
            self.trace.event(
                Events.TOOL_RESULT, tool=call.name, ok=False, error=str(exc)
            )
            return f"ERROR: {exc}"

        payload = output.model_dump_json()
        self.trace.event(
            Events.TOOL_RESULT,
            tool=call.name,
            ok=True,
            result_bytes=len(payload),
            result_preview=payload[:500],
        )
        return payload

    # --- helpers -----------------------------------------------------------

    def _result(self, **kwargs: object) -> AgentResult:
        return AgentResult(
            agent=self.spec.name,
            run_id=self.trace.run_id,
            model=self.model.name,
            **kwargs,  # type: ignore[arg-type]
        )
