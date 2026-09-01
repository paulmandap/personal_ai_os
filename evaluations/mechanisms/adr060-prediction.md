# ADR-060 prediction — written BEFORE any arm was run

**Date:** 2026-09-01 · **Author:** Claude Code, reviewed by Paul

Pre-registered because ADR-053 set a bar from a single observation and that bar
then disqualified the intervention it came from. The reading below is fixed now,
while the result is unknown.

---

## What is being explained

`delegation` on `qwen2.5:3b-instruct`, 2026-09-01, scored **0/45** — every case,
every run, `iterations: 2.0`, two empty turns. `routes_task_work_to_task_agent`
scored **5/5 in ten consecutive historical runs** and is now 0/15. `paios eval`
flagged it BELOW the historical low.

## Why the roster is the suspect

Only what the Master is *sent* can make the Master produce an empty turn, and
the empty turn happens before any tool executes.

| candidate | status |
|---|---|
| `master.py`, `MASTER_SYSTEM_PROMPT` | unchanged since 2026-08-28 |
| `delegate.py` schema + description | unchanged since 2026-08-28 |
| ADR-051 fidelity correction (`8c6118f`) | **cannot reach the Master** — `checks_answer_fidelity` is set only by `finance`, `research`, `task_agent`; `MasterAgent` leaves it `False` |
| Ollama server | 0.33.2, unchanged |
| model blobs | 3B unchanged for 4 weeks; 7B for 4 days — both predate the last good run |
| **`{roster}` grew from 3 agents to 4** | **`agents/research.yaml`, 2026-08-31, `390c046`** |

`MasterAgent._roster()` interpolates every registered agent except itself into
the system prompt. **Adding a manifest is therefore a prompt change to the
Master**, and delegation was last run 2026-08-30 — the day before.

**H1:** the fourth roster entry caused the collapse.

## Arms

Same code (HEAD + the uncommitted ADR-059 instrument), same day, serial — the
7B holds ~5.8 GB of 8 GB. `delegation`, `--repeat 15`, 45 runs per arm.

| arm | roster | how |
|---|---|---|
| **A** 3B shipped | 4 agents (finance, ping, research, task_agent) | default `agents/` |
| **B** 3B pre-Phase-6 | 3 agents (finance, ping, task_agent) | `PAIOS_PATHS__AGENTS_DIR` → a copy without `research.yaml` |
| **C** 7B shipped | 4 agents | default `agents/` — is the collapse small-model-specific? |

Arm B changes **only** which manifests are discovered. No file in `agents/` is
edited, moved or deleted.

## The reading, fixed in advance

Judged on `routes_task_work_to_task_agent`, because it is the case with a
stable historical band: **10 runs, all 4/5 or 5/5 → 80–100%.** The bar is the
bottom of an observed distribution, not a single sample.

| outcome | condition | conclusion |
|---|---|---|
| **H1 confirmed** | arm B ≥ **12/15** (80%) *and* arm A ≤ 3/15 | the roster is the cause |
| **H1 rejected** | arm B ≤ 3/15 | the roster is **not** the cause; the regression is unexplained and Phase 7 pauses |
| **Ambiguous** | arm B is 4–11/15 | partial or noisy; **claim no cause**, needs a further arm |

## Mechanism, not just score

Every arm runs with `--trace-dir`. The count that matters is
**`model.empty_payload` events per arm** (ADR-059). If H1 is right, arm B must
show substantially fewer empty turns — not merely a better score. A score that
improves without the empty turns disappearing means something else moved and H1
is not supported.

## Guards

- **If both arms are ~0, do not go looking for a fix in this commit.** Record it
  and stop. CLAUDE.md: a regression is not fixed in the commit that finds it.
- **Whatever this shows, it is ADR-060** — including "not the roster". A recorded
  negative result is a successful outcome (ADR-035/038/039/047/053/054).
- **No prompt clause.** If H1 is confirmed, the fix is a design question about
  roster size and shape against a small model. ADR-035, ADR-053 and ADR-054 are
  three recorded failures of the prompt-wording reflex; a fourth is not owed a
  turn.
- Arm B is a **diagnostic configuration, not the shipped one**, so it is run
  `--no-save` and must never enter the `delegation` history series. Arms A and C
  are the shipped configuration and **are** saved: the regression belongs in the
  record.

