"""The Master Agent.

It is an ordinary :class:`BaseAgent`. The only thing that makes it a master is
its manifest: one tool, ``delegate``, and no others. That is deliberate --
§7's rule *"prefer delegation when a specialised agent exists"* is enforced by
what the agent can reach, not by asking a prompt to stay disciplined.

The one thing it needs beyond configuration is the roster of agents it can call,
which is a runtime fact rather than a manifest fact: adding an agent to
``agents/`` must make it routable without editing this file or the Master's
manifest.
"""

from __future__ import annotations

from personal_ai_os.agents.base import BaseAgent

MASTER_SYSTEM_PROMPT = """\
You are the Master agent of a local personal AI system. You coordinate; you do \
not do specialised work yourself.

Your only tool is `delegate`, which hands an objective to a specialised agent \
and returns its result.

Agents you can delegate to:
{roster}

How to work:
- Decide which agent should handle the request. If several steps are needed, \
delegate them one at a time and use each result to decide the next.
- The agent you call cannot see this conversation. Write a complete, \
self-contained objective containing every detail it needs.
- Use what the agent actually returned. Never invent a result, and never claim \
work was done if the delegation failed.
- If a delegation fails or is refused, say so plainly and report what you could \
not complete.
- When the request is fully handled, reply to the user directly with no further \
tool calls. Summarise what was done in plain language.
"""


class MasterAgent(BaseAgent):
    """Routes objectives to specialised agents."""

    def system_prompt(self) -> str:
        base = self._system_prompt or self.spec.system_prompt or MASTER_SYSTEM_PROMPT
        return base.replace("{roster}", self._roster())

    def _roster(self) -> str:
        """Every agent except this one, as a bulleted name + description list.

        Excluding itself matters: a model shown its own name in a list of
        delegation targets will eventually try it, and while the cycle guard
        refuses that, the refusal costs an iteration for no reason.
        """
        others = {
            name: desc
            for name, desc in self.context.agent_roster.items()
            if name != self.spec.name
        }
        if not others:
            return "  (none available - you must answer directly)"
        return "\n".join(f"  - {name}: {desc}" for name, desc in sorted(others.items()))
