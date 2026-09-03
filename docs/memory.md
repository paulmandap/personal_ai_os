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

`paios doctor` reports the live schema version. **The shipped schema is v4.**

### v4 — `plan_steps.depth`

v3 recorded a plan as a flat `seq` list, which described every run exactly,
because before Phase 7.8 no manifest but `master.yaml` held `delegate` and every
step was necessarily depth 1. A coordinator between the Master and the
specialists breaks that: `seq` alone reads a grandchild as its parent's sibling.

`depth` is written from the runtime's own value — the same one stamped on the
`delegate.start` trace event — so the store and the trace cannot disagree about
the shape of a run.

**`DEFAULT 1` states a fact rather than filling an unknown.** No pre-existing row
can have been written at another depth, so backfilling to 1 is a proof, not a
guess. Together, `seq` (depth-first order, guaranteed by INV-1 committing a
parent's row before its child runs) and `depth` reconstruct the tree; neither
does alone.

**`resume_objective` is deliberately unchanged by v4** and a test pins that a
nested plan composes the same text as a flat one. It is the only model-facing
string in the module and ADR-065 records it as covered by no benchmark, so
rewording it is a separate, measured act.

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

## Sensitive domains (FUTURE — NOT IMPLEMENTED)

A future Health & Wellness domain would need memory guarantees this layer does
not currently provide. Recording the gap now, because it is easier to build
namespacing before there is data in the store than after:

| Needed | Today |
|---|---|
| Separate namespace per sensitive domain | One namespace; any tool holding `ctx.store` reaches everything |
| "Never store this" as a real category | No such concept — storage is all-or-nothing |
| User-facing forget | No delete path above `TaskStore.delete` |
| Storage gated on *sensitivity*, not just severity | The permission enum has one axis (ADR-019) |

There is also a **trace** problem, which is the sharper one:
`RunTrace` records tool arguments in full, and redaction keys on *secrets*
(ADR-011), not sensitivity. A wellness domain on today's tracing would write
emotional conversations verbatim into `runs/*.jsonl`, permanently, in plain
text. That must be fixed before the first such agent runs, not after.

See [`health-wellness.md`](health-wellness.md) for the memory categories and the
principle that emotional conversation is **not persisted by default**.

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
