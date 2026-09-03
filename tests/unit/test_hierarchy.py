"""A real depth-2 delegation chain, driven offline.

**Phase 7.8, claim A only.** These prove the RUNTIME supports nested delegation
and records its depth correctly. They say nothing whatever about whether a local
model chooses to form a hierarchy -- that is claim B, and only the live `master`
suite can answer it. ADR-069 keeps the two apart deliberately, because a
deterministic script proving the machinery works is the easiest thing in the
world to mistake for evidence about the model.

Everything below runs through the **real** `Runtime`: the real `_delegate_from`
closure, the real `run_sub_agent`, the real `AgentRegistry`. Before this file the
deepest anything ever reached was 1 -- `depth=2` appeared only as a value chosen
to trip the guard, always paired with a delegate callable asserted never to run.
So `_delegate_from`'s ``depth + 1`` had been exercised exactly once, 0 -> 1, and
`run_sub_agent`'s ``call_stack=(*call_stack, name)`` had never produced a
two-element stack at all.

Each agent gets its own `ScriptedModel` by naming a distinct `model:` in its
manifest, which `ModelRegistry._build` caches per name. Sharing one script across
three agents would work only while every agent took exactly the turns predicted,
and would fail in a way that pointed at the wrong thing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from personal_ai_os.config.loader import load_settings
from personal_ai_os.core.types import Message
from personal_ai_os.memory.plans import PlanStore, StepStatus
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import TaskStore
from personal_ai_os.models.fake import ScriptedModel, text_response, tool_call_response
from personal_ai_os.models.registry import ModelRegistry
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import AllowAllBroker
from personal_ai_os.runtime import Runtime

# --- manifests ------------------------------------------------------------
#
# `week_planner` here mirrors the shipped manifest's SHAPE -- one tool,
# `delegate`, and `read` -- rather than importing it, so these keep testing the
# mechanism if the shipped description is ever reworded.


def _manifest(name: str, *, tools: list[str], model: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Test agent {name}.",
        "version": "0.1.0",
        "model": {"model": model},
        "tools": tools,
        "permissions": ["read", "write"] if "add_task" in tools else ["read"],
        "system_prompt": f"You are {name}.",
        "max_iterations": 6,
    }


def _write_agents(root: Path, manifests: list[dict[str, Any]]) -> None:
    agents = root / "agents"
    agents.mkdir(exist_ok=True)
    for manifest in manifests:
        (agents / f"{manifest['name']}.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8"
        )


def _runtime(root: Path, db: Path, scripts: dict[str, list]) -> Runtime:
    settings = load_settings(root, use_env=False)
    registry = ModelRegistry(
        settings.models,
        factory=lambda name, tier, s: ScriptedModel(scripts[name], name=name),
    )
    return Runtime.build(
        settings=settings,
        store=Store(db),
        broker=AllowAllBroker(),
        models=registry,
        configure_logging=False,
    )


def _delegate_to(agent: str, objective: str, call_id: str):
    return tool_call_response(
        "delegate", {"agent": agent, "objective": objective}, call_id=call_id
    )


def _capture(sink: list[list[Message]], reply: str):
    """A scripted turn that records what the model was shown before answering.

    Asserting on the recorded conversation proves the refusal reached the MODEL,
    not merely the trace. A guard whose message never gets read is a guard the
    agent cannot recover from.
    """

    def turn(messages: list[Message]):
        sink.append(list(messages))
        return text_response(reply)

    return turn


def _delegations(trace: RunTrace) -> list[tuple[str, str, int]]:
    return [
        (
            str(e.data.get("parent_agent")),
            str(e.data.get("child_agent")),
            int(e.data.get("depth", -1)),
        )
        for e in trace.events
        if e.type == Events.DELEGATE_START
    ]


THREE_DEEP = [
    _manifest("master", tools=["delegate"], model="master-model"),
    _manifest("week_planner", tools=["delegate"], model="planner-model"),
    _manifest("task_agent", tools=["add_task"], model="leaf-model"),
]


@pytest.fixture
def chain(workspace: Path, tmp_path: Path) -> Iterator[Runtime]:
    """master -> week_planner -> task_agent, all three real agents."""
    _write_agents(workspace, THREE_DEEP)
    runtime = _runtime(
        workspace,
        tmp_path / "chain.db",
        {
            "master-model": [
                _delegate_to("week_planner", "plan the week", "m1"),
                text_response("Your week is planned."),
            ],
            "planner-model": [
                _delegate_to("task_agent", "add a task to buy oat milk", "p1"),
                text_response("Specialist done."),
            ],
            "leaf-model": [
                tool_call_response("add_task", {"title": "Buy oat milk"}, call_id="l1"),
                text_response("Task added."),
            ],
        },
    )
    yield runtime
    runtime.close()


class TestTheChainActuallyForms:
    def test_a_depth_2_delegation_runs_end_to_end(self, chain: Runtime):
        trace = RunTrace.disabled(agent="master")
        result = chain.run_agent("master", "plan my week", trace=trace)
        assert result.ok
        assert _delegations(trace) == [
            ("master", "week_planner", 1),
            ("week_planner", "task_agent", 2),
        ]

    def test_the_call_stack_grows_to_three(self, chain: Runtime):
        """`run_sub_agent`'s ``(*call_stack, name)`` at its second application.

        Every two-element stack in the suite before this was a hand-written
        literal. This one is produced by the runtime.
        """
        seen: list[tuple[str, ...]] = []
        original = chain.create_agent

        def spy(name: str, **kwargs):
            agent = original(name, **kwargs)
            seen.append(agent.context.call_stack)
            return agent

        chain.create_agent = spy  # type: ignore[method-assign]
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))

        assert seen == [
            ("master",),
            ("master", "week_planner"),
            ("master", "week_planner", "task_agent"),
        ]

    def test_depth_is_stamped_0_1_2_on_the_agents(self, chain: Runtime):
        seen: list[int] = []
        original = chain.create_agent

        def spy(name: str, **kwargs):
            agent = original(name, **kwargs)
            seen.append(agent.context.depth)
            return agent

        chain.create_agent = spy  # type: ignore[method-assign]
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))
        assert seen == [0, 1, 2]

    def test_the_leaf_really_did_the_work(self, chain: Runtime):
        """Otherwise the chain could 'form' while nothing happened at the bottom."""
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))
        assert [t.title for t in TaskStore(chain.store).list()] == ["Buy oat milk"]

    def test_the_leafs_own_events_carry_depth_2(self, chain: Runtime):
        """ADR-016's stamping, at a depth it had never reached."""
        trace = RunTrace.disabled(agent="master")
        chain.run_agent("master", "plan my week", trace=trace)
        leaf = [e for e in trace.events if e.data.get("agent") == "task_agent"]
        assert leaf, "the leaf produced no events"
        assert {e.data.get("depth") for e in leaf} == {2}


