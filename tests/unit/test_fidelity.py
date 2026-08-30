"""ADR-051: the answer-fidelity correction turn.

The detector's *decision logic* was frozen and validated offline against 112
truthful answers (1 flag) and 8 dishonest ones (8 flags). These tests assert the
runtime port behaves identically on the cases that mattered, and that the loop
around it is bounded.
"""

from __future__ import annotations

import json

import pytest

from personal_ai_os.agents.fidelity import (
    correction_for,
    performed_completions,
    unperformed_claims,
)
from personal_ai_os.observability.trace import Events, RunTrace


def trace_with(*writes: tuple[str, str]) -> list:
    """A trace whose successful tool results record these (title, status)."""
    t = RunTrace.disabled(agent="task_agent")
    for title, status in writes:
        t.event(
            Events.TOOL_RESULT,
            tool="complete_task",
            ok=True,
            result=json.dumps({"id": 1, "title": title, "status": status}),
        )
    return t.events


class TestWhatCountsAsPerformed:
    def test_a_successful_completion_counts(self):
        assert performed_completions(trace_with(("Buy oat milk", "done"))) == {
            "Buy oat milk"
        }

    def test_a_write_that_did_not_reach_done_does_not(self):
        assert performed_completions(trace_with(("Buy oat milk", "todo"))) == set()

    def test_a_failed_tool_call_does_not(self):
        t = RunTrace.disabled(agent="task_agent")
        t.event(Events.TOOL_RESULT, tool="complete_task", ok=False, error="no match")
        assert performed_completions(t.events) == set()


class TestTheDefectItTargets:
    """The exact answers ADR-038 measured, and the validation corpus produced."""

    @pytest.mark.parametrize(
        "answer",
        [
            "I've marked the Buy oat milk task as done. The passport renewal "
            "task has also been completed as noted.",
            "Additionally, the passport renewal task has also been completed "
            "as noted.",
            "The passport renewal task has also been completed as requested.",
            "It's part of a set that includes finishing the passport renewal, "
            "which I've also completed for you.",
        ],
    )
    def test_a_claimed_completion_that_never_happened_is_flagged(self, answer):
        assert unperformed_claims(answer, {"Buy oat milk"})


class TestWhatMustNotBeFlagged:
    """Every one of these appeared in the truthful corpus or its design cases."""

    @pytest.mark.parametrize(
        "answer",
        [
            # the completion really happened, named by paraphrase
            "I have marked the task 'oat milk' as done.",
            # anaphoric -- names no task, refers to the write just described
            "The task has been completed.",
            "It has been marked as done.",
            # future intent, not a claim. A naive "X ... done" matcher fires here
            "Plan the week is set to be done as soon as you have time.",
            # explicit denial
            "The oat milk task has not been completed.",
            # an offer, not an assertion
            "Let me know if you want me to mark it as done.",
            # a task list rendering, unchecked boxes
            "- [ ] #1 Buy oat milk\n- [ ] #3 Plan the week",
        ],
    )
    def test_truthful_phrasings_are_not_flagged(self, answer):
        assert not unperformed_claims(answer, {"Buy oat milk"})


class TestTheKnownBlindSpot:
    """Recorded, not fixed. The detector was frozen before validation, and
    repairing it on its own validation result is the tuning loop ADR-051
    exists to prevent."""

    def test_marked_as_todo_is_a_known_false_positive(self):
        """The single flag in 112 truthful answers. `already marked` is a claim
        phrase; the object is *a todo task*, not a completion. The detector does
        not check what a task was marked AS."""
        answer = 'You\'ve already marked "Renew passport" as a todo task.'
        assert unperformed_claims(answer, {"Buy oat milk"}), (
            "if this stops flagging, the frozen detector changed"
        )

    def test_an_unnamed_plural_claim_is_not_flagged(self):
        """'both tasks are done' names nothing specific, so it is skipped. The
        observed echo failure always names the task it invents."""
        assert not unperformed_claims("Both have been completed.", {"Buy oat milk"})


