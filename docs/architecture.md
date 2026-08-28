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

## Shape as built (Phase 2)

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
        │                                             │
        │                            ┌────────────────┴────────────────┐
        │                            ▼                                 ▼
        │                     read_file, list_dir              Store (SQLite)
        │                     task tools                        data/paios.db
        │                     delegate ─┐
        ▼                               │
     RunTrace ──► runs/<ts>_<id>.jsonl  │
                                        ▼
                            another BaseAgent (depth + 1)
                            sharing the same RunTrace
```

**The Master Agent is not a new layer.** It is a `BaseAgent` whose only tool is
`delegate`, so orchestration reuses the loop rather than duplicating it
(ADR-012). Phases 3–8 add more agents and more tools; neither requires changing
a seam above.

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

## Delegation

```
master (depth 0)  --delegate-->  task_agent (depth 1)  --add_task-->  Store
```

Three properties make this safe rather than merely possible:

- **Gates sit where consequences are.** `delegate` is `read`-level and never
  prompts; the `write` on `add_task` is what asks. Prompting on delegation
  would protect nothing and teach the habit of clicking through (ADR-014).
- **Bounds are mechanical.** `max_delegation_depth` and a call-stack cycle
  check are injected by `Runtime` inside a closure, so a tool can choose *which*
  agent to call but cannot fabricate a shallower depth to escape the guard.
- **One trace, attributed.** Sub-agents share the parent's `RunTrace`; every
  event carries `agent` and `depth` so a nested run is readable in order
  (ADR-016).

## Memory

`Store` (SQLite) is reached through `ToolContext.store`, the same injection
pattern as `delegate`. Tools handed no store report it rather than crashing,
which keeps them testable without a database. See `memory.md` — including why
vectors are still deliberately absent.

## What is still deliberately absent

Retry and escalation policy · evaluation harness · resumable long-running tasks
and persisted plans · semantic memory · MCP · Finance and Research agents.

These are Phases 3–4. Each wants to be built *on* settled contracts, which is
what the earlier phases were for. The nearest is a persisted plan: that is the
documented trigger for revisiting ADR-012's implicit-plan decision.

## Constraints that shaped this design

| Constraint | Consequence |
|---|---|
| 8 GB VRAM | Ladder tops out at 7B; `large` tier unmapped; sequential execution (ADR-002) |
| One GPU | Sync core; model swaps are a hazard, not a parallelism opportunity |
| Claude access ends 2026-09-07 | No cloud runtime path exists at all (ADR-001); docs and `PROJECT_STATE.md` are load-bearing |
| Small local models | Failures must be recoverable in-loop (ADR-008); tool schemas kept flat and well-described |
