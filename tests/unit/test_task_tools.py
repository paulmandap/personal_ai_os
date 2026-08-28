"""Task tools -- the first tools that persist, and the first at `write` level."""

from __future__ import annotations

import pytest

from personal_ai_os.core.errors import ToolExecutionError, ToolInputError
from personal_ai_os.memory.tasks import TaskPriority, TaskStatus, TaskStore
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.builtin.tasks import (
    AddTaskTool,
    CompleteTaskTool,
    ListTasksTool,
    UpdateTaskTool,
)


def call(tool, args: dict, ctx: ToolContext):
    """Validate then run, the way the agent loop does."""
    return tool.run(tool.validate_input(args), ctx)


class TestPermissionLevels:
    def test_mutations_are_write_and_reads_are_read(self):
        assert AddTaskTool().permission is PermissionLevel.WRITE
        assert UpdateTaskTool().permission is PermissionLevel.WRITE
        assert CompleteTaskTool().permission is PermissionLevel.WRITE
        assert ListTasksTool().permission is PermissionLevel.READ


class TestAddTask:
    def test_creates_a_task(self, tool_context: ToolContext, tasks: TaskStore):
        result = call(AddTaskTool(), {"title": "Write the docs"}, tool_context)
        assert result.id is not None
        assert tasks.count() == 1

    def test_accepts_priority_and_due_date(self, tool_context: ToolContext):
        result = call(
            AddTaskTool(),
            {"title": "Ship", "priority": "high", "due_date": "2026-09-07"},
            tool_context,
        )
        assert result.priority is TaskPriority.HIGH
        assert result.due_date == "2026-09-07"

    def test_missing_title_is_a_recoverable_input_error(self, tool_context):
        with pytest.raises(ToolInputError, match="title"):
            AddTaskTool().validate_input({})

    def test_bad_due_date_is_caught_at_validation(self, tool_context):
        with pytest.raises(ToolInputError, match="due_date"):
            AddTaskTool().validate_input({"title": "x", "due_date": "next tuesday"})

    def test_bad_priority_is_caught_at_validation(self, tool_context):
        with pytest.raises(ToolInputError, match="priority"):
            AddTaskTool().validate_input({"title": "x", "priority": "urgent"})

    def test_approval_prompt_shows_the_title(self, tool_context):
        """The prompt must say what is being written, not just 'add_task'."""
        tool = AddTaskTool()
        args = tool.validate_input({"title": "Delete production"})
        assert "Delete production" in tool.describe_resource(args)


class TestAddTaskDuplicateGuard:
    """The referent check `add_task` was missing (ADR-030).

    The titles here are the ones qwen2.5:7b actually produced when told to
    leave an existing "Renew passport" task alone -- not invented examples.
    """

    @pytest.mark.parametrize(
        "invented",
        [
            "Apply for passport renewal",
            "Get passport renewed",
            "Get passport",
            "Apply for passport",
        ],
    )
    def test_a_reworded_copy_is_refused(self, tool_context, tasks: TaskStore, invented):
        tasks.add("Renew passport")
        with pytest.raises(ToolExecutionError, match="already has"):
            call(AddTaskTool(), {"title": invented}, tool_context)
        assert tasks.count() == 1

    def test_the_refusal_names_the_task_and_its_status(self, tool_context, tasks):
        tasks.add("Renew passport")
        with pytest.raises(ToolExecutionError) as exc:
            call(AddTaskTool(), {"title": "Apply for passport renewal"}, tool_context)
        # The agent answers from this message, so it has to carry the fact.
        assert "Renew passport" in str(exc.value)
        assert "todo" in str(exc.value)

    @pytest.mark.parametrize(
        ("existing", "wanted"),
        [
            ("Buy bread", "Buy milk"),
            ("Call dad", "Call mum"),
            ("Book hotel", "Book flights"),
            ("Submit timesheet", "Submit report"),
            ("Renew licence", "Renew passport"),
            ("Renew passport", "Book dentist appointment"),
        ],
    )
    def test_sharing_only_a_leading_verb_is_not_a_duplicate(
        self, tool_context, tasks: TaskStore, existing, wanted
    ):
        """The false-positive case that killed the obvious implementation.

        Two-word titles are verb plus object; scoring on overlap alone flags
        every pair that starts with the same verb, which would block most
        legitimate adds.
        """
        tasks.add(existing)
        call(AddTaskTool(), {"title": wanted}, tool_context)
        assert tasks.count() == 2

    def test_a_finished_task_does_not_block_a_new_one(self, tool_context, tasks):
        """You renew a passport more than once in a lifetime."""
        created = tasks.add("Renew passport")
        assert created.id is not None
        tasks.complete(created.id)
        call(AddTaskTool(), {"title": "Renew passport"}, tool_context)
        assert tasks.count() == 2

    def test_confirm_duplicate_allows_a_genuine_second_task(self, tool_context, tasks):
        tasks.add("Buy oat milk")
        with pytest.raises(ToolExecutionError):
            call(AddTaskTool(), {"title": "Buy oat milk again"}, tool_context)
        call(
            AddTaskTool(),
            {"title": "Buy oat milk again", "confirm_duplicate": True},
            tool_context,
        )
        assert tasks.count() == 2

    def test_an_empty_list_never_collides(self, tool_context, tasks: TaskStore):
        call(AddTaskTool(), {"title": "Renew passport"}, tool_context)
        assert tasks.count() == 1


