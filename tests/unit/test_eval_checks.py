"""Every check, against hand-built results and traces.

These matter more than they look: a broken check makes a suite report a number
that is confidently wrong, which is worse than having no number at all.
"""

from __future__ import annotations

import pytest

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.core.types import Message
from personal_ai_os.evaluation.checks import (
    RunContext,
    claimed_items,
    known_checks,
    run_check,
)
from personal_ai_os.memory.tasks import TaskStatus, TaskStore
from personal_ai_os.observability.trace import Events, TraceEvent


def result(
    *,
    ok: bool = True,
    stop: StopReason = StopReason.ANSWERED,
    output: str = "done",
    iterations: int = 2,
    tool_calls: int = 1,
) -> AgentResult:
    return AgentResult(
        ok=ok,
        agent="task_agent",
        run_id="r",
        stop_reason=stop,
        output=output,
        iterations=iterations,
        tool_calls=tool_calls,
    )


def event(type_: str, **data) -> TraceEvent:
    return TraceEvent(seq=1, ts="t", run_id="r", type=type_, data=data)


def ctx(*, res: AgentResult | None = None, events=None, store=None) -> RunContext:
    return RunContext(result=res or result(), events=list(events or []), store=store)


def outcome(name: str, context: RunContext, **params):
    return run_check(name, context, params)


class TestRegistry:
    def test_unknown_check_fails_loudly_rather_than_passing(self):
        assert not run_check("nope", ctx(), {}).passed

    def test_a_raising_check_fails_instead_of_aborting_the_suite(self):
        # task_field_is with no params raises KeyError internally.
        assert not run_check("task_field_is", ctx(), {}).passed

    def test_registry_is_populated(self):
        assert "answered" in known_checks()


class TestResultChecks:
    def test_answered_passes_on_a_clean_run(self):
        assert outcome("answered", ctx()).passed

    def test_answered_fails_on_max_iterations(self):
        r = result(ok=False, stop=StopReason.MAX_ITERATIONS)
        got = outcome("answered", ctx(res=r))
        assert not got.passed and "max_iterations" in got.detail

    def test_max_iterations_under(self):
        c = ctx(res=result(iterations=3))
        assert outcome("max_iterations_under", c, value=4).passed
        assert not outcome("max_iterations_under", c, value=2).passed

    def test_output_contains_is_case_insensitive(self):
        c = ctx(res=result(output="Your PASSPORT is due"))
        assert outcome("output_contains", c, value="passport").passed

    def test_output_not_contains(self):
        c = ctx(res=result(output="nothing here"))
        assert outcome("output_not_contains", c, value="passport").passed
        assert not outcome("output_not_contains", c, value="nothing").passed


class TestTraceChecks:
    def test_called_tool(self):
        c = ctx(events=[event(Events.TOOL_REQUESTED, tool="add_task")])
        assert outcome("called_tool", c, value="add_task").passed
        assert not outcome("called_tool", c, value="list_tasks").passed

    def test_did_not_call_tool(self):
        c = ctx(events=[event(Events.TOOL_REQUESTED, tool="add_task")])
        assert outcome("did_not_call_tool", c, value="delete_task").passed
        assert not outcome("did_not_call_tool", c, value="add_task").passed

    def test_first_tool_is_respects_order(self):
        c = ctx(
            events=[
                event(Events.TOOL_REQUESTED, tool="list_tasks"),
                event(Events.TOOL_REQUESTED, tool="complete_task"),
            ]
        )
        assert outcome("first_tool_is", c, value="list_tasks").passed
        assert not outcome("first_tool_is", c, value="complete_task").passed

    def test_first_tool_is_fails_when_no_tool_was_called(self):
        assert not outcome("first_tool_is", ctx(), value="add_task").passed

    def test_no_invalid_arguments(self):
        clean = ctx(events=[event(Events.TOOL_RESULT, tool="add_task", ok=True)])
        assert outcome("no_invalid_arguments", clean).passed

        dirty = ctx(
            events=[
                event(
                    Events.TOOL_RESULT,
                    tool="add_task",
                    ok=False,
                    error="invalid arguments for add_task: due_date: ...",
                )
            ]
        )
        assert not outcome("no_invalid_arguments", dirty).passed

    def test_execution_errors_are_not_counted_as_invalid_arguments(self):
        """A tool that ran and failed is a different failure from one that
        never ran because its arguments did not validate."""
        c = ctx(
            events=[
                event(Events.TOOL_RESULT, tool="read_file", ok=False, error="no such file")
            ]
        )
        assert outcome("no_invalid_arguments", c).passed

    def test_no_permission_denials(self):
        granted = ctx(events=[event(Events.PERMISSION_DECISION, tool="add_task", granted=True)])
        assert outcome("no_permission_denials", granted).passed

        denied = ctx(events=[event(Events.PERMISSION_DECISION, tool="add_task", granted=False)])
        assert not outcome("no_permission_denials", denied).passed

    def test_delegated_to(self):
        c = ctx(events=[event(Events.DELEGATE_START, child_agent="task_agent")])
        assert outcome("delegated_to", c, value="task_agent").passed
        assert not outcome("delegated_to", c, value="finance").passed


