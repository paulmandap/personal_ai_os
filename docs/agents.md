# Agents

An agent is a YAML manifest plus, optionally, a Python class. The manifest is
the source of truth; the class is only needed when an agent must shape its own
prompt or post-process its own result.

## Adding an agent

Drop a file in `agents/`. Nothing else — no registration call, no import, no
edit to any orchestrator. That is the point of the registry: adding a Finance
Agent later must not require touching the Master Agent.

```yaml
name: finance
description: >
  Analyses spending, budgets and affordability questions.

model:
  role: reason          # prefer a role over a model name

tools:
  - read_file
  - list_dir

permissions:
  - read                # must cover every tool listed above

max_iterations: 8
timeout_s: 300
requires_human_approval: false
```

Verify it with `paios agents`, which shows what each manifest resolves to.

## Manifest fields

| Field | Meaning |
|---|---|
| `name` | Unique identifier; how you invoke it |
| `description` | What it does. A future Master Agent will route on this, so write it for a reader who is choosing between agents |
| `entrypoint` | `module:Class`. Omit for the generic `BaseAgent` loop |
| `model` | `role`, `tier`, `complexity` or an explicit `model` — see below |
| `tools` | Tool names. Only these are advertised to the model |
| `permissions` | Levels this agent may exercise. Checked against `tools` at load |
| `system_prompt` / `system_prompt_file` | Inline, or a path relative to `agents/` |
| `max_iterations` | Loop ceiling. Default 6 |
| `requires_human_approval` | Force a prompt for every tool call, whatever policy says |

## Choose a role, not a model

```yaml
model:
  role: reason        # good — survives every model change
  # tier: medium      # acceptable — ties you to a rung
  # model: qwen2.5:7b-instruct   # avoid — ties you to one file on one machine
```

An agent that says *"this is reasoning work"* keeps working when the ladder
changes. An agent that names a model has to be edited. Roles are mapped in
`config/default.yaml` under `models.roles`, so re-pointing every reasoning
agent at a better model is a one-line change.

## The two load-time checks

The registry refuses a manifest that:

1. **names an unregistered tool** — catches typos immediately rather than on
   the first run that happens to need that tool;
2. **uses a tool whose permission level it did not declare.**

The second is the one that matters. Without it, an agent declaring
`permissions: [read]` could be handed a tool that deletes things and nothing
would object until it did. Requiring the manifest to state its own blast radius
— and holding it to that claim — turns the registry from a lookup table into a
safety boundary.

```
AgentSpecError: agent 'archivist' requests tools needing permission(s)
delete but does not declare them. Add them to the manifest's `permissions:`
list to make the blast radius explicit.
```

## When to write a class

Subclass `BaseAgent` when the agent needs to:

- build its prompt from runtime facts (see `agents/builtin/ping.py`, which
  injects the workspace root — without it a small model guesses absolute paths
  that the jail then refuses, wasting an iteration on a self-inflicted error);
- validate or reshape its output;
- override `tool_schemas()` to expose tools conditionally.

Otherwise omit `entrypoint`. A useful agent can be pure configuration, and the
fewer classes there are, the less there is to keep in sync with the manifests.

## What the model actually sees

Only the tools in `spec.tools` are advertised. An agent cannot call what it was
never shown, which keeps the manifest an accurate description of what the agent
can do — not an aspiration.

## Results

`AgentResult` carries `ok`, `output`, `stop_reason`, `iterations`,
`tool_calls`, `model`, and the full `transcript`.

`stop_reason` is one of `answered`, `max_iterations`, `model_error`,
`agent_error`. Hitting the iteration ceiling is reported as a failure — the
last partial thought is *not* passed off as an answer.

## Testing an agent

Use `ScriptedModel`, and never a live model, for behaviour tests:

```python
agent = BaseAgent(
    spec,
    model=ScriptedModel([
        tool_call_response("read_file", {"path": "README.md"}),
        text_response("It is a test workspace."),
    ]),
    tools=default_registry(),
    broker=AllowAllBroker(),
    context=tool_context,
)
result = agent.run("What does the README say?")
```

Deterministic, millisecond-fast, no GPU. Live-model behaviour belongs in
`tests/integration/`, where it is measured rather than asserted — see
`evaluation.md`.

---

# Delegation and the Master Agent

## A sub-agent is a tool

