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

**Phase 1 (Foundation) — complete.** See [`PROJECT_STATE.md`](PROJECT_STATE.md).

Working today: the model abstraction, an Ollama provider, a deterministic model
router, an agent registry driven by YAML manifests, a typed tool interface with
a filesystem jail, a permission system, JSONL run tracing, and a CLI.

One agent (`ping`) exercises the whole path end to end.

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

```powershell
paios run ping "Read config/default.yaml and tell me which model is on the medium tier."
```

```
[runtime]  agent ping -> qwen2.5:7b-instruct (role 'reason' -> tier 'medium')

The medium tier maps to the model "qwen2.5:7b-instruct".

  [ok] agent=ping model=qwen2.5:7b-instruct iterations=3 tool_calls=2
  trace: paios trace 33abea87
```

Then replay exactly what happened:

```powershell
paios trace 33abea87 -v
```

Other commands:

```powershell
paios models     # the model ladder and live availability
paios agents     # registered agents, their tools and permissions
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
        │
        ▼
     RunTrace ──► runs/<timestamp>_<id>.jsonl
```

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
pytest -q                 # 163 unit tests — pass with Ollama STOPPED
pytest -m integration     # 8 live tests against real local models
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
| [memory.md](docs/memory.md) | Planned (Phase 2) |
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
