# Project State

**Last updated:** 2026-08-28
**Updated by:** Claude Code (development assistant), reviewed by Paul

> This file is the handoff document. It must be enough for a future local agent
> — with no access to any previous conversation — to open the repository and
> know exactly where work stopped and what to do next.

---

## Current Phase

**Phase 4 — Evaluation harness. Complete.**

Phases 1, 2 and 4 are done. **Phase 3 (Finance + Research agents) was
deliberately deferred** until there was a way to measure agent quality; that
now exists, so Phase 3 is next.

---

## Current Objective

Continue the roadmap toward Phase 3 while preserving the model-agnostic,
local-first architecture.

Phase 4 existed to answer two questions that had been open and unmeasurable.
Both now have numbers:

### 1. Does the anti-embellishment prompt fix work? **Yes — 20/20.**

Both `qwen2.5:7b` and `qwen2.5:3b` score 100% on the `embellishment` suite,
in *both* directions: they no longer invent due dates or priorities, and they
still record values the user actually stated.

### 2. Can `qwen2.5:3b` drive an agent loop? **Yes — and it matches the 7B.**

| | 7B | 3B |
|---|---|---|
| `tool_calling` pass rate | 90% | **90%** |
| throughput | ≈32 tok/s | **≈63 tok/s** |
| `embellishment` | 100% | 100% |

The small tier is real, not decorative. Failure *profiles* differ slightly (the
3B is better at error recovery, the 7B at the multi-step completion), so this
is not a reason to abandon the ladder — but it is strong evidence that routing
easy work to the 3B costs nothing and roughly doubles speed.

### 3. What the harness found that nobody asked it to

`completes_the_right_task` scored **0/5 on both models**. Identical failure
across two very different models is the signal that a *design* is at fault, not
a model.

Given *"I finished buying the oat milk, mark that done"*, both models guessed
`complete_task(id=1)` and completed **"Renew passport"**. The 7B then
hallucinated a task list containing an item that had never existed — while
correctly stating, one sentence earlier, that it had completed the wrong task.

The prompt already forbade this. **A prompt instruction was not the lever.**
The fix was structural (ADR-022): `complete_task` now accepts a `title`, and
matching scores by token coverage rather than substring containment.

**0/5 → 5/5. Suite 75% → 90% on both models.**

The lesson: the failure was invisible in normal use. The agent answered
fluently every time and described the wrong action confidently. Only checking
the database caught it.

---

## Completed

### Phase 4 — Evaluation

- `evaluation/case.py` — YAML suites; check names validated at **load** time
- `evaluation/checks.py` — 15 checks reading the result, the trace, or the
  database. No judge model (ADR-020)
- `evaluation/runner.py` — isolated fixture workspace per repetition; model
  pinning; `RecordingBroker` so the gate is provably consulted
- `evaluation/report.py` — pass rates (ADR-021), per-check rates keyed on
  label, metric spread, save/load, side-by-side comparison
- `evaluations/cases/` — `embellishment` (4 cases) and `tool_calling` (4 cases)
- `paios eval list | run | compare`
- **Fix found by the harness:** title-based `complete_task` + token matching

### Phase 2 — Master, Task Agent, memory

Delegation via sub-agents-as-tools (ADR-012); SQLite store; four task tools;
`master` and `task_agent`.

### Phase 1 — Foundation

Model abstraction + Ollama provider; deterministic router; agent registry;
typed tools + filesystem jail; permission gate; JSONL traces; CLI.

### Recorded, not built

Health & Wellness domain — `docs/health-wellness.md`, ADR-017/018/019.
**Do not implement until explicitly requested.**

---

## In Progress

Nothing. Phase 4 is closed.

---

## Known Problems

1. **`survives_a_bad_start` scores 60–80%.** After a failed tool call the model
   sometimes skips the explicit fallback instruction. Real, measured, and low
   priority — it is a compound conditional instruction, the hardest kind.

2. **`update_task` still takes only an id**, so it has the same latent flaw
   ADR-022 fixed in `complete_task`. Not yet exercised by any case, which is
   the only reason it has not shown up.

3. **The 7B hallucinated a task list** during the id-guessing failure. The
   prompt now forbids stating task contents no tool returned, but that is a
   prompt fix and therefore unproven. **No case measures it yet** — worth
   adding one.

4. **`write: ask` prompts on every task mutation** in normal use. Correct
   default; `write: auto` in `config/local.yaml` is defensible because the
   filesystem jail already bounds where writes land.

5. **Result files are ~30 KB each** and are committed. Fine now; if the
   directory grows unwieldy, trim `detail` on passing checks.

6. **`large` tier unmapped** — 14B at Q4 (~9 GB) exceeds 8 GB VRAM.

7. **Model swapping can crash Ollama's runner.** Evict between size changes —
   integration tests use `fresh_vram`; the eval CLI needs a manual unload
   between `--model` runs.