---

# PART 2 — the prompt bisection

**Written 2026-09-01, BEFORE the bisection was run.** Part 1 above is left
exactly as written, including the hypothesis it got wrong.

## Erratum on Part 1, found before Part 2 was designed

**Arm B did not test what it claimed.** `EvalRunner._build_runtime_settings`
hardcodes `paths.agents_dir` to `repo_root/agents` **and** calls
`load_settings(..., use_env=False)`, which never merges `env_overrides()`. The
`PAIOS_PATHS__AGENTS_DIR` override was ignored twice over, so **arm B ran the
shipped 4-agent roster and was byte-identical to arm A.** The 1/45 vs 0/45 gap
is sampling noise between two identical configurations.

**"H1 rejected" is withdrawn. The roster hypothesis is untested and open.**
What survives Part 1: the 3B at ~1/90 and the 7B at 45/45 (both arms were the
shipped config, so that is 90 runs of it), and the direct `httpx` probe, which
bypassed the harness entirely.

The bisection below substitutes roster strings **directly into the prompt**, so
it is immune to that defect and tests the roster properly as a by-product.

## What is being explained

Under `MASTER_SYSTEM_PROMPT` the 3B emits `content:''` and no tool call. Under a
*trivial* system prompt, the same model with the same `delegate` schema emits a
structurally valid call. Which part of the prompt does it?

## Construction — deterministic, pinned offline

`P = MASTER_SYSTEM_PROMPT`; `HEAD, SEP, TAIL = P.partition("How to work:")`;
`BULLETS` = the 5 bullets; `build(bullets, roster)` reassembles them.

**`build(BULLETS, ROSTER_4) == P.replace("{roster}", ROSTER_4)` byte for byte**,
asserted by `test_adr060_bisect_master_prompt.py`. `FULL` is therefore not a
special case, and a later prompt edit fails the tests instead of silently
changing what is measured. An empty bullet list drops the `How to work:` header
too — a dangling header is an artifact no shipped config produces.

## Stage 1 — 3B, 17 arms, n=15 each

`FULL` · `FULL_UNSEEDED` · `HEAD_ONLY` · `TRIVIAL` · `MINUS_1..5` (necessity) ·
`ONLY_1..5` (sufficiency) · `STRUCT_4` (bullet-count control, 4 inert bullets) ·
`ROSTER_3` · `ROSTER_1` (secondary).

## Metrics

| metric | definition |
|---|---|
| **`CALL_ANY`** | any parsed tool call. **Primary endpoint** |
| `VALID_DELEGATION` | a `delegate` call naming an agent this arm advertises, non-empty objective. Not applicable where no roster is shown |
| `TEXT` | content, no call. **Never recovery** |
| `EMPTY` | neither |

`prompt_eval_count` is recorded on every call, so **prompt length is a measured
covariate, not an assumed control.**

## Thresholds (on `CALL_ANY`, n=15)

`RESTORED` ≥ **12** · `SUPPRESSED` ≤ **3** · `MIXED` 4–11 → **no causal claim**.

Verified against the binomial rather than chosen for plausibility: at a true
rate of 0.2, P(≥12) ≈ 0; at 0.5, 0.018; at 0.8, **0.648**. The wide `MIXED` band
forces "no claim" on marginal arms. **Cost, stated in advance: only ~65% power
at a true rate of 0.8, so absence of `RESTORED` is not evidence of absence.**
Symmetrically, `SUPPRESSED` fires 65% of the time at a true rate of 0.2, so it
does not mean "never calls".

## Validity — checked before any 3B arm is read

7B `FULL` must return `CALL_ANY` ≥ **13/15** in both the opening and closing
blocks. Reference: 7B `delegated_to=task_agent` is **55/55 across 11 stored
runs**; rule of three gives p ≥ 0.9455, at which P(X≥13 | n=15) = **0.955** —
a 4.5% false-void rate, against 19.6% for a ≥14 floor.

| event | consequence |
|---|---|
| opening control < 13 | **run void**, no 3B arm interpreted or recorded |
| closing control < 13 | **3B results NOT interpretable; complete run repeated.** No per-arm salvage — a drifted environment cannot be partially trusted |

