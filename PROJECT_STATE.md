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

- `evaluation/taxonomy.py` — F001–F015, severity-ranked; all 21 checks mapped
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

1. **`contradiction_is_surfaced` — improved, not solved.** 7B 47% → **60%**,
   3B 0% → 27% (at `repeat: 15`). Two structural fixes landed (ADR-030,
   ADR-031) and both original mechanisms are gone: no duplicates, no empty
   responses. The **residual failure is a dishonesty**, and it is now measured
   rather than merely described: the agent calls `complete_task`, marking the
   task done, then writes *"it seems you haven't started the passport task
   yet"*. The new `answer_matches_task_status` check names it — on the 7B it
   fails 6 of 15 runs, **overlapping perfectly** with `task_matching`, so it
   adds no new failures; it explains the existing ones.

   Saying the opposite of what you did is worse than doing the wrong thing:
   the user cannot see that it happened. No structural fix is obvious.
   **Unfixed, and now precisely characterised.**

   The earlier diagnosis here — "both models act on the first half" — was
   **wrong for the 7B** and is corrected in ADR-030. Only the 3B did that.
2. **`vague_request_is_clarified` on the 3B: 0/5** — it invents a task titled
   "thing I mentioned earlier" rather than asking. Was 20% before ADR-031; now
   that it no longer goes silent, it acts, and acting means inventing. The 7B
   is 100%.
2a. ~~**`two_writes_in_one_request`**~~ **FIXED — ADR-032.** 1/5 → **15/15 on
   both models**, against a stricter oracle. `add_transaction` now returns the
   resulting balance, so the closing figure is a tool-returned one. The real
   finding was that *every* run had been stating a balance no tool returned;
   the passing branch was inventing the right number by luck. The fresh
   holdout case covering the same competence passed 5/5.
2b. ~~**`completes_the_right_task` (3B): 2/5**~~ **SETTLED at 9/15 (60%)** on
   `repeat: 15`. Neither the 100% one sample suggested nor the 2/5 that looked
   like a regression — it was this case's spread. **Not a regression from
   ADR-030.** The residual mechanism is real though: `complete_task` calls
   rejected by input validation, ~20% of runs. Shares that mechanism with
   `ordering_matters` (3B, 2/5).
2c. **Five runs is too few to judge a case.** On unchanged code at temperature
   0.2–0.3, `contradiction_is_surfaced` returned 0/5 and 2/5 on the same model;
   even at `repeat: 15` two runs of identical code gave 11/15 and 8/15. The
   *mechanism* is stable where the rate is not — judge a fix by which failure
   remains, not by the score. `contradiction_is_surfaced` is now `repeat: 15`.
3. **The groundedness detectors need verifying — this is the recurring one.**
   Four separate results now hang on them and none has been checked against a
   transcript:
   - `planning` holdout 60% and `delegation` holdout 60%, both failing
     `no_unsupported_task_claims`;
   - `transfer_moves_both_legs` fluctuating 4/5–5/5 on `no_unsupported_amounts`
     reporting an invented `1500.00`;
   - one `finance` run flagging `800.00` in "upcoming bills of PHP 800.00".

   Either the model is summing figures itself — a real ADR-024 violation worth
   knowing about — or the detectors are crying wolf, which this project has
   already been caught by three times. **Do not act on these numbers until one
   of them is checked against the actual transcript.** Highest-value next task.
4. ~~**Holdout is thin**~~ **Replenished: 4 → 7 cases.** Three fresh ones added
   2026-08-28, written before the ADR-032 fix was measured and never run during
   it. All three passed 5/5 on their single unbiased run.
5. **`robustness` and `finance` are drifting toward saturation** on the 7B
   (80% and 96%).
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
- ~~**Try structure before training on the 3B delegation gap.**~~ **Done, and
  it did not work.** ADR-031 gives the model a second turn after an empty one.
  Delegation went 33% → 40%: 1 of 10 empty runs recovered, within noise. When
  the 3B goes silent on finance routing it stays silent when nudged. The
  structural attempt has been made and measured; the gap survives it. That
  removes the last cheap objection to treating this as a genuine capability
  limit.

