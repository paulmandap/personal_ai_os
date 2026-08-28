# Project State

**Last updated:** 2026-08-28
**Updated by:** Claude Code (development assistant), reviewed by Paul

> The handoff document. It must be enough for a future local agent — with no
> access to any previous conversation — to open the repository and know exactly
> where work stopped and what to do next.

---

## Current Phase

**Phase 5 — Harder benchmarks, holdout, failure taxonomy. Complete.**

Phases 1, 2, 3, 4 and 5 done. Research Agent is the remaining Phase 3 item,
deferred because it needs the first `external_action` tool.

---

## Current Objective

Continue the roadmap while preserving the model-agnostic, local-first
architecture.

### The headline: the models are NOT interchangeable

Phase 4 concluded "the 3B matches the 7B everywhere". **That was wrong** — an
artifact of four saturated suites. Harder benchmarks broke the tie immediately:

| Suite | 7B | 3B |
|---|---|---|
| tool_calling · embellishment · hallucination · finance | 100% | 100% |
| robustness | 80% | 68% |
| planning | 93% | **67%** |
| delegation | 100% | **33%** |
| throughput | ≈37 tok/s | ≈69 tok/s |

On `delegation` the 3B returns an **empty response** rather than routing — not
a wrong answer, no answer. **`reason` must not move to `small`.** The Master
specifically needs the 7B. The 3B remains good for leaf agents at ~2× speed.

This is the first genuine capability gap the project has found, and the first
honest justification for considering training (Phases 10–16).

### Three defects the harder suites found

| Defect | Symptom | Fix |
|---|---|---|
| Empty turn scored as success | A run producing literally nothing was `answered` | `StopReason.EMPTY_RESPONSE` |
| Transfer could create money | Debit leg failed on a guessed account name, credit succeeded; ledger gained ₱5,000 | Atomic `transfer` tool (ADR-029) + fuzzy account resolution |
| `update_task` demanded an id | Same flaw as ADR-022 | `find` selector |

**All architectural. None would have been fixed by training.**

### Detector false positives: a recurring hazard

**Three times now**, a groundedness detector's first version has produced false
positives that would have manufactured work aimed at the wrong thing:

1. Phase 3: substring matching reported a 55% hallucination rate — all false.
2. This phase: a magnitude floor let a real invented ₱450 through.
3. This phase: grounding read only *successful* tool payloads, so an agent
   quoting a balance back from a tool's **refusal message** was scored a
   critical unsupported claim — on a holdout case. It scored 0/5; after the
   fix, 10/10.

**Always verify a detector against real transcripts before believing it.**

---

## Completed (Phase 5)

- `evaluation/taxonomy.py` — F001–F015, severity-ranked; all 20 checks mapped
- `split: train|validation|holdout` + `--split holdout` (ADR-027); holdout
  recorded in the result body *and* filename
- `category:` on cases; reports aggregate by category and failure kind
- Three suites: `robustness`, `planning`, `delegation` (15 cases, 4 holdout)
- Atomic `transfer` + fuzzy account resolution + mixed-currency refusal
- `update_task` `find` selector; monetary-context amount detection
- ADRs 027 (holdout by convention), 028 (stored data is data), 029 (atomic
  operations are one tool)

---

## Known Problems

1. **`contradiction_is_surfaced` is the weakest case** — 7B 0/5, 3B 40%. Given
   "mark the passport task done — actually no, I haven't started it", both
   models act on the first half. Real, measured, unfixed.
2. **`vague_request_is_clarified` on the 3B: 0/5** — it invents a task rather
   than asking. The 7B is 100%.
3. **`planning` holdout (7B): 40%**, failing `no_unsupported_task_claims`.
   Measured with the *task*-claims detector, which was not the one fixed above,
   so the number stands — but it has not been individually verified.
4. **Holdout is thin** — 4 cases, one already spent (ADR-027). Needs
   replenishing before it can carry weight.
5. **`robustness` and `finance` are drifting toward saturation** on the 7B
   (80% and 100%).
6. Multi-currency refuses rather than converts; no bank import; `write: ask`
   prompts on every mutation.
7. Training blocked on disk: ~22 GB needed, 5.5 GB free, and a GGUF cannot be
   fine-tuned.

---

## Deferred commitments

