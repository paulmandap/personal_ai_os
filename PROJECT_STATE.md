# Project State

**Last updated:** 2026-08-28
**Updated by:** Claude Code (development assistant), reviewed by Paul

> This file is the handoff document. It must be enough for a future local agent
> — with no access to any previous conversation — to open the repository and
> know exactly where work stopped and what to do next.

---

## Current Phase

**Phase 3 — Groundedness + Finance Agent. Complete.**

Phases 1, 2, 3 and 4 are done. Research Agent is the remaining Phase 3 item and
is deliberately deferred (it needs the first `external_action` tool).

---

## Current Objective

Continue the roadmap while preserving the model-agnostic, local-first
architecture.

### Every suite is now at 100% on both models

| Suite | 7B | 3B |
|---|---|---|
| `tool_calling` | 100% | 100% |
| `embellishment` | 100% | 100% |
| `hallucination` | 100% | 100% |
| `finance` | 100% | 100% |
| throughput | ≈40 tok/s | ≈70 tok/s |

**The benchmarks are saturated.** That is the textbook signal that a benchmark
has stopped providing improvement signal — not proof of general capability. The
right response is harder cases, not a training run. See
`docs/iterative-improvement.md`.

### Hallucination: asked for, measured, already solved

The Phase 2 prompt fix worked. Both models score 100% on the `hallucination`
suite, and `no_unsupported_task_claims` / `no_unsupported_amounts` now guard
the regression deterministically (ADR-025).

**The near-miss worth remembering.** The detector's first version reported a
55% hallucination rate. Every flag was a false positive — an apostrophe parsed
as a quote delimiter, and real stored due dates counted as invented. Reporting
that number would have sent this project into fine-tuning to fix a problem that
did not exist.

### Every failure found so far has been architectural

Three defects, three models failing identically, three structural fixes:

| Defect | Symptom | Fix |
|---|---|---|
| `complete_task` demanded an id | Both models invented one, completed the wrong task | Accept a title (ADR-022) |
| Substring title matching | A paraphrase matched nothing; agent narrated instead of acting | Token-coverage matching |
| Optional fields rejected `null` | **Every** write call failed; agent claimed success anyway | `ToolInput` base class |

**None would have been fixed by training.** Training on top of them would have
taught the models to work around bugs while hiding them. This is the central
evidence for the local-first bet, and it is why the answer to "the models
perform badly" was "no — the architecture did".

---

## Completed

### Phase 3 — Groundedness + Finance

- `no_unsupported_task_claims`, `no_unsupported_amounts`, `account_balance_is`
  (19 checks total)
- `evaluations/cases/hallucination.yaml` (4 cases), `finance.yaml` (5 cases)
- `memory/finance.py` — accounts, transactions, commitments, goals; **integer
  minor units** (ADR-023); migration 2
- `affordability_check` — the arithmetic lives in the tool (ADR-024)
- 9 finance tools; `agents/finance.yaml` + `FinanceAgent`
- `paios finance [amount]` — ledger and affordability with no model involved
- `ToolInput` base class — an explicit `null` on an optional field means "not
  supplied". **Applies to every tool**; lifted `tool_calling` on the 3B from
  90% to 100% as a side effect
- Traces now carry full tool payloads; `paios trace` truncates at display time
- `docs/iterative-improvement.md` — Phases 9–16 mapped against what exists

### Phases 1, 2, 4

Foundation · Master + Task agent + SQLite memory · evaluation harness.

### Recorded, not built

Health & Wellness domain (`docs/health-wellness.md`, ADR-017/018/019) and
teacher-guided improvement (`docs/iterative-improvement.md`, ADR-026).
**Do not implement either until explicitly requested.**

---

## In Progress

Nothing. Phase 3 is closed.

---

## Known Problems

1. **The benchmarks are saturated.** 100% across four suites on both models
   means they have stopped measuring anything. Harder cases are the next
   priority — ambiguity, contradiction, multi-step planning, long context.

2. **`update_task` still takes only an id**, carrying the flaw ADR-022 fixed in
   `complete_task`. Still unexercised by any case, which is the only reason it
   has not surfaced.

3. **Groundedness detectors are tuned against false positives**, so they miss
   subtle invention — an altered detail inside an otherwise real item. They
   catch whole fabricated entities, which is what was observed.

4. **`no_unsupported_amounts` ignores figures below 1000** to avoid flagging
   counts and list indices. A small invented amount would pass.

5. **Multi-currency is nominal.** Accounts carry a currency code but balances
   are summed as if one currency. Fine for one country; wrong the day it isn't.

6. **`write: ask` prompts on every mutation** in normal use. Correct default;
   `write: auto` in `config/local.yaml` is defensible given the filesystem jail.

7. **Model swapping can crash Ollama's runner.** Unload between `--model` eval
   runs; integration tests use `fresh_vram`.

