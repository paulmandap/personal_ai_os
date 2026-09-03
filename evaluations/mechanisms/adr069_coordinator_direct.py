"""Run the coordinator DIRECTLY, bypassing the Master's routing.

**Supplementary to ADR-069's pre-registered acceptance criterion, not part of
it.** The `master` suite's hierarchy case asks one question with two ways to
fail, and a 0/5 there cannot tell them apart:

    1. the Master never chose `week_planner`            -- a ROUTING failure
    2. `week_planner` never reached both specialists    -- a COORDINATION failure

The suite's per-check rates separate those (`delegated_by master->week_planner`
fails in case 1, the other two in case 2), but only if the Master picked the
coordinator at least sometimes. If it never does, the coordinator's own behaviour
is simply unmeasured, and reporting "hierarchy not demonstrated" would be true of
the SYSTEM while saying nothing about the AGENT.

So this starts the run at `week_planner`, which `paios run` allows for any
registered agent. It answers exactly one question:

    given that it is invoked, does the local 7B coordinator delegate to both of
    its specialists?

**It does NOT exercise depth 2.** A top-level `week_planner` sits at depth 0 and
its specialists at depth 1, so this is the same shape as an ordinary Master run
and proves nothing about the depth budget. Only the `master` suite case puts a
coordinator at depth 1 and a specialist at depth 2. Conflating the two would be
claiming a hierarchy from a flat run, which is precisely the confusion
`delegated_by` was added to prevent.

**This is n=5 on one objective and is reported as an observation, never as a
score.** It cannot substitute for the suite result, because bypassing the Master
removes the routing step that a real run must perform.

    & .venv\\Scripts\\python.exe evaluations\\mechanisms\\adr069_coordinator_direct.py

Writes nothing. Prints the delegation edges observed per run.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from personal_ai_os.memory.store import Store
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]

OBJECTIVE = "Plan the week ahead for me: what I have due, and whether I can cover it."
RUNS = 5


def main() -> int:
    edges_per_run: list[list[str]] = []
    depths: Counter[int] = Counter()

    for i in range(1, RUNS + 1):
        # A throwaway in-memory store per run, so runs cannot see each other's
        # state -- the same isolation the eval harness gives each repetition.
        runtime = Runtime.build(
            workspace_root=REPO, store=Store.in_memory(), configure_logging=False
        )
        try:
            from personal_ai_os.memory.finance import FinanceStore
            from personal_ai_os.memory.tasks import TaskStore

            tasks = TaskStore(runtime.store)
            tasks.add("Renew car insurance", due_date="2026-09-05")
            tasks.add("Pay the electricity bill", due_date="2026-09-07")
            FinanceStore(runtime.store).set_balance("cash", "6200")

            trace = RunTrace.disabled(agent="week_planner")
            result = runtime.run_agent("week_planner", OBJECTIVE, trace=trace)

            edges = []
            for e in trace.events:
                if e.type == Events.DELEGATE_START:
                    parent = str(e.data.get("parent_agent"))
                    child = str(e.data.get("child_agent"))
                    edges.append(f"{parent}->{child}")
                    depths[int(e.data.get("depth", -1))] += 1
            edges_per_run.append(edges)
            print(f"run {i}: ok={result.ok} tool_calls={result.tool_calls} {edges}")
            print(f"        {result.output.strip()[:160]}")
        finally:
            runtime.close()

    both = sum(
        1
        for edges in edges_per_run
        if "week_planner->task_agent" in edges and "week_planner->finance" in edges
    )
    any_hop = sum(1 for edges in edges_per_run if edges)
    print(
        json.dumps(
            {
                "objective": OBJECTIVE,
                "runs": RUNS,
                "reached_both_specialists": both,
                "reached_at_least_one": any_hop,
                "delegation_depths_seen": dict(depths),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