class TestTheGuardsFireOnTheProductionPath:
    """The guards were only ever tested against a hand-built `ToolContext`.

    These drive them through the real closure, which is where `depth` and
    `call_stack` are actually injected. **This proves the mechanism works on the
    production code path. It does NOT mean the shipped configuration exercises
    it** -- no shipped manifest gives `delegate` to a depth-2 agent, so in a real
    run the depth guard cannot fire at all.
    """

    def test_the_depth_guard_refuses_a_third_hop_and_the_model_is_told(
        self, workspace: Path, tmp_path: Path
    ):
        seen: list[list[Message]] = []
        _write_agents(
            workspace,
            [
                _manifest("master", tools=["delegate"], model="master-model"),
                _manifest("week_planner", tools=["delegate"], model="planner-model"),
                # A depth-2 agent that HOLDS delegate. No shipped manifest does.
                _manifest("deep_agent", tools=["delegate"], model="deep-model"),
                _manifest("task_agent", tools=["add_task"], model="leaf-model"),
            ],
        )
        runtime = _runtime(
            workspace,
            tmp_path / "deep.db",
            {
                "master-model": [
                    _delegate_to("week_planner", "go", "m1"),
                    text_response("done"),
                ],
                "planner-model": [
                    _delegate_to("deep_agent", "go deeper", "p1"),
                    text_response("could not go deeper"),
                ],
                "deep-model": [
                    _delegate_to("task_agent", "one more hop", "d1"),
                    _capture(seen, "I was refused and am reporting it."),
                ],
                "leaf-model": [text_response("never reached")],
            },
        )
        try:
            trace = RunTrace.disabled(agent="master")
            result = runtime.run_agent("master", "go", trace=trace)
            assert result.ok

            # The refusal happened, and the fourth agent never ran.
            assert ("deep_agent", "task_agent", 3) not in _delegations(trace)
            assert [c for _, c, _ in _delegations(trace)] == [
                "week_planner",
                "deep_agent",
            ]

            assert seen, "the deep agent never took a second turn"
            observation = "\n".join(m.content for m in seen[0] if m.content)
            assert "delegation depth limit reached (2)" in observation
        finally:
            runtime.close()

    def test_the_cycle_guard_refuses_a_return_to_the_master(
        self, workspace: Path, tmp_path: Path
    ):
        seen: list[list[Message]] = []
        _write_agents(
            workspace,
            [
                _manifest("master", tools=["delegate"], model="master-model"),
                _manifest("week_planner", tools=["delegate"], model="planner-model"),
            ],
        )
        runtime = _runtime(
            workspace,
            tmp_path / "cycle.db",
            {
                "master-model": [
                    _delegate_to("week_planner", "go", "m1"),
                    text_response("done"),
                ],
                "planner-model": [
                    _delegate_to("master", "hand it back", "p1"),
                    _capture(seen, "I cannot call the master."),
                ],
            },
        )
        try:
            runtime.run_agent("master", "go", trace=RunTrace.disabled(agent="m"))
            assert seen, "the planner never took a second turn"
            observation = "\n".join(m.content for m in seen[0] if m.content)
            assert "refusing to delegate to 'master'" in observation
            # The chain is named, so the model can see WHY rather than guessing.
            assert "master -> week_planner" in observation
        finally:
            runtime.close()


