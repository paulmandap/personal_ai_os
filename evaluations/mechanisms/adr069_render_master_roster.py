"""Render the Master's roster exactly as the model receives it.

**This is a manipulation check, not an experiment.** ADR-060's arm B configured
an instrument through a channel it deliberately ignores, never checked that the
manipulation landed, and published "H1 rejected" from an experiment that changed
nothing (ADR-063). PROJECT_STATE measurement rule 8 exists because of that, and
this script is the cheapest possible way to honour it: one assertion that the
roster the Master reads really does contain what the arm claims.

ADR-069's A/B moves a single manifest into `agents/`. Arm A must render FOUR
roster entries, arm B FIVE. If arm B's render does not name `week_planner`, the
arm manipulated nothing and no number from it is reportable.

    & .venv\\Scripts\\python.exe evaluations\\mechanisms\\adr069_render_master_roster.py

It reads the same `agents/` directory the evaluation harness reads
(`EvalRunner._settings_for` pins `agents_dir` to the repository's own), and
renders through `MasterAgent.system_prompt()` rather than reconstructing the
string -- ADR-062's lesson that an obtained prompt beats a reconstructed one.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from personal_ai_os.agents.builtin.master import MasterAgent
from personal_ai_os.agents.registry import AgentRegistry
from personal_ai_os.memory.store import Store
from personal_ai_os.models.fake import ScriptedModel
from personal_ai_os.observability.trace import RunTrace
from personal_ai_os.permissions.broker import AllowAllBroker
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.registry import default_registry

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    tools = default_registry()
    registry = AgentRegistry.from_dir(REPO / "agents", tools=tools)
    names = registry.names()

    spec = registry.get("master")
    agent = MasterAgent(
        spec,
        model=ScriptedModel([]),
        tools=tools,
        broker=AllowAllBroker(),
        context=ToolContext(
            agent="master",
            run_id="roster-check",
            store=Store.in_memory(),
            agent_roster={s.name: s.description.strip() for s in registry.all()},
            call_stack=("master",),
        ),
        trace=RunTrace.disabled(agent="master"),
    )

    prompt = agent.system_prompt()
    roster = agent._roster()
    entries = [line for line in roster.splitlines() if line.strip().startswith("- ")]

    print(f"registered agents ({len(names)}): {', '.join(names)}")
    print(f"roster entries seen by the Master: {len(entries)}")
    print(f"week_planner in roster: {'week_planner' in roster}")
    print(f"system prompt: {len(prompt)} chars, "
          f"sha256 {hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]}")
    print("\n--- roster as rendered ---")
    print(roster)
    return 0


if __name__ == "__main__":
    sys.exit(main())
