# Memory

**Status: SQLite store implemented (Phase 2).** Tasks persist. Semantic
retrieval is still deliberately absent — see "Why no vectors yet".

## What exists

```
memory/store.py     Store      -- connection, versioned schema, write transactions
memory/tasks.py     TaskStore  -- the first real domain
```

One SQLite file at `paths.db_path` (default `data/paios.db`, gitignored via
`*.db`). The schema is created on first use and its version recorded, so the
next migration has somewhere to start.

```python
store = Store(Path("data/paios.db"))
store.connect()                      # creates + migrates, idempotent
tasks = TaskStore(store)
tasks.add("Renew passport", due_date="2026-09-07")
```

`TaskStore` returns pydantic `Task` models, never raw `sqlite3.Row` objects, so
this boundary is typed like every other one and nothing above it needs to know
SQL exists.

## Reaching it from a tool

The store arrives on `ToolContext`, the same way delegation does:

```python
def run(self, args, ctx: ToolContext):
    if ctx.store is None:
        raise ToolExecutionError("task storage is not available in this context")
    return TaskStore(ctx.store).add(args.title)
```

A tool handed no store explains itself rather than crashing, and stays
constructible in a test without a database.

## Migrations

`MIGRATIONS` in `store.py` is an ordered list of `(version, sql)`. **Append
only** — never edit a shipped migration, because a database that already
applied it will not see the change.

```python
MIGRATIONS = [
    (1, "CREATE TABLE tasks (...)"),
    (2, "ALTER TABLE tasks ADD COLUMN blocked_by INTEGER"),   # future
]
```

`paios doctor` reports the live schema version.

## Threading

`Tool.execute` runs tools inside a single-worker `ThreadPoolExecutor`, so a
store opened on the main thread is used from a worker thread. Hence
`check_same_thread=False` plus an `RLock` around every operation.

The lock is not defending against the sync core running two things at once — it
does not (ADR-002). It defends against the one case where concurrency *can*
happen: a tool that exceeded its timeout is still running in the background
after the caller moved on.

## The six kinds, and where they stand

| Kind | Status | Where |
|---|---|---|
| Short-term | exists | The message list inside one `BaseAgent.run` |
| Project state | exists | `PROJECT_STATE.md`, maintained by hand |
| Episodic | exists, unindexed | `runs/*.jsonl` records every run in full |
| **Structured / task** | **implemented** | `tasks` table |
| Long-term (preferences) | not built | Would be a `facts` table |
| Semantic | deliberately absent | See below |
| Procedural | not built | Likely files, not rows |

Episodic memory is largely a matter of *indexing what is already written*
rather than capturing something new — the traces are complete.

## Why no vectors yet

The test for adding semantic retrieval is concrete and has not been met:

> An agent fails a task because it could not **find** a fact it had stored.

Until that happens, a vector store is infrastructure without a problem. It also
brings an embedding model that competes for the same 8 GB of VRAM the reasoning
model needs — a real, measurable cost paid for a hypothetical benefit.

At one person's data volume, `SELECT ... WHERE status IN ('todo','doing')` is
not the bottleneck, and pretending otherwise would be building for a scale this
system will never see.

## Open questions

- **Who writes long-term memory** — the agent mid-run, or a separate
  consolidation step? A separate step is more controllable and far easier to
  evaluate.
- **What stops memory filling with low-value facts?** Some quality gate is
  needed — the same problem Phase 8 has with trajectories.
- **How does memory interact with permissions?** Storing a fact learned from a
  `read` is not obviously itself a `read`.
- **Retention.** Does anything expire, and who decides?

## Inspecting it

```powershell
paios doctor          # schema version and task count
paios tasks           # the task list, no model involved
paios tasks add "Something" --priority high --due 2026-09-07
paios tasks done 3
```

`paios tasks` exists partly for its own sake and partly as verification: it is
how you check that what an agent *claimed* it saved is actually in the database.
