# Personal AI OS

A local-first, model-agnostic personal agent platform. Give it an objective; it
decomposes the work, routes it to a specialised agent, uses tools, asks
permission before anything consequential, and records the whole run.

Everything runs on your own machine through [Ollama](https://ollama.com). There
is no cloud model provider in this codebase — not as a default, and not as a
fallback.

> **The organising principle:** the agent system owns the intelligence
> workflow, not the model.

---

## Status

**Phase 2 (Master Agent, Task Agent, Memory) — complete.** See
[`PROJECT_STATE.md`](PROJECT_STATE.md).

Working today: the model abstraction and Ollama provider, a deterministic model
router, an agent registry driven by YAML manifests, typed tools with a
filesystem jail, a permission gate, SQLite persistence, delegation between
agents, JSONL run tracing, and a CLI.

Three agents: `master` (delegates), `task_agent` (manages a real task list),
and `ping` (verifies the machine works).

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally
- A GPU helps. Built and tested on an RTX 3050 (8 GB VRAM) with 16 GB RAM.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

ollama pull qwen2.5:3b-instruct     # small tier  ~2.2 GB
ollama pull qwen2.5:7b-instruct     # medium tier ~4.7 GB

paios doctor
```

`paios doctor` verifies configuration, the filesystem jail, model availability,
registered tools and agents, and the permission policy — in one command.

## Use

Give the Master an objective and it routes the work:

```powershell
paios run master "Add a task to renew my passport, then tell me what's on my list."
```

```
[runtime]  agent master -> qwen2.5:7b-instruct (role 'reason' -> tier 'medium')
[runtime]  master -> delegating to task_agent (depth 1)

I've added "Renew my passport" to your list. You now have three tasks:
...
  [ok] agent=master model=qwen2.5:7b-instruct iterations=2 tool_calls=1
  trace: paios trace 63847bb8
```

Then replay exactly what happened, sub-agent and all:

```powershell
paios trace 63847bb8 -v
```

```
    5  tool.requested
    6  permission.decision
    7    delegate.start
    9    model.request        <- task_agent's own work, nested
   13    tool.result
   16    delegate.end
   17  tool.result            <- back in master
```

Other commands:

```powershell
paios doctor     # does this machine work?
paios models     # the model ladder and live availability
paios agents     # registered agents, their tools and permissions
paios tasks      # the task list, with no model involved
paios tasks add "Something" --priority high --due 2026-09-07
```

## How it fits together

```
                        paios CLI
                            │
                       Runtime  (composition root)
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  AgentRegistry       ModelRouter          ToolRegistry
   (agents/*.yaml)          │              (+ PermissionBroker)
        │                   ▼
        │             AgentModel  ◄── the seam every model goes through
        │              ├── OllamaModel
        │              └── ScriptedModel (tests)
        ▼
    BaseAgent ── loop ──► PermissionBroker ──► Tool
        │                                       │
        │                        ┌──────────────┴──────────────┐
        ▼                        ▼                             ▼
     RunTrace            Store (SQLite)                    delegate
   runs/*.jsonl           data/paios.db                        │
                                                               ▼
                                              another BaseAgent (depth + 1)
```

**The Master Agent is not a new layer.** It is a `BaseAgent` whose only tool is
`delegate`, so orchestration reuses the loop instead of duplicating it — and
inherits the permission gate, tracing and failure recovery unchanged.

Read [`docs/architecture.md`](docs/architecture.md) for the reasoning.

## Adding an agent

Drop a YAML file in `agents/`. No code, no registration:

```yaml
name: finance
description: Analyses spending, budgets and affordability questions.
model:
  role: reason          # a role, not a model name — survives model changes
tools:
  - read_file
  - list_dir
permissions:
  - read                # must cover every tool listed above
max_iterations: 8
```

The registry refuses, at load time, any manifest naming an unregistered tool or
using a tool whose permission level it did not declare — so that
`permissions:` list is a checked statement of blast radius, not a comment.

## Testing

```powershell
pytest -q                 # 239 unit tests — pass with Ollama STOPPED
pytest -m integration     # 11 live tests against real local models
```

The unit suite blocks network sockets outright. That is deliberate: if these
tests ever need a model server, the model has quietly become the architecture.

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | How the pieces fit, and why |
| [agents.md](docs/agents.md) | Writing agents |
| [tools.md](docs/tools.md) | Writing tools; the filesystem jail |
| [permissions.md](docs/permissions.md) | The authorisation model |
| [model-routing.md](docs/model-routing.md) | The local model ladder |
| [memory.md](docs/memory.md) | SQLite persistence, and why not vectors |
| [evaluation.md](docs/evaluation.md) | Planned (Phase 4) |
| [development-workflow.md](docs/development-workflow.md) | Day-to-day process |
| [decisions.md](docs/decisions.md) | Architecture decision records |

## Why local-only

This system is built to outlive the tools that built it. A hosted model would
quietly absorb every difficulty a local model exposes — and each of those
difficulties is a place the *architecture* needs to do more work. Hiding them
behind a stronger model means discovering all of them at once, on the day that
model goes away.

So the constraint is the design: if it does not work on a 7B running on a
consumer GPU, it is not finished.

## Licence

Private project. All rights reserved.
