"""Delegation -- running a sub-agent as a tool.

This is the whole Master Agent mechanism (ADR-012). Because a sub-agent is
reached through the ordinary tool interface, delegation inherits the permission
gate, the trace events, argument validation, and failure-as-observation
recovery without any of them being rewritten.

**Why `read` and not something heavier (ADR-014).** Delegating is not itself
consequential -- it computes. Everything consequential the sub-agent does is
gated separately by its own tools, at the point where the consequence actually
happens. Prompting here as well would ask the user to approve something that
protects nothing, which is how people learn to click through prompts without
reading them. Runaway delegation is bounded by the depth limit and by the
calling agent's own iteration ceiling, not by a dialog.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from personal_ai_os.core.errors import AgentNotFoundError, ToolExecutionError
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext, ToolInput


class DelegateInput(ToolInput):
    agent: str = Field(
        description=(
            "Name of the agent to hand this work to. Must be one of the agents "
            "listed in your instructions."
        )
    )
    objective: str = Field(
        min_length=1,
        description=(
            "A complete, self-contained instruction for that agent. It cannot "
            "see this conversation, so include every detail it needs."
        ),
    )


class DelegateOutput(BaseModel):
    agent: str
    ok: bool
    output: str
    iterations: int = 0
    tool_calls: int = 0
    error: str | None = None


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Hand a piece of work to a specialised agent and get its result back. "
        "Prefer this over trying to do specialised work yourself. The agent you "
        "call cannot see this conversation, so write a complete instruction."
    )
    Input = DelegateInput
    Output = DelegateOutput
    permission = PermissionLevel.READ
    # Generous: a sub-agent runs a full loop of its own, each turn a local
    # model call. This bounds a wedged sub-agent, not a merely slow one.
    timeout_s = 600.0

    def describe_resource(self, args: DelegateInput) -> str:  # type: ignore[override]
        return args.agent

    def run(self, args: DelegateInput, ctx: ToolContext) -> DelegateOutput:  # type: ignore[override]
        if ctx.delegate is None:
            raise ToolExecutionError(
                "delegation is not available in this context; no sub-agent "
                "runner was provided"
            )

        # Depth guard: bounds recursion regardless of what the model decides.
        if ctx.depth >= ctx.max_delegation_depth:
            raise ToolExecutionError(
                f"delegation depth limit reached ({ctx.max_delegation_depth}). "
                f"Do the remaining work yourself or report what is incomplete."
            )

        # Cycle guard: the call stack contains every agent above this one,
        # including the caller, so this refuses both self-delegation and
        # longer loops such as master -> a -> master.
        if args.agent in ctx.call_stack:
            chain = " -> ".join(ctx.call_stack)
            raise ToolExecutionError(
                f"refusing to delegate to {args.agent!r}: it is already running "
                f"in this chain ({chain}). Choose a different agent or finish "
                f"the work here."
            )

        try:
            result = ctx.delegate(args.agent, args.objective)
        except AgentNotFoundError as exc:
            # Already carries the list of registered agents.
            raise ToolExecutionError(str(exc)) from exc

        # A failed sub-agent is a result, not an exception: the caller gets to
        # decide whether to try another agent, work around it, or report it.
        return DelegateOutput(
            agent=args.agent,
            ok=result.ok,
            output=result.output,
            iterations=result.iterations,
            tool_calls=result.tool_calls,
            error=result.error,
        )
