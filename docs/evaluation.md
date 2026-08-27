# Evaluation

> **Status: not implemented.** Phase 4. This document records the intended
> design, and the reasons it matters more here than in most projects.

## Why this matters disproportionately

The central bet of this project is that a small local model plus good
architecture beats a large model plus a thin wrapper. That is an empirical
claim, and without evaluation it stays an opinion.

Two concrete decisions are blocked on having measurements:

- **Which local model belongs at each tier?** Popularity is not evidence. A
  model that generates fluently but calls tools badly is worse than useless
  here.
- **Should routing stay deterministic?** ADR-009 says yes *until data says
  otherwise*. Without data, that decision can never be revisited honestly.

## What already exists to build on

Phase 1 produced most of the raw material:

- **`runs/*.jsonl` traces** record every model call, tool call, permission
  decision and timing (ADR-005). An eval harness reads these rather than
  needing new instrumentation.
- **`Usage.tokens_per_second`** measures throughput from `eval_duration`,
  excluding model load, so a cold start does not make a fast model look slow.
- **`ScriptedModel`** makes the non-model parts deterministic, so an eval
  measures the model rather than the harness.
- **The integration suite** already contains the seed of a real eval: it asks a
  live 7B to read a file and extract a value, and checks the answer.

## What to measure

| Dimension | Why it matters here |
|---|---|
| Tool selection accuracy | Wrong tool = wasted iteration; the failure mode small models show first |
| Structured output validity | Malformed arguments burn the iteration budget |
| Task completion | Did it actually answer? |
| Failure recovery | Does it correct itself after an `ERROR:` observation? |
| Unnecessary escalation | Is the router sending easy work to the slow tier? |
| Hallucination | Did it invent file contents it never read? |
| Permission violations | Must be zero. Any non-zero result is a bug, not a score |
| Latency / throughput | On this hardware, a capability that is too slow is not a capability |

## Intended shape

```
evaluations/
├── cases/
│   ├── tool_selection/
│   ├── recovery/
│   └── extraction/
└── results/          # one file per (model, suite, date), for regression tracking
```

A case declares an objective, a fixture workspace, and assertions over the
resulting `AgentResult` **and its trace** — the trace being where "did it pick
the right tool first?" is actually answerable.

## Principles worth fixing now

**Faster must not silently beat better.** A model that halves latency and
doubles the tool-error rate is a regression. Results should be reported as a
profile, not collapsed into a single number that hides the trade.

**Track regressions over time.** The point is comparing *this* model against
*last* model, and this prompt against last prompt. A single run tells you
almost nothing.

**Permission violations are not a metric.** They are a defect. Zero, or the
build is broken.

**Evaluate the system, not the model.** The claim under test is about
architecture. Measuring a raw model on a benchmark answers a different
question than the one this project is asking.

## Relationship to Phase 8

Trajectory capture (§31) needs a quality signal to decide which runs are worth
learning from. That signal is the evaluation score. Bad agent behaviour makes
bad training data, so distillation cannot sensibly precede evaluation — which
is why the phases are ordered this way.