**7B `TRIVIAL` is exploratory, not a control**: ADR-060 saw the 7B EMPTY there on
n=1, so it cannot be a positive control. It is measured to correct my own
single observation.

## Decision table

| pattern | reading |
|---|---|
| `ONLY_k` SUPPRESSED **and** `MINUS_k` RESTORED, exactly one k | candidate mechanism, bullet k → Stage 2 |
| ↳ Stage 2 `MINUS_k_REFILLED` RESTORED | mechanism scoped to **bullet k's content** |
| ↳ Stage 2 `MINUS_k_REFILLED` SUPPRESSED | mechanism scoped to **instruction count** |
| no single `MINUS_k` RESTORED, `HEAD_ONLY` RESTORED | **interaction effect**; no bullet named |
| several `MINUS_k` RESTORED **and** `STRUCT_4` RESTORED | **common structural/count effect implicated; specific content NOT isolated** — equally consistent with redundancy, interaction, ordering, formatting, count and token length |
| `HEAD_ONLY` SUPPRESSED, `TRIVIAL` RESTORED | cause is in `HEAD` → constrained second bisection, not a mechanism |
| `FULL` MIXED | **stochastic regime**; report a cliff, name no cause |
| `ROSTER_3`/`ROSTER_1` RESTORED while `FULL` SUPPRESSED | roster implicated **but confounded with length**. Not causal from Stage 1; triggers `ROSTER_3_PADDED` |
| `FULL` vs `FULL_UNSEEDED` disagree by label | seeding moved the regime; seeded arms reported as a separate configuration |

## Stage 2 — conditional, declared now

| trigger | arm | separates |
|---|---|---|
| bullet k implicated | `MINUS_k_REFILLED` = 4 real bullets + 1 inert, count back to 5 | content vs count |
| a roster arm RESTORED | `ROSTER_3_PADDED` = `ROSTER_3` + one inert agent entry | "fewer agents" vs "shorter prompt" |

## Naming a mechanism requires all three

1. `ONLY_k` SUPPRESSED, **and** 2. `MINUS_k` RESTORED, **and** 3. Stage 2
separates content from count. Anything less is **suggestive only** — a single
`MINUS_k` restoring shows *that prompt surface matters*, not that bullet k is
independently causal.

**Scope of any finding:** *a mechanism affecting first-turn `delegate` emission
by `qwen2.5:3b-instruct`, under the tested objective, roster, tool schema and
generation configuration.* **One objective is tested**
(`"Add a task to renew my passport."`); `routes_money_work_to_finance` — 0/50
lifetime, an older and different failure — is **not** covered.

## Inconclusive protocol

If no arm reaches `RESTORED`, or `FULL` is `MIXED`, or the pattern matches no
row: **declare no mechanism, record the arms, stop.** No prompt-wording loop —
ADR-035/053/054 are three recorded failures of it. The next controlled
experiment is specified now: move measurement to the **template layer**, compare
`/api/chat` against `/api/generate` with the Qwen2.5 template applied manually,
to see whether a partial `<tool_call>` block is emitted and consumed. ADR-059
localised the loss there.

## Conditions held constant, recorded into the result JSON

`temperature` **0.3 for both models** — `--model` overrides only each tier's
`model` field, and the Master is `role: reason` → tier `medium`; running the 3B
at `small`'s 0.2 would measure a configuration the suite never runs.
`num_ctx` 8192 · `keep_alive` 5m · `seed = 20260901 + rep` (recorded; production
sends none, hence `FULL_UNSEEDED`) · arm order shuffled per replicate with
`ARM_ORDER_SEED = 20260901` · `top_p`/`top_k`/`repeat_penalty` **not sent and
therefore unpinned** — a server upgrade changes them silently.

**Model order:** 7B control → 3B block → 7B control, so drift *during* the 3B
block is detectable rather than assumed absent.

## Guards

- **Read the arms, never a total.**
- **ADR-061 is diagnostic only.** No prompt edit in that commit; the Master is
  not modified to make the experiment pass.
- The fix, if any, is a **separate pre-registered experiment**. "Move the
  instruction into the tool description" is a *candidate*, not a conclusion.
- **`delegation` is the only suite with `agent: master`**, so a Master-prompt
  change can reach nothing else. Canary set is `delegation` on both models; no
  full sweep is owed.