class TestThePlanRecordsTheShape:
    def test_both_hops_are_steps_on_one_plan_with_their_depths(self, chain: Runtime):
        """Schema v4. Before it, these two rows were indistinguishable."""
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))
        plans = PlanStore(chain.store)
        assert len(plans.list()) == 1
        plan = plans.list()[0]
        assert plan.id is not None
        steps = plans.steps(plan.id)
        assert [(s.seq, s.agent, s.depth) for s in steps] == [
            (1, "week_planner", 1),
            (2, "task_agent", 2),
        ]

    def test_seq_is_depth_first_because_a_parent_commits_before_its_child(
        self, chain: Runtime
    ):
        """INV-1's consequence at depth 2, stated as a property.

        The parent's row is created before the child can run and finished after
        it returns, so `seq` orders the plan depth-first. That is what makes
        `seq` plus `depth` a tree; `seq` alone is a list that reads a grandchild
        as a sibling.
        """
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))
        plans = PlanStore(chain.store)
        plan = plans.list()[0]
        assert plan.id is not None
        parent, child = plans.steps(plan.id)
        assert parent.depth < child.depth and parent.seq < child.seq
        assert parent.status is StepStatus.DONE and child.status is StepStatus.DONE
        assert parent.updated_at >= child.updated_at

    def test_the_nested_plan_is_still_one_plan(self, chain: Runtime):
        """`plan_id` is threaded down unchanged, so depth 2 does not open a second
        plan. A per-hop plan would make `paios plans` unreadable and would break
        `plans.run_id`'s UNIQUE constraint on the shared run."""
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))
        assert len(PlanStore(chain.store).list()) == 1


class TestTheCoordinatorsSpecialistListIsPresentationOnly:
    """ADR-069 claim C: **not implemented, deliberately.**

    A pinning test in this project's convention for a known blind spot -- it
    asserts today's behaviour and says so, so the day someone adds structural
    scoping this fails loudly and the ADR gets re-derived rather than the
    assertion edited.

    The manifest prompt names a coordinator's specialists; nothing enforces it.
    `Runtime.tool_context` builds `agent_roster` from every registered agent and
    hands the same dict to every agent at every depth, so a coordinator can name
    any agent in the registry. The depth and cycle guards are the only real
    bound, and they bound the SHAPE of delegation, never its targets.
    """

    def test_the_coordinator_sees_every_registered_agent(self, chain: Runtime):
        rosters: dict[str, list[str]] = {}
        original = chain.create_agent

        def spy(name: str, **kwargs):
            agent = original(name, **kwargs)
            rosters[name] = sorted(agent.context.agent_roster)
            return agent

        chain.create_agent = spy  # type: ignore[method-assign]
        chain.run_agent("master", "plan my week", trace=RunTrace.disabled(agent="m"))

        everyone = sorted(["master", "task_agent", "week_planner"])
        assert rosters["week_planner"] == everyone
        assert rosters["task_agent"] == everyone, (
            "even the leaf, which holds no delegate tool, is handed the roster"
        )

    def test_a_coordinator_can_reach_an_agent_its_prompt_never_named(
        self, workspace: Path, tmp_path: Path
    ):
        """The limitation, demonstrated rather than described.

        `week_planner`'s prompt names task_agent. It delegates to `ping` anyway,
        and nothing stops it. Under the shipped configuration this is bounded by
        the guards and by the sub-agent's own tool scope (ADR-066 property A) --
        which is why it is recorded as a limitation and not as a vulnerability.
        """
        _write_agents(
            workspace,
            [
                _manifest("master", tools=["delegate"], model="master-model"),
                _manifest("week_planner", tools=["delegate"], model="planner-model"),
                _manifest("ping", tools=["list_dir"], model="other-model"),
            ],
        )
        manifest = workspace / "agents" / "week_planner.yaml"
        spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        spec["system_prompt"] = "Your specialists are: task_agent. Use only those."
        manifest.write_text(yaml.safe_dump(spec), encoding="utf-8")

        runtime = _runtime(
            workspace,
            tmp_path / "loose.db",
            {
                "master-model": [
                    _delegate_to("week_planner", "go", "m1"),
                    text_response("done"),
                ],
                "planner-model": [
                    _delegate_to("ping", "list the workspace", "p1"),
                    text_response("listed"),
                ],
                "other-model": [text_response("here is the listing")],
            },
        )
        try:
            trace = RunTrace.disabled(agent="master")
            result = runtime.run_agent("master", "go", trace=trace)
            assert result.ok
            assert ("week_planner", "ping", 2) in _delegations(trace)
        finally:
            runtime.close()
