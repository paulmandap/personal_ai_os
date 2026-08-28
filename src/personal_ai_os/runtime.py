"""Composition root.

Every wire in the system is connected here and nowhere else. Components take
their collaborators as constructor arguments and never reach for globals, so
this is the single place that knows how the pieces fit -- which is what makes
each of them independently testable with a substitute.

It is also the only component that can build an agent, which is why delegation
is handed out from here as a closure rather than reached for by a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_ai_os.agents.base import AgentResult, BaseAgent
from personal_ai_os.agents.registry import AgentRegistry
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.config.loader import load_settings
from personal_ai_os.config.schema import Settings
from personal_ai_os.memory.store import Store
from personal_ai_os.models.registry import ModelRegistry
from personal_ai_os.models.router import ModelRouter, ModelSelection
from personal_ai_os.observability.logging import get_logger, setup_logging
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import CLIPermissionBroker, PermissionBroker
from personal_ai_os.tools.base import DelegateFn, ToolContext
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
    store: Store

    # --- construction ------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        settings: Settings | None = None,
        workspace_root: Path | None = None,
        broker: PermissionBroker | None = None,
        tools: ToolRegistry | None = None,
        store: Store | None = None,
        models: ModelRegistry | None = None,
        configure_logging: bool = True,
    ) -> Runtime:
        cfg = settings or load_settings(workspace_root)
        if configure_logging:
            setup_logging(cfg.observability.log_level)

        tool_registry = tools or default_registry()
        # Injectable so the evaluation harness can drive a whole runtime from a
        # ScriptedModel and prove itself correct with Ollama stopped.
        model_registry = models or ModelRegistry(cfg.models)

        # Connect eagerly: a broken database should surface at startup, not
        # midway through an agent run that has already prompted the user.
        db = store or Store(cfg.resolved_path(cfg.paths.db_path))
        db.connect()

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
            store=db,
        )

    # --- paths -------------------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        return self.settings.resolved_path(self.settings.paths.runs_dir)

    def tool_context(
        self,
        *,
        agent: str = "",
        run_id: str = "",
        depth: int = 0,
        call_stack: tuple[str, ...] = (),
        delegate: DelegateFn | None = None,
    ) -> ToolContext:
        return ToolContext(
            allowed_roots=self.settings.resolved_allowed_roots(),
            workspace_root=self.settings.workspace_root.resolve(),
            agent=agent,
            run_id=run_id,
            store=self.store,
            delegate=delegate,
            agent_roster={s.name: s.description.strip() for s in self.agents.all()},
            depth=depth,
            call_stack=call_stack,
            max_delegation_depth=self.settings.agent_defaults.max_delegation_depth,
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

    def create_agent(
        self,
        name: str,
        *,
        trace: RunTrace,
        depth: int = 0,
        call_stack: tuple[str, ...] = (),
    ) -> BaseAgent:
        spec = self.agents.get(name)
        selection = self.select_model(spec)
        trace.event(
            Events.ROUTER_SELECT, agent=spec.name, depth=depth, **selection.as_trace()
        )
        log.info(
            "agent %s -> %s (%s)", spec.name, selection.model.name, selection.reason
        )

        stack = call_stack or (spec.name,)
        agent_cls = self.agents.resolve_class(spec)
        return agent_cls(
            spec,
            model=selection.model,
            tools=self.tools,
            broker=self.broker,
            context=self.tool_context(
                agent=spec.name,
                run_id=trace.run_id,
                depth=depth,
                call_stack=stack,
                delegate=self._delegate_from(trace, depth, stack),
            ),
            trace=trace,
            system_prompt=self.agents.system_prompt_for(spec),
            max_iterations=spec.max_iterations
            or self.settings.agent_defaults.max_iterations,
        )

    # --- delegation --------------------------------------------------------

    def _delegate_from(
        self, trace: RunTrace, depth: int, call_stack: tuple[str, ...]
    ) -> DelegateFn:
        """Build the delegate callable handed to one agent's tools.

        The closure carries the trace, the current depth and the call stack, so
        a tool cannot fabricate a shallower depth or a shorter stack to escape
        the guards -- it only gets to choose *which* agent to call.
        """

        def delegate(agent_name: str, objective: str) -> AgentResult:
            return self.run_sub_agent(
                agent_name,
                objective,
                trace=trace,
                depth=depth + 1,
                call_stack=call_stack,
            )

        return delegate

    def run_sub_agent(
        self,
        name: str,
        objective: str,
        *,
        trace: RunTrace,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> AgentResult:
        """Run one agent inside another's trace."""
        parent = call_stack[-1] if call_stack else ""
        trace.event(
            Events.DELEGATE_START,
            parent_agent=parent,
            child_agent=name,
            depth=depth,
            objective=objective,
        )
        log.info("%s -> delegating to %s (depth %d)", parent or "?", name, depth)

        agent = self.create_agent(
            name, trace=trace, depth=depth, call_stack=(*call_stack, name)
        )
        result = agent.run(objective)

        trace.event(
            Events.DELEGATE_END,
            parent_agent=parent,
            child_agent=name,
            depth=depth,
            ok=result.ok,
            stop_reason=result.stop_reason.value,
            iterations=result.iterations,
            tool_calls=result.tool_calls,
            error=result.error,
        )
        return result

    # --- top-level runs ----------------------------------------------------

    def run_agent(
        self, name: str, objective: str, *, trace: RunTrace | None = None
    ) -> AgentResult:
        """Run one agent.

        Creates and owns a trace by default. A caller may supply its own --
        the evaluation harness does, so it can score the events afterwards
        without reading the file back off disk.
        """
        if trace is not None:
            return self._run_traced(name, objective, trace)

        with RunTrace.create(
            agent=name,
            runs_dir=self.runs_dir,
            enabled=self.settings.observability.trace_enabled,
            redact_keys=self.settings.observability.redact_keys,
        ) as owned:
            return self._run_traced(name, objective, owned)

    def _run_traced(self, name: str, objective: str, trace: RunTrace) -> AgentResult:
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
        self.store.close()
