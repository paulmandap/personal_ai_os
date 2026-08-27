"""Identifier and timestamp helpers.

Run identifiers are sortable and human-readable because a future local agent
will read them out of a directory listing, not out of a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso() -> str:
    """ISO-8601 UTC timestamp, e.g. ``2026-08-27T20:14:02.481Z``."""
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    """A short, unique run identifier, e.g. ``a3f1c9d2``."""
    return uuid.uuid4().hex[:8]


def new_tool_call_id(index: int) -> str:
    """A synthetic tool-call id.

    Ollama does not always return ids for tool calls, but the conversation
    still needs to bind each result to its request, so we mint our own.
    """
    return f"call_{index}_{uuid.uuid4().hex[:6]}"


def run_filename(run_id: str, *, at: datetime | None = None) -> str:
    """Trace filename: timestamp first so the directory sorts chronologically."""
    stamp = (at or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{run_id}.jsonl"
