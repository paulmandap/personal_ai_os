"""Tool interface.

A tool declares four things the rest of the system relies on: what it is
called, what arguments it accepts, what class of consequence running it
carries, and how long it may take.

The argument schema is derived from a pydantic model, so the schema a tool
*advertises* to the model and the schema it *validates against* are the same
object. They cannot drift.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ValidationError, model_validator

from personal_ai_os.core.errors import (
    PathNotAllowedError,
    ToolExecutionError,
    ToolInputError,
    ToolTimeoutError,
)
from personal_ai_os.core.types import ToolSchema
from personal_ai_os.memory.store import Store
from personal_ai_os.permissions.types import PermissionLevel

if TYPE_CHECKING:  # pragma: no cover
    from personal_ai_os.agents.base import AgentResult


#: Runs a sub-agent and returns its result. Supplied by the Runtime, which is
#: the only component that knows how to build an agent. Typed as a forward
#: reference because `agents.base` imports this module, not the other way
#: round -- the dependency points one way on purpose.
DelegateFn = Callable[[str, str], "AgentResult"]


@dataclass
class ToolContext:
    """Ambient facts and capabilities a tool may need.

    Tools receive their world instead of reaching for it, which is what makes
    them testable without a configured process. The optional capabilities below
    are the same idea: a tool that needs a database or the ability to delegate
    is handed one, and reports a clear error when it was not -- rather than
    importing a global and working only inside a fully wired process.
    """

    allowed_roots: list[Path] = field(default_factory=list)
    workspace_root: Path = field(default_factory=Path.cwd)
    agent: str = ""
    run_id: str = ""

    # --- capabilities (None when not available in this context) ---
    #: Structured memory, for tools that persist things.
    store: Store | None = None
    #: Ability to run another agent.
    delegate: DelegateFn | None = None
    #: Agents available to delegate to, name -> description. Kept here rather
    #: than duplicated into the Master's manifest so that adding an agent makes
    #: it routable without editing any other file.
    agent_roster: dict[str, str] = field(default_factory=dict)

    # --- delegation bookkeeping ---
    #: How many delegations deep this agent is. The top-level agent is 0.
    depth: int = 0
    #: Agents currently on the delegation stack, used to refuse cycles.
    call_stack: tuple[str, ...] = ()
    max_delegation_depth: int = 2

    extras: dict[str, Any] = field(default_factory=dict)


# --- Filesystem jail -------------------------------------------------------


def _is_within(child: Path, parent: Path) -> bool:
    """Containment test that survives Windows path semantics.

    ``normcase`` folds case and unifies separators, which matters here: on
    Windows ``C:\\Paul\\x`` and ``c:\\paul\\x`` are the same file, and a naive
    string comparison would let one of them out of the jail.
    """
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def resolve_within_roots(candidate: str | Path, roots: list[Path]) -> Path:
    """Resolve a path and prove it lands inside an allowed root.

    This runs *before* the tool does, and it is orthogonal to permissions: the
    permission policy decides whether an action is allowed at all, while this
    decides which paths are even nameable. A read granted by policy still
    cannot reach outside the workspace.

    ``Path.resolve`` collapses ``..`` and follows symlinks first, so neither
    traversal (``../../Windows``) nor a symlink pointing outside can smuggle a
    path past the containment check.
    """
    if not roots:
        raise PathNotAllowedError(
            "no allowed filesystem roots are configured; refusing all paths"
        )

    raw = Path(candidate).expanduser()
    if not raw.is_absolute():
        raw = roots[0] / raw

    resolved = raw.resolve()
    for root in roots:
        if _is_within(resolved, root.resolve()):
            return resolved

    allowed = ", ".join(str(r) for r in roots)
    raise PathNotAllowedError(
        f"path {resolved} is outside every allowed root ({allowed})"
    )


# --- Tool ------------------------------------------------------------------


class ToolInput(BaseModel):
    """Base for every tool's input model.

    Models routinely emit *all* the fields they were shown, using ``null`` for
    the ones they have no value for -- ``{"description": null, "occurred_on":
    null, ...}``. Pydantic rejects ``null`` for a ``str`` field with a default
    of ``""``, which is technically correct and practically useless: the call
    was well-formed in every way that matters.

    Observed cost of not doing this: a finance agent's `add_transaction` failed
    on every attempt because it passed `description: null`, and the agent then
    told the user it had recorded the spending anyway.

    So an explicit null on an optional field is treated as "not supplied", and
    the field's default applies. A null on a *required* field still errors,
    because there the model really has omitted something.
    """

    @model_validator(mode="before")
    @classmethod
    def _null_means_omitted(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if not (
                value is None
                and (field := cls.model_fields.get(key)) is not None
                and not field.is_required()
            )
        }


class Tool(ABC):
    """Base class for every tool."""

    name: ClassVar[str]
    description: ClassVar[str]
    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]
    permission: ClassVar[PermissionLevel] = PermissionLevel.READ
    timeout_s: ClassVar[float] = 30.0
    #: Force a human prompt even where policy would auto-approve the level.
    requires_human_approval: ClassVar[bool] = False

    def schema(self) -> ToolSchema:
        """What this tool looks like to a model."""
        params = self.Input.model_json_schema()
        # Inlined $defs keep the payload readable for smaller models, which
        # handle a flat schema noticeably better than a $ref-laden one.
        params.pop("$defs", None)
        params.pop("title", None)
        return ToolSchema(
            name=self.name, description=self.description, parameters=params
        )

    def validate_input(self, raw: dict[str, Any]) -> BaseModel:
        """Coerce raw model-supplied arguments into the typed input model.

        Raises :class:`ToolInputError`, which the agent loop treats as
        *recoverable*: the message is handed back to the model so it can fix
        its own call rather than the run dying.
        """
        try:
            return self.Input.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()
            )
            raise ToolInputError(
                f"invalid arguments for {self.name}: {problems}"
            ) from exc

    def describe_resource(self, args: BaseModel) -> str:
        """What this call will act on, shown in approval prompts and traces."""
        for field_name in ("path", "url", "target", "recipient", "query"):
            value = getattr(args, field_name, None)
            if value:
                return str(value)
        return ""

    def execute(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        """Run with a timeout guard.

        Honest caveat: this bounds how long the *caller* waits, not how long
        the tool runs. Python cannot kill a thread, so a runaway tool keeps
        going in the background until the process exits. Bounding the wait is
        still worth having -- it stops one stuck tool from hanging a whole
        agent run -- but it is not a hard kill, and tools that touch slow
        resources should carry their own internal timeouts too.
        """
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{self.name}") as pool:
            future = pool.submit(self.run, args, ctx)
            try:
                return future.result(timeout=self.timeout_s)
            except FutureTimeout as exc:
                raise ToolTimeoutError(
                    f"{self.name} exceeded its {self.timeout_s}s timeout"
                ) from exc
            except (ToolExecutionError, ToolInputError, PathNotAllowedError):
                raise
            except Exception as exc:
                raise ToolExecutionError(f"{self.name} failed: {exc}") from exc

    @abstractmethod
    def run(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        """Do the work. Implementations receive validated, typed arguments."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Tool {self.name} permission={self.permission.value}>"
