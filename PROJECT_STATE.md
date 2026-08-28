# Project State

**Last updated:** 2026-08-27
**Updated by:** Claude Code (development assistant), reviewed by Paul

> This file is the handoff document. It must be enough for a future local agent
> — with no access to any previous conversation — to open the repository and
> know exactly where work stopped and what to do next.

---

## Current Phase

**Phase 2 — Master Agent, Task Agent, Memory. Complete.**

Next: Phase 3 — Finance and Research agents, or Phase 4 reliability work. See
Next Steps for the recommendation.

---

## Current Objective

Phase 1 built contracts. Phase 2 made them a system: a Master that delegates, a
Task Agent that does real work, and persistence so that work survives the
process.

Two questions the phase was really testing, and their answers:

1. **Does the registry abstraction hold?** *Yes.* Two new, independently
   motivated agents were added with **zero changes to `agents/registry.py`**.
   Pinned by `tests/unit/test_agent_registry.py::TestShippedManifests`.
2. **Can delegation reuse the agent loop?** *Yes.* The Master is a `BaseAgent`
   with one tool. No second orchestration engine was written (ADR-012).

---

## Completed

### Phase 2

**Persistence (`memory/`)**
- `Store` — SQLite, versioned append-only migrations, WAL, guarded write
  transactions, `RLock` + `check_same_thread=False` for the tool worker thread
- `TaskStore` + `Task` — typed CRUD; ordering done in SQL so `limit` returns the
  *most important* rows, not an arbitrary page
- Shared `validate_iso_date` used by both the model and the tool schemas

**Task tools** — `add_task`, `list_tasks`, `update_task`, `complete_task`.
First tools at `write` level, so the first real exercise of the `ask` path.

**Delegation**
- `DelegateTool` — one generic `delegate(agent, objective)` (ADR-013)
- Guards: depth limit, call-stack cycle check, missing-capability handling
- `ToolContext` extended with `store`, `delegate`, `agent_roster`, `depth`,
  `call_stack`, `max_delegation_depth`
- `Runtime.run_sub_agent` + closure injection so a tool cannot fake its depth

**Agents** — `master` (tool: `delegate` only) and `task_agent`.

**CLI** — `paios tasks [list|add|done]`; `paios trace` indents by depth;
`paios doctor` reports the database and schema version.

### Phase 1 (unchanged)

Model abstraction + Ollama provider · deterministic router · agent registry ·
typed tools + filesystem jail · permission gate · JSONL traces · CLI · docs.

---

## In Progress

Nothing. Phase 2 is closed.

---

## Known Problems

1. **`write: ask` prompts on every task mutation.** Correct as a default, and
   genuinely annoying in daily use. `write: auto` in `config/local.yaml` is a
   defensible override *because the filesystem jail already bounds where writes
   can land* — the jail, not the prompt, is what makes it safe. Shipped default
   stays `ask`.

2. **The 7B embellishes stored data.** Observed live: asked to add a task, it
   invented `priority: high` and a due date nobody mentioned. The Task Agent
   prompt now forbids this explicitly, but it is a *prompt* fix, not a
   structural one — it should be measured, not assumed fixed. First real
   candidate for the Phase 4 evaluation suite.

3. **The Master's plan is implicit.** It decides one delegation at a time, so
   there is nothing to inspect before execution and nothing to resume from.
   This is the documented revisit trigger for ADR-012 and blocks §27/§28
   resumability.

4. **Tool timeouts bound the wait, not the thread** (carried over from Phase 1).
   Python cannot kill a thread. This is why `Store` holds a lock.

5. **`large` tier still unmapped** — 14B at Q4 (~9 GB) exceeds 8 GB VRAM.

6. **Model swapping can crash Ollama's runner** — integration tests evict
   models between size changes (`fresh_vram`). Master and Task Agent share the
   medium tier deliberately, so delegation causes no swap.

7. **Disk is tight** — ~3 GB free of 476 GB on C:.

8. **No evaluation harness, no retry policy, no resumability.** Phase 4.

---

## Blockers

None.

---

## Next Steps

**Recommended: Phase 4 reliability before Phase 3 breadth.**

The reasoning: known problems 2 and 3 are both *measurement* problems. Adding a
Finance Agent now would add a third agent whose output quality is equally
unmeasured, on top of a Master whose routing quality is unmeasured. Evaluation
is what turns "the prompt now forbids inventing due dates" into something known
rather than hoped.

