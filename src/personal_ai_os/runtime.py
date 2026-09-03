"""Composition root.

Every wire in the system is connected here and nowhere else. Components take
their collaborators as constructor arguments and never reach for globals, so
this is the single place that knows how the pieces fit -- which is what makes
each of them independently testable with a substitute.

It is also the only component that can build an agent, which is why delegation
is handed out from here as a closure rather than reached for by a tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from personal_ai_os.agents.base import AgentResult, BaseAgent
from personal_ai_os.agents.registry import AgentRegistry
from personal_ai_os.agents.spec import AgentSpec
from personal_ai_os.config.loader import load_settings
from personal_ai_os.config.schema import Settings
from personal_ai_os.memory.plans import PlanStore
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
    #: Extra ambient facts handed to every tool via `ToolContext.extras`.
    #:
    #: Set after construction rather than passed to `build()`, so the injected
    #: `runtime_builder` the evaluation harness supplies keeps its three-argument
    #: signature. Empty in an ordinary process: the only current user is the
    #: harness seeding `Setup.web` for `fetch_page`.
    tool_extras: dict[str, object] = field(default_factory=dict)

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
    def plans(self) -> PlanStore:
        """What top-level runs actually did (ADR-065).

        A thin wrapper over the same `Store`, built per access like any other
        typed view of it. Nothing the model can see comes from here on a normal
        run -- only `plans resume` composes anything model-facing.
        """
        return PlanStore(self.store)

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
            # Copied, not shared: a tool must not be able to mutate what the
            # next tool in the same run sees.
            extras=dict(self.tool_extras),
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
        plan_id: int | None = None,
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
                delegate=self._delegate_from(trace, depth, stack, plan_id),
            ),
            trace=trace,
            system_prompt=self.agents.system_prompt_for(spec),
            max_iterations=spec.max_iterations
            or self.settings.agent_defaults.max_iterations,
        )

    # --- delegation --------------------------------------------------------

    def _delegate_from(
        self,
        trace: RunTrace,
        depth: int,
        call_stack: tuple[str, ...],
        plan_id: int | None = None,
    ) -> DelegateFn:
        """Build the delegate callable handed to one agent's tools.

        The closure carries the trace, the current depth, the call stack and the
        plan, so a tool cannot fabricate a shallower depth, a shorter stack or a
        different plan to escape the guards -- it only gets to choose *which*
        agent to call.
        """

        def delegate(agent_name: str, objective: str) -> AgentResult:
            return self.run_sub_agent(
                agent_name,
                objective,
                trace=trace,
                depth=depth + 1,
                call_stack=call_stack,
                plan_id=plan_id,
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
        plan_id: int | None = None,
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

        # INV-1, first half: the intent is COMMITTED before the sub-agent can
        # run (ADR-065). `begin_step` returns only after its transaction closes,
        # so "no step row" provably means "never started". Invoking inside the
        # write transaction would break that and make resume *under*-report a
        # delegation that did execute -- the dangerous direction, since
        # over-reporting is safe under at-least-once and under-reporting is not.
        step_id: int | None = None
        if plan_id is not None:
            step_id = self.plans.begin_step(
                plan_id, run_id=trace.run_id, agent=name, objective=objective
            ).id

        agent = self.create_agent(
            name,
            trace=trace,
            depth=depth,
            call_stack=(*call_stack, name),
            plan_id=plan_id,
        )
        result = agent.run(objective)

        # INV-1, second half: the outcome is recorded only AFTER the result
        # exists. A crash anywhere above leaves the step `pending`, which is
        # exactly the "may or may not have happened" state resume reports.
        if step_id is not None:
            self.plans.finish_step(
                step_id, ok=result.ok, output=result.output, error=result.error
            )

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

    def resume_plan(self, plan_id: int) -> AgentResult:
        """Continue a plan a crash left `running` (ADR-065).

        **This is the one model-facing part of plan persistence, and it is
        confined to this path.** The composed objective tells the model what was
        already carried out -- and, separately, what was started but never
        confirmed. Normal runs send the model nothing from the plan store.

        Refuses a terminal plan: re-attempting a failed objective is a new run
        and a new plan, which keeps the failure record intact.

        Approvals are **not** replayed. Every action here passes through the
        same permission gate as a fresh run, because a stored approval is worse
        than none -- it invites trusting yesterday's consent for today's action.
        """
        plans = self.plans
        plan = plans.get(plan_id)
        objective = plans.resume_objective(plan_id)  # raises if not resumable

        with RunTrace.create(
            agent=plan.agent,
            runs_dir=self.runs_dir,
            enabled=self.settings.observability.trace_enabled,
            redact_keys=self.settings.observability.redact_keys,
        ) as owned:
            return self._run_traced(plan.agent, objective, owned, plan_id=plan_id)

    def _run_traced(
        self, name: str, objective: str, trace: RunTrace, *, plan_id: int | None = None
    ) -> AgentResult:
        """Run a top-level agent, recording what it did (ADR-065).

        A plan is opened here and closed only on a **positively observed**
        outcome. Nothing in a `finally` -- a cleanup block cannot run after
        SIGKILL or power loss, so an interrupted run simply leaves the plan
        `running`. That is the crash signature and the only way a stale
        `running` row can exist.

        `plan_id` is supplied when resuming, so the resumed run appends to the
        plan it is continuing rather than opening a second one.
        """
        if plan_id is None:
            plan_id = self.plans.open(
                run_id=trace.run_id, agent=name, objective=objective
            ).id

        agent = self.create_agent(name, trace=trace, plan_id=plan_id)
        result = agent.run(objective)

        if plan_id is not None:
            self.plans.close(plan_id, ok=result.ok)

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
