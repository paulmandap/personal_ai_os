"""Tool registry.

Agents never import tools directly; they name them, and the registry resolves
the name. That indirection is what lets an agent manifest be a YAML file and
what lets the registry refuse an agent that asks for a tool nobody registered.
"""

from __future__ import annotations

from personal_ai_os.core.errors import ToolNotFoundError
from personal_ai_os.core.types import ToolSchema
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool


class ToolRegistry:
    """Name -> tool, with schema and permission lookup."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"tool {tool.name!r} is already registered; pass replace=True "
                f"if shadowing it is intended"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise ToolNotFoundError(
                f"no tool named {name!r}. Registered tools: {known}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def schemas(self, names: list[str] | None = None) -> list[ToolSchema]:
        """Schemas for the named tools, or for all of them."""
        selected = self.names() if names is None else names
        return [self.get(n).schema() for n in selected]

    def permissions_for(self, names: list[str]) -> set[PermissionLevel]:
        """Every permission level the named tools can require.

        Used at agent-load time to reject a manifest that requests a tool
        whose consequences it never declared.
        """
        return {self.get(n).permission for n in names}

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools


def default_registry() -> ToolRegistry:
    """The tools available to a normal run.

    Imported lazily so that importing the registry module does not drag in
    every tool implementation.

    ``delegate`` is registered here like any other tool. It is stateless -- the
    ability to actually run a sub-agent arrives per-run on ``ToolContext`` --
    which is what keeps the tool registry from needing to know about the agent
    registry, and avoids the circular dependency that per-agent delegation
    tools would create (ADR-013).
    """
    from personal_ai_os.tools.builtin.finance import (
        AddCommitmentTool,
        AddGoalTool,
        AddTransactionTool,
        AffordabilityCheckTool,
        ListAccountsTool,
        ListCommitmentsTool,
        ListGoalsTool,
        ListTransactionsTool,
        SetBalanceTool,
    )
    from personal_ai_os.tools.builtin.list_dir import ListDirTool
    from personal_ai_os.tools.builtin.read_file import ReadFileTool
    from personal_ai_os.tools.builtin.tasks import (
        AddTaskTool,
        CompleteTaskTool,
        ListTasksTool,
        UpdateTaskTool,
    )
    from personal_ai_os.tools.delegate import DelegateTool

    return ToolRegistry(
        [
            ReadFileTool(),
            ListDirTool(),
            AddTaskTool(),
            ListTasksTool(),
            UpdateTaskTool(),
            CompleteTaskTool(),
            ListAccountsTool(),
            ListTransactionsTool(),
            ListCommitmentsTool(),
            ListGoalsTool(),
            AffordabilityCheckTool(),
            SetBalanceTool(),
            AddTransactionTool(),
            AddCommitmentTool(),
            AddGoalTool(),
            DelegateTool(),
        ]
    )