class TestListTasks:
    def test_returns_structured_and_rendered_output(
        self, tool_context: ToolContext, tasks: TaskStore
    ):
        tasks.add("first")
        tasks.add("second")
        result = call(ListTasksTool(), {}, tool_context)
        assert result.count == 2
        assert "first" in result.summary and "second" in result.summary

    def test_empty_list_says_so_rather_than_being_blank(self, tool_context):
        result = call(ListTasksTool(), {}, tool_context)
        assert result.count == 0
        assert result.summary == "(no tasks)"

    def test_hides_completed_by_default(self, tool_context, tasks: TaskStore):
        done = tasks.add("finished")
        assert done.id is not None
        tasks.complete(done.id)
        assert call(ListTasksTool(), {}, tool_context).count == 0

    def test_include_done_reveals_them(self, tool_context, tasks: TaskStore):
        done = tasks.add("finished")
        assert done.id is not None
        tasks.complete(done.id)
        assert call(ListTasksTool(), {"include_done": True}, tool_context).count == 1


class TestUpdateAndComplete:
    def test_update_changes_a_field(self, tool_context, tasks: TaskStore):
        task = tasks.add("before")
        result = call(
            UpdateTaskTool(), {"id": task.id, "title": "after"}, tool_context
        )
        assert result.title == "after"

    def test_complete_marks_done(self, tool_context, tasks: TaskStore):
        task = tasks.add("finish me")
        result = call(CompleteTaskTool(), {"id": task.id}, tool_context)
        assert result.status is TaskStatus.DONE
        assert result.completed_at

    def test_unknown_id_points_the_model_at_list_tasks(self, tool_context):
        """A recoverable error should say how to recover."""
        with pytest.raises(ToolExecutionError, match="list_tasks"):
            call(CompleteTaskTool(), {"id": 999}, tool_context)


class TestCompleteByTitle:
    """Measured behaviour drove this: given only an `id` parameter, both the 3B
    and the 7B guessed an id instead of calling list_tasks, and completed the
    wrong task. Accepting a title removes the need to know an id at all."""

    def test_completes_the_task_named(self, tool_context, tasks: TaskStore):
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        result = call(CompleteTaskTool(), {"title": "oat milk"}, tool_context)
        assert result.title == "Buy oat milk"
        assert result.status is TaskStatus.DONE

    def test_title_match_is_case_insensitive_and_partial(self, tool_context, tasks):
        tasks.add("Buy Oat Milk Today")
        assert call(CompleteTaskTool(), {"title": "oat milk"}, tool_context).status is TaskStatus.DONE

    def test_the_other_tasks_are_left_alone(self, tool_context, tasks: TaskStore):
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        call(CompleteTaskTool(), {"title": "oat milk"}, tool_context)
        still_open = [t.title for t in tasks.list()]
        assert still_open == ["Renew passport"]

    def test_no_match_lists_the_open_tasks(self, tool_context, tasks: TaskStore):
        tasks.add("Renew passport")
        with pytest.raises(ToolExecutionError, match="Renew passport"):
            call(CompleteTaskTool(), {"title": "dentist"}, tool_context)

    def test_ambiguity_is_refused_rather_than_guessed(self, tool_context, tasks):
        """Completing whichever sorted first would be silently wrong."""
        tasks.add("Buy oat milk")
        tasks.add("Buy oat milk again")
        with pytest.raises(ToolExecutionError, match="2 open tasks match"):
            call(CompleteTaskTool(), {"title": "oat milk"}, tool_context)
        assert len(tasks.list()) == 2

    def test_already_completed_tasks_are_not_matched(self, tool_context, tasks):
        done = tasks.add("Buy oat milk")
        assert done.id is not None
        tasks.complete(done.id)
        with pytest.raises(ToolExecutionError, match="no open task matches"):
            call(CompleteTaskTool(), {"title": "oat milk"}, tool_context)

    def test_neither_argument_is_a_validation_error(self, tool_context):
        with pytest.raises(ToolInputError, match="exactly one"):
            CompleteTaskTool().validate_input({})

    def test_both_arguments_is_a_validation_error(self, tool_context):
        with pytest.raises(ToolInputError, match="exactly one"):
            CompleteTaskTool().validate_input({"id": 1, "title": "x"})

    def test_approval_prompt_shows_the_title(self, tool_context):
        tool = CompleteTaskTool()
        args = tool.validate_input({"title": "oat milk"})
        assert "oat milk" in tool.describe_resource(args)

    def test_update_unknown_id_also_recovers(self, tool_context):
        with pytest.raises(ToolExecutionError, match="list_tasks"):
            call(UpdateTaskTool(), {"id": 999, "title": "x"}, tool_context)


