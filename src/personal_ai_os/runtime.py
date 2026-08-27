"""Composition root.

Every wire in the system is connected here and nowhere else. Components take
their collaborators as constructor arguments and never reach for globals, so
this is the single place that knows how the pieces fit -- which is what makes
each of them independently testable with a substitute.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_ai_os.agents.base import AgentResult, BaseAgent
from personal_ai_os.agents.registry import AgentRegistry
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.config.loader import load_settings
from personal_ai_os.config.schema import Settings
from personal_ai_os.models.registry import ModelRegistry
from personal_ai_os.models.router import ModelRouter, ModelSelection
from personal_ai_os.observability.logging import get_logger, setup_logging
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import CLIPermissionBroker, PermissionBroker
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.registry import ToolRegistry, default_registry

log = get_logger("runtime")


@dataclass
class Runtime:
    """A fully wired system, ready to run agents."""

    settings: Settings
    models: ModelRegistry
    router: ModelRouter
    tools: ToolRegistry
    agents: AgentRegistry
    broker: PermissionBroker

    # --- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        settings: Settings | None = None,
        workspace_root: Path | None = None,
        broker: PermissionBroker | None = None,
        tools: ToolRegistry | None = None,
        configure_logging: bool = True,
    ) -> Runtime:
        cfg = settings or load_settings(workspace_root)
        if configure_logging:
            setup_logging(cfg.observability.log_level)

        tool_registry = tools or default_registry()
        model_registry = ModelRegistry(cfg.models)

        return cls(
            settings=cfg,
            models=model_registry,
            router=ModelRouter(model_registry),
            tools=tool_registry,
            agents=AgentRegistry.from_dir(
                cfg.resolved_path(cfg.paths.agents_dir), tools=tool_registry
            ),
            broker=broker
            or CLIPermissionBroker(
                cfg.permissions.policy, interactive=cfg.permissions.interactive
            ),
        )

    # --- paths -------------------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        return self.settings.resolved_path(self.settings.paths.runs_dir)

    def tool_context(self, *, agent: str = "", run_id: str = "") -> ToolContext:
        return ToolContext(
            allowed_roots=self.settings.resolved_allowed_roots(),
            workspace_root=self.settings.workspace_root.resolve(),
            agent=agent,
            run_id=run_id,
        )

    # --- agents ------------------------------------------------------------

    def select_model(self, spec: AgentSpec) -> ModelSelection:
        pref = spec.model
        return self.router.select(
            model_name=pref.model,
            role=pref.role,
            tier=pref.tier,
            complexity=pref.complexity,
        )

    def create_agent(self, name: str, *, trace: RunTrace) -> BaseAgent:
        spec = self.agents.get(name)
        selection = self.select_model(spec)
        trace.event(Events.ROUTER_SELECT, agent=spec.name, **selection.as_trace())
        log.info(
            "agent %s -> %s (%s)", spec.name, selection.model.name, selection.reason
        )

        agent_cls = self.agents.resolve_class(spec)
        return agent_cls(
            spec,
            model=selection.model,
            tools=self.tools,
            broker=self.broker,
            context=self.tool_context(agent=spec.name, run_id=trace.run_id),
            trace=trace,
            system_prompt=self.agents.system_prompt_for(spec),
            max_iterations=spec.max_iterations
            or self.settings.agent_defaults.max_iterations,
        )

    def run_agent(self, name: str, objective: str) -> AgentResult:
        """Run one agent under a fresh trace."""
        with RunTrace.create(
            agent=name,
            runs_dir=self.runs_dir,
            enabled=self.settings.observability.trace_enabled,
            redact_keys=self.settings.observability.redact_keys,
        ) as trace:
            agent = self.create_agent(name, trace=trace)
            result = agent.run(objective)
            trace.event(
                "run.result",
                ok=result.ok,
                stop_reason=result.stop_reason.value,
                iterations=result.iterations,
                tool_calls=result.tool_calls,
                model=result.model,
            )
            return result

    def close(self) -> None:
        self.models.close()
