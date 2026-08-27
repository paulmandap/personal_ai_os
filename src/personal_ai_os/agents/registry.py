"""Agent registry -- discovery, validation and construction.

Two checks run when a manifest loads, and both turn a class of runtime failure
into a startup failure:

* an agent may not request a tool nobody registered;
* an agent may not request a tool whose permission level it never declared.

The second is the one worth having. Without it, an agent declaring
``permissions: [read]`` could quietly be handed a tool that deletes things,
and nothing would notice until it did. Making the manifest state its own blast
radius -- and checking that claim -- turns the registry from a lookup table
into a safety boundary.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.core.errors import AgentNotFoundError, AgentSpecError
from personal_ai_os.observability.logging import get_logger
from personal_ai_os.tools.registry import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover
    from personal_ai_os.agents.base import BaseAgent

log = get_logger("agents")


class AgentRegistry:
    """Holds validated agent specs and builds agent instances from them."""

    def __init__(
        self,
        specs: list[AgentSpec] | None = None,
        *,
        tools: ToolRegistry,
        agents_dir: Path | None = None,
    ) -> None:
        self._tools = tools
        self._agents_dir = agents_dir
        self._specs: dict[str, AgentSpec] = {}
        for spec in specs or []:
            self.register(spec)

    # --- loading -----------------------------------------------------------

    @classmethod
    def from_dir(cls, agents_dir: Path, *, tools: ToolRegistry) -> AgentRegistry:
        """Load every ``*.yaml`` manifest in a directory."""
        registry = cls(tools=tools, agents_dir=agents_dir)
        if not agents_dir.is_dir():
            log.warning("agents directory %s does not exist", agents_dir)
            return registry

        for path in sorted(agents_dir.glob("*.yaml")):
            registry.register(cls._load_spec(path))
        return registry

    @staticmethod
    def _load_spec(path: Path) -> AgentSpec:
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise AgentSpecError(f"{path} is not valid YAML: {exc}") from exc
        except OSError as exc:
            raise AgentSpecError(f"cannot read {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise AgentSpecError(f"{path} must contain a YAML mapping")

        try:
            return AgentSpec.model_validate(raw)
        except ValidationError as exc:
            raise AgentSpecError(f"{path} is not a valid agent manifest:\n{exc}") from exc

    def register(self, spec: AgentSpec) -> None:
        self._validate(spec)
        if spec.name in self._specs:
            raise AgentSpecError(f"agent {spec.name!r} is declared more than once")
        self._specs[spec.name] = spec

    def _validate(self, spec: AgentSpec) -> None:
        """Reject a manifest that could not run safely, before it ever runs."""
        unknown = [name for name in spec.tools if not self._tools.has(name)]
        if unknown:
            known = ", ".join(self._tools.names()) or "<none>"
            raise AgentSpecError(
                f"agent {spec.name!r} requests unregistered tool(s): "
                f"{', '.join(unknown)}. Registered tools: {known}"
            )

        undeclared = sorted(
            level.value
            for level in self._tools.permissions_for(spec.tools)
            if not spec.declares(level)
        )
        if undeclared:
            raise AgentSpecError(
                f"agent {spec.name!r} requests tools needing permission(s) "
                f"{', '.join(undeclared)} but does not declare them. Add them to "
                f"the manifest's `permissions:` list to make the blast radius "
                f"explicit."
            )

    # --- lookup ------------------------------------------------------------

    def get(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError:
            known = ", ".join(sorted(self._specs)) or "<none>"
            raise AgentNotFoundError(
                f"no agent named {name!r}. Registered agents: {known}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def all(self) -> list[AgentSpec]:
        return [self._specs[n] for n in self.names()]

    def __len__(self) -> int:
        return len(self._specs)

    # --- construction ------------------------------------------------------

    def resolve_class(self, spec: AgentSpec) -> type[BaseAgent]:
        """Import the agent class named by the manifest's entrypoint.

        Deferred until an agent is actually run, so one broken manifest cannot
        stop the whole registry -- and ``paios agents`` still lists everything.
        """
        module_path, _, class_name = spec.entrypoint.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise AgentSpecError(
                f"agent {spec.name!r}: cannot import {module_path!r}: {exc}"
            ) from exc

        cls = getattr(module, class_name, None)
        if cls is None:
            raise AgentSpecError(
                f"agent {spec.name!r}: {module_path!r} has no attribute {class_name!r}"
            )

        from personal_ai_os.agents.base import BaseAgent

        if not (isinstance(cls, type) and issubclass(cls, BaseAgent)):
            raise AgentSpecError(
                f"agent {spec.name!r}: {spec.entrypoint} is not a BaseAgent subclass"
            )
        return cls

    def system_prompt_for(self, spec: AgentSpec) -> str | None:
        """Resolve an agent's prompt, preferring an external file if named."""
        if spec.system_prompt_file:
            base = self._agents_dir or Path.cwd()
            path = base / spec.system_prompt_file
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AgentSpecError(
                    f"agent {spec.name!r}: cannot read system_prompt_file {path}: {exc}"
                ) from exc
        return spec.system_prompt