8. **Training is blocked on disk**: ~22 GB needed against 5.5 GB free, and a
   GGUF cannot be fine-tuned. Not urgent — Phases 9–11 need none of it.

---

## Blockers

None.

---

## Next Steps

**Harder benchmarks, before anything else.** Four saturated suites cannot tell
you whether a change helped. Concretely:

1. **Adversarial and ambiguous cases** — contradictory instructions, missing
   data, long context, multi-step planning, conflicting priorities. Find where
   the 3B actually breaks.
2. **Holdout split + failure taxonomy** — the two real Phase 9 gaps
   (`docs/iterative-improvement.md`). The holdout must exist *before* any
   teacher-generated data, or the benchmark is contaminated from round one.
3. **Then** re-compare 3B vs 7B on the harder suites. If the 3B still matches,
   the routing decision is settled and `reason` should move to `small`.

Smaller items, both cheap:

- Give `update_task` the same title lookup as `complete_task` (problem 2).
- Research Agent — needs the first `external_action` tool, so it is also the
  first real exercise of that permission level.

Before starting: `paios doctor` and `pytest -q` for a green baseline.

---

## Last Successful Test

**2026-08-28**

```
pytest -q                 ->  427 passed  (sockets blocked, Ollama not needed)
pytest -m integration     ->  15 passed   (live qwen2.5:3b + 7b)
```

All evaluation results committed in `evaluations/results/`, including the
pre-fix runs — the regression history is the point.

Live end to end:

```
paios run finance "My BPI account has 20000 pesos. Rent is 8000 due on the 28th."
paios finance 5000
  ->  discretionary PHP 12,000.00, verdict affordable, PHP 7,000.00 remaining
```

Every figure computed by `affordability_check`, none by the model.

---

## Last User-Approved Change

Paul approved, in this session: Phase 3 scope (groundedness first, then
Finance), manual finance data entry, and recording the Phase 9–16 amendment as
a map against what exists.

Phases 1, 2 and 4 are committed and pushed (`f561e9d`, `61965a9`, `20b6e5e`,
`5de9ec9`). **Phase 3 is uncommitted** — 36 files.

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
| 013 | One generic `delegate` tool |
| 014 | `delegate` is `read`-level; gates belong where consequences are |
| 015 | SQLite for structured memory; vectors deferred |
| 016 | Every trace event carries its agent and depth |
| 017–019 | *(future)* Health & Wellness domain, safety, sensitivity axis |
| 020 | Deterministic trace-based scoring; no judge model |
| 021 | A case result is a pass rate over N runs, not a boolean |
| 022 | Identify a task by title; ids invite guessing |
| **023** | **Money is stored as integer minor units** |
| **024** | **The model explains; the tool computes** |
| **025** | **Groundedness is a set comparison, not a judgement** |
| **026** | *(future)* The teacher is an abstraction, never a runtime dependency |

---

## Model Configuration

Hardware: RTX 3050 **8 GB VRAM**, 16 GB RAM, Ryzen 5 3600, Windows 10.

| Tier | Model | Measured |
|---|---|---|
| `small` | `qwen2.5:3b-instruct` | 100% on all four suites, ≈70 tok/s |
| `medium` | `qwen2.5:7b-instruct` | 100% on all four suites, ≈40 tok/s |
| `large` | — | unmapped; 14B Q4 exceeds 8 GB VRAM |

**Open decision:** `reason` could move to `small` — the 3B matches the 7B
everywhere measured, at nearly twice the speed. Held back only because
saturated benchmarks are weak evidence. Revisit after harder suites exist.

---

## Agent Configuration

| Agent | Model | Tools | Permissions |
|---|---|---|---|
| `master` | role `reason` → medium | `delegate` | `read` |
| `task_agent` | role `reason` → medium | 4 task tools | `read`, `write` |
| `finance` | role `reason` → medium | 9 finance tools | `read`, `write` |
| `ping` | role `reason` → medium | `read_file`, `list_dir` | `read` |

`max_delegation_depth: 2`. The registry has needed **no changes** to accept
three new agents since Phase 1.

---

## Pending Experiments

- **Where does the 3B actually break?** Unknown — nothing has beaten it yet.
- **Should `reason` route to `small`?** Blocked on harder benchmarks.
- **Is there any capability gap worth training for?** On current evidence, no.
  Every failure so far was architectural.
- **How often does the Master delegate wrongly?** Unmeasured; trivial with two
  sub-agents, a real question at four or five.

---

## Repository Facts

- ~5,750 lines of source, ~3,800 lines of tests
- 442 tests: 427 unit (offline, sockets blocked), 15 integration (live)
- 19 evaluation checks, 4 suites, 17 cases
- 3 runtime dependencies (`pydantic`, `httpx`, `pyyaml`)
- 4 commits. Phase 3 uncommitted (36 files).