1. **Evaluation harness** (`evaluations/`) — read `runs/*.jsonl`, score tool
   selection, structured-output validity, task completion, and hallucination.
   The traces already contain everything needed; nothing new to instrument.
2. **Measure the embellishment fix** — does the 7B still invent due dates?
3. **Measure the 3B** — can it drive the Task Agent? This is the open question
   from Phase 1 and it decides how much routing work is worth doing.
4. *Then* Phase 3 agents, with a way to tell whether they work.

If you would rather build breadth first, the Finance Agent is the natural next
one — it exercises a genuinely different shape (numeric reasoning, and
`requires_human_approval` on anything touching money).

Before starting: `paios doctor` and `pytest -q` for a green baseline.

---

## Last Successful Test

**2026-08-27**

```
pytest -q                 ->  239 passed  (sockets blocked, Ollama not needed)
pytest -m integration     ->  11 passed   (live qwen2.5:3b + 7b)
```

Live delegation verified end to end:

```
paios run master "Add a task to review the Phase 2 delegation design,
                  then tell me everything on my list."
```

→ master delegated to `task_agent`, which called `add_task` and `list_tasks`,
and the task is really in `data/paios.db`. Trace
`runs/20260827T134226Z_63847bb8.jsonl` shows the sub-agent's whole run nested
under `delegate.start` / `delegate.end`.

---

## Last User-Approved Change

Paul approved, in this session:
- the Phase 2 plan (sub-agents as tools, full scope, both agents on the 7B)
- Phase 1 was committed (`f561e9d`) and pushed to
  `github.com/paulmandap/personal_ai_os` (private)

**Phase 2 is uncommitted** — 28 changed/new files awaiting review.

---

## Important Architecture Decisions

Full records in [`docs/decisions.md`](docs/decisions.md).

| ADR | Decision |
|---|---|
| 001 | **Local-first. No cloud provider, not even as fallback** |
| 002 | Synchronous core — one GPU holds one model |
| 003 | Direct HTTP to Ollama, not a vendor SDK |
| 004 | Pydantic as the single typing layer |
| 005 | JSONL traces = observability **and** future trajectory data |
| 006 | One permission gate, in the agent loop |
| 007 | `src/` layout, pip + venv |
| 008 | Recoverable failures are observations, not exceptions |
| 009 | Deterministic router until evaluation data says otherwise |
| 010 | Ollama wire-format findings, verified empirically |
| 011 | Trace redaction matches key segments, not substrings |
| **012** | **A sub-agent is a tool; the Master reuses the agent loop** |
| **013** | One generic `delegate` tool — per-agent tools create a registry cycle |
| **014** | `delegate` is `read`-level; gates belong where consequences are |
| **015** | SQLite for structured memory; vectors still deferred |
| **016** | Every trace event carries its agent and depth |

---

## Model Configuration

Hardware: RTX 3050 **8 GB VRAM**, 16 GB RAM, Ryzen 5 3600, Windows 10.

| Tier | Model | Size | Status |
|---|---|---|---|
| `small` | `qwen2.5:3b-instruct` | ~2.2 GB | installed, unproven for tool calling |
| `medium` | `qwen2.5:7b-instruct` | ~4.7 GB (5.1 GB resident) | the workhorse |
| `large` | — | — | unmapped by design |

Roles: `classify`/`extract` → small; `plan`/`reason`/`code` → medium.

---

## Agent Configuration

| Agent | Model | Tools | Permissions |
|---|---|---|---|
| `master` | role `reason` → medium | `delegate` | `read` |
| `task_agent` | role `reason` → medium | 4 task tools | `read`, `write` |
| `ping` | role `reason` → medium | `read_file`, `list_dir` | `read` |

`max_delegation_depth: 2`. Master and Task Agent share the medium tier so
delegation causes no VRAM swap.

---

## Pending Experiments

- **Does the anti-embellishment prompt work?** Needs measurement (problem 2).
- **Can the 3B drive an agent loop?** Still open from Phase 1. Decides how much
  the router is worth investing in.
- **How often does the Master delegate wrongly?** Unmeasured. With one
  sub-agent the choice is trivial; the question becomes real at three or four.
- **Is an explicit plan needed?** Blocked on resumability (problem 3).

---

## Repository Facts

- ~3,680 lines of source, ~2,400 lines of tests
- 250 tests: 239 unit (offline), 11 integration (live)
- 3 runtime dependencies (`pydantic`, `httpx`, `pyyaml`)
- 1 commit (`f561e9d`, Phase 1). Phase 2 uncommitted.
