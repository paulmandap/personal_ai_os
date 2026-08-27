"""List the contents of a directory inside the workspace."""

from __future__ import annotations

from pydantic import BaseModel, Field

from personal_ai_os.core.errors import ToolExecutionError
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext, resolve_within_roots

DEFAULT_MAX_ENTRIES = 200

# Noise a model does not benefit from seeing, and which crowds out the
# entries that actually answer the question.
SKIP_ALWAYS = {".git", "__pycache__", ".venv", ".pytest_cache", "node_modules"}


class ListDirInput(BaseModel):
    path: str = Field(
        default=".",
        description=(
            "Directory to list, relative to the workspace root. "
            "Defaults to the workspace root itself."
        ),
    )
    max_entries: int = Field(
        default=DEFAULT_MAX_ENTRIES,
        ge=1,
        le=2000,
        description="Stop after this many entries.",
    )


class DirEntry(BaseModel):
    name: str
    type: str  # "file" | "dir"
    size_bytes: int | None = None


class ListDirOutput(BaseModel):
    path: str
    entries: list[DirEntry]
    truncated: bool


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "List the files and subdirectories of a directory inside the workspace. "
        "Use this to discover what exists before reading a specific file."
    )
    Input = ListDirInput
    Output = ListDirOutput
    permission = PermissionLevel.READ
    timeout_s = 15.0

    def run(self, args: ListDirInput, ctx: ToolContext) -> ListDirOutput:  # type: ignore[override]
        path = resolve_within_roots(args.path, ctx.allowed_roots)

        if not path.exists():
            raise ToolExecutionError(f"no such directory: {path}")
        if not path.is_dir():
            raise ToolExecutionError(f"{path} is a file; use read_file instead")

        try:
            children = sorted(
                path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError as exc:
            raise ToolExecutionError(f"cannot list {path}: {exc}") from exc

        entries: list[DirEntry] = []
        for child in children:
            if child.name in SKIP_ALWAYS:
                continue
            if len(entries) >= args.max_entries:
                break
            is_dir = child.is_dir()
            size: int | None = None
            if not is_dir:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            entries.append(
                DirEntry(name=child.name, type="dir" if is_dir else "file", size_bytes=size)
            )

        visible = sum(1 for c in children if c.name not in SKIP_ALWAYS)
        return ListDirOutput(
            path=str(path), entries=entries, truncated=visible > len(entries)
        )
