"""Typed configuration.

Configuration is validated into pydantic models at startup, so a typo in a
YAML key fails immediately and loudly rather than surfacing as an
``AttributeError`` twenty minutes into an agent run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from personal_ai_os.permissions.types import PermissionLevel

Tier = Literal["small", "medium", "large"]
PolicyAction = Literal["auto", "ask", "deny"]


class OllamaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:11434"
    request_timeout_s: float = 120.0
    keep_alive: str = "5m"


class TierSettings(BaseModel):
    """One rung of the local model ladder.

    ``model = None`` means the rung is declared but unmapped -- the router
    degrades to the next rung down rather than failing. This is how an 8 GB
    GPU can honestly say "I have no large tier" without breaking routing.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str | None = None
    temperature: float = 0.3
    num_ctx: int = 8192


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama"] = "ollama"
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    tiers: dict[Tier, TierSettings] = Field(default_factory=dict)
    #: Named workloads mapped onto tiers, e.g. ``{"classify": "small"}``.
    roles: dict[str, Tier] = Field(default_factory=dict)
    default_tier: Tier = "small"

    @field_validator("tiers")
    @classmethod
    def _require_small(cls, v: dict[Tier, TierSettings]) -> dict[Tier, TierSettings]:
        if "small" not in v:
            raise ValueError(
                "the 'small' tier must be configured; it is the floor the "
                "router degrades to"
            )
        return v


class PermissionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What to do when an action at each level is requested.
    policy: dict[PermissionLevel, PolicyAction] = Field(default_factory=dict)
    #: When false, every 'ask' becomes a 'deny' (non-interactive runs).
    interactive: bool = True


class PathSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Filesystem roots tools are allowed to touch. Anything resolving outside
    #: every root is refused -- see tools/base.py resolve_within_roots().
    allowed_roots: list[Path] = Field(default_factory=list)
    agents_dir: Path = Path("agents")
    runs_dir: Path = Path("runs")


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    trace_enabled: bool = True
    #: Substrings that mark a key as secret; matching values are redacted
    #: before anything is written to a trace file.
    redact_keys: list[str] = Field(
        default_factory=lambda: [
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "credential",
            "authorization",
        ]
    )


class AgentDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = 6
    timeout_s: float = 300.0


class Settings(BaseModel):
    """The fully resolved configuration for one process."""

    model_config = ConfigDict(extra="forbid")

    workspace_root: Path = Path(".")
    models: ModelSettings
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    agent_defaults: AgentDefaults = Field(default_factory=AgentDefaults)

    def resolved_allowed_roots(self) -> list[Path]:
        """Allowed roots as absolute paths, defaulting to the workspace root."""
        roots = self.paths.allowed_roots or [self.workspace_root]
        base = self.workspace_root.resolve()
        return [(base / r).resolve() if not r.is_absolute() else r.resolve() for r in roots]

    def resolved_path(self, p: Path) -> Path:
        """Resolve a configured path relative to the workspace root."""
        return p.resolve() if p.is_absolute() else (self.workspace_root / p).resolve()