Postponed deliberately, with the trigger that brings them back. Written here
because after 2026-09-07 there is no conversation to remember them.

- **`TeacherModel` protocol** — required as the **first task of Phase 10**.
  Skipped because it would have had zero implementations and zero callers.
  Shape recorded in `docs/iterative-improvement.md`. Also noted in `CLAUDE.md`.
- **Try structure before training on the 3B delegation gap.** Every failure so
  far has been architectural; an empty response to a single-tool decision
  smells like a prompt or schema problem, and that is hours of work against
  days.

---

## Next Steps

1. **Fix `contradiction_is_surfaced`** (problem 1). Both models fail it, which
   by ADR-022's rule means the design is at fault, not the model. Start there.
2. **Replenish the holdout** — 4 cases is too few, and one is spent.
3. **Try structure on the 3B delegation gap** before any training.
4. Then either the Research Agent (first `external_action` tool) or Phase 10.

Before starting: `paios doctor` and `pytest -q` for a green baseline.

---

## Last Successful Test

**2026-08-28** — Ollama **0.33.1** (upgraded from 0.33.0; ADR-010's wire-format
findings still hold — the integration suite confirms).

```
pytest -q                 ->  483 passed  (sockets blocked, Ollama not needed)
pytest -m integration     ->   16 passed  (live qwen2.5:3b + 7b)

finance      7B 100%   3B 100%
robustness   7B  80%   3B  68%
planning     7B  93%   3B  67%
delegation   7B 100%   3B  33%
robustness holdout (7B) 100%
```

All results committed in `evaluations/results/`, pre-fix runs included — the
regression history is the point.

---

## Last User-Approved Change

Paul approved: three new suites; holdout via `split:` + `--split` flag;
deferring `TeacherModel` to Phase 10.

Phases 1–4 committed and pushed (`f561e9d`, `61965a9`, `20b6e5e`, `5de9ec9`,
`cf628df`). **Phase 5 is uncommitted.**

---

## Important Architecture Decisions

Full records in [`docs/decisions.md`](docs/decisions.md). ADR-001 (local-first,
no cloud provider) overrides everything.

| ADR | Decision |
|---|---|
| 001–011 | Local-first · sync core · direct HTTP · pydantic · JSONL traces · one permission gate · `src/` layout · failures as observations · deterministic router · Ollama wire format · segment redaction |
| 012–016 | Sub-agents are tools · one `delegate` tool · `delegate` is `read` · SQLite memory · traces carry agent + depth |
| 017–019 | *(future)* Health & Wellness domain, cross-cutting safety, sensitivity axis |
| 020–022 | Deterministic scoring, no judge · pass rate over N runs · identify by title, not id |
| 023–026 | Money as integer minor units · the model explains, the tool computes · groundedness is a set comparison · *(future)* the teacher is an abstraction |
| **027** | **Holdout protected by convention and one flag; a studied case is spent** |
| **028** | **Instructions inside stored data are data** |
| **029** | **Atomic operations belong in one tool** |

---

## Model Configuration

RTX 3050 **8 GB VRAM**, 16 GB RAM, Ryzen 5 3600, Windows 10.

| Tier | Model | Measured |
|---|---|---|
| `small` | `qwen2.5:3b-instruct` | Good for leaf agents; **cannot drive the Master** (33%) |
| `medium` | `qwen2.5:7b-instruct` | The workhorse; required for `master` |
| `large` | — | unmapped; 14B Q4 exceeds VRAM |

Roles: `classify`/`extract` → small; `plan`/`reason`/`code` → medium.
**Do not move `reason` to `small`** — measured, not assumed.

---

## Agent Configuration

| Agent | Tools | Permissions |
|---|---|---|
| `master` | `delegate` | `read` |
| `task_agent` | 4 task tools | `read`, `write` |
| `finance` | 10 finance tools | `read`, `write` |
| `ping` | `read_file`, `list_dir` | `read` |

`max_delegation_depth: 2`. The registry has needed **no changes** to accept
three new agents since Phase 1.

---

## Repository Facts

- 499 tests: 483 unit (offline, sockets blocked), 16 integration (live)
- 7 evaluation suites, 32 cases, 4 holdout · 20 checks · 15 failure codes
- 3 runtime dependencies (`pydantic`, `httpx`, `pyyaml`)
- 5 commits. Phase 5 uncommitted.