8. **Four gaps recorded for sensitive domains** (traces store arguments
   verbatim; `delegate` passes free text; `Store` is one namespace; permissions
   have one axis). None are bugs today. See the Health & Wellness section of
   this file's history and ADR-019.

---

## Blockers

None.

---

## Next Steps

**Phase 3 — Finance and Research agents.** The reason to defer it has been
removed: there is now a way to tell whether a new agent works.

1. **Finance Agent.** Exercises genuinely new shapes: numeric reasoning, and
   `requires_human_approval` on anything touching money — the first real use of
   that flag. Needs a `transactions` domain in `memory/`.
2. **Write its evaluation cases alongside it, not after.** Phase 4's lesson is
   that fluent output hides wrong actions; a finance agent that is confidently
   wrong about money is worse than one that is slow.
3. **Research Agent** after, since it likely needs web access and therefore the
   first `external_action` tool.

Two smaller items worth doing first, both cheap:

- Add a hallucination case (known problem 3) — the harness exists, the case
  does not.
- Give `update_task` the same title-based lookup as `complete_task` (ADR-022).

Before starting: `paios doctor` and `pytest -q` for a green baseline.

---

## Last Successful Test

**2026-08-28**

```
pytest -q                 ->  341 passed  (sockets blocked, Ollama not needed)
pytest -m integration     ->  13 passed   (live qwen2.5:3b + 7b)
```

Evaluation results in `evaluations/results/` (committed, including the pre-fix
runs — the regression history is the point):

```
embellishment  7b  20/20 (100%)      tool_calling  7b  18/20 (90%)
embellishment  3b  20/20 (100%)      tool_calling  3b  18/20 (90%)
```

`paios tasks` confirms the real database was never touched by any eval run.

---

## Last User-Approved Change

Paul approved, in this session:
- Phase 4 plan: evaluation harness, deterministic scoring, cases targeting the
  two open questions

Phases 1 and 2 are committed and pushed (`f561e9d`, `61965a9`). The Health &
Wellness documentation amendment and all of Phase 4 are **uncommitted**.

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
| 012 | A sub-agent is a tool; the Master reuses the agent loop |
| 013 | One generic `delegate` tool — per-agent tools create a registry cycle |
| 014 | `delegate` is `read`-level; gates belong where consequences are |
| 015 | SQLite for structured memory; vectors deferred |
| 016 | Every trace event carries its agent and depth |
| 017 | *(future)* Health & Wellness is a domain agent; it **is** the coordinator |
| 018 | *(future)* Safety is cross-cutting; crisis resources are static data |
| 019 | *(future)* Permissions need a sensitivity axis orthogonal to severity |
| **020** | **Deterministic trace-based scoring; no judge model** |
| **021** | **A case result is a pass rate over N runs, not a boolean** |
| **022** | **Identify a task by title; ids invite guessing** |

---

## Model Configuration

Hardware: RTX 3050 **8 GB VRAM**, 16 GB RAM, Ryzen 5 3600, Windows 10.

| Tier | Model | Size | Status |
|---|---|---|---|
| `small` | `qwen2.5:3b-instruct` | ~2.2 GB | **measured: 90% on tool_calling, ≈63 tok/s** |
| `medium` | `qwen2.5:7b-instruct` | ~4.7 GB | **measured: 90% on tool_calling, ≈32 tok/s** |
| `large` | — | — | unmapped by design |

Roles: `classify`/`extract` → small; `plan`/`reason`/`code` → medium.

**Open decision:** the measurements suggest `reason` could move to `small` for
the task agent's workload. Not changed yet — one suite is thin evidence for a
routing change, and Phase 3's agents will exercise harder reasoning.

---

## Agent Configuration

| Agent | Model | Tools | Permissions |
|---|---|---|---|
| `master` | role `reason` → medium | `delegate` | `read` |
| `task_agent` | role `reason` → medium | 4 task tools | `read`, `write` |
| `ping` | role `reason` → medium | `read_file`, `list_dir` | `read` |

`max_delegation_depth: 2`.

---

## Pending Experiments

- **Should `reason` route to the 3B?** The data says it might. One suite is not
  enough; revisit after Phase 3 adds harder reasoning workloads.
- **Does the anti-hallucination prompt work?** Unmeasured (known problem 3).
- **How often does the Master delegate wrongly?** Unmeasured. Trivial with one
  sub-agent; becomes a real question at three or four.
- **Is an explicit plan needed?** Still blocked on resumability (ADR-012's
  revisit trigger).

---

## Repository Facts

- ~4,700 lines of source, ~3,190 lines of tests
- 354 tests: 341 unit (offline, sockets blocked), 13 integration (live)
- 3 runtime dependencies (`pydantic`, `httpx`, `pyyaml`)
- 2 commits. Health & Wellness docs + Phase 4 uncommitted (24 files).
