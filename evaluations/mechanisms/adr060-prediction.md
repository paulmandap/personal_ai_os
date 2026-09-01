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
