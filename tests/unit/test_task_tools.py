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
