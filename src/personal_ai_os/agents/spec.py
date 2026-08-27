"""Agent manifests.

An agent is declared in YAML and discovered at runtime, not hard-coded into
the orchestrator. That is the point of the registry: adding a Finance Agent
later must not require editing the Master Agent, so the Master Agent has to
learn what exists by reading manifests rather than by importing classes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_ai_os.config.schema import Tier
from personal_ai_os.models.router import TaskComplexity
from personal_ai_os.permissions.types import PermissionLevel

DEFAULT_ENTRYPOINT = "personal_ai_os.agents.base:BaseAgent"


class ModelPreference(BaseModel):
    """What model this agent wants, expressed as loosely as it can afford.

    Preferring ``role`` over ``model`` is the whole discipline: an agent that
    says "this is planning work" keeps working when the models change, while
    one that names ``qwen2.5:7b-instruct`` has to be edited.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str | None = None
    role: str | None = None
    tier: Tier | None = None
    complexity: TaskComplexity | None = None


class AgentSpec(BaseModel):
    """One agent's declared identity, capabilities and limits."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    description: str
    version: str = "0.1.0"

    #: ``module:ClassName`` implementing the agent. Defaults to the generic
    #: tool-calling loop, so a useful agent can be pure configuration.
    entrypoint: str = DEFAULT_ENTRYPOINT

    model: ModelPreference = Field(default_factory=ModelPreference)

    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    #: Consequences this agent is allowed to have. Checked against the tools
    #: it requests when the registry loads it.
    permissions: list[PermissionLevel] = Field(default_factory=list)

    system_prompt: str | None = None
    #: Path to a prompt file, relative to the agents directory. Long prompts
    #: belong in their own file where they can be diffed sensibly.
    system_prompt_file: str | None = None

    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None

    max_iterations: int | None = Field(default=None, ge=1, le=100)
    timeout_s: float | None = Field(default=None, gt=0)
    requires_human_approval: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"agent name {v!r} must be alphanumeric with underscores or hyphens"
            )
        return v

    @field_validator("entrypoint")
    @classmethod
    def _valid_entrypoint(cls, v: str) -> str:
        if v.count(":") != 1 or not all(part.strip() for part in v.split(":")):
            raise ValueError(
                f"entrypoint {v!r} must be 'module.path:ClassName'"
            )
        return v

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            dupes = sorted({t for t in v if v.count(t) > 1})
            raise ValueError(f"duplicate tools listed: {', '.join(dupes)}")
        return v

    def declares(self, level: PermissionLevel) -> bool:
        return level in self.permissions
