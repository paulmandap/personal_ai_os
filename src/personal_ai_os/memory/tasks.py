"""Tasks -- the first real persistent domain.

`TaskStore` returns pydantic `Task` models rather than raw `sqlite3.Row`
objects, so this boundary is typed like every other one in the codebase and
nothing above it has to know SQL exists.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.core.ids import utc_iso
from personal_ai_os.memory.store import Store


class TaskNotFoundError(PersonalAIOSError):
    """No task with that id. Recoverable: reported back to the model."""


#: At least half the search terms must appear in the title.
TITLE_MATCH_THRESHOLD = 0.5

#: Words carrying no identifying signal. Kept deliberately short -- an
#: aggressive list would strip words that actually distinguish two tasks.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "my", "our", "your", "this", "that", "these", "those",
        "to", "for", "of", "on", "in", "at", "and", "or", "it", "is", "was",
        "please", "task", "item", "one", "done", "finished", "complete",
    }
)

_WORD = re.compile(r"[a-z0-9]+")


def significant_words(text: str) -> set[str]:
    """Lowercase content words, with a light plural/gerund fold.

    The fold is what lets "buying" match "Buy". It is deliberately crude --
    a real stemmer is a dependency and a source of surprises, and the job here
    is only to survive the handful of endings a person naturally varies.
    """
    words: set[str] = set()
    for raw in _WORD.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        word = raw
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 2 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        words.add(word)
    return words


def validate_iso_date(value: str | None) -> str | None:
    """Accept an ISO date or nothing, and say so clearly when neither.

    Shared by the `Task` model and by the task tools' input schemas, so the
    rule is enforced identically at both boundaries. The message names the
    expected format because a model reads it and gets one more attempt.
    """
    if value is None or value == "":
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"due_date must be an ISO date like '2026-09-07', got {value!r}"
        ) from None
    return value


class TaskStatus(str, Enum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def is_open(self) -> bool:
        return self in {TaskStatus.TODO, TaskStatus.DOING}


class TaskPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"high": 0, "normal": 1, "low": 2}[self.value]


class Task(BaseModel):
    """One unit of work the user cares about."""

    id: int | None = None
    title: str = Field(min_length=1, max_length=500)
    notes: str = ""
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority = TaskPriority.NORMAL
    due_date: str | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @field_validator("due_date")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        return validate_iso_date(v)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(**dict(row))

    def summary(self) -> str:
        """One line, for CLI listings and model-facing output."""
        marker = {"todo": " ", "doing": "~", "done": "x", "cancelled": "-"}[
            self.status.value
        ]
        due = f"  due {self.due_date}" if self.due_date else ""
        prio = "" if self.priority is TaskPriority.NORMAL else f"  ({self.priority.value})"
        return f"[{marker}] #{self.id} {self.title}{prio}{due}"


class TaskStore:
    """CRUD over the `tasks` table."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # --- create ------------------------------------------------------------

    def add(
        self,
        title: str,
        *,
        notes: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        due_date: str | None = None,
    ) -> Task:
        # Validate before touching the database, so a bad due_date fails with
        # the model-friendly message rather than a constraint error.
        task = Task(
            title=title, notes=notes, priority=priority, due_date=due_date
        )
        now = utc_iso()
        with self._store.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks
                    (title, notes, status, priority, due_date, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.title,
                    task.notes,
                    task.status.value,
                    task.priority.value,
                    task.due_date,
                    now,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid or 0)
        return self.get(task_id)

    # --- read --------------------------------------------------------------

    def get(self, task_id: int) -> Task:
        row = self._store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise TaskNotFoundError(f"no task with id {task_id}")
        return Task.from_row(row)

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        include_done: bool = False,
        limit: int = 50,
    ) -> list[Task]:
        """Open tasks first, highest priority first, soonest due date first.

        Ordering is done in SQL rather than in Python so a future `limit` on a
        large table still returns the *most important* rows, not an arbitrary
        page of them.
        """
        clauses: list[str] = []
        params: list[object] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        elif not include_done:
            clauses.append("status IN ('todo', 'doing')")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        rows = self._store.query(
            f"""
            SELECT * FROM tasks
            {where}
            ORDER BY
                CASE status WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END,
                CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                due_date IS NULL, due_date,
                id
            LIMIT ?
            """,
            tuple(params),
        )
        return [Task.from_row(r) for r in rows]

    def find_by_title(self, needle: str, *, include_done: bool = False) -> list[Task]:
        """Find tasks by what someone *called* them, not by exact substring.

        Exists because a title is the handle a person actually has. Requiring
        an integer id the user never mentioned is what pushes a model into
        guessing one.

        Matching is token coverage, not substring containment: the score is the
        fraction of the *search terms* that appear in the title. Measured
        reason — a model asked to complete "the oat milk one" searched for
        ``"buying the oat milk"``, which contains no task title as a substring
        and matched nothing, while obviously meaning "Buy oat milk".

        Scoring on the search terms rather than the title keeps genuine
        ambiguity ambiguous: "oat milk" covers both "Buy oat milk" and "Buy oat
        milk again" completely, so both tie and the caller is told to be more
        specific rather than a winner being invented.

        Returns the best-scoring tasks, or several when they tie. Empty when
        nothing reaches the threshold.
        """
        candidates = self.list(include_done=include_done, limit=500)
        terms = significant_words(needle)
        if not terms:
            return []

        scored: list[tuple[float, Task]] = []
        for task in candidates:
            title_words = significant_words(task.title)
            if not title_words:
                continue
            covered = len(terms & title_words) / len(terms)
            if covered >= TITLE_MATCH_THRESHOLD:
                scored.append((covered, task))

        if not scored:
            return []

        best = max(score for score, _ in scored)
        return [task for score, task in scored if score == best]

    def count(self, *, status: TaskStatus | None = None) -> int:
        if status is None:
            row = self._store.query_one("SELECT COUNT(*) AS n FROM tasks")
        else:
            row = self._store.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE status = ?", (status.value,)
            )
        return int(row["n"]) if row else 0

    # --- update ------------------------------------------------------------

    def update(
        self,
        task_id: int,
        *,
        title: str | None = None,
        notes: str | None = None,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
        due_date: str | None = None,
        clear_due_date: bool = False,
    ) -> Task:
        """Partial update. Only the fields given are changed."""
        existing = self.get(task_id)

        merged = existing.model_copy(
            update={
                k: v
                for k, v in {
                    "title": title,
                    "notes": notes,
                    "status": status,
                    "priority": priority,
                    "due_date": due_date,
                }.items()
                if v is not None
            }
        )
        if clear_due_date:
            merged.due_date = None
        # Re-validate the merged result so an invalid due_date is caught here.
        merged = Task.model_validate(merged.model_dump())

        now = utc_iso()
        completed_at = merged.completed_at
        if merged.status is TaskStatus.DONE and existing.status is not TaskStatus.DONE:
            completed_at = now
        elif merged.status is not TaskStatus.DONE:
            completed_at = None

        with self._store.write() as conn:
            conn.execute(
                """
                UPDATE tasks
                   SET title = ?, notes = ?, status = ?, priority = ?,
                       due_date = ?, updated_at = ?, completed_at = ?
                 WHERE id = ?
                """,
                (
                    merged.title,
                    merged.notes,
                    merged.status.value,
                    merged.priority.value,
                    merged.due_date,
                    now,
                    completed_at,
                    task_id,
                ),
            )
        return self.get(task_id)

    def complete(self, task_id: int) -> Task:
        return self.update(task_id, status=TaskStatus.DONE)

    # --- delete ------------------------------------------------------------

    def delete(self, task_id: int) -> None:
        self.get(task_id)  # raises TaskNotFoundError if absent
        with self._store.write() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