---

## Next Steps

1. **Verify a groundedness detector against a real transcript** (problem 3).
   Four reported numbers now depend on detectors nobody has checked, and this
   project has been fooled by exactly this three times. Pick the
   `transfer_moves_both_legs` `1500.00` case, read the transcript, and settle
   whether the model summed it or the regex invented it. Cheap, and everything
   downstream of those numbers is unreliable until it is done.
2. **`complete_task` arguments rejected by validation on the 3B** — ~20% of
   runs, and it is the shared mechanism behind `completes_the_right_task` (60%)
   and `ordering_matters` (2/5). Read the rejected arguments before designing
   anything; ADR-022 and ADR-030 both started from exactly this symptom.
3. The residual dishonesty (problem 1). No structural fix is obvious. The new
   `answer_matches_task_status` check makes it measurable, which is the
   precondition for working on it at all.
4. Then either the Research Agent (first `external_action` tool) or Phase 10.

Before starting: `paios doctor` and `pytest -q` for a green baseline.

---

## Last Successful Test

**2026-08-28** — Ollama **0.33.1** (upgraded from 0.33.0; ADR-010's wire-format
findings still hold — the integration suite confirms).

```
pytest -q                 ->  535 passed  (sockets blocked, Ollama not needed)
pytest -m integration     ->   16 passed  (live qwen2.5:3b + 7b)

                          before ADR-030/031/032    after
two_writes_in_one_request (repeat 15)
               7B / 3B       1/5                    15/15 · 15/15   ADR-032
contradiction_is_surfaced (repeat 15)
               7B            7/15  (47%)             9/15  (60%)
               3B            0/15  ( 0%)             4/15  (27%)
robustness     7B            74%                     80%
robustness     3B            46%                     54%
planning       7B            73%                    100%
planning       3B            67%                     88%
finance        7B / 3B      100%                     96% · 100%      see problem 3
tool_calling   3B          "100%" (one sample)        80%  (repeat 15; truly 60%)
delegation     3B            33%                     40%             ADR-031
holdout        7B      (4 cases)                     31/35 (89%), 7 cases
```

Mechanisms, which are steadier than the rates: the 7B's duplicate-task failure
went 8 of 8 → 0; the 3B's empty responses on that case 14 of 15 → 0; and every
`two_writes` run now quotes a tool-returned balance instead of inventing one.

**One regression was caused and caught here.** Guidance added to
`add_transaction`'s *description* made `set_balance` salient on every turn, and
qwen2.5:3b began assembling a transfer from two `set_balance` calls — zeroing
an account. `transfer_moves_both_legs` fell from six consecutive 5/5 runs to
2/5. Removing the sentence restored 5/5 while `two_writes` stayed 15/15, so the
structural change did all the work. See ADR-032's consequences.

All results committed in `evaluations/results/`, pre-fix runs included — the
regression history is the point.

---

## Last User-Approved Change

Paul approved: measuring `contradiction_is_surfaced` before changing anything;
then two structural fixes measured separately (ADR-030 `add_task` referent
guard, ADR-031 empty-turn retry), with `confirm_duplicate` as the escape hatch.

Phases 1–5 committed and pushed (`f561e9d`, `61965a9`, `20b6e5e`, `5de9ec9`,
`cf628df`, `83224ee`, `bfcab5f`). The ADR-032 work is **uncommitted**.

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
| **030** | **A create tool must resolve its referent** |
| **031** | **An empty turn is a stumble, not a terminus** |
| **032** | **A tool that changes state returns the state it produced** |

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

- 551 tests: 535 unit (offline, sockets blocked), 16 integration (live)
- 7 evaluation suites, 35 cases, 7 holdout · 21 checks · 15 failure codes
- 3 runtime dependencies (`pydantic`, `httpx`, `pyyaml`)
- 6 commits. The ADR-032 work is uncommitted.