class TestRecovery:
    def test_passes_when_no_errors_occurred(self):
        got = outcome("recovered_after_error", ctx())
        assert got.passed and "no tool errors" in got.detail

    def test_passes_when_an_error_occurred_but_it_still_answered(self):
        c = ctx(events=[event(Events.TOOL_RESULT, tool="x", ok=False, error="boom")])
        got = outcome("recovered_after_error", c)
        assert got.passed and "recovered" in got.detail

    def test_fails_when_an_error_ended_the_run(self):
        c = ctx(
            res=result(ok=False, stop=StopReason.MAX_ITERATIONS),
            events=[event(Events.TOOL_RESULT, tool="x", ok=False, error="boom")],
        )
        assert not outcome("recovered_after_error", c).passed


class TestClaimExtraction:
    def test_finds_markdown_bullets(self):
        items = claimed_items("Here are your tasks:\n- Renew passport\n* Buy oat milk")
        assert "Renew passport" in items and "Buy oat milk" in items

    def test_finds_numbered_lines(self):
        assert "Renew passport" in claimed_items("1. Renew passport\n2. Buy oat milk")

    def test_finds_json_titles(self):
        """The shape the observed hallucination actually took."""
        text = '```json\n[{"id": 2, "title": "Schedule dentist appointment"}]\n```'
        assert "Schedule dentist appointment" in claimed_items(text)

    def test_strips_status_decoration(self):
        assert "Buy oat milk" in claimed_items("- **Buy oat milk** (Completed)")

    def test_strips_leading_ids(self):
        assert "Renew passport" in claimed_items("- #3 Renew passport")

    def test_deduplicates(self):
        items = claimed_items('- Buy oat milk\n- Buy oat milk\n"Buy oat milk"')
        assert items.count("Buy oat milk") == 1


