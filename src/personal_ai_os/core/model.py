"""The model abstraction.

This is the seam the whole project is built to protect. Agents, tools, memory
and orchestration depend on ``AgentModel`` and never on a concrete backend, so
swapping the model underneath is a configuration change rather than a rewrite.

The interface is synchronous on purpose (docs/decisions.md, ADR-002): a single
8 GB GPU holds one model at a time, so concurrent inference would cause model
swapping and run slower, not faster.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from personal_ai_os.core.types import (
    Message,
    ModelHealth,
    ModelResponse,
    ToolSchema,
)


class AgentModel(ABC):
    """A local text-generation model that may support tool calling.

    Implementations must translate to and from their own wire format inside
    ``generate``, and must raise the typed errors from
    ``personal_ai_os.core.errors`` rather than provider-specific exceptions.
    """

    #: Concrete model identifier, e.g. ``"qwen2.5:7b-instruct"``.
    name: str
    #: Backend identifier, e.g. ``"ollama"``.
    provider: str

    @abstractmethod
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
        """Produce one assistant turn.

        Args:
            messages: Conversation so far, oldest first.
            tools: Tools to advertise. ``None`` means "no tools available".
            temperature: Overrides the model's configured default.
            max_tokens: Cap on generated tokens.
            json_schema: When given, constrain output to this JSON Schema.
            timeout_s: Overrides the configured request timeout.

        Raises:
            ModelUnavailableError: Server unreachable or model not installed.
            ModelTimeoutError: No response within the timeout.
            ModelProtocolError: Reply could not be parsed.
        """

    @abstractmethod
    def health(self) -> ModelHealth:
        """Report whether this model is usable right now, without generating.

        Must not raise: an unreachable server is a *result*, not an exception.
        This is what lets ``paios doctor`` diagnose a machine in one command.
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.provider}:{self.name}>"
