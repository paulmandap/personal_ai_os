"""Tool schemas, the filesystem jail, and the builtin tools."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from personal_ai_os.core.errors import (
    PathNotAllowedError,
    ToolExecutionError,
    ToolInputError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext, resolve_within_roots
from personal_ai_os.tools.builtin.list_dir import ListDirTool
from personal_ai_os.tools.builtin.read_file import ReadFileTool
from personal_ai_os.tools.registry import ToolRegistry, default_registry


class TestPathJail:
    """The jail bounds which paths are nameable at all.

    It is orthogonal to permissions: a read that policy auto-approves still
    cannot reach outside the workspace.
    """

    def test_accepts_a_path_inside_a_root(self, workspace: Path):
        resolved = resolve_within_roots("README.md", [workspace])
        assert resolved == (workspace / "README.md").resolve()

    def test_relative_paths_resolve_against_the_first_root(self, workspace: Path):
        assert resolve_within_roots("src/app.py", [workspace]).parent.name == "src"

    def test_rejects_parent_traversal(self, workspace: Path):
        with pytest.raises(PathNotAllowedError):
            resolve_within_roots("../../../Windows/System32/config", [workspace])

    def test_rejects_an_absolute_path_outside(self, workspace: Path):
        outside = Path(os.environ.get("SystemRoot", "/etc")) / "hosts"
        with pytest.raises(PathNotAllowedError):
            resolve_within_roots(outside, [workspace])

    def test_rejects_a_sibling_with_a_shared_prefix(self, tmp_path: Path):
        """`/work` must not be treated as containing `/workspace-evil`."""
        root = tmp_path / "work"
        root.mkdir()
        sibling = tmp_path / "work-evil"
        sibling.mkdir()
        with pytest.raises(PathNotAllowedError):
            resolve_within_roots(sibling / "x.txt", [root])

    @pytest.mark.skipif(os.name != "nt", reason="Windows path casing")
    def test_case_differences_still_match_on_windows(self, workspace: Path):
        shouty = Path(str(workspace).upper())
        assert resolve_within_roots("README.md", [shouty])

    def test_no_roots_configured_refuses_everything(self):
        with pytest.raises(PathNotAllowedError, match="no allowed"):
            resolve_within_roots("anything", [])

    def test_accepts_the_root_itself(self, workspace: Path):
        assert resolve_within_roots(workspace, [workspace]) == workspace.resolve()


class TestToolSchema:
    def test_schema_is_derived_from_the_input_model(self):
        schema = ReadFileTool().schema()
        assert schema.name == "read_file"
        assert "path" in schema.parameters["properties"]
        assert schema.parameters["required"] == ["path"]

    def test_openai_format_wraps_the_function(self):
        wire = ReadFileTool().schema().to_openai_format()
        assert wire["type"] == "function"
        assert wire["function"]["name"] == "read_file"
        assert "parameters" in wire["function"]

    def test_descriptions_survive_into_the_schema(self):
        """Field descriptions are what the model actually reads."""
        props = ReadFileTool().schema().parameters["properties"]
        assert props["path"]["description"]
        assert props["max_bytes"]["description"]


class TestInputValidation:
    def test_missing_required_field_is_a_tool_input_error(self):
        with pytest.raises(ToolInputError, match="path"):
            ReadFileTool().validate_input({})

    def test_out_of_range_value_is_reported_with_the_field_name(self):
        with pytest.raises(ToolInputError, match="max_bytes"):
            ReadFileTool().validate_input({"path": "x", "max_bytes": 10_000_000})

    def test_valid_arguments_produce_a_typed_model(self):
        args = ReadFileTool().validate_input({"path": "README.md"})
        assert args.path == "README.md"


class TestReadFile:
    def test_reads_a_file(self, tool_context: ToolContext):
        tool = ReadFileTool()
        out = tool.run(tool.validate_input({"path": "README.md"}), tool_context)
        assert "Test Workspace" in out.content
        assert out.truncated is False

    def test_truncates_and_says_so(self, tool_context: ToolContext):
        tool = ReadFileTool()
        out = tool.run(
            tool.validate_input({"path": "README.md", "max_bytes": 5}), tool_context
        )
        assert out.truncated is True
        assert len(out.content) == 5
        assert out.size_bytes > 5

    def test_missing_file_reports_clearly(self, tool_context: ToolContext):
        tool = ReadFileTool()
        with pytest.raises(ToolExecutionError, match="no such file"):
            tool.run(tool.validate_input({"path": "nope.txt"}), tool_context)

    def test_directory_is_redirected_to_list_dir(self, tool_context: ToolContext):
        tool = ReadFileTool()
        with pytest.raises(ToolExecutionError, match="list_dir"):
            tool.run(tool.validate_input({"path": "src"}), tool_context)

    def test_escape_attempt_is_refused(self, tool_context: ToolContext):
        tool = ReadFileTool()
        with pytest.raises(PathNotAllowedError):
            tool.run(tool.validate_input({"path": "../../secrets.txt"}), tool_context)

    def test_declares_read_permission(self):
        assert ReadFileTool().permission is PermissionLevel.READ


class TestListDir:
    def test_lists_entries(self, tool_context: ToolContext):
        tool = ListDirTool()
        out = tool.run(tool.validate_input({}), tool_context)
        names = {e.name for e in out.entries}
        assert {"README.md", "src", "config"} <= names

    def test_directories_sort_before_files(self, tool_context: ToolContext):
        tool = ListDirTool()
        out = tool.run(tool.validate_input({}), tool_context)
        types = [e.type for e in out.entries]
        assert types == sorted(types, key=lambda t: t != "dir")

    def test_noise_directories_are_hidden(self, tool_context: ToolContext, workspace):
        (workspace / "__pycache__").mkdir()
        tool = ListDirTool()
        out = tool.run(tool.validate_input({}), tool_context)
        assert "__pycache__" not in {e.name for e in out.entries}

    def test_file_is_redirected_to_read_file(self, tool_context: ToolContext):
        tool = ListDirTool()
        with pytest.raises(ToolExecutionError, match="read_file"):
            tool.run(tool.validate_input({"path": "README.md"}), tool_context)


class TestTimeoutGuard:
    def test_a_slow_tool_raises_rather_than_hanging(self, tool_context: ToolContext):
        import time

        class SlowInput(BaseModel):
            pass

        class SlowTool(Tool):
            name = "slow"
            description = "sleeps"
            Input = SlowInput
            Output = SlowInput
            permission = PermissionLevel.READ
            timeout_s = 0.05

            def run(self, args, ctx):
                time.sleep(3)
                return SlowInput()

        with pytest.raises(ToolTimeoutError):
            SlowTool().execute(SlowInput(), tool_context)


class TestToolRegistry:
    def test_default_registry_has_the_builtins(self):
        assert default_registry().names() == [
            "add_task",
            "complete_task",
            "delegate",
            "list_dir",
            "list_tasks",
            "read_file",
            "update_task",
        ]

    def test_unknown_tool_lists_what_is_available(self):
        with pytest.raises(ToolNotFoundError, match="read_file"):
            default_registry().get("send_email")

    def test_duplicate_registration_is_rejected(self):
        registry = ToolRegistry([ReadFileTool()])
        with pytest.raises(ValueError, match="already registered"):
            registry.register(ReadFileTool())

    def test_shadowing_is_allowed_when_explicit(self):
        registry = ToolRegistry([ReadFileTool()])
        registry.register(ReadFileTool(), replace=True)
        assert len(registry) == 1

    def test_permissions_for_reports_every_level_involved(self):
        levels = default_registry().permissions_for(["read_file", "list_dir"])
        assert levels == {PermissionLevel.READ}

    def test_schemas_can_be_filtered_to_a_subset(self):
        schemas = default_registry().schemas(["read_file"])
        assert [s.name for s in schemas] == ["read_file"]