class TestGroundedness:
    """Deterministic hallucination detection (ADR-025)."""

    def grounded_ctx(self, store, output: str, objective: str = "What is on my list?"):
        res = result(output=output)
        res.transcript = [Message.user(objective)]
        return ctx(res=res, store=store)

    def test_reporting_real_tasks_passes(self, store, tasks: TaskStore):
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        c = self.grounded_ctx(store, "You have:\n- Renew passport\n- Buy oat milk")
        assert outcome("no_unsupported_task_claims", c).passed

    def test_the_observed_hallucination_is_caught(self, store, tasks: TaskStore):
        """Verbatim from a real qwen2.5:7b run in Phase 4."""
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        tasks.add("Submit thesis draft")
        output = (
            'I have marked the task "Renew passport" as done. Here are your '
            "current tasks:\n\n```json\n[\n  {\n    \"id\": 2,\n"
            '    "title": "Schedule dentist appointment",\n    "status": "todo"\n'
            "  }\n]\n```"
        )
        got = outcome("no_unsupported_task_claims", self.grounded_ctx(store, output))
        assert not got.passed
        assert "Schedule dentist appointment" in got.detail

    def test_completed_decoration_still_counts_as_grounded(self, store, tasks):
        tasks.add("Buy oat milk")
        c = self.grounded_ctx(store, "- Buy oat milk (Completed)")
        assert outcome("no_unsupported_task_claims", c).passed

    def test_meta_commentary_is_not_flagged(self, store, tasks: TaskStore):
        """'You have 3 tasks' must not read as a task called that."""
        tasks.add("Renew passport")
        c = self.grounded_ctx(
            store, "You have 3 tasks currently pending.\n- Renew passport"
        )
        assert outcome("no_unsupported_task_claims", c).passed

    def test_an_honest_empty_answer_passes(self, store):
        c = self.grounded_ctx(store, "You have no tasks on your list right now.")
        assert outcome("no_unsupported_task_claims", c).passed

    def test_inventing_from_an_empty_database_is_caught(self, store):
        """Nothing real to anchor to, so anything stated is fabricated."""
        c = self.grounded_ctx(store, "- Buy groceries\n- Call the dentist")
        got = outcome("no_unsupported_task_claims", c)
        assert not got.passed
        assert "Buy groceries" in got.detail

    def test_repeating_the_users_own_words_is_not_invention(self, store):
        """Echoing the request back before a tool returns is fine."""
        c = self.grounded_ctx(
            store,
            '- Buy oat milk',
            objective="Add a task to buy oat milk.",
        )
        assert outcome("no_unsupported_task_claims", c).passed

    def test_single_word_claims_are_skipped(self, store, tasks: TaskStore):
        """Too little signal to judge; flagging them would be noise."""
        tasks.add("Renew passport")
        c = self.grounded_ctx(store, "- Renew passport\n- Groceries")
        assert outcome("no_unsupported_task_claims", c).passed

    def test_padding_a_short_list_is_caught(self, store, tasks: TaskStore):
        tasks.add("Renew passport")
        c = self.grounded_ctx(
            store,
            "- Renew passport\n- Water the plants\n- Book flight tickets",
        )
        got = outcome("no_unsupported_task_claims", c)
        assert not got.passed
        assert "Water the plants" in got.detail

    def test_threshold_is_configurable(self, store, tasks: TaskStore):
        tasks.add("Renew passport")
        c = self.grounded_ctx(store, "- Renew driving licence")
        # "renew" is grounded, "driving"/"licence" are not -> 1/3 supported.
        assert outcome("no_unsupported_task_claims", c, threshold=0.3).passed
        assert not outcome("no_unsupported_task_claims", c, threshold=0.5).passed

    def test_reports_clearly_without_a_store(self):
        got = outcome("no_unsupported_task_claims", ctx())
        assert not got.passed and "no store" in got.detail


