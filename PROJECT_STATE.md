# Project State

**Last updated:** 2026-08-27
**Updated by:** Claude Code (development assistant), reviewed by Paul

> This file is the handoff document. It must be enough for a future local agent
> — with no access to any previous conversation — to open the repository and
> know exactly where work stopped and what to do next.

---

## Current Phase

**Phase 1 — Foundation. Complete.**

Next: Phase 2 — Master Agent, Task Agent, memory layer.

---

## Current Objective

Phase 1's goal was **not features**. It was to establish contracts that a local
model can keep building on after Claude Code access ends on **2026-09-07**.

Concretely: make it true that `pytest` passes with Ollama stopped and the
network unplugged, while `paios run ping` produces a real answer from a local
model that called a real, permission-gated tool. Both hold.

---

## Completed

### Foundation
- `src/` layout, PEP 621 `pyproject.toml`, venv, editable install, `paios` CLI
- **Three runtime dependencies:** `pydantic`, `httpx`, `pyyaml`
- Git repository initialised. **No commits made** — Paul commits.

### Core contracts (`core/`)
- Provider-neutral types: `Message`, `ToolCall`, `ModelResponse`, `ToolSchema`,
  `Usage`, `ModelHealth`
- `AgentModel` ABC — `generate()` + `health()`, synchronous (ADR-002)
- Typed error hierarchy separating recoverable from fatal failures

### Models (`models/`)
- `OllamaModel` — direct HTTP via httpx, wire format **verified empirically**
  against Ollama 0.33.0 before anything depended on it (ADR-010)
- `ScriptedModel` — the second `AgentModel` implementation; makes the entire
  agent loop testable with no GPU and no network
- `ModelRegistry` — lazy, cached, config-driven
- `ModelRouter` — deterministic; precedence model → role → complexity →
  default; degrades **down** the local ladder only (ADR-009)

### Tools (`tools/`)
- `Tool` base: pydantic input model → advertised JSON Schema (cannot drift)
- Filesystem jail `resolve_within_roots()` — Windows-safe case folding,
  separator-aware prefix matching, resolves `..` and symlinks before checking
- `read_file`, `list_dir`
- Timeout guard, with its limitation documented honestly (bounds the wait, not
  the thread)

### Permissions (`permissions/`)
- Seven levels, `read` → `destructive`, with config policy `auto|ask|deny`
- `PolicyBroker`, `CLIPermissionBroker`, plus test doubles
- Unconfigured level → **deny**; `requires_human_approval` overrides `auto`;
  non-interactive sessions **refuse** rather than self-approve
- Single enforcement point in the agent loop (ADR-006)

### Agents (`agents/`)
- `AgentSpec` YAML manifests
- `AgentRegistry` — directory discovery, lazy entrypoint resolution, and two
  load-time refusals: unregistered tool, and undeclared permission
- `BaseAgent` loop; recoverable failures fed back as observations (ADR-008)
- `PingAgent` + `agents/ping_agent.yaml` — the vertical slice

### Observability (`observability/`)
- JSONL run traces, append-and-flush per event (survives a crash)
- Fixed event vocabulary; doubles as Phase 8 trajectory substrate (ADR-005)
- Segment-based secret redaction (ADR-011)

### CLI
`paios doctor | models | agents | run | trace`

### Documentation
`README.md`, `CLAUDE.md`, this file, and nine `docs/*.md` including 11 ADRs.

---

## In Progress

Nothing. Phase 1 is closed.

---

## Known Problems

1. **`large` tier is unmapped.** A 14B at Q4 (~9 GB) exceeds this machine's
   8 GB VRAM and would spill to CPU at ~5–10 tok/s. The router degrades past
   it. Mapping it is a one-line config edit if that trade is ever worth making.

2. **Tool timeouts bound the wait, not the thread.** Python cannot kill a
   thread; a runaway tool keeps running in the background until the process
   exits. Documented in `docs/tools.md`. Tools touching slow resources should
   carry their own internal timeouts.

3. **Model swapping can crash Ollama's runner.** Switching between the 3B and
   7B under load produced `wsarecv: connection forcibly closed`. The
   integration suite evicts models explicitly (`fresh_vram` fixture). Any new
   test using a different model size must do the same.

4. **Disk is nearly full.** ~3.4 GB free of 476 GB on C:. Enough for now;
   pulling another model will need space cleared first.

