# Teacher-guided improvement (Phases 9–16)

> **These 9–16 are a SEPARATE scheme from `CLAUDE.md`'s build phases 1–8, and
> the two are not sequential** (ADR-058). This is a teacher-guided roadmap
> *mapped against* the project rather than a continuation of it — which is why
> Phase 9 below is already largely delivered, by Phase 4. Neither scheme is
> renumbered; ADRs and commit messages cite these numbers.

> **Mostly FUTURE.** Phase 9 is largely built; Phases 10–11 are feasible now;
> Phases 12–16 are blocked on hardware. This document maps the plan against
> what actually exists, because a roadmap that restates its own ambitions is
> less useful than one that says what is already done and what is impossible.

The goal: use a stronger teacher model to critique local agents, generate
training data, and iteratively improve them — until the local agents are good
enough that the teacher is optional.

---

## The one non-negotiable

**The teacher is an abstraction, never a runtime dependency** (ADR-026).

```python
class TeacherModel(Protocol):
    def evaluate(...): ...
    def critique(...): ...
    def generate_reference(...): ...
    def create_adversarial_case(...): ...
```

No Anthropic SDK, no API key, no cloud call path — in the runtime *or* the
training pipeline. Teacher output is **data**: critiques, reference answers and
generated cases are written to versioned files and committed. Everything must
remain usable after Claude Code access ends on 2026-09-07.

This is ADR-001 applied to training. A pipeline that dies with its teacher is
the same trap as a runtime that does, and worse — it would have shaped the
local models around a critic that no longer exists.

---

## Phase 9 — Agent evaluation & benchmarking: **~70% built**

Phase 4 delivered most of this.

| Required | Status |
|---|---|
| Benchmark registry | **Built** — `evaluations/cases/*.yaml`, 4 suites |
| Multiple trials | **Built** — `repeat`, default 3, suites use 5 (ADR-021) |
| Graders | **Built** — 19 deterministic checks (ADR-020) |
| Transcripts | **Built** — `runs/*.jsonl`, full tool payloads |
| Metrics | **Built** — iterations, tool calls, invalid args, tok/s |
| Baseline reports | **Built** — committed to `evaluations/results/` |
| Model comparison | **Built** — `paios eval compare` |
| **Holdout set** | **Built** — `split:` + `--split holdout` (ADR-027) |
| **Failure taxonomy** | **Built** — F001–F015, severity-ranked |
| **Per-category metrics** | **Built** — `category:` on cases, aggregated |
| **`TeacherModel` protocol** | **Deferred to Phase 10** — see below |

**Phase 9 is now essentially complete.** The holdout was created before any
teacher-generated data existed, which is the only moment a split is
trustworthy.

### The one remaining item: `TeacherModel`

Deliberately **not** built in Phase 5. It would have been a Protocol with zero
implementations and zero callers — the speculative abstraction `CLAUDE.md`
forbids — and there is no training system yet for it to abstract. An interface
designed before its first use is usually wrong in ways only that first use
reveals.

**It is required as the first task of Phase 10**, where teacher critique
becomes real and something concrete plugs into it. The shape it should take is
recorded above. This is a deferral with a named trigger, not a decision to skip
it.

---

## Phases 10–11 — Teacher critique & dataset generation: **feasible now**

These need no training libraries. The loop is:

```
run a suite  ->  collect failing transcripts  ->  teacher critiques them
             ->  curate  ->  versioned dataset file
```

The teacher is Claude Code, invoked by hand. Slow, and it survives September.

Requirements worth honouring from the spec:

- **Store `student_attempt`, `teacher_critique` and `teacher_reference`
  separately.** Never overwrite the attempt — the comparison is the point.
- **Do not train on every teacher response.** Curate: correctness, relevance,
  duplication, safety.
- **Do not trust the teacher automatically.** Where a deterministic grader and
  the teacher disagree, flag it for review. This project already has strong
  deterministic graders; they should be the tiebreak, not the teacher.

---

## Phases 12–16 — Training: **blocked on hardware**

### A GGUF cannot be fine-tuned

The 6.16 GB of Ollama models on this machine are **quantized inference
artefacts**. Training needs the original HF safetensors — a second, larger
download alongside the GGUF already present. This is the single most commonly
missed constraint here.

### Disk budget

