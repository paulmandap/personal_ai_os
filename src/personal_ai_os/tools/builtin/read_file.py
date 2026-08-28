"""Read a text file from inside the workspace."""

from __future__ import annotations

from pydantic import BaseModel, Field

from personal_ai_os.core.errors import ToolExecutionError
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.tools.base import Tool, ToolContext, ToolInput, resolve_within_roots

DEFAULT_MAX_BYTES = 64_000


class ReadFileInput(ToolInput):
    path: str = Field(
        description=(
            "Path to the file to read. Relative paths are resolved against the "
            "workspace root. Must stay inside the workspace."
        )
    )
    max_bytes: int = Field(
        default=DEFAULT_MAX_BYTES,
        ge=1,
        le=1_000_000,
        description="Stop after this many bytes. The result says if it truncated.",
    )


class ReadFileOutput(BaseModel):
    path: str
    content: str
    size_bytes: int
    truncated: bool


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read the contents of a UTF-8 text file inside the workspace. "
        "Use this to inspect source code, configuration or notes before "
        "answering questions about them."
    )
    Input = ReadFileInput
    Output = ReadFileOutput
    permission = PermissionLevel.READ
    timeout_s = 15.0

    def run(self, args: ReadFileInput, ctx: ToolContext) -> ReadFileOutput:  # type: ignore[override]
        path = resolve_within_roots(args.path, ctx.allowed_roots)

        if not path.exists():
            raise ToolExecutionError(f"no such file: {path}")
        if path.is_dir():
            raise ToolExecutionError(f"{path} is a directory; use list_dir instead")

        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ToolExecutionError(f"cannot read {path}: {exc}") from exc

        truncated = len(raw) > args.max_bytes
        content = raw[: args.max_bytes].decode("utf-8", errors="replace")

        return ReadFileOutput(
            path=str(path),
            content=content,
            size_bytes=len(raw),
            truncated=truncated,
        )
