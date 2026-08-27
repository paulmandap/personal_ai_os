"""A scripted model for tests.

This is not a mock bolted on after the fact -- it is the proof that the
abstraction holds. Every piece of agent logic (the tool loop, argument
validation, permission refusal, iteration limits) is exercised through
:class:`ScriptedModel` in milliseconds with no GPU and no network, which is
why ``pytest`` must pass with Ollama stopped.

It is also the second implementation of :class:`AgentModel`, and a second
implementation is the only real evidence that an interface is not secretly
shaped around its first one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from personal_ai_os.core.model import AgentModel
from personal_ai_os.core.types import (
    FinishReason,
    Message,
    ModelHealth,
    ModelResponse,
    ToolCall,
    ToolSchema,
    Usage,
)

PROVIDER = "fake"

#: A scripted turn: either a canned response, or a callable that builds one
#: from the conversation so far (for tests that need to react to input).
ScriptedTurn = ModelResponse | Callable[[list[Message]], ModelResponse]


def text_response(content: str, *, model: str = "scripted") -> ModelResponse:
    """A plain assistant answer with no tool calls."""
    return ModelResponse(
        message=Message.assistant(content),
        model=model,
        provider=PROVIDER,
        usage=Usage(prompt_tokens=0, completion_tokens=len(content.split())),
        finish_reason=FinishReason.STOP,
    )


def tool_call_response(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "call_test_0",
    content: str = "",
    model: str = "scripted",
) -> ModelResponse:
    """An assistant turn that requests one tool."""
    return ModelResponse(
        message=Message.assistant(
            content, tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]
        ),
        model=model,
        provider=PROVIDER,
        usage=Usage(prompt_tokens=0, completion_tokens=0),
        finish_reason=FinishReason.TOOL_CALLS,
    )


class ScriptedModel(AgentModel):
    """Returns pre-written responses in order, recording what it was asked."""

    provider = PROVIDER

    def __init__(
        self,
        responses: list[ScriptedTurn] | None = None,
        *,
        name: str = "scripted",
        healthy: bool = True,
    ) -> None:
        self.name = name
        self._responses: list[ScriptedTurn] = list(responses or [])
        self._healthy = healthy
        #: Every ``generate`` call, for assertions.
        self.calls: list[dict[str, Any]] = []

    def queue(self, *responses: ScriptedTurn) -> ScriptedModel:
        self._responses.extend(responses)
        return self

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_messages(self) -> list[Message]:
        return self.calls[-1]["messages"] if self.calls else []

    @property
    def exhausted(self) -> bool:
        return not self._responses

    def generate(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        timeout_s: float | None = None,
    ) -> ModelResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools or []),
                "tool_names": [t.name for t in (tools or [])],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_schema": json_schema,
            }
        )

        if not self._responses:
            raise RuntimeError(
                f"ScriptedModel ran out of responses on call #{len(self.calls)}. "
                "The agent asked for more turns than the test scripted -- either "
                "queue another response or assert on why the loop kept going."
            )

        turn = self._responses.pop(0)
        return turn(messages) if callable(turn) else turn

    def health(self) -> ModelHealth:
        return ModelHealth(
            provider=PROVIDER,
            model=self.name,
            server_reachable=self._healthy,
            model_available=self._healthy,
            detail="" if self._healthy else "scripted as unhealthy",
        )