| Item | Size |
|---|---|
| PyTorch + bundled CUDA runtime | ~6 GB |
| transformers, peft, trl, datasets, accelerate, bitsandbytes | ~1 GB |
| Qwen2.5-3B-Instruct safetensors (trainable) | ~6.2 GB |
| LoRA adapters + checkpoints across rounds | ~2 GB |
| Merge → GGUF conversion workspace (model present twice) | ~6 GB |
| Datasets, logs, misc | ~1 GB |
| **Total** | **~22 GB** |
| **Recommended free** | **30 GB** |

Currently **11.2 GB free** (re-derived 2026-09-01; the 5.5 GB previously recorded
here was stale), so ~19 GB must be reclaimed — **but not yet.**
Phases 9–11 need none of it, and space freed now would sit idle.

### Target the 3B, not the 7B

QLoRA on a 7B leaves too little of 8 GB VRAM for activations, gradients and
optimizer state at useful sequence lengths, and needs ~40 GB of disk. The
measured data already supports the smaller target: **the 3B matches the 7B on
every suite in this repository, at roughly twice the throughput.**

---

## Is there a capability gap to close? **Yes — now there is.**

The earlier answer to this was "no". That answer came from four saturated
suites where both models scored 100%, and it was wrong — or rather, it was an
artifact of benchmarks that had stopped measuring anything.

Harder suites broke the tie immediately:

| Suite | 7B | 3B |
|---|---|---|
| `tool_calling`, `embellishment`, `hallucination`, `finance` | 100% | 100% |
| `robustness` | 85% | 90% |
| `planning` | 93% | **67%** |
| `delegation` | 100% | **33%** |

**The models are not interchangeable.** On `planning` the 3B loses track across
dependent steps.

> **The `delegation` half of this claim was corrected on 2026-09-01 and the
> correction matters most to *this* document, because Phases 12–16 were
> justified by it.**
>
> It read: *"the 3B returned an empty response rather than routing a money
> question — not a wrong answer, no answer."* Three things are now measured:
> the 33% is a **sum** hiding 49/50 on task routing against 0/50 on money
> routing; **"no answer" is false** — all 83 empty runs emitted 25–79 tokens the
> runtime discarded (**ADR-059**); and the failure **reproduces against Ollama
> with no harness, no runtime and no agent loop**, disappearing when the Master's
> system prompt is replaced by a trivial one (**ADR-060**).
>
> **Do not treat this as an established capability gap until the ADR-060
> bisection has run.** "Architecture still comes first" is the rule stated below,
> and this is that rule applying to the one finding that was supposed to justify
> skipping it.

Two consequences:

**For routing.** `reason` must not move to `small`. The Master specifically
needs the 7B. The 3B remains fine for the leaf agents it was measured on, and
is nearly twice as fast there.

**For training.** There is a concrete target — *make a 3B able to drive the
Master* — and it is exactly the kind of narrow, well-specified gap distillation
suits, with a number to improve and a holdout to verify it on.

**But it is not yet established as a gap that training should close.** ADR-060
showed the same 3B emitting a valid `delegate` call under a trivial system prompt
and none under the Master's, with no harness involved. **Training a model to
overcome a prompt surface would be the most expensive available fix for it**, and
would bake the defect in as a capability. The bisection that separates the two is
~10 GPU-minutes and is unrun. **Run it before spending 25 GB.**

### But architecture still comes first

Phase 5's own findings say so. The harder suites found three defects, and all
three were architectural:

- an empty model turn counted as a successful answer
- a transfer could half-complete and create money (ADR-029)
- `update_task` demanded an id, like `complete_task` before it (ADR-022)

Every failure this project has found so far has been a design problem, fixed in
minutes, and **none would have been fixed by training**. Before spending 25 GB
and several days on Phase 12, exhaust the cheaper explanation: check whether the
3B's delegation failure is a prompt or tool-surface problem first. An empty
response to a single-tool decision smells like one.

---

## Recommended order

1. ~~Harder benchmarks~~ — **done.** They found the gap.
2. ~~Holdout split + failure taxonomy~~ — **done.**
3. **Try structure first on the delegation gap.** A clearer Master prompt, a
   simpler `delegate` schema, or an explicit routing hint. Measure against
   `delegation`. This is hours of work against days.
4. **If structure does not close it**, that is the justified start of
   Phases 10–11: `TeacherModel`, teacher critique on failing delegation
   transcripts, curated dataset.
5. **Then** free the disk for Phase 12, targeting the 3B.

The order matters because step 3 is cheap and has worked every previous time.

---

## Related

[`evaluation.md`](evaluation.md) · [`decisions.md`](decisions.md) ADR-020/021/026 ·
[`model-routing.md`](model-routing.md)