5. **No memory, no Master Agent, no evaluation harness.** Deliberate — those
   are Phases 2–4 and were left out so they could be built on settled
   contracts. See `docs/memory.md` and `docs/evaluation.md` for intended design.

---

## Blockers

None.

---

## Next Steps

Recommended order for Phase 2:

1. **Task Agent** — a second real agent. The first genuine test of whether the
   registry abstraction holds, since `ping` was written alongside it and may
   have shaped it.
2. **Memory layer (SQLite)** — start with structured state; add vectors only
   when retrieval is *measured* to be the bottleneck (`docs/memory.md`).
3. **Master Agent** — objective decomposition and delegation. Must route
   through the same permission gate; no second path to `Tool.execute`.
4. **A `write` tool** — the first non-`read` permission level exercised
   end to end, and the first real test of the `ask` policy path.

Before starting: run `paios doctor` and `pytest -q` to confirm a green
baseline.

---

## Last Successful Test

**2026-08-27**

```
pytest -q                 ->  163 passed  (sockets blocked, Ollama not needed)
pytest -m integration     ->  8 passed    (live qwen2.5:3b + 7b)
```

Live end-to-end run: `paios run ping "Read config/default.yaml and tell me
which model is mapped to the medium tier."` → correct answer in 3 iterations,
2 tool calls, trace `runs/20260827T124046Z_33abea87.jsonl`.

---

## Last User-Approved Change

Paul approved, in this session:
- the Phase 1 plan (skeleton + vertical slice, sync core, pull 7B, `git init`)
- deleting the failed 4.36 GB Ollama partial download and running
  `pip cache purge` to free disk space

**No commits have been made.** The working tree is uncommitted and awaiting
review.

---

## Important Architecture Decisions

Full records in [`docs/decisions.md`](docs/decisions.md).

| ADR | Decision |
|---|---|
| 001 | **Local-first. No cloud provider, not even as fallback** — a fallback fails open and masks every weakness until the day it is gone |
| 002 | Synchronous core — one GPU holds one model; concurrency thrashes |
| 003 | Direct HTTP to Ollama, not a vendor SDK — keeps the provider seam real |
| 004 | Pydantic as the single typing layer — one dep, four jobs |
| 005 | JSONL traces = observability **and** future trajectory data |
| 006 | One permission gate, in the agent loop |
| 007 | `src/` layout, pip + venv, uv-compatible |
| 008 | Recoverable failures are observations, not exceptions |
| 009 | Deterministic router until evaluation data says otherwise |
| 010 | Ollama wire-format findings, verified empirically |
| 011 | Trace redaction matches key segments, not substrings |

---

## Model Configuration

Hardware: RTX 3050 **8 GB VRAM**, 16 GB RAM, Ryzen 5 3600, Windows 10.

| Tier | Model | Size | Status |
|---|---|---|---|
| `small` | `qwen2.5:3b-instruct` | ~2.2 GB | installed |
| `medium` | `qwen2.5:7b-instruct` | ~4.7 GB (5.1 GB resident, 100% GPU) | installed — the workhorse |
| `large` | — | — | unmapped by design |

Roles: `classify`/`extract` → small; `plan`/`reason`/`code` → medium.

Change models in `config/local.yaml`. No code edit required.

---

## Agent Configuration

| Agent | Model | Tools | Permissions |
|---|---|---|---|
| `ping` | role `reason` → medium | `read_file`, `list_dir` | `read` |

---

## Pending Experiments

- **Can the 3B drive the agent loop reliably?** The 7B is the workhorse; the 3B
  is unproven for tool calling. This is the first real question for the Phase 4
  evaluation framework, and the answer shapes how much routing work is worth
  doing.
- **How often does the recovery path fire?** It worked on the very first live
  run (the model exceeded a `max_bytes` bound and corrected itself). Worth
  measuring, since frequent recovery means the tool schemas need better
  descriptions rather than the loop needing more iterations.
- **Is `role`-based routing enough**, or is per-task complexity assessment
  needed? Deferred until there is data (ADR-009).

---

## Repository Facts

- ~2,740 lines of source, ~1,670 lines of tests
- 171 tests: 163 unit (offline), 8 integration (live)
- 3 runtime dependencies
- **0 commits** — the working tree awaits Paul's review
