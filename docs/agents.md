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
