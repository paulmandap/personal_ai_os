# Architecture

## The organising principle

> The agent system owns the intelligence workflow, not the model.

Everything below follows from that. A model is a replaceable component reached
through one interface; the parts that make the system *useful* — routing,
tools, permissions, memory, evaluation, state — live outside it and survive it.

The practical test: **`pytest` passes with Ollama stopped and the network
unplugged.** If that ever stops being true, the model has quietly become the
architecture. `tests/unit/conftest.py` blocks sockets so the property is
enforced rather than trusted.

## Shape as built (Phase 1)

```
                        paios CLI
                            │
                       Runtime  (composition root — the only place
                            │    that knows how the pieces connect)
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  AgentRegistry       ModelRouter          ToolRegistry
   (agents/*.yaml)          │              (+ PermissionBroker)
        │                   ▼
        │            ModelRegistry
        │                   │
        │                   ▼
        │             AgentModel  ◄── the seam
        │              ├── OllamaModel   (real, local)
        │              └── ScriptedModel (tests)
        ▼
    BaseAgent  ── the loop ──►  PermissionBroker ──► Tool
        │
        ▼
     RunTrace  ──►  runs/<ts>_<id>.jsonl
```

Phases 2–8 add a Master Agent above `BaseAgent`, a memory layer beside it, and
more agents in the registry. None of those require changing the seams above.

## The layers, and why each exists

### `core/` — contracts

`types.py` defines `Message`, `ToolCall`, `ModelResponse`, `ToolSchema`. These
are provider-neutral: nothing above the provider boundary knows what Ollama's
JSON looks like.

That translation boundary — not the `AgentModel` ABC — is the actual mechanism
of model-agnosticism. An ABC fixes the *shape of the call*; keeping a
provider's data format from leaking upward is what lets the model be replaced
without touching agents, tools or memory.

`model.py` is the whole contract: `generate()` and `health()`.

### `models/` — providers, registry, router

`OllamaModel` translates in both directions and raises typed errors.
`ScriptedModel` is the second implementation — and a second implementation is
the only real evidence that an interface is not secretly shaped around its
first one.

`ModelRegistry` builds models lazily from config and caches them.
`ModelRouter` picks one deterministically (see `model-routing.md`).

### `tools/` — capabilities

A tool declares its name, a pydantic input model, a permission level and a
timeout. `Tool.schema()` derives the advertised JSON Schema *from* the
validating model, so the two cannot drift.

`resolve_within_roots()` is the filesystem jail. It is orthogonal to
permissions: policy decides whether an action is allowed, the jail decides
which paths are nameable at all. A read that policy auto-approves still cannot
escape the workspace.

### `permissions/` — authorisation

`PermissionLevel` (read → destructive), `PermissionRequest`,
`PermissionDecision`, and brokers that turn one into the other.

The system distinguishes *a model wants to act* (`ToolCall`) from *the user
authorised the act* (`PermissionDecision`), and only a broker converts between
them.

### `agents/` — orchestration

`AgentSpec` is a YAML manifest. `AgentRegistry` loads, validates and
constructs. `BaseAgent` is the loop.

The registry refuses, at load time, an agent that names an unregistered tool or
requests a tool whose permission level it never declared. That second check is
what makes a manifest's `permissions:` list a real statement of blast radius
rather than documentation.

### `observability/` — traces

One append-only JSONL file per run. Same artifact serves debugging today and
trajectory capture later (ADR-005).

### `runtime.py` — composition root

Every wire is connected here and nowhere else. Components take collaborators as
constructor arguments and never reach for globals, which is exactly what makes
each one testable with a substitute.

## The loop

```
build [system, ...history, user]

repeat up to max_iterations:
    response = model.generate(messages, tools=declared_only)

    no tool calls?  ──►  return the answer

    for each tool call:
        tool declared for this agent?   no ──►  observation: ERROR
        arguments validate?             no ──►  observation: ERROR
        broker grants permission?       no ──►  observation: DENIED
        run it                              ──►  observation: result JSON
        append observation as a TOOL message

exhausted ──► return failure, honestly
```

The ordering matters: arguments are validated *before* the broker is consulted,
so a human is never prompted about a call that could not run anyway.

Recoverable failures become observations rather than exceptions (ADR-008). A
small model will misuse tools; the loop is shaped to let it notice and correct.
Observed on the first live run — the 7B exceeded a `max_bytes` bound, was told,
and fixed its own call on the next turn.

## What is deliberately absent in Phase 1

Master Agent · memory layer · retry and recovery policy · evaluation harness ·
resumable long-running tasks · MCP · any agent beyond `ping`.

These are Phases 2–4. They are absent because each of them wants to be built
*on* settled contracts, and the contracts were what Phase 1 was for.

## Constraints that shaped this design

| Constraint | Consequence |
|---|---|
| 8 GB VRAM | Ladder tops out at 7B; `large` tier unmapped; sequential execution (ADR-002) |
| One GPU | Sync core; model swaps are a hazard, not a parallelism opportunity |
| Claude access ends 2026-09-07 | No cloud runtime path exists at all (ADR-001); docs and `PROJECT_STATE.md` are load-bearing |
| Small local models | Failures must be recoverable in-loop (ADR-008); tool schemas kept flat and well-described |
