"""Provider-neutral conversation types.

Nothing in this module knows about Ollama, or about any other inference
backend. Provider adapters (``personal_ai_os.models.*``) translate to and from
their own wire formats at their own boundary.

That translation boundary -- not the ``AgentModel`` ABC -- is what actually
makes this system model-agnostic. An ABC only fixes the *shape* of the call;
it is keeping every provider's data format from leaking upward that lets a
model be replaced without touching agents, tools, memory or orchestration.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(str, Enum):
    """Who authored a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """A model's *request* to invoke a tool.

    A ``ToolCall`` is an intent, never an execution. It becomes an execution
    only after the permission broker grants it -- see
    ``personal_ai_os.agents.base.BaseAgent._execute_tool_call``.
    """

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One turn in a conversation."""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # Set on TOOL messages to bind a result back to the request that caused it.
    tool_call_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _check_tool_message(self) -> Message:
        if self.role is Role.TOOL and not self.name:
            raise ValueError("a TOOL message must carry the tool `name`")
        if self.role is not Role.ASSISTANT and self.tool_calls:
            raise ValueError("only ASSISTANT messages may carry tool_calls")
        return self

    # Constructors -- these read better at call sites than kwargs everywhere.

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls, content: str = "", tool_calls: list[ToolCall] | None = None
    ) -> Message:
        return cls(
            role=Role.ASSISTANT, content=content, tool_calls=list(tool_calls or [])
        )

    @classmethod
    def tool(cls, content: str, *, name: str, tool_call_id: str | None = None) -> Message:
        return cls(
            role=Role.TOOL, content=content, name=name, tool_call_id=tool_call_id
        )


class Usage(BaseModel):
    """Token and timing accounting for a single model call."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_duration_ms: float | None = None
    load_duration_ms: float | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    @property
    def tokens_per_second(self) -> float | None:
        """Generation throughput, useful when comparing local models."""
        if not self.completion_tokens or not self.total_duration_ms:
            return None
        seconds = self.total_duration_ms / 1000.0
        return self.completion_tokens / seconds if seconds > 0 else None


class FinishReason(str, Enum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    ERROR = "error"


class ModelResponse(BaseModel):
    """The result of one ``AgentModel.generate`` call."""

    model_config = ConfigDict(protected_namespaces=())

    message: Message
    model: str
    provider: str
    usage: Usage | None = None
    finish_reason: FinishReason = FinishReason.STOP
    # The untouched provider payload. Kept for debugging and for replaying
    # traces, but never read by anything above the provider boundary.
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.message.tool_calls)


class ToolSchema(BaseModel):
    """A tool as advertised *to a model*.

    Providers encode this into whatever their API expects. Tools produce it
    from their pydantic input model, so a tool's advertised schema can never
    drift from the schema it actually validates against.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_format(self) -> dict[str, Any]:
        """The `{"type": "function", ...}` shape used by Ollama and others."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ModelHealth(BaseModel):
    """Whether a configured model can actually be used right now."""

    model_config = ConfigDict(protected_namespaces=())

    provider: str
    model: str
    server_reachable: bool
    model_available: bool
    detail: str = ""
    #: The inference server's own version string, e.g. ``"0.33.2"``.
    #:
    #: Here rather than anywhere else because `health()` is the seam's one way
    #: to ask a server about itself without generating, and because evaluation
    #: must never import a provider to find this out (ADR-041).
    #:
    #: **Best effort, and empty is not a failure.** A provider that reports no
    #: version leaves it blank -- `FakeModel` always does -- and a server that
    #: cannot be asked must still report its health honestly rather than raise.
    runtime_version: str = ""

    @property
    def ok(self) -> bool:
        return self.server_reachable and self.model_available
