"""Structured run traces.

Every agent run appends JSON Lines to ``runs/<timestamp>_<run_id>.jsonl``.

Two properties are deliberate:

*Append-and-flush per event.* If the process is killed mid-run, everything up
to that moment is already on disk. That is what makes a run inspectable after
a crash -- and it is the substrate the later resumability work builds on.

*One format, two purposes.* This is the observability log (docs §26) and the
raw material for trajectory capture (docs §31). Designing it properly once
means the distillation work later costs nothing to collect data for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator

from pydantic import BaseModel, Field

from personal_ai_os.core.ids import new_run_id, run_filename, utc_iso, utc_now

REDACTED = "***"

DEFAULT_REDACT_KEYS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
)


_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _segments(key: str) -> list[str]:
    """Split a key into comparable word segments.

    ``prompt_tokens`` -> ``[prompt, tokens]``; ``accessToken`` ->
    ``[access, token]``; ``OPENAI_API_KEY`` -> ``[openai, api, key]``.
    """
    return [s for s in _NON_ALNUM.split(_CAMEL_BOUNDARY.sub("_", key).lower()) if s]


def _looks_secret(key: str, markers: tuple[tuple[str, ...], ...]) -> bool:
    """True when a marker appears as a run of whole segments in the key.

    Segment matching rather than substring matching, because substrings
    over-match badly: ``token`` is inside ``prompt_tokens``, and redacting
    token counts would silently destroy the throughput telemetry that model
    comparison depends on. ``access_token`` still matches, because ``token``
    is a whole segment there.
    """
    segs = _segments(key)
    for marker in markers:
        span = len(marker)
        if any(
            tuple(segs[i : i + span]) == marker for i in range(len(segs) - span + 1)
        ):
            return True
    return False


def redact(value: Any, keys: tuple[str, ...] | list[str]) -> Any:
    """Recursively replace values whose key names a secret.

    Catches ``OPENAI_API_KEY``, ``db_password`` and ``accessToken``; leaves
    ``prompt_tokens`` and ``tokens_used`` alone.
    """
    markers = tuple(tuple(_segments(k)) for k in keys if _segments(k))

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _looks_secret(k, markers):
                out[k] = REDACTED
            else:
                out[k] = redact(v, keys)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, keys) for v in value]
    return value


class TraceEvent(BaseModel):
    """One line of a trace file."""

    seq: int
    ts: str
    run_id: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


# Canonical event types. Kept as constants so a typo in one call site does not
# silently create a parallel event stream nobody queries.
class Events:
    RUN_START = "run.start"
    RUN_END = "run.end"
    ROUTER_SELECT = "router.select"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    TOOL_REQUESTED = "tool.requested"
    PERMISSION_DECISION = "permission.decision"
    TOOL_RESULT = "tool.result"
    ERROR = "error"
    RETRY = "retry"
    # Delegation. A sub-agent shares its parent's trace, so one file holds a
    # whole nested run; `depth` on these events is what makes the nesting
    # legible when reading it back.
    DELEGATE_START = "delegate.start"
    DELEGATE_END = "delegate.end"


class RunTrace:
    """Records one agent run.

    Usable as a context manager, which guarantees a ``run.end`` event even
    when the body raises::

        with RunTrace.create(agent="ping", runs_dir=Path("runs")) as trace:
            trace.event(Events.MODEL_REQUEST, model="qwen2.5:7b-instruct")
    """

    def __init__(
        self,
        *,
        run_id: str,
        agent: str,
        path: Path | None,
        enabled: bool = True,
        redact_keys: tuple[str, ...] | list[str] = DEFAULT_REDACT_KEYS,
    ) -> None:
        self.run_id = run_id
        self.agent = agent
        self.path = path
        self.enabled = enabled and path is not None
        self._redact_keys = tuple(redact_keys)
        self._seq = 0
        self._started = utc_now()
        #: Events kept in memory as well, so tests can assert on a run without
        #: touching the filesystem.
        self.events: list[TraceEvent] = []

        if self.enabled and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(
        cls,
        *,
        agent: str,
        runs_dir: Path,
        run_id: str | None = None,
        enabled: bool = True,
        redact_keys: tuple[str, ...] | list[str] = DEFAULT_REDACT_KEYS,
    ) -> RunTrace:
        rid = run_id or new_run_id()
        path = runs_dir / run_filename(rid) if enabled else None
        return cls(
            run_id=rid, agent=agent, path=path, enabled=enabled, redact_keys=redact_keys
        )

    @classmethod
    def disabled(cls, *, agent: str = "", run_id: str | None = None) -> RunTrace:
        """A trace that records in memory but never writes. Used by tests."""
        return cls(run_id=run_id or new_run_id(), agent=agent, path=None, enabled=False)

    def event(self, type: str, **data: Any) -> TraceEvent:
        """Record one event. Never raises -- tracing must not break a run."""
        self._seq += 1
        evt = TraceEvent(
            seq=self._seq,
            ts=utc_iso(),
            run_id=self.run_id,
            type=type,
            data=redact(data, self._redact_keys),
        )
        self.events.append(evt)

        if self.enabled and self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(evt.model_dump_json() + "\n")
                    fh.flush()
            except OSError:
                # A full disk or a locked file must not abort the agent run.
                # Losing observability is bad; losing the work is worse.
                self.enabled = False
        return evt

    def error(self, exc: BaseException, **context: Any) -> TraceEvent:
        return self.event(
            Events.ERROR,
            error_type=type(exc).__name__,
            message=str(exc),
            **context,
        )

    @property
    def elapsed_ms(self) -> float:
        return (utc_now() - self._started).total_seconds() * 1000.0

    def __enter__(self) -> RunTrace:
        self.event(Events.RUN_START, agent=self.agent)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            self.error(exc, phase="run")
        self.event(
            Events.RUN_END,
            agent=self.agent,
            ok=exc is None,
            elapsed_ms=round(self.elapsed_ms, 1),
        )


def read_trace(path: Path) -> Iterator[TraceEvent]:
    """Replay a trace file.

    Malformed lines are skipped rather than fatal: a trace truncated by a
    crash is exactly the trace you most want to be able to read.
    """
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield TraceEvent.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue


def find_trace(runs_dir: Path, run_id: str) -> Path | None:
    """Locate a trace file by run id."""
    matches = sorted(runs_dir.glob(f"*_{run_id}.jsonl"))
    return matches[-1] if matches else None


def latest_trace(runs_dir: Path) -> Path | None:
    """Most recent trace. Filenames are timestamp-first, so sorting suffices."""
    matches = sorted(runs_dir.glob("*.jsonl"))
    return matches[-1] if matches else None