class TestUpdateByFind:
    """`update_task` carried the same flaw ADR-022 fixed in complete_task.

    The selector is `find`, not `title`, because `title` already means the
    *new* title on this tool -- naming both the same would make
    `update_task(title=...)` ambiguous.
    """

    def test_updates_the_task_named(self, tool_context, tasks: TaskStore):
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        result = call(
            UpdateTaskTool(), {"find": "oat milk", "priority": "high"}, tool_context
        )
        assert result.title == "Buy oat milk"
        assert result.priority is TaskPriority.HIGH

    def test_find_and_title_are_different_things(self, tool_context, tasks: TaskStore):
        """Select by `find`, rename via `title`."""
        tasks.add("Buy oat milk")
        result = call(
            UpdateTaskTool(),
            {"find": "oat milk", "title": "Buy almond milk"},
            tool_context,
        )
        assert result.title == "Buy almond milk"

    def test_a_paraphrase_resolves(self, tool_context, tasks: TaskStore):
        tasks.add("Renew passport")
        result = call(
            UpdateTaskTool(), {"find": "renewing my passport", "notes": "urgent"},
            tool_context,
        )
        assert result.title == "Renew passport"

    def test_ambiguity_is_refused(self, tool_context, tasks: TaskStore):
        tasks.add("Buy oat milk")
        tasks.add("Buy oat milk again")
        with pytest.raises(ToolExecutionError, match="2 open tasks match"):
            call(UpdateTaskTool(), {"find": "oat milk", "notes": "x"}, tool_context)

    def test_no_match_lists_the_open_tasks(self, tool_context, tasks: TaskStore):
        tasks.add("Renew passport")
        with pytest.raises(ToolExecutionError, match="Renew passport"):
            call(UpdateTaskTool(), {"find": "dentist", "notes": "x"}, tool_context)

    def test_an_id_still_works(self, tool_context, tasks: TaskStore):
        task = tasks.add("Renew passport")
        result = call(
            UpdateTaskTool(), {"id": task.id, "notes": "by id"}, tool_context
        )
        assert result.notes == "by id"

    def test_neither_selector_is_a_validation_error(self, tool_context):
        with pytest.raises(ToolInputError, match="exactly one"):
            UpdateTaskTool().validate_input({"notes": "x"})

    def test_both_selectors_is_a_validation_error(self, tool_context):
        with pytest.raises(ToolInputError, match="exactly one"):
            UpdateTaskTool().validate_input({"id": 1, "find": "x", "notes": "y"})


class TestMissingStore:
    def test_tools_report_clearly_when_no_database_was_provided(self, settings):
        """A tool handed no capability explains itself instead of crashing."""
        bare = ToolContext(workspace_root=settings.workspace_root)
        with pytest.raises(ToolExecutionError, match="not available"):
            call(ListTasksTool(), {}, bare)


class TestSchemas:
    def test_enum_fields_are_advertised_as_choices(self):
        """The model needs to see the allowed values, not guess them."""
        schema = AddTaskTool().schema().parameters
        rendered = str(schema)
        assert "high" in rendered and "normal" in rendered and "low" in rendered

    def test_every_field_carries_a_description(self):
        for tool in (AddTaskTool(), ListTasksTool(), UpdateTaskTool(), CompleteTaskTool()):
            props = tool.schema().parameters["properties"]
            missing = [k for k, v in props.items() if not v.get("description")]
            assert not missing, f"{tool.name} fields without description: {missing}"