class TestGroundednessFalsePositives:
    """Regressions from the detector's first version.

    Both of these were flagged as hallucinations on a real run when the agent
    had behaved correctly. A detector that cries wolf is worse than none -- it
    would have reported a 55% hallucination rate that did not exist.
    """

    def grounded_ctx(self, store, output: str, objective: str = "What is on my list?"):
        res = result(output=output)
        res.transcript = [Message.user(objective)]
        return ctx(res=res, store=store)

    def test_apostrophes_do_not_create_phantom_claims(self):
        """"couldn't find a task titled" once yielded "t find a task titled"."""
        items = claimed_items('I couldn\'t find a task titled "dentist appointment".')
        assert not any("t find" in i for i in items), items
        assert "dentist appointment" in items

    def test_a_real_due_date_is_not_an_invention(self, store, tasks: TaskStore):
        """Grounding on titles alone made stored dates look fabricated."""
        tasks.add("Renew passport", due_date="2026-09-07")
        c = self.grounded_ctx(store, "1. **Renew passport** - Due on 2026-09-07")
        assert outcome("no_unsupported_task_claims", c).passed

    def test_a_real_priority_is_not_an_invention(self, store, tasks: TaskStore):
        from personal_ai_os.memory.tasks import TaskPriority

        tasks.add("Submit thesis draft", priority=TaskPriority.HIGH)
        c = self.grounded_ctx(store, "1. **Submit thesis draft** - High priority")
        assert outcome("no_unsupported_task_claims", c).passed

    def test_a_correctly_reported_list_passes_verbatim(self, store, tasks: TaskStore):
        """The exact output of a real, correct qwen2.5:7b run."""
        from personal_ai_os.memory.tasks import TaskPriority

        tasks.add("Renew passport", due_date="2026-09-07")
        tasks.add("Buy oat milk")
        tasks.add("Submit thesis draft", priority=TaskPriority.HIGH)
        output = (
            "Here are your current tasks:\n\n"
            "1. **Submit thesis draft** - High priority (not due yet)\n"
            "2. **Renew passport** - Due on 2026-09-07\n"
            "3. **Buy oat milk** (not due yet)\n\n"
            "Let me know if you need help with any of these!"
        )
        got = outcome("no_unsupported_task_claims", self.grounded_ctx(store, output))
        assert got.passed, got.detail

    def test_a_correct_refusal_passes_verbatim(self, store, tasks: TaskStore):
        """Also from a real run -- the agent declined honestly and was flagged.

        Naming a thing in order to say it does *not* exist is the opposite of
        inventing it. Here the name is grounded because the user supplied it,
        which is why the transcript is part of the grounding set.
        """
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        output = (
            'I couldn\'t find a task titled "dentist appointment" in your list. '
            'The open tasks are "Renew passport" and "Buy oat milk". '
            "Could you please confirm which task you completed?"
        )
        c = self.grounded_ctx(
            store, output, objective="I finished the dentist appointment task."
        )
        got = outcome("no_unsupported_task_claims", c)
        assert got.passed, got.detail

    def test_it_still_catches_a_real_invention_after_the_fixes(self, store, tasks):
        """The loosening must not have blinded it."""
        tasks.add("Renew passport", due_date="2026-09-07")
        c = self.grounded_ctx(
            store, "1. **Renew passport** - Due on 2026-09-07\n2. **Call the dentist**"
        )
        got = outcome("no_unsupported_task_claims", c)
        assert not got.passed
        assert "Call the dentist" in got.detail


class TestStoreChecks:
    def test_task_count(self, store, tasks: TaskStore):
        tasks.add("one")
        c = ctx(store=store)
        assert outcome("task_count", c, value=1).passed
        assert not outcome("task_count", c, value=2).passed

    def test_task_field_absent_passes_when_unset(self, store, tasks: TaskStore):
        tasks.add("plain")
        assert outcome("task_field_absent", ctx(store=store), value="due_date").passed

    def test_task_field_absent_fails_when_invented(self, store, tasks: TaskStore):
        """The embellishment check: a date the user never gave."""
        tasks.add("invented", due_date="2026-09-07")
        got = outcome("task_field_absent", ctx(store=store), value="due_date")
        assert not got.passed and "2026-09-07" in got.detail

    def test_task_field_is_unwraps_enums(self, store, tasks: TaskStore):
        tasks.add("normal one")
        assert outcome(
            "task_field_is", ctx(store=store), field="priority", value="normal"
        ).passed

    def test_single_task_checks_fail_when_ambiguous(self, store, tasks: TaskStore):
        tasks.add("one")
        tasks.add("two")
        got = outcome("task_field_absent", ctx(store=store), value="due_date")
        assert not got.passed and "exactly 1" in got.detail

    def test_task_matching_finds_by_title(self, store, tasks: TaskStore):
        tasks.add("Renew passport")
        milk = tasks.add("Buy oat milk")
        assert milk.id is not None
        tasks.complete(milk.id)

        c = ctx(store=store)
        assert outcome("task_matching", c, title="oat milk", field="status", value="done").passed
        assert outcome("task_matching", c, title="passport", field="status", value="todo").passed

    def test_task_matching_fails_when_the_title_is_ambiguous(self, store, tasks):
        tasks.add("buy milk")
        tasks.add("buy milk again")
        got = outcome("task_matching", ctx(store=store), title="buy milk", field="status", value="todo")
        assert not got.passed and "matched" in got.detail

    def test_task_title_contains(self, store, tasks: TaskStore):
        tasks.add("Write the Finance Agent")
        c = ctx(store=store)
        assert outcome("task_title_contains", c, value="finance agent").passed
        assert not outcome("task_title_contains", c, value="passport").passed

    def test_store_checks_report_clearly_without_a_store(self):
        got = outcome("task_count", ctx(), value=1)
        assert not got.passed and "no store" in got.detail
