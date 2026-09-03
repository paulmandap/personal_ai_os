# The `week_planner` coordinator — built, measured, and NOT shipped

**ADR-069. Rejected on 2026-09-03 by a rule declared before the run.**

Nothing here is dead code kept out of sentiment. It is the *artefact* of a
measured negative result, preserved because the finding is only checkable if the
thing that produced it still exists.

## What is in this directory

| file | what it is |
|---|---|
| `week_planner.yaml` | the domain agent. One manifest, no Python — `tools: [delegate]`, `permissions: [read]`, prompt in `system_prompt:` |
| `hierarchy_case.yaml` | the `master` case `the_coordinator_reaches_both_of_its_specialists`, removed from the suite when the agent was withdrawn |

## Why it was withdrawn

**Not because the hierarchy did not work.** It does — `tests/unit/test_hierarchy.py`
still ships and still proves it, offline and deterministically: a real
`master -> week_planner -> task_agent` chain through the real `Runtime`, the
call stack growing to three, depth stamped 0/1/2, both guards firing on the
production closure, and `plan_steps` recording the shape.

It was withdrawn because **putting a sixth agent in the Master's roster made the
7B Master measurably worse**, and the rule for that was written down first:

> Two or more regression-sensitive cases below 5/5 in arm B → the coordinator
> does not ship as it stands, and that is recorded as a rejection rather than
> reworded until it passes.

Two fell. `carries_a_deadline_into_the_objective` 5,5,5,5,5 → **4/5**, and
`question_then_instruction_does_both` → **0/5**.

## The number that matters

| arm | roster | time | `empty_response` | overall |
|---|---|---|---|---|
| A | 5 agents | 06:21 | 1/55 (1.8%) | 43/55 (78%) |
| **B** | **6 agents** | 09:13 | **15/65 (23%)** | 34/65 (52%) |
| A′ | 5 agents | **09:22** | **0/65 (0%)** | 49/65 (75%) |
| B′ (traced) | 6 agents | 09:37 | — | 36/65 (55%) |

**A′ ran nine minutes after B and was the cleanest of the four.** That is what
rules out an ADR-061-style temporal regime: under drift the *latest* run should
be the worst, and it was the best. The outcome alternates with the manifest.

## The agent itself is sound — this is worth restoring if the roster defect is fixed

`../adr069_coordinator_direct.py` invokes `week_planner` directly, bypassing the
Master:

| | |
|---|---|
| reached **both** specialists | **5 of 5** |
| `tool_calls` per run | 2, every run |

**Nothing is wrong with this manifest.** The coordinator does its job every time
it is asked. What failed is the Master's willingness to ask, and what broke the
Master was the roster entry. So this is parked pending a fix to Known Problem 19,
not pending a rewrite of itself.

*(That probe runs `week_planner` at top level, so its specialists are at depth 1.
It is not evidence about depth 2, and its prompt instructs both delegations where
the Master's does not.)*

## Restoring it — and what must be re-measured first

```powershell
Move-Item evaluations\mechanisms\adr069-rejected\week_planner.yaml agents\
# then paste hierarchy_case.yaml back into evaluations/cases/master.yaml
```

Two tests then need their roster assertions restored in
`tests/unit/test_agent_registry.py` (`test_every_shipped_manifest_loads`, and the
delegate-holder and no-Python-module assertions ADR-069 removed).

**Do not restore it without re-running the A/B.** The rejection was not about
this manifest's wording — it was about roster size, and any sixth agent is
expected to reproduce it. The thing that would justify restoring this one is a
fix to the underlying defect, not a better description.

**And do not treat rewording the description as that fix.** ADR-035, ADR-053 and
ADR-054 are three recorded failures of the prompt-wording reflex; the mechanism
here is upstream of prompt wording (see ADR-069's payload evidence: 50 of 50
empty turns carried 30–56 generated tokens and no `tool_calls` key).