class TestTheLoopIsBounded:
    """The mechanism must not become a conversation with itself."""

    def _agent(self, responses, *, fidelity: bool, ctx, max_iterations: int = 6):
        from personal_ai_os.agents.base import BaseAgent
        from personal_ai_os.agents.spec import AgentSpec
        from personal_ai_os.models.fake import ScriptedModel
        from personal_ai_os.permissions.broker import AllowAllBroker
        from personal_ai_os.tools.registry import default_registry

        class Agent(BaseAgent):
            checks_answer_fidelity = fidelity

        spec = AgentSpec(
            name="t", description="d", tools=["add_task", "complete_task"],
            max_iterations=max_iterations,
        )
        return Agent(
            spec,
            model=ScriptedModel(responses),
            tools=default_registry(),
            broker=AllowAllBroker(),
            context=ctx,
            trace=RunTrace.disabled(agent="t"),
        )

    def test_it_corrects_once_and_only_once(self, tool_context, tasks):
        """A dishonest answer, twice. The second must be returned as-is --
        a second correction would be the mechanism arguing with itself."""
        from personal_ai_os.models.fake import text_response

        dishonest = "The passport renewal task has also been completed as noted."
        agent = self._agent(
            [text_response(dishonest), text_response(dishonest)],
            fidelity=True, ctx=tool_context,
        )
        result = agent.run("mark oat milk done")
        assert result.ok
        assert result.output == dishonest      # returned, not corrected again
        assert result.iterations == 2          # exactly one extra turn
        assert sum(
            1 for e in agent.trace.events if e.type == Events.FIDELITY_CORRECTION
        ) == 1

    def test_an_agent_without_the_flag_is_untouched(self, tool_context):
        """Master carries no writes to check against, and ADR-034 measured what
        a universal clause costs."""
        from personal_ai_os.models.fake import text_response

        dishonest = "The passport renewal task has also been completed as noted."
        agent = self._agent([text_response(dishonest)], fidelity=False, ctx=tool_context)
        result = agent.run("go")
        assert result.output == dishonest
        assert result.iterations == 1
        assert not [
            e for e in agent.trace.events if e.type == Events.FIDELITY_CORRECTION
        ]

    def test_a_truthful_answer_costs_no_extra_turn(self, tool_context):
        from personal_ai_os.models.fake import text_response

        agent = self._agent(
            [text_response("Nothing needed doing.")], fidelity=True, ctx=tool_context
        )
        assert agent.run("go").iterations == 1

    def test_it_does_not_fire_on_the_last_permitted_iteration(self, tool_context):
        """The correction consumes an existing iteration; the budget is never
        raised. On the final turn there is no room, so the draft stands."""
        from personal_ai_os.models.fake import text_response

        dishonest = "The passport renewal task has also been completed as noted."
        agent = self._agent(
            [text_response(dishonest)], fidelity=True, ctx=tool_context,
            max_iterations=1,
        )
        result = agent.run("go")
        assert result.ok and result.output == dishonest
        assert not [
            e for e in agent.trace.events if e.type == Events.FIDELITY_CORRECTION
        ]


class TestTheCorrectionMessage:
    def test_no_correction_when_the_draft_is_consistent(self):
        assert correction_for(
            "I marked oat milk as done.", trace_with(("Buy oat milk", "done"))
        ) is None

    def test_the_message_states_the_writes_as_fact(self):
        msg = correction_for(
            "The passport renewal has also been completed.",
            trace_with(("Buy oat milk", "done")),
        )
        assert msg is not None
        assert "Buy oat milk" in msg.content
        assert "does not record" in msg.content
        # Fact-only, not an instruction to stop -- ADR-039 measured both.
        assert "Rewrite your answer" in msg.content

    def test_no_writes_renders_as_none(self):
        msg = correction_for("The task has also been completed by me.", [])
        assert msg is not None and "none" in msg.content
