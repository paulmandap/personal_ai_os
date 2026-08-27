# Tools

A tool is a typed, permissioned, bounded capability. Four declarations carry
the weight: what it is called, what it accepts, what class of consequence it
carries, and how long it may take.

## Anatomy

```python
class ReadFileInput(BaseModel):
    path: str = Field(description="Path to read. Relative to the workspace root.")
    max_bytes: int = Field(default=64_000, ge=1, le=1_000_000, description="...")

class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the contents of a UTF-8 text file inside the workspace."
    Input = ReadFileInput
    Output = ReadFileOutput
    permission = PermissionLevel.READ
    timeout_s = 15.0

    def run(self, args: ReadFileInput, ctx: ToolContext) -> ReadFileOutput:
        path = resolve_within_roots(args.path, ctx.allowed_roots)
        ...
```

`run` receives **validated, typed** arguments. It never sees raw model output.

## Schemas cannot drift

`Tool.schema()` derives the advertised JSON Schema from `Input` — the same
model that validates incoming arguments. There is no second definition to keep
in sync, so a tool can never advertise something it rejects or accept something
it never advertised.

Write real `description=` text on every field. It is not documentation for you;
it is the only thing the model reads when deciding how to call the tool. Vague
descriptions are the most common cause of malformed calls from small models.

Bounds are worth setting for the same reason — and they work. On the first live
run the 7B requested `max_bytes: 1048576`, the `le=1_000_000` bound rejected it,
and the model corrected itself on the next turn.

## Two independent controls

These are often confused. They are not the same thing:

| | Question it answers | Where |
|---|---|---|
| **Permission** | May this *kind of action* happen at all? | `permissions/broker.py` |
| **Path jail** | Which paths are even nameable? | `resolve_within_roots()` |

A read that policy auto-approves still cannot escape the workspace. Both must
pass. `Path.resolve()` collapses `..` and follows symlinks *before* the
containment check, so neither traversal nor a symlink pointing outward gets
through.

Containment uses `os.path.normcase`, which matters on Windows: `C:\Paul\x` and
`c:\paul\x` are the same file, and a naive string comparison would let one of
them out. Prefix matching is separator-aware, so `/work` does not "contain"
`/work-evil`.

## Choosing a permission level

| Level | Use for |
|---|---|
| `read` | Inspecting local data |
| `write` | Creating or modifying files inside the workspace |
| `external_action` | Anything leaving this machine |
| `send_message` | Email, chat, anything a human receives |
| `spend_money` | Purchases, paid APIs |
| `delete` | Destroying data |
| `destructive` | Irreversible system-level operations |

When unsure, pick the *higher* level. The cost of an unnecessary prompt is
small; the cost of an unprompted consequential action is not.

Set `requires_human_approval = True` for a tool that always needs a human
regardless of level policy.

## Timeouts: an honest caveat

`Tool.execute` bounds how long the *caller waits*, not how long the tool runs.
Python cannot kill a thread, so a runaway tool keeps going in the background
until the process exits.

This is still worth having — one stuck tool cannot hang a whole agent run — but
it is not a hard kill. Tools that touch slow resources should carry their own
internal timeouts (e.g. pass `timeout=` to `httpx`) rather than relying on this.

## Registering

```python
registry = ToolRegistry([ReadFileTool(), ListDirTool()])
```

Add builtins to `default_registry()` in `tools/registry.py`. Duplicate names
are refused unless `replace=True` is passed explicitly, so shadowing is always
deliberate.

## Failure behaviour

Raise the typed errors from `core/errors.py`:

| Raise | When | Loop's response |
|---|---|---|
| `ToolInputError` | Arguments do not validate | Reported to the model — recoverable |
| `ToolExecutionError` | The work failed | Reported to the model — recoverable |
| `PathNotAllowedError` | Path outside the jail | Reported to the model — recoverable |
| `ToolTimeoutError` | Exceeded `timeout_s` | Reported to the model — recoverable |

All of these come back to the model as observations so it can try something
else (ADR-008). Do not raise bare exceptions — `Tool.execute` wraps them as
`ToolExecutionError`, but the message will be less useful than one you wrote.

## Testing

Test `run()` directly with a `ToolContext`; no agent, no model, no registry:

```python
tool = ReadFileTool()
out = tool.run(tool.validate_input({"path": "README.md"}), tool_context)
```

Always include an escape test. `tests/unit/test_tools.py::TestPathJail` covers
parent traversal, absolute paths outside the root, shared-prefix siblings and
Windows case folding.