The Master has no orchestration engine. It is an ordinary `BaseAgent` whose
manifest declares exactly one tool:

```yaml
name: master
tools:
  - delegate
permissions:
  - read
```

That single line does the work of a second engine. Delegation inherits the
permission gate, the trace events, argument validation and
failure-as-observation recovery — none of it rewritten (ADR-012).

It also makes delegation *discipline* structural rather than aspirational: the
Master cannot read a file or touch a task, so "prefer delegation when a
specialised agent exists" holds without a prompt having to stay disciplined
about it.

## The flow

```
master (depth 0)
  └─ delegate(agent="task_agent", objective="Add a task to renew my passport")
       ├─ permission gate  (read -> auto)
       ├─ depth guard      (0 < 2, ok)
       ├─ cycle guard      ("task_agent" not in ("master",), ok)
       └─ task_agent (depth 1)
            ├─ list_tasks   -> gate -> result
            ├─ add_task     -> gate -> result
            └─ answers
       └─ DelegateOutput{ok, output, iterations, tool_calls} back as an observation
master decides what to do next
```

## Guards

Runaway delegation is bounded mechanically, not by prompting:

| Guard | Mechanism |
|---|---|
| Recursion | `max_delegation_depth` (default 2) |
| Cycles | `call_stack` refuses `master → a → master`, and self-delegation |
| Breadth | The caller's own `max_iterations` |

The depth and call stack live on `ToolContext` and are injected by `Runtime` in
a closure, so a tool cannot fabricate a shallower depth to escape them — it only
chooses *which* agent to call.

**Delegation is `read`-level and is not prompted** (ADR-014). Delegating
computes; it is not itself consequential. Everything consequential the
sub-agent does is gated by its own tools, where the consequence actually
happens. A prompt here would protect nothing, and prompts that protect nothing
teach people to click through prompts.

## Adding a delegatable agent

Drop a manifest in `agents/`. Nothing else — the Master's roster is built at
runtime from the registry, so a new agent becomes routable without editing the
Master, its manifest, or any code.

Write the `description` carefully. It is what the Master reads when choosing,
so it should say *when to use this agent*, not just what it is:

```yaml
description: >
  Creates, updates, completes and reports on the user's tasks. Use this for
  anything about what the user has to do, deadlines, or things to remember.
```

## Writing an objective

The sub-agent cannot see the Master's conversation. `DelegateInput.objective`
says so in its schema description, because the most common delegation failure
is a one-word objective that assumes shared context.

## Nested traces

A sub-agent shares its parent's `RunTrace`, so one file holds the whole nested
run. Every event carries `agent` and `depth` (ADR-016), and `paios trace`
indents by depth:

```
    5  tool.requested
    6  permission.decision
    7    delegate.start
    9    model.request          <- task_agent's own work
   13    tool.result
   16    delegate.end
   17  tool.result              <- back in master
```

## Testing delegation

Pass a `delegate` callable directly on `ToolContext` — no runtime needed:

```python
context=ToolContext(
    agent="master",
    delegate=lambda agent, objective: sub_result(output="task added"),
    agent_roster={"task_agent": "Manages tasks."},
    call_stack=("master",),
)
```

`tests/unit/test_delegate.py` covers the guards, including that a refused
delegation never invokes the runner.

## Domain agents (FUTURE — NOT IMPLEMENTED)

A *domain agent* coordinates several specialists in one subject area, so the
master routes to a subject rather than to a capability:

```
master  →  health_wellness  →  emotional_support | lifestyle_wellness | reflection
```

It needs no new machinery. A domain agent is a `BaseAgent` whose tool is
`delegate` — structurally identical to the master, one level down.

Two rules make it work:

- **The coordinator *is* the domain agent.** Not a separate agent beneath it.
  Coordination is what a domain agent does, and an extra hop costs a full local
  model call while deciding nothing. It would also exceed
  `max_delegation_depth: 2` (ADR-017).
- **The master stays ignorant of the inside.** It knows the domain exists and
  what it is for. Adding a fourth specialist later changes nothing above the
  domain agent — which is the property that keeps orchestration flat as domains
  multiply.

The one domain designed in full is
[`health-wellness.md`](health-wellness.md) — including memory boundaries,
sensitivity-aware sharing, and why its safety layer is cross-cutting rather than
an agent in the tree. Nothing in it is built.
