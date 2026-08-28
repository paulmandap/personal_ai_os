# Teacher-guided improvement (Phases 9–16)

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
| **Holdout set** | **Missing** — no train/validation/holdout split |
| **Failure taxonomy** | **Missing** — no F001–F015 classification |
| **Per-category metrics** | **Partial** — per-case and per-check, not per-category |
| **`TeacherModel` protocol** | **Missing** |

**Do these four before anything else.** They are cheap, need no new libraries,
and the holdout split in particular must exist *before* any teacher-generated
data does — otherwise the benchmark is contaminated from the first round and
every later measurement is worthless.

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

Currently **5.5 GB free**, so ~25 GB must be reclaimed — **but not yet.**
Phases 9–11 need none of it, and space freed now would sit idle.

### Target the 3B, not the 7B

QLoRA on a 7B leaves too little of 8 GB VRAM for activations, gradients and
optimizer state at useful sequence lengths, and needs ~40 GB of disk. The
measured data already supports the smaller target: **the 3B matches the 7B on
every suite in this repository, at roughly twice the throughput.**

---

## The question worth asking before any of this

**Is there a capability gap to close?**

Current measured state, both models:

| Suite | 7B | 3B |
|---|---|---|
| `tool_calling` | 100% | 100% |
| `embellishment` | 100% | 100% |
| `hallucination` | 100% | 100% |
| `finance` | 100% | 100% |

Every failure this project has found so far was **architectural, not a model
limitation**:

- `complete_task` demanded an id the user never gave → both models invented one
  (ADR-022)
- substring matching failed on a reasonable paraphrase → the agent narrated an
  action instead of taking it
- an optional field rejected explicit `null` → every write call failed, and the
  agent claimed success anyway

Each was fixed with structure, in minutes, and each took a suite from failing
to 100%. **None would have been fixed by training** — and training on top of
them would have taught the model to work around bugs while hiding them.

So the honest prerequisite for Phase 12 is not disk space. It is a benchmark
the local models actually fail, for reasons that are not defects in this
repository. **The current suites are saturated** — 100% across the board is the
textbook signal that a benchmark has stopped providing improvement signal, and
the right response is harder and more adversarial cases, not a training run.

---

## Recommended order

1. **Harder benchmarks.** Ambiguity, contradiction, multi-step planning,
   long context, adversarial inputs. Find where the 3B actually breaks.
2. **Holdout split + failure taxonomy** (the Phase 9 gaps).
3. **Compare 3B against 7B on the harder suites.** If the 3B still matches,
   the routing decision is settled and the training question changes shape.
4. **Only then**, if a real gap exists: Phases 10–11 to build datasets, and
   free the disk for Phase 12.

Do not skip step 1. Training against a saturated benchmark optimises for a
number that has stopped meaning anything.

---

## Related

[`evaluation.md`](evaluation.md) · [`decisions.md`](decisions.md) ADR-020/021/026 ·
[`model-routing.md`](model-routing.md)
