"""SQLite store and the task domain."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_ai_os.memory.store import LATEST_VERSION, Store, StoreError
from personal_ai_os.memory.tasks import (
    Task,
    TaskNotFoundError,
    TaskPriority,
    TaskStatus,
    TaskStore,
)


class TestSchema:
    def test_fresh_database_is_migrated_to_latest(self, tmp_path: Path):
        with Store(tmp_path / "db" / "test.db") as store:
            assert store.version == LATEST_VERSION

    def test_parent_directory_is_created(self, tmp_path: Path):
        path = tmp_path / "nested" / "deeper" / "test.db"
        with Store(path):
            assert path.exists()

    def test_reopening_does_not_remigrate(self, tmp_path: Path):
        path = tmp_path / "test.db"
        with Store(path) as first:
            TaskStore(first).add("survives")
        with Store(path) as second:
            assert second.version == LATEST_VERSION
            assert TaskStore(second).count() == 1

    def test_connect_is_idempotent(self, store: Store):
        assert store.connect() is store.connect()

    def test_unopenable_path_is_a_store_error(self, tmp_path: Path):
        # A directory where the database file should be.
        (tmp_path / "taken.db").mkdir()
        with pytest.raises(StoreError, match="cannot open"):
            Store(tmp_path / "taken.db").connect()

    def test_data_really_persists_across_processes(self, tmp_path: Path):
        """The whole point of the layer: work outlives the process."""
        path = tmp_path / "test.db"
        with Store(path) as first:
            TaskStore(first).add("remember me")
        with Store(path) as second:
            assert TaskStore(second).list()[0].title == "remember me"


class TestTaskModel:
    def test_rejects_an_empty_title(self):
        with pytest.raises(ValueError):
            Task(title="")

    def test_rejects_a_malformed_due_date(self):
        """The message names the format, because a model reads it and retries."""
        with pytest.raises(ValueError, match="ISO date"):
            Task(title="x", due_date="next friday")

    def test_accepts_an_iso_due_date(self):
        assert Task(title="x", due_date="2026-09-07").due_date == "2026-09-07"

    def test_empty_due_date_becomes_none(self):
        assert Task(title="x", due_date="").due_date is None

    def test_summary_is_one_readable_line(self):
        task = Task(
            id=3, title="Ship it", priority=TaskPriority.HIGH, due_date="2026-09-07"
        )
        text = task.summary()
        assert "#3" in text and "Ship it" in text and "high" in text


class TestCrud:
    def test_add_returns_a_persisted_task(self, tasks: TaskStore):
        task = tasks.add("Write tests")
        assert task.id is not None
        assert task.status is TaskStatus.TODO
        assert task.created_at and task.updated_at

    def test_add_validates_before_writing(self, tasks: TaskStore):
        with pytest.raises(ValueError, match="ISO date"):
            tasks.add("x", due_date="whenever")
        assert tasks.count() == 0

    def test_get_missing_id_raises(self, tasks: TaskStore):
        with pytest.raises(TaskNotFoundError, match="999"):
            tasks.get(999)

    def test_update_changes_only_given_fields(self, tasks: TaskStore):
        task = tasks.add("Original", notes="keep me")
        assert task.id is not None
        updated = tasks.update(task.id, title="Renamed")
        assert updated.title == "Renamed"
        assert updated.notes == "keep me"

    def test_update_revalidates_the_merged_result(self, tasks: TaskStore):
        task = tasks.add("x")
        assert task.id is not None
        with pytest.raises(ValueError, match="ISO date"):
            tasks.update(task.id, due_date="soon")

    def test_completing_sets_completed_at(self, tasks: TaskStore):
        task = tasks.add("Finish")
        assert task.id is not None
        done = tasks.complete(task.id)
        assert done.status is TaskStatus.DONE
        assert done.completed_at

    def test_reopening_clears_completed_at(self, tasks: TaskStore):
        task = tasks.add("Finish")
        assert task.id is not None
        tasks.complete(task.id)
        reopened = tasks.update(task.id, status=TaskStatus.TODO)
        assert reopened.completed_at is None

    def test_delete_removes_it(self, tasks: TaskStore):
        task = tasks.add("Temporary")
        assert task.id is not None
        tasks.delete(task.id)
        assert tasks.count() == 0

    def test_delete_missing_id_raises(self, tasks: TaskStore):
        with pytest.raises(TaskNotFoundError):
            tasks.delete(404)


class TestListing:
    def test_open_tasks_only_by_default(self, tasks: TaskStore):
        keep = tasks.add("open")
        gone = tasks.add("closed")
        assert gone.id is not None
        tasks.complete(gone.id)
        assert [t.title for t in tasks.list()] == [keep.title]

    def test_include_done_shows_everything(self, tasks: TaskStore):
        tasks.add("open")
        done = tasks.add("closed")
        assert done.id is not None
        tasks.complete(done.id)
        assert len(tasks.list(include_done=True)) == 2

    def test_filtering_by_status(self, tasks: TaskStore):
        tasks.add("a")
        doing = tasks.add("b")
        assert doing.id is not None
        tasks.update(doing.id, status=TaskStatus.DOING)
        found = tasks.list(status=TaskStatus.DOING)
        assert [t.title for t in found] == ["b"]

    def test_in_progress_sorts_above_todo(self, tasks: TaskStore):
        tasks.add("later")
        active = tasks.add("active")
        assert active.id is not None
        tasks.update(active.id, status=TaskStatus.DOING)
        assert tasks.list()[0].title == "active"

    def test_high_priority_sorts_first(self, tasks: TaskStore):
        tasks.add("normal one")
        tasks.add("urgent", priority=TaskPriority.HIGH)
        assert tasks.list()[0].title == "urgent"

    def test_dated_tasks_sort_above_undated(self, tasks: TaskStore):
        tasks.add("someday")
        tasks.add("friday", due_date="2026-09-07")
        assert tasks.list()[0].title == "friday"

    def test_limit_returns_the_most_important_not_an_arbitrary_page(
        self, tasks: TaskStore
    ):
        """Ordering happens in SQL, so a limit keeps the rows that matter."""
        for i in range(10):
            tasks.add(f"normal {i}")
        tasks.add("urgent", priority=TaskPriority.HIGH)
        assert tasks.list(limit=1)[0].title == "urgent"

class TestFindByTitle:
    """Matching a task by what someone *called* it.

    Driven by a measured failure: a model asked to complete "the oat milk one"
    searched for "buying the oat milk", which contains no title as a substring
    and matched nothing.
    """

    @pytest.fixture
    def seeded(self, tasks: TaskStore) -> TaskStore:
        tasks.add("Renew passport")
        tasks.add("Buy oat milk")
        tasks.add("Submit thesis draft")
        return tasks

    def test_exact_title_matches(self, seeded: TaskStore):
        assert [t.title for t in seeded.find_by_title("Buy oat milk")] == ["Buy oat milk"]

    def test_partial_title_matches(self, seeded: TaskStore):
        assert [t.title for t in seeded.find_by_title("oat milk")] == ["Buy oat milk"]

    def test_a_paraphrase_matches(self, seeded: TaskStore):
        """The exact string that failed against substring matching."""
        found = seeded.find_by_title("buying the oat milk")
        assert [t.title for t in found] == ["Buy oat milk"]

    def test_gerund_folds_to_stem(self, seeded: TaskStore):
        assert [t.title for t in seeded.find_by_title("renewing my passport")] == [
            "Renew passport"
        ]

    def test_stopwords_do_not_dilute_the_score(self, seeded: TaskStore):
        assert [t.title for t in seeded.find_by_title("the thesis draft")] == [
            "Submit thesis draft"
        ]

    def test_unrelated_words_match_nothing(self, seeded: TaskStore):
        assert seeded.find_by_title("dentist appointment") == []

    def test_a_genuine_tie_returns_both(self, tasks: TaskStore):
        """Ambiguity must stay ambiguous rather than picking a winner."""
        tasks.add("Buy oat milk")
        tasks.add("Buy oat milk again")
        assert len(tasks.find_by_title("oat milk")) == 2

    def test_a_more_specific_search_breaks_the_tie(self, tasks: TaskStore):
        tasks.add("Buy oat milk")
        tasks.add("Buy oat milk again")
        assert [t.title for t in tasks.find_by_title("oat milk again")] == [
            "Buy oat milk again"
        ]

    def test_completed_tasks_are_excluded_by_default(self, seeded: TaskStore):
        milk = seeded.find_by_title("oat milk")[0]
        assert milk.id is not None
        seeded.complete(milk.id)
        assert seeded.find_by_title("oat milk") == []
        assert len(seeded.find_by_title("oat milk", include_done=True)) == 1

    def test_only_stopwords_matches_nothing(self, seeded: TaskStore):
        """Otherwise 'the task' would match everything equally."""
        assert seeded.find_by_title("the task") == []


class TestCounting:
    def test_count_by_status(self, tasks: TaskStore):
        tasks.add("a")
        done = tasks.add("b")
        assert done.id is not None
        tasks.complete(done.id)
        assert tasks.count() == 2
        assert tasks.count(status=TaskStatus.DONE) == 1
