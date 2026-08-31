"""`fetch_page` -- the first `external_action` tool, and the seeding behind it.

Offline by construction: the tool has no HTTP client, so there is nothing here
for `tests/unit/conftest.py`'s socket block to catch. That is the design (see
the module docstring), not an accident of the tests.
"""

from __future__ import annotations

import pytest

from personal_ai_os.core.errors import ToolExecutionError
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import ToolContext
from personal_ai_os.tools.builtin.web import WEB_PAGES, MAX_CHARS, FetchPageTool
from personal_ai_os.tools.registry import default_registry


def ctx_with(pages: dict[str, str] | None, tmp_path) -> ToolContext:
    extras = {} if pages is None else {WEB_PAGES: pages}
    return ToolContext(workspace_root=tmp_path, extras=extras)


def call(tool, args: dict, ctx):
    return tool.run(tool.validate_input(args), ctx)


class TestPermissionLevel:
    def test_it_is_external_action_not_read(self):
        """The level names the consequence the action will carry once a
        transport exists. Shipping it as `read` and promoting it later would
        change the user's permission surface underneath them."""
        assert FetchPageTool().permission is PermissionLevel.EXTERNAL_ACTION

    def test_it_is_registered(self):
        assert "fetch_page" in default_registry().names()

    def test_it_is_the_only_external_action_tool(self):
        """Recorded so the next one is a deliberate act. Nothing used this level
        before `fetch_page`."""
        external = [
            t.name for t in default_registry().all()
            if t.permission is PermissionLevel.EXTERNAL_ACTION
        ]
        assert external == ["fetch_page"]


class TestSeededReading:
    def test_it_returns_a_seeded_page(self, tmp_path):
        out = call(
            FetchPageTool(),
            {"url": "https://example.com/a"},
            ctx_with({"https://example.com/a": "hello world"}, tmp_path),
        )
        assert out.content == "hello world"
        assert out.url == "https://example.com/a"
        assert out.truncated is False

    def test_a_long_page_is_truncated_and_says_so(self, tmp_path):
        """A silent truncation would let an answer be built on a partial page
        with nothing recording that it was partial."""
        out = call(
            FetchPageTool(),
            {"url": "https://example.com/long"},
            ctx_with({"https://example.com/long": "x" * (MAX_CHARS + 500)}, tmp_path),
        )
        assert len(out.content) == MAX_CHARS
        assert out.truncated is True


class TestRefusals:
    def test_no_page_source_reports_the_missing_capability(self, tmp_path):
        """The `ctx.store is None` pattern: a tool handed no capability explains
        itself instead of crashing."""
        with pytest.raises(ToolExecutionError, match="no page source"):
            call(FetchPageTool(), {"url": "https://example.com/a"}, ctx_with(None, tmp_path))

    def test_the_refusal_says_there_is_no_network_client(self, tmp_path):
        """So a reader of a failing trace is not left wondering whether a real
        fetch was attempted and failed."""
        with pytest.raises(ToolExecutionError, match="no network client"):
            call(FetchPageTool(), {"url": "https://example.com/a"}, ctx_with(None, tmp_path))

    def test_an_unseeded_url_names_only_the_miss(self, tmp_path):
        """ADR-042: a failed lookup must not hand the model a menu. Listing the
        available URLs here is the same affordance that got a task completed
        nobody asked about."""
        pages = {"https://example.com/other": "unrelated"}
        with pytest.raises(ToolExecutionError) as exc:
            call(FetchPageTool(), {"url": "https://example.com/missing"}, ctx_with(pages, tmp_path))
        message = str(exc.value)
        assert "https://example.com/missing" in message
        assert "other" not in message

    def test_the_failure_stays_recoverable(self, tmp_path):
        """A ToolExecutionError is handed back to the model as an observation,
        so a failed fetch does not end the run."""
        pages = {"https://example.com/a": "ok"}
        with pytest.raises(ToolExecutionError):
            call(FetchPageTool(), {"url": "https://nope"}, ctx_with(pages, tmp_path))
        assert call(
            FetchPageTool(), {"url": "https://example.com/a"}, ctx_with(pages, tmp_path)
        ).content == "ok"


class TestNoNetworkSurface:
    def test_the_module_imports_no_http_client(self):
        """The load-bearing property of this increment, asserted rather than
        promised: the attack surface is measured before any transport exists.

        Parses the imports rather than grepping the source. The first version
        scanned the whole file and failed on the word "sockets" in the module
        docstring -- a detector matching prose instead of code, which is the
        same class of error as the groundedness extractor that could not tell an
        assertion from a quotation.
        """
        import ast
        import inspect

        import personal_ai_os.tools.builtin.web as web

        tree = ast.parse(inspect.getsource(web))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        network = {"httpx", "requests", "urllib", "socket", "aiohttp", "http"}
        assert not (imported & network), (
            f"the web tool imports a network client: {sorted(imported & network)}"
        )


class TestApprovalPrompt:
    def test_the_prompt_names_the_url(self, tmp_path):
        """`external_action` is `ask` in the shipped config, so this string is
        what a person decides on."""
        tool = FetchPageTool()
        args = tool.validate_input({"url": "https://example.com/thing"})
        assert "https://example.com/thing" in tool.describe_resource(args)
